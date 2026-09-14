"""Mealie Sous Chef: Streamable HTTP MCP server for Mealie with built-in OAuth, for Claude custom connectors."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, Literal

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import recipe_ingredients, taxonomy
from .auth import MealieOAuthProvider
from .config import Settings
from .ingredients import (
    ingredients_payload,
    instructions_payload,
    orphaned_step_references,
    parse_lines,
    parsed_ingredient_summary,
)
from .mealie import MealieClient, MealieError, plan_entry, recipe_full, recipe_summary, shopping_item
from .nutrition import (
    NUTRIENTS,
    encode_food_extras,
    estimate_recipe_nutrition,
    nutrition_patch,
    read_food_profile,
    validate_per_100g,
)
from .nutrition_sources import NutritionSources, SourceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mealie_sous_chef")

settings = Settings()
oauth = MealieOAuthProvider(settings)
_mealie: MealieClient | None = None
_nutrition: NutritionSources | None = None


def mealie() -> MealieClient:
    assert _mealie is not None, "Mealie client not initialised"
    return _mealie


def nutrition() -> NutritionSources:
    assert _nutrition is not None, "Nutrition client not initialised"
    return _nutrition


def _iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from e


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    global _mealie, _nutrition
    _mealie = MealieClient(str(settings.mealie_url), settings.mealie_api_token)
    _nutrition = NutritionSources(settings.mcp_data_dir, settings.usda_api_key)
    try:
        yield
    finally:
        await _mealie.aclose()
        await _nutrition.aclose()
        _mealie = None
        _nutrition = None


mcp = MCPServer(
    name="Mealie",
    instructions=(
        "Tools for the user's self-hosted Mealie instance: search and read recipes, import recipes "
        "from URLs, manage shopping lists, and plan meals. Dates are ISO (YYYY-MM-DD). Recipes are "
        "addressed by slug (preferred) or id. Recipe ingredients link to Mealie's Foods/Units database "
        "(parse_ingredients, manage_taxonomy); per-serving nutrition is computed from those foods "
        "(lookup_nutrition -> set_food_nutrition -> compute_recipe_nutrition)."
    ),
    auth_server_provider=oauth,
    auth=AuthSettings(
        # Plain strings on purpose: AuthSettings preserves the empty path so the
        # advertised issuer is the canonical "https://host" (no trailing slash).
        issuer_url=settings.public_base,  # type: ignore[arg-type]
        resource_server_url=settings.mcp_endpoint,  # type: ignore[arg-type]
        # Tokens are only ever minted by this same process for this same resource.
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[settings.mcp_scope],
            default_scopes=[settings.mcp_scope],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[settings.mcp_scope],
    ),
    lifespan=lifespan,
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
READ_EXTERNAL = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


# ---------------------------------------------------------------- custom routes

@mcp.custom_route("/login", methods=["GET"])
async def login_get(request: Request) -> Response:
    return oauth.login_page(request)


@mcp.custom_route("/login", methods=["POST"])
async def login_post(request: Request) -> Response:
    return await oauth.handle_login(request)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> Response:
    try:
        await mealie().get("/api/app/about")
        return JSONResponse({"ok": True, "mealie": "reachable"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "mealie": str(e)}, status_code=503)


@mcp.custom_route("/", methods=["GET"])
async def index(_: Request) -> Response:
    return PlainTextResponse(f"Mealie Sous Chef MCP server. Add {settings.mcp_endpoint} as a custom connector in Claude.\n")


# ---------------------------------------------------------------- recipes

@mcp.tool(annotations=READ)
async def search_recipes(
    query: str = "",
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    page: int = 1,
    per_page: int = 15,
    order_by: Literal["name", "rating", "created_at", "date_added", "last_made"] = "name",
    order_direction: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    """Search recipes by free text, optionally filtered by tag/category slugs.

    Returns summaries (slug, name, tags, times). Use get_recipe for ingredients and steps.
    Leave query empty to list everything.
    """
    params: dict[str, Any] = {
        "page": page,
        "perPage": min(max(per_page, 1), 50),
        "orderBy": order_by,
        "orderDirection": order_direction,
    }
    if query:
        params["search"] = query
    if tags:
        params["tags"] = tags
    if categories:
        params["categories"] = categories
    data = await mealie().get("/api/recipes", params)
    return {
        "page": data.get("page"),
        "total_pages": data.get("total_pages"),
        "total": data.get("total"),
        "recipes": [recipe_summary(r) for r in data.get("items", [])],
    }


@mcp.tool(annotations=READ)
async def get_recipe(slug: str) -> dict[str, Any]:
    """Get a full recipe (ingredients, instructions, notes, nutrition) by slug or id."""
    return recipe_full(await mealie().get(f"/api/recipes/{slug}"))


@mcp.tool(annotations=WRITE)
async def import_recipe_from_url(url: str, include_tags: bool = True) -> dict[str, Any]:
    """Scrape a recipe web page into Mealie. Returns the new recipe."""
    slug = await mealie().post("/api/recipes/create/url", {"url": url, "includeTags": include_tags})
    return recipe_full(await mealie().get(f"/api/recipes/{slug}"))


def _with_notes(recipe: dict[str, Any], warnings: list[str], needs_review: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(recipe)
    if warnings:
        out["warnings"] = warnings
    if needs_review:
        out["needs_review"] = needs_review
        out["needs_review_hint"] = (
            "these lines were saved as plain text because linking them would have lost information; "
            "fix them with review_recipe_ingredients -> edit_recipe_ingredients"
        )
    return out


@mcp.tool(annotations=WRITE)
async def create_recipe(
    name: str,
    ingredients: list[str | dict[str, Any]],
    instructions: list[str],
    description: str = "",
    servings: float | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Create a recipe from scratch. Instructions are one string per step; times
    are free text ("20 minutes").

    Ingredients can mix plain strings and structured objects in the same list:
      - "2 cups flour" -- run through Mealie's parser, linked to an existing
        food/unit when one matches, otherwise kept as free text.
      - {"quantity": 2, "food_id": "...", "unit_id": "...", "note": "sifted"} --
        already resolved (ids from parse_ingredients or manage_taxonomy). Also
        accepts "title" (section header shown above this ingredient) and
        "original_text". Ids are verified; an unknown id is an error.
    A line is only linked when the parse accounts for every word; otherwise
    ("salt and pepper to taste" -> just salt) it's saved as plain text and listed
    in `needs_review`. If the parser fails, lines are saved as text with `warnings`.
    Use parse_ingredients first to preview matches."""
    # Build (and validate) ingredients before creating anything, so bad input
    # doesn't leave an empty recipe behind.
    built = await ingredients_payload(mealie(), ingredients)
    slug = await mealie().post("/api/recipes", {"name": name})
    patch: dict[str, Any] = {
        "description": description,
        "recipeIngredient": built.payload,
        "recipeInstructions": [{"text": step} for step in instructions],
    }
    if servings is not None:
        patch["recipeServings"] = servings
    if prep_time:
        patch["prepTime"] = prep_time
    if cook_time:
        patch["performTime"] = cook_time
    if total_time:
        patch["totalTime"] = total_time
    if source_url:
        patch["orgURL"] = source_url
    saved = await mealie().patch(f"/api/recipes/{slug}", patch)
    return _with_notes(recipe_full(saved), built.warnings, built.needs_review)


