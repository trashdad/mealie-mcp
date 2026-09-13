"""Mealie MCP server: Streamable HTTP transport + built-in OAuth, for Claude custom connectors."""

from __future__ import annotations

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
from mcp_types import ToolAnnotations

from .auth import MealieOAuthProvider
from .config import Settings
from .mealie import (
    MealieClient,
    build_ingredient_payload,
    estimate_recipe_nutrition,
    parsed_ingredient_summary,
    plan_entry,
    recipe_full,
    recipe_summary,
    shopping_item,
    taxonomy_item,
)
from .nutrition import NutritionClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mealie_mcp")

settings = Settings()
oauth = MealieOAuthProvider(settings)
_mealie: MealieClient | None = None
_nutrition: NutritionClient | None = None


def mealie() -> MealieClient:
    assert _mealie is not None, "Mealie client not initialised"
    return _mealie


def nutrition() -> NutritionClient:
    assert _nutrition is not None, "Nutrition client not initialised"
    return _nutrition


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    global _mealie, _nutrition
    _mealie = MealieClient(str(settings.mealie_url), settings.mealie_api_token)
    _nutrition = NutritionClient(settings.mcp_data_dir, settings.usda_api_key)
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
        "addressed by slug (preferred) or id."
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
    return PlainTextResponse(f"Mealie MCP server. Add {settings.mcp_endpoint} as a custom connector in Claude.\n")


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


async def _ingredients_payload(items: list[Any]) -> list[dict[str, Any]]:
    """Accepts a mix of plain strings ("2 cups flour") and structured dicts
    ({"quantity": 2, "food_id": "...", "unit_id": "...", "note": "diced"}).
    Plain strings are batch-parsed through Mealie's own ingredient parser in a
    single call, matched against existing Foods/Units where possible; anything
    unmatched folds back into free text rather than being dropped."""
    strings = [i for i in items if isinstance(i, str)]
    parsed_by_text: dict[str, dict] = {}
    if strings:
        results = await mealie().post("/api/parser/ingredients", {"parser": "nlp", "ingredients": strings})
        for original, result in zip(strings, results):
            parsed_by_text[original] = result.get("ingredient", {})

    payload = []
    for item in items:
        if isinstance(item, str):
            payload.append(build_ingredient_payload(parsed_by_text.get(item, {}), original=item))
        else:
            # Already-structured input: {"food_id": ..., "unit_id": ...} -> {"food": {"id": ...}}
            normalized = dict(item)
            if item.get("food_id"):
                normalized["food"] = {"id": item["food_id"]}
            if item.get("unit_id"):
                normalized["unit"] = {"id": item["unit_id"]}
            payload.append(build_ingredient_payload(normalized))
    return payload


