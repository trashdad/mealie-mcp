"""Thin async client for the Mealie REST API (v1 API, Mealie 2.x/3.x paths)."""

from __future__ import annotations

from typing import Any

import httpx

from mcp.server.mcpserver.exceptions import ToolError


class MealieError(ToolError):
    """Raised for any Mealie API / network failure. Subclasses ToolError so the
    message reaches the model instead of a generic 'error executing tool'.

    `status_code` is the HTTP status Mealie answered with (None for network
    failures), so callers can tell a 409 conflict from a 404 or a 500."""

    def __init__(self, message: str, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class MealieClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
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
            raise MealieError(f"Mealie {method} {path} -> {r.status_code}: {detail}", r.status_code, detail)
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            raise MealieError(f"Mealie {method} {path} returned a non-JSON response: {r.text[:200]!r}") from e

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