@mcp.tool(annotations=WRITE)
async def update_recipe(
    slug: str,
    name: str | None = None,
    description: str | None = None,
    ingredients: list[str | dict[str, Any]] | None = None,
    instructions: list[str] | None = None,
    servings: float | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    rating: int | None = None,
) -> dict[str, Any]:
    """Update parts of a recipe. Only the fields you pass are changed; ingredients/
    instructions replace the whole list when given, using the same mixed
    plain-string/structured-object input as create_recipe.

    To fix or link a few ingredients of an existing recipe, prefer
    review_recipe_ingredients + edit_recipe_ingredients: they change only what you
    target. Here, lines identical to an existing ingredient (its original text or
    display text) and steps identical to an existing step are kept as they are,
    preserving their links; the reply warns if step->ingredient links were lost."""
    patch: dict[str, Any] = {}
    warnings: list[str] = []
    needs_review: list[dict[str, Any]] = []
    current = await mealie().get(f"/api/recipes/{slug}") if (ingredients is not None or instructions is not None) else {}
    if name is not None:
        patch["name"] = name
    if description is not None:
        patch["description"] = description
    if ingredients is not None:
        built = await ingredients_payload(mealie(), ingredients, existing=current.get("recipeIngredient"))
        patch["recipeIngredient"], warnings, needs_review = built.payload, built.warnings, built.needs_review
    if instructions is not None:
        patch["recipeInstructions"], _ = instructions_payload(instructions, current.get("recipeInstructions"))
    if servings is not None:
        patch["recipeServings"] = servings
    if prep_time is not None:
        patch["prepTime"] = prep_time
    if cook_time is not None:
        patch["performTime"] = cook_time
    if total_time is not None:
        patch["totalTime"] = total_time
    if rating is not None:
        patch["rating"] = rating
    if not patch:
        raise ToolError("nothing to update")
    saved = await mealie().patch(f"/api/recipes/{slug}", patch)
    if ingredients is not None or instructions is not None:
        orphaned = orphaned_step_references(saved.get("recipeInstructions"), saved.get("recipeIngredient") or [])
        if orphaned:
            warnings.append(
                f"step(s) {orphaned} were linked to ingredients that no longer exist; "
                "edit_recipe_ingredients changes ingredients without breaking those links"
            )
    return _with_notes(recipe_full(saved), warnings, needs_review)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_recipe(slug: str) -> str:
    """Permanently delete a recipe by slug. Confirm with the user first."""
    await mealie().delete(f"/api/recipes/{slug}")
    return f"Deleted recipe {slug}"


