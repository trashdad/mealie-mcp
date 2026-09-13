"""Thin async client for the Mealie REST API (v1 API, Mealie 2.x/3.x paths)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from mcp.server.mcpserver.exceptions import ToolError


class MealieError(ToolError):
    """Raised for any Mealie API / network failure. Subclasses ToolError so the
    message reaches the model instead of a generic 'error executing tool'."""


class MealieClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, **kw: Any) -> Any:
        if not path.startswith("/api/"):
            raise MealieError("path must start with /api/")
        try:
            r = await self._http.request(method, path, **kw)
            # Mealie < 2.0 kept household resources under /api/groups/...
            if r.status_code == 404 and "/api/households/" in path:
                r = await self._http.request(method, path.replace("/api/households/", "/api/groups/", 1), **kw)
        except httpx.HTTPError as e:
            raise MealieError(f"Could not reach Mealie ({method} {path}): {e.__class__.__name__}: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise MealieError(f"Mealie {method} {path} -> {r.status_code}: {detail}")
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, json=json, params=params)

    async def put(self, path: str, json: Any = None) -> Any:
        return await self.request("PUT", path, json=json)

    async def patch(self, path: str, json: Any = None) -> Any:
        return await self.request("PATCH", path, json=json)

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("DELETE", path, params=params)


# ---------- ingredient payload building (used by create_recipe/update_recipe) ----------

def build_ingredient_payload(item: dict, original: str = "") -> dict:
    """Turn one ingredient (already parsed, or already structured) into the shape
    Mealie's recipe PATCH wants. Food/unit references need an id to link to an
    existing entry; anything unresolved folds back into free-text `note` rather
    than being silently dropped, so nothing about the original line is lost."""
    food, unit = item.get("food"), item.get("unit")
    payload: dict[str, Any] = {"quantity": item.get("quantity") or 0}

    unresolved: list[str] = []
    for value, key in ((unit, "unit"), (food, "food")):
        if isinstance(value, dict) and value.get("id"):
            payload[key] = {"id": value["id"]}
        elif isinstance(value, dict) and value.get("name"):
            unresolved.append(value["name"])
        elif isinstance(value, str):
            unresolved.append(value)

    if unresolved and "food" not in payload and "unit" not in payload and original:
        # Nothing resolved at all -- the original line reads better than a
        # reassembled fragment ("pinch saffron"), and keeping the parsed
        # quantity here would double it up ("500 500 g flour").
        note = " ".join(p for p in [original, item.get("note") or ""] if p).strip()
        payload["quantity"] = 0
    else:
        note = " ".join(p for p in [*unresolved, item.get("note") or ""] if p).strip()
    payload["note"] = note
    payload["display"] = note
    payload["originalText"] = item.get("original_text") or original or note
    # Mealie mints reference ids client-side; a null one fails validation.
    payload["referenceId"] = item.get("reference_id") or str(uuid.uuid4())
    if item.get("title"):
        payload["title"] = item["title"]
    return payload


# ---------- response shaping (keep tool output small for the model) ----------

def _names(items: list[dict] | None) -> list[str]:
    return [i.get("name", "") for i in (items or [])]


def recipe_summary(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "slug": r.get("slug"),
        "name": r.get("name"),
        "description": r.get("description") or "",
        "tags": _names(r.get("tags")),
        "categories": _names(r.get("recipeCategory")),
        "rating": r.get("rating"),
        "servings": r.get("recipeServings"),
        "yield": r.get("recipeYield"),
        "total_time": r.get("totalTime"),
        "prep_time": r.get("prepTime"),
        "cook_time": r.get("performTime") or r.get("cookTime"),
        "last_made": r.get("lastMade"),
    }


def recipe_full(r: dict) -> dict:
    out = recipe_summary(r)
    ingredients = []
    for ing in r.get("recipeIngredient") or []:
        text = ing.get("display") or ing.get("note") or ing.get("originalText") or ""
        if ing.get("title"):
            ingredients.append(f"[{ing['title']}]")
        ingredients.append(text.strip())
    steps = []
    for i, step in enumerate(r.get("recipeInstructions") or [], start=1):
        title = f"{step['title']}: " if step.get("title") else ""
        steps.append(f"{i}. {title}{(step.get('text') or '').strip()}")
    out.update(
        {
            "ingredients": [x for x in ingredients if x],
            "instructions": steps,
            "notes": [{"title": n.get("title"), "text": n.get("text")} for n in (r.get("notes") or [])],
            "nutrition": {k: v for k, v in (r.get("nutrition") or {}).items() if v},
            "source_url": r.get("orgURL"),
            "tools": _names(r.get("tools")),
        }
    )
    return out


def shopping_item(i: dict) -> dict:
    return {
        "id": i.get("id"),
        "text": i.get("display") or i.get("note") or "",
        "quantity": i.get("quantity"),
        "unit": (i.get("unit") or {}).get("name"),
        "food": (i.get("food") or {}).get("name"),
        "note": i.get("note"),
        "checked": i.get("checked", False),
        "label": (i.get("label") or {}).get("name"),
    }


def taxonomy_item(item: dict) -> dict:
    """Shapes a food or unit for manage_taxonomy's replies. Unit-only fields are
    simply absent on a food and vice versa, so one shape covers both."""
    extras = item.get("extras") or {}
    out = {
        "id": item.get("id"),
        "name": item.get("name"),
        "plural_name": item.get("pluralName"),
        "description": item.get("description") or "",
    }
    if "standardQuantity" in item or "standardUnit" in item:  # unit-only
        out["standard_quantity"] = item.get("standardQuantity")
        out["standard_unit"] = item.get("standardUnit")
        out["abbreviation"] = item.get("abbreviation") or ""
    if extras.get("nutrition_per_100g"):  # food-only, when we've set it
        out["nutrition_per_100g"] = extras["nutrition_per_100g"]
        out["nutrition_source"] = extras.get("nutrition_source")
    return out


def parsed_ingredient_summary(p: dict) -> dict:
    ing = p.get("ingredient", {})
    food = ing.get("food") or {}
    unit = ing.get("unit") or {}
    return {
        "input": p.get("input"),
        "quantity": ing.get("quantity"),
        "unit_name": unit.get("name"),
        "unit_id": unit.get("id"),  # present only if it matched an existing unit
        "food_name": food.get("name"),
        "food_id": food.get("id"),  # present only if it matched an existing food
        "note": ing.get("note") or "",
        "confidence": p.get("confidence", {}).get("average"),
    }


# Rough mass-equivalent for common units when Mealie's own unit record doesn't
# have standardQuantity/standardUnit set. These are approximate (water-density
# assumptions for volume units) and only used as a fallback -- accuracy improves
# once a unit's standardQuantity/standardUnit is set via update_unit.
_FALLBACK_GRAMS_PER_UNIT = {
    "g": 1, "gram": 1, "grams": 1,
    "kg": 1000, "kilogram": 1000,
    "oz": 28.35, "ounce": 28.35,
    "lb": 453.6, "pound": 453.6,
    "ml": 1, "milliliter": 1,
    "l": 1000, "liter": 1,
    "cup": 240, "cups": 240,
    "tbsp": 15, "tablespoon": 15,
    "tsp": 5, "teaspoon": 5,
}

_NUTRIENT_KEYS = ["calories", "protein_g", "fat_g", "carbs_g", "fiber_g", "sugar_g", "sodium_mg"]


def _grams_for(quantity: float, unit: dict | None) -> tuple[float | None, bool]:
    """Returns (grams, is_estimate). None grams means we couldn't convert at all."""
    if unit is None:
        return None, False
    std_qty, std_unit = unit.get("standardQuantity"), (unit.get("standardUnit") or "").lower()
    if std_qty and std_unit in ("g", "ml"):
        return quantity * std_qty, False
    fallback = _FALLBACK_GRAMS_PER_UNIT.get((unit.get("name") or "").strip().lower())
    if fallback:
        return quantity * fallback, True
    return None, False