@mcp.tool(annotations=WRITE)
async def create_recipe(
    name: str,
    ingredients: list[Any],
    instructions: list[str],
    description: str = "",
    servings: int | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Create a recipe from scratch. Instructions are one string per step; times
    are free text ("20 minutes").

    Ingredients can mix plain strings and structured dicts in the same list:
      - "2 cups flour" -- run through Mealie's parser automatically, linked to
        an existing food/unit when one matches, otherwise kept as free text.
      - {"quantity": 2, "food_id": "...", "unit_id": "...", "note": "diced"} --
        already resolved (food_id/unit_id from parse_ingredients or manage_taxonomy).
    Use parse_ingredients first if you want to see/adjust matches before saving
    rather than trusting the automatic parse."""
    slug = await mealie().post("/api/recipes", {"name": name})
    patch: dict[str, Any] = {
        "description": description,
        "recipeIngredient": await _ingredients_payload(ingredients),
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
    return recipe_full(await mealie().patch(f"/api/recipes/{slug}", patch))


@mcp.tool(annotations=WRITE)
async def update_recipe(
    slug: str,
    name: str | None = None,
    description: str | None = None,
    ingredients: list[Any] | None = None,
    instructions: list[str] | None = None,
    servings: int | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    rating: int | None = None,
) -> dict[str, Any]:
    """Update parts of a recipe. Only the fields you pass are changed; ingredients/
    instructions replace the whole list when given. Ingredients accept the same
    mixed plain-string/structured-dict input as create_recipe -- see its docstring."""
    patch: dict[str, Any] = {}
    if name is not None:
        patch["name"] = name
    if description is not None:
        patch["description"] = description
    if ingredients is not None:
        patch["recipeIngredient"] = await _ingredients_payload(ingredients)
    if instructions is not None:
        patch["recipeInstructions"] = [{"text": step} for step in instructions]
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
        raise ValueError("nothing to update")
    return recipe_full(await mealie().patch(f"/api/recipes/{slug}", patch))


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
# Mealie's "Foods" database is what recipe ingredients link to once parsed into
# structured form (create_recipe/update_recipe do this automatically for plain-
# text ingredients). Nutrition data lives in each food's `extras` field (Mealie's
# generic per-item key/value store) under `nutrition_per_100g`, since Mealie
# itself only stores a static, manually-entered nutrition total per recipe.

_TAXONOMY_PATHS = {"foods": "/api/foods", "units": "/api/units"}
_MERGE_KEYS = {"foods": ("fromFood", "toFood"), "units": ("fromUnit", "toUnit")}


async def _taxonomy_apply(
    resource: str,
    action: str,
    name: str | None = None,
    item_id: str | None = None,
    data: dict[str, Any] | None = None,
    merge_into: str | None = None,
) -> dict[str, Any]:
    path = _TAXONOMY_PATHS[resource]

    if action == "create":
        if not name:
            raise ValueError("create requires a name")
        return taxonomy_item(await mealie().post(path, {**(data or {}), "name": name}))

    if action == "merge":
        if not item_id or not merge_into:
            raise ValueError("merge requires item_id (the one being folded in) and merge_into (the one kept)")
        from_key, to_key = _MERGE_KEYS[resource]
        merged = await mealie().put(f"{path}/merge", {from_key: item_id, to_key: merge_into})
        return {**taxonomy_item(merged or {}), "merged": item_id, "into": merge_into}

    if action == "update":
        if not item_id:
            raise ValueError("update requires an item_id (from action='list')")
        if name is None and not data:
            raise ValueError("update requires name and/or data")
        current = await mealie().get(f"{path}/{item_id}")
        payload = {**current, **(data or {})}
        if name is not None:
            payload["name"] = name
        return taxonomy_item(await mealie().put(f"{path}/{item_id}", payload))

    if not item_id:
        raise ValueError("delete requires an item_id (from action='list')")
    try:
        await mealie().delete(f"{path}/{item_id}")
    except Exception as e:  # noqa: BLE001
        # Mealie refuses the delete (409-style) if a recipe/shopping item still
        # references this row. Merge is the fix; surface that instead of a raw error.
        raise ValueError(
            f"{resource} item {item_id!r} is still referenced elsewhere, so Mealie refused "
            f"the delete ({e}). Merge it into another item instead: action='merge', "
            f"item_id={item_id!r}, merge_into=<the one to keep>."
        ) from e
    return {"deleted": item_id}


@mcp.tool(annotations=WRITE)
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
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """List, create, update, merge, or delete entries in Mealie's Foods or Units
    database -- what recipe ingredients link to (create_recipe/update_recipe link
    them automatically for plain-text input; use this for browsing, cleanup, and
    tidying duplicates).

    action="list" (default): query/page/per_page apply; everything else ignored.
    action="create": needs `name`; `data` may add e.g. {"pluralName": "..."} for
      foods, or {"standardQuantity": 240, "standardUnit": "g"} for units (set
      this so compute_recipe_nutrition can convert the unit to grams exactly
      instead of approximating).
    action="update": needs `item_id`; `name` and/or `data` change fields, others
      keep their current value.
    action="merge": needs `item_id` (folded away) and `merge_into` (kept);
      repoints every recipe/shopping-item using `item_id` to `merge_into` and
      deletes `item_id`. Use this instead of delete when duplicates exist (e.g.
      "jasmine rice" and "Jasmine Rice") -- delete alone fails if anything still
      references the item.
    action="delete": needs `item_id`. Fails (with a merge suggestion) if the
      item is still referenced anywhere.

    `items`: batch any non-list action -- a list of dicts using the same keys
    (name, item_id, data, merge_into), each run in one pass. Errors on one item
    don't stop the rest; the reply separates results from errors.
    """
    if resource not in _TAXONOMY_PATHS:
        raise ValueError(f"resource must be one of {', '.join(_TAXONOMY_PATHS)}")

    if action == "list":
        if items:
            raise ValueError("items batches writes, not list")
        params: dict[str, Any] = {"page": page, "perPage": min(max(per_page, 1), 50)}
        if query:
            params["search"] = query
        data_page = await mealie().get(_TAXONOMY_PATHS[resource], params)
        return {
            "page": data_page.get("page"),
            "total_pages": data_page.get("total_pages"),
            "total": data_page.get("total"),
            "items": [taxonomy_item(i) for i in data_page.get("items", [])],
        }

    if items:
        results, errors = [], []
        for i, batch_item in enumerate(items):
            try:
                results.append(
                    await _taxonomy_apply(
                        resource,
                        action,
                        name=batch_item.get("name"),
                        item_id=batch_item.get("item_id"),
                        data=batch_item.get("data"),
                        merge_into=batch_item.get("merge_into"),
                    )
                )
            except Exception as e:  # noqa: BLE001
                errors.append({"index": i, "item": batch_item, "error": str(e)})
        return {"results": results, "errors": errors}

    return await _taxonomy_apply(resource, action, name=name, item_id=item_id, data=data, merge_into=merge_into)


# ---------------------------------------------------------------- ingredient parser

@mcp.tool(annotations=READ)
async def parse_ingredients(
    lines: list[str],
    parser: Literal["nlp", "brute"] = "nlp",
) -> list[dict[str, Any]]:
    """Parse free-text ingredient lines ("2 cups flour") into structured
    quantity/unit/food, matched against Mealie's existing Foods/Units where
    possible. Read-only/inspection only -- create_recipe and update_recipe run
    this automatically, so you only need this tool to preview or adjust matches
    before saving (e.g. picking a different food than the parser's top guess)."""
    data = await mealie().post("/api/parser/ingredients", {"parser": parser, "ingredients": lines})
    return [parsed_ingredient_summary(p) for p in data]


# ---------------------------------------------------------------- nutrition

@mcp.tool(annotations=READ)
async def lookup_nutrition(food_name: str, max_results: int = 5) -> dict[str, list[dict[str, Any]]]:
    """Search USDA FoodData Central and Open Food Facts for a food's nutrition
    (per 100g: calories, protein_g, fat_g, carbs_g, fiber_g, sugar_g, sodium_mg).
    Returns candidates from both sources for you/the user to pick the best match --
    USDA is generally better for whole/generic foods, Open Food Facts for branded
    products. Pick one and call set_food_nutrition with its per_100g data."""
    return await nutrition().search_both(food_name, max_results)


@mcp.tool(annotations=WRITE)
async def set_food_nutrition(
    food_id: str,
    per_100g: dict[str, float],
    source: Literal["usda", "off", "manual"] = "usda",
    source_id: str | None = None,
) -> dict[str, Any]:
    """Attach nutrition data (per 100g) to a Mealie food, so compute_recipe_nutrition
    can use it. per_100g keys: calories, protein_g, fat_g, carbs_g, fiber_g, sugar_g,
    sodium_mg (omit any you don't have). Get values from lookup_nutrition, or pass
    source="manual" for numbers off a package label yourself."""
    current = await mealie().get(f"/api/foods/{food_id}")
    extras = current.get("extras") or {}
    extras["nutrition_per_100g"] = per_100g
    extras["nutrition_source"] = source
    if source_id:
        extras["nutrition_source_id"] = source_id
    current["extras"] = extras
    return food_summary(await mealie().put(f"/api/foods/{food_id}", current))


@mcp.tool(annotations=WRITE)
async def compute_recipe_nutrition(slug: str, save: bool = True) -> dict[str, Any]:
    """Roll up per-serving nutrition for a recipe from its linked ingredients'
    cached nutrition (set via set_food_nutrition) and quantities, and (if save=true)
    write the result into the recipe's nutrition field. Reports any ingredients it
    couldn't account for (unlinked to a food, or that food has no nutrition cached)
    so you know the total is a floor, not necessarily complete. Requires ingredients
    to be structured (see update_recipe_ingredients_structured) -- free-text
    ingredients are always reported as unmatched."""
    recipe = await mealie().get(f"/api/recipes/{slug}")
    result = estimate_recipe_nutrition(recipe.get("recipeIngredient") or [], recipe.get("recipeServings") or 1)
    if save:
        n = result["per_serving"]
        nutrition_patch = {
            "calories": str(n.get("calories", 0)),
            "proteinContent": str(n.get("protein_g", 0)),
            "fatContent": str(n.get("fat_g", 0)),
            "carbohydrateContent": str(n.get("carbs_g", 0)),
            "fiberContent": str(n.get("fiber_g", 0)),
            "sugarContent": str(n.get("sugar_g", 0)),
            "sodiumContent": str(n.get("sodium_mg", 0)),
        }
        await mealie().patch(f"/api/recipes/{slug}", {"nutrition": nutrition_patch})
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
        raise ValueError("no items given")
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
    await mealie().post(
        f"/api/households/shopping/lists/{list_id}/recipe/{recipe_id}",
        {"recipeIncrementQuantity": scale},
    )
    return await get_shopping_list(list_id)


# ---------------------------------------------------------------- meal plans

@mcp.tool(annotations=READ)
async def get_meal_plan(start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    """Get meal plan entries between two dates (inclusive). Defaults to today through +6 days."""
    start = date.fromisoformat(start_date) if start_date else date.today()
    end = date.fromisoformat(end_date) if end_date else start + timedelta(days=6)
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
        raise ValueError("provide recipe_id or title")
    body: dict[str, Any] = {"date": date.fromisoformat(day).isoformat(), "entryType": entry_type}
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