@mcp.tool(annotations=READ)
async def list_tags_and_categories() -> dict[str, list[str]]:
    """List all tag and category slugs (usable as filters in search_recipes)."""
    tags = await mealie().get("/api/organizers/tags", {"perPage": -1})
    cats = await mealie().get("/api/organizers/categories", {"perPage": -1})
    return {
        "tags": sorted(t["slug"] for t in tags.get("items", [])),
        "categories": sorted(c["slug"] for c in cats.get("items", [])),
    }


# ---------------------------------------------------------------- foods & units

@mcp.tool(annotations=DESTRUCTIVE)
async def manage_taxonomy(
    resource: Literal["foods", "units"],
    action: Literal["list", "create", "update", "merge", "delete"] = "list",
    query: str = "",
    page: int = 1,
    per_page: int = 25,
    name: str | None = None,
    item_id: str | None = None,
    data: dict[str, Any] | None = None,
    merge_into: str | None = None,
    force: bool = False,
    add_alias: bool = True,
    nutrition: Literal["missing", "present"] | None = None,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """List, create, update, merge, or delete entries in Mealie's Foods or Units
    database -- what structured recipe ingredients link to.

    action="list" (default): query/page/per_page apply. For foods,
      nutrition="missing" or "present" filters on stored nutrition data.
    action="create": needs `name`. `data` may add fields, e.g. {"pluralName": "..."};
      for units {"abbreviation": "tbsp", "standardQuantity": 0.5, "standardUnit": "fluid_ounce"}
      (standardUnit one of gram, kilogram, ounce, pound, milliliter, liter,
      fluid_ounce, cup) -- this lets compute_recipe_nutrition weigh the unit.
    action="update": needs `item_id`; `name` and/or `data` change fields, the rest
      keep their values. For food nutrition use set_food_nutrition instead.
    action="merge": needs `item_id` (folded away) and `merge_into` (kept). Every
      recipe ingredient and shopping item using item_id is repointed, then item_id
      is deleted. The right fix for duplicates ("Jasmine Rice" vs "jasmine rice").
      With add_alias=true (default) the dropped item's names become aliases of the
      kept one, so Mealie's parser keeps matching them, and a food's nutrition data
      is carried over if the kept food has none.
    action="delete": needs `item_id`. Refuses if any recipe or shopping item still
      uses it (Mealie would silently unlink those ingredients) and suggests merge;
      force=true deletes anyway. Only your own household's shopping lists are
      checked; on Postgres, Mealie itself blocks deleting anything a list uses.

    `items`: batch a write action -- a list of objects with keys name, item_id,
    data, merge_into, force, add_alias. Runs in order; one failure doesn't stop
    the rest; the reply lists results and errors separately.
    """
    return await taxonomy.run(
        mealie(),
        resource,
        action,
        query=query,
        page=page,
        per_page=per_page,
        name=name,
        item_id=item_id,
        data=data,
        merge_into=merge_into,
        force=force,
        add_alias=add_alias,
        nutrition=nutrition,
        items=items,
    )


# ---------------------------------------------------------------- recipe ingredients (in place)

@mcp.tool(annotations=READ)
async def review_recipe_ingredients(
    slug: str,
    only: Literal["all", "needs_attention"] = "all",
    suggest: bool = True,
) -> dict[str, Any]:
    """Inspect a recipe's ingredients one by one, to decide what to fix.

    Each row has `ref` (stable id for edit_recipe_ingredients) and `position`,
    the current quantity/unit/food/note, `used_in_steps`, and:
      status: linked | unlinked | suspect (linked food's name says something the
        original line doesn't) | sub_recipe
      nutrition: ready | approximate | missing_data | cannot_weigh | no_quantity |
        not_linked, with nutrition_note saying why and what fixes it.
    With suggest=true, unlinked/suspect rows get a `suggestion`: the line re-parsed,
    matching food candidates from the Foods database, a `grade` (exact / check /
    candidate / no_match -- explained in `grades`) and a `proposed_edit` you can
    pass to edit_recipe_ingredients as-is or adjusted. Nothing is changed here;
    ask the user when a match is a judgement call."""
    return await recipe_ingredients.review(mealie(), slug, only, suggest)


@mcp.tool(annotations=WRITE)
async def edit_recipe_ingredients(slug: str, edits: list[dict[str, Any]]) -> dict[str, Any]:
    """Change specific ingredients of an existing recipe in place. Untouched
    ingredients, their step links and original scraped text stay exactly as they
    are. All edits are checked first and applied in one save -- if any is invalid,
    nothing changes.

    Address ingredients by `ref` (from review_recipe_ingredients; preferred) or by
    1-based position in the recipe as it was before this call. Each edit is one of:
      {"ref", "food_id"?, "unit_id"?, "quantity"?, "note"?, "title"?}  update;
          omitted keys unchanged, null clears (food_id: null unlinks)
      {"ref", "create_food": "name"} / "create_unit"  link to that food/unit,
          creating it only if no item with that exact name exists
      {"ref", "text": "2 cups rice"}  re-parse the line; rejected if the parse
          would lose words (then set fields explicitly)
      {"ref", "delete": true}
      {"insert_after": ref | position | "start" | "end", "text" | fields...}  add
      {"ref", "move_after": ref | position | "start", ...optional field changes}
    Returns the updated rows (same shape as the review, without suggestions)."""
    return await recipe_ingredients.edit(mealie(), slug, edits)


# ---------------------------------------------------------------- ingredient parser

@mcp.tool(annotations=READ)
async def parse_ingredients(
    lines: list[str],
    parser: Literal["nlp", "brute"] = "nlp",
) -> list[dict[str, Any]]:
    """Parse free-text ingredient lines ("2 cups flour") into structured
    quantity/unit/food, matched against Mealie's existing Foods/Units where
    possible (food_id/unit_id are null when nothing matched). Read-only --
    create_recipe and update_recipe parse automatically; use this to preview or
    to pick different matches, then pass structured ingredient objects."""
    lines = [line for line in lines if line.strip()]
    if not lines:
        return []
    results = await parse_lines(mealie(), lines, parser)
    return [parsed_ingredient_summary(p, line) for p, line in zip(results, lines, strict=True)]


# ---------------------------------------------------------------- nutrition

MAX_LOOKUP_NAMES = 10


@mcp.tool(annotations=READ_EXTERNAL)
async def lookup_nutrition(
    food_name: str | None = None,
    food_names: list[str] | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    """Search USDA FoodData Central and Open Food Facts for a food's nutrition,
    per 100g, keyed: calories, protein_g, fat_g, saturated_fat_g, trans_fat_g,
    carbs_g, fiber_g, sugar_g, sodium_mg, cholesterol_mg (absent = source has no
    figure). USDA is best for whole/generic foods (Foundation and SR Legacy
    entries are listed first), Open Food Facts for branded products. Pick the
    best candidate and pass its per_100g to set_food_nutrition. If one source is
    down or rate-limited the reply still has the other plus an `errors` entry.

    Pass food_name for one food (default 5 candidates per source), or food_names
    (up to 10) to look several up at once (default 3 per source); the batch reply
    is {"results": {name: result | {"error": ...}}}."""
    if (food_name is None) == (food_names is None):
        raise ToolError("pass exactly one of food_name or food_names")
    if food_name is not None:
        try:
            return await nutrition().search(food_name, max_results or 5)
        except SourceError as e:
            raise ToolError(str(e)) from e

    names = list(dict.fromkeys(n.strip() for n in food_names or [] if n and n.strip()))
    if not names:
        raise ToolError("food_names is empty")
    if len(names) > MAX_LOOKUP_NAMES:
        raise ToolError(f"at most {MAX_LOOKUP_NAMES} names per call (got {len(names)})")
    semaphore = asyncio.Semaphore(3)

    async def one(name: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                return name, await nutrition().search(name, max_results or 3)
            except SourceError as e:
                return name, {"error": str(e)}

    return {"results": dict(await asyncio.gather(*(one(n) for n in names)))}


@mcp.tool(annotations=WRITE)
async def set_food_nutrition(
    food_id: str | None = None,
    per_100g: dict[str, float] | None = None,
    source: Literal["usda", "off", "manual"] = "manual",
    source_id: str | None = None,
    grams_per_ml: float | None = None,
    portion_grams: dict[str, float] | None = None,
    fetch_measures: bool = True,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Store nutrition data on a Mealie food so compute_recipe_nutrition can use it
    (kept in the food's extras, reused by every recipe using that food).

    With source="usda" and its source_id, the same USDA record's household measures
    are stored too (official weights for 1 cup, 1 tbsp, 1 large, 1 clove...), so
    cups and pieces of this food convert to grams accurately. Values already on the
    food or passed here win; fetch_measures=false skips it.

    per_100g: keys calories, protein_g, fat_g, saturated_fat_g, trans_fat_g,
      carbs_g, fiber_g, sugar_g, sodium_mg, cholesterol_mg -- omit unknowns.
      Replaces any previous per_100g. From lookup_nutrition (source "usda"/"off"
      with its source_id), or source="manual" for label figures converted to per 100g.
    grams_per_ml: density for volume units (e.g. flour 0.53, olive oil 0.92,
      honey 1.42). Without it volume is weighed as water and flagged approximate.
    portion_grams: weight of one of a unit for this food, merged into existing
      values, e.g. {"each": 50} for "2 eggs" (no unit), {"clove": 5} for garlic,
      {"cup": 125} for flour. A value of 0 removes that entry.
    Pass any combination; at least one is required.

    items: batch several foods in one call -- a list of objects with the same keys
    (food_id, per_100g, source, source_id, grams_per_ml, portion_grams, fetch_measures). Runs in
    order; one failure doesn't stop the rest; reply lists results and errors."""
    if items is None:
        if food_id is None:
            raise ToolError("pass food_id (or items for a batch)")
        return await _set_food_nutrition_one(
            food_id, per_100g, source, source_id, grams_per_ml, portion_grams, fetch_measures
        )
    if any(v is not None for v in (food_id, per_100g, source_id, grams_per_ml, portion_grams)):
        raise ToolError("pass either items (batch) or food_id and its values (single), not both")
    if not items:
        raise ToolError("items is empty")
    results, errors = [], []
    for i, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise ToolError("each item must be an object")
            unknown = set(item) - _NUTRITION_ITEM_KEYS
            if unknown:
                raise ToolError(f"unknown key(s) {sorted(unknown)}; items accept {sorted(_NUTRITION_ITEM_KEYS)}")
            if not item.get("food_id"):
                raise ToolError("food_id is required")
            if item.get("source", "manual") not in ("usda", "off", "manual"):
                raise ToolError("source must be usda, off or manual")
            saved = await _set_food_nutrition_one(
                item["food_id"],
                item.get("per_100g"),
                item.get("source", "manual"),
                item.get("source_id"),
                item.get("grams_per_ml"),
                item.get("portion_grams"),
                bool(item.get("fetch_measures", fetch_measures)),
            )
            results.append({"index": i, **saved})
        except ToolError as e:
            errors.append({"index": i, "food_id": (item or {}).get("food_id") if isinstance(item, dict) else None, "error": str(e)})
    return {"succeeded": len(results), "failed": len(errors), "results": results, "errors": errors}


_NUTRITION_ITEM_KEYS = {"food_id", "per_100g", "source", "source_id", "grams_per_ml", "portion_grams", "fetch_measures"}


async def _set_food_nutrition_one(
    food_id: str,
    per_100g: Any,
    source: str,
    source_id: str | None,
    grams_per_ml: Any,
    portion_grams: Any,
    fetch_measures: bool = True,
) -> dict[str, Any]:
    if per_100g is None and grams_per_ml is None and portion_grams is None:
        raise ToolError("pass per_100g, grams_per_ml and/or portion_grams")
    try:
        if per_100g is not None and not isinstance(per_100g, dict):
            raise ValueError("per_100g must be an object")
        if portion_grams is not None and not isinstance(portion_grams, dict):
            raise ValueError("portion_grams must be an object")
        clean = validate_per_100g(per_100g) if per_100g is not None else None
        if grams_per_ml is not None and (
            isinstance(grams_per_ml, bool) or not isinstance(grams_per_ml, int | float) or not (0 <= grams_per_ml <= 25)
        ):
            raise ValueError("grams_per_ml must be a number between 0 (removes it) and 25")
        current = await taxonomy.get_existing(mealie(), "foods", food_id, role="food_id")
    except ValueError as e:
        raise ToolError(str(e)) from e

    measures_report: dict[str, Any] | None = None
    if fetch_measures and source == "usda" and source_id:
        existing = read_food_profile(current)
        try:
            measures = await nutrition().usda_household_measures(source_id)
        except SourceError as e:
            measures_report = {"warning": f"couldn't fetch USDA household measures: {e}"}
        else:
            explicit = {k for k in (portion_grams or {})}
            added = {
                k: v for k, v in measures["portion_grams"].items()
                if k not in existing.portion_grams and k not in explicit
            }
            if added:
                portion_grams = {**added, **(portion_grams or {})}
            density_added = None
            if grams_per_ml is None and existing.grams_per_ml is None and measures.get("grams_per_ml"):
                grams_per_ml = density_added = measures["grams_per_ml"]
            measures_report = {
                "source": f"USDA FDC {source_id} ({measures.get('description')})",
                "portion_grams_added": sorted(added),
                "grams_per_ml_added": density_added,
            }
            if not measures["portion_grams"]:
                measures_report["note"] = "USDA lists no household measures for this record"
    try:
        current["extras"] = encode_food_extras(
            current.get("extras"),
            per_100g=clean,
            source=source,
            source_id=source_id,
            grams_per_ml=grams_per_ml,
            portion_grams=portion_grams,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e
    saved = await mealie().put(f"/api/foods/{food_id}", current)
    result = taxonomy.taxonomy_item("foods", saved)
    if measures_report:
        result["household_measures"] = measures_report
    return result


@mcp.tool(annotations=WRITE)
async def compute_recipe_nutrition(
    slug: str,
    save: bool = True,
    allow_partial: bool = False,
    servings: float | None = None,
) -> dict[str, Any]:
    """Estimate per-serving nutrition for a recipe from its linked foods' stored
    nutrition (set_food_nutrition) and ingredient quantities, and optionally write
    it into the recipe's nutrition block.

    The reply is explicit about gaps: `unaccounted` (ingredients not linked to a
    food, food without nutrition data, or a unit it can't weigh -- each with the
    fix), `approximate` (volume weighed as water), `incomplete_nutrients`
    (counted foods missing some nutrient), `skipped` (no quantity, e.g. "salt to
    taste"). Unknown nutrients are null, never 0.

    save=true writes only when every ingredient was accounted for, unless
    allow_partial=true -- so a recipe's existing (often publisher-supplied)
    nutrition isn't overwritten by an undercount. Nutrients with no data keep
    their existing value. servings overrides the recipe's own servings."""
    recipe = await mealie().get(f"/api/recipes/{slug}")
    ingredients = recipe.get("recipeIngredient") or []
    if not ingredients:
        raise ToolError(f"recipe {slug!r} has no ingredients to compute nutrition from")
    if servings is not None and servings <= 0:
        raise ToolError("servings must be > 0")
    result = estimate_recipe_nutrition(ingredients, servings or recipe.get("recipeServings"))
    previous = {k: v for k, v in (recipe.get("nutrition") or {}).items() if v not in (None, "")}
    result["previous_nutrition"] = previous

    reason = None
    if not save:
        reason = "save=false"
    elif not result["counted"]:
        reason = "no ingredient could be counted"
    elif not result["complete"] and not allow_partial:
        reason = (
            f"{len(result['unaccounted'])} ingredient(s) unaccounted for; fix them or pass "
            "allow_partial=true to save this undercount"
        )
    if reason:
        result["saved"] = False
        result["not_saved_reason"] = reason
        return result

    patch, left_unchanged = nutrition_patch(result["per_serving"], recipe.get("nutrition"))
    await mealie().patch(f"/api/recipes/{slug}", {"nutrition": patch})
    result["saved"] = True
    if left_unchanged:
        result["left_unchanged"] = [NUTRIENTS[k] for k in left_unchanged]
    return result


# ---------------------------------------------------------------- shopping lists

@mcp.tool(annotations=READ)
async def list_shopping_lists() -> list[dict[str, Any]]:
    """List shopping lists (id + name). Use get_shopping_list for the items."""
    data = await mealie().get("/api/households/shopping/lists", {"perPage": -1})
    return [{"id": s["id"], "name": s["name"]} for s in data.get("items", [])]


@mcp.tool(annotations=READ)
async def get_shopping_list(list_id: str, include_checked: bool = False) -> dict[str, Any]:
    """Get a shopping list with its items. Unchecked items only unless include_checked=true."""
    s = await mealie().get(f"/api/households/shopping/lists/{list_id}")
    items = [shopping_item(i) for i in s.get("listItems", [])]
    if not include_checked:
        items = [i for i in items if not i["checked"]]
    return {"id": s["id"], "name": s["name"], "items": items}


@mcp.tool(annotations=WRITE)
async def add_shopping_items(list_id: str, items: list[str]) -> dict[str, Any]:
    """Add free-text items ("2 lemons", "olive oil") to a shopping list."""
    payload = [{"shoppingListId": list_id, "note": text.strip(), "checked": False} for text in items if text.strip()]
    if not payload:
        raise ToolError("no items given")
    res = await mealie().post("/api/households/shopping/items/create-bulk", payload)
    created = res.get("createdItems", res) if isinstance(res, dict) else res
    return {"added": [shopping_item(i) for i in created]}


@mcp.tool(annotations=WRITE)
async def update_shopping_item(
    item_id: str,
    checked: bool | None = None,
    text: str | None = None,
    quantity: float | None = None,
) -> dict[str, Any]:
    """Check/uncheck, rename or change the quantity of one shopping list item."""
    item = await mealie().get(f"/api/households/shopping/items/{item_id}")
    if checked is not None:
        item["checked"] = checked
    if text is not None:
        item["note"] = text
        item["display"] = text
        item["food"] = None
        item["foodId"] = None
        item["unit"] = None
        item["unitId"] = None
    if quantity is not None:
        item["quantity"] = quantity
    return shopping_item(await mealie().put(f"/api/households/shopping/items/{item_id}", item))


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_shopping_items(item_ids: list[str]) -> str:
    """Remove items from a shopping list by item id."""
    for item_id in item_ids:
        await mealie().delete(f"/api/households/shopping/items/{item_id}")
    return f"Deleted {len(item_ids)} item(s)"


@mcp.tool(annotations=WRITE)
async def add_recipe_to_shopping_list(list_id: str, recipe_id: str, scale: float = 1.0) -> dict[str, Any]:
    """Add all ingredients of a recipe (by recipe id, not slug) to a shopping list."""
    try:
        await mealie().post(
            f"/api/households/shopping/lists/{list_id}/recipe",
            [{"recipeId": recipe_id, "recipeIncrementQuantity": scale}],
        )
    except MealieError as e:
        if e.status_code not in (404, 405, 422):
            raise
        # Older Mealie: only the (now deprecated) per-recipe endpoint exists.
        await mealie().post(
            f"/api/households/shopping/lists/{list_id}/recipe/{recipe_id}",
            {"recipeIncrementQuantity": scale},
        )
    return await get_shopping_list(list_id)


# ---------------------------------------------------------------- meal plans

@mcp.tool(annotations=READ)
async def get_meal_plan(start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    """Get meal plan entries between two dates (inclusive). Defaults to today through +6 days."""
    start = _iso_date(start_date, "start_date") if start_date else date.today()
    end = _iso_date(end_date, "end_date") if end_date else start + timedelta(days=6)
    data = await mealie().get(
        "/api/households/mealplans",
        {"start_date": start.isoformat(), "end_date": end.isoformat(), "perPage": -1, "orderBy": "date", "orderDirection": "asc"},
    )
    return [plan_entry(e) for e in data.get("items", [])]


@mcp.tool(annotations=WRITE)
async def add_meal_plan_entry(
    day: str,
    entry_type: Literal["breakfast", "lunch", "dinner", "side", "snack", "drink", "dessert"] = "dinner",
    recipe_id: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Plan a meal on a date (YYYY-MM-DD). Give a recipe_id (from search_recipes/get_recipe)
    or a free-text title for something not in Mealie."""
    if not recipe_id and not title:
        raise ToolError("provide recipe_id or title")
    body: dict[str, Any] = {"date": _iso_date(day, "day").isoformat(), "entryType": entry_type}
    if recipe_id:
        body["recipeId"] = recipe_id
    if title:
        body["title"] = title
    if note:
        body["text"] = note
    return plan_entry(await mealie().post("/api/households/mealplans", body))


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_meal_plan_entry(entry_id: str) -> str:
    """Remove a meal plan entry by its id (from get_meal_plan)."""
    await mealie().delete(f"/api/households/mealplans/{entry_id}")
    return f"Deleted meal plan entry {entry_id}"


# ---------------------------------------------------------------- escape hatch

@mcp.tool(annotations=READ)
async def mealie_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Read-only GET against any Mealie API path (must start with /api/), for things the other
    tools don't cover. Response is returned raw and may be large."""
    return await mealie().get(path, params)


# ---------------------------------------------------------------- entrypoint

def main() -> None:
    log.info("Mealie: %s | public URL: %s | MCP endpoint: %s", settings.mealie_url, settings.public_base, settings.mcp_endpoint)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.mcp_host,
    )
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