def estimate_recipe_nutrition(ingredients: list[dict], servings: float) -> dict:
    """Sum per-100g nutrition (stored in each food's extras.nutrition_per_100g) across
    a recipe's structured ingredients, scaled by quantity/unit, divided by servings to
    match Mealie's per-serving nutrition convention. Ingredients without a linked food,
    or whose food has no cached nutrition data, are reported separately rather than
    silently skipped."""
    totals = dict.fromkeys(_NUTRIENT_KEYS, 0.0)
    used_estimate_conversion = False
    unmatched: list[str] = []

    for ing in ingredients:
        food = ing.get("food")
        label = (food or {}).get("name") or ing.get("note") or ing.get("display") or "(unnamed ingredient)"
        per_100g = ((food or {}).get("extras") or {}).get("nutrition_per_100g")
        if not food or not per_100g:
            unmatched.append(f"{label}: no linked food with cached nutrition")
            continue
        grams, is_estimate = _grams_for(ing.get("quantity") or 0, ing.get("unit"))
        if grams is None:
            unmatched.append(f"{label}: couldn't convert its unit to grams/ml")
            continue
        used_estimate_conversion = used_estimate_conversion or is_estimate
        factor = grams / 100.0
        for key in _NUTRIENT_KEYS:
            val = per_100g.get(key)
            if val is not None:
                totals[key] += val * factor

    servings = servings or 1
    per_serving = {k: round(v / servings, 1) for k, v in totals.items()}
    return {
        "per_serving": per_serving,
        "servings_used": servings,
        "unmatched_ingredients": unmatched,
        "used_approximate_unit_conversion": used_estimate_conversion,
    }


def plan_entry(e: dict) -> dict:
    recipe = e.get("recipe") or {}
    return {
        "id": e.get("id"),
        "date": e.get("date"),
        "type": e.get("entryType"),
        "title": e.get("title") or recipe.get("name") or "",
        "text": e.get("text") or "",
        "recipe_id": e.get("recipeId"),
        "recipe_slug": recipe.get("slug"),
    }
