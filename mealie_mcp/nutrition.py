"""Nutrition lookups against USDA FoodData Central and Open Food Facts, with a
small on-disk cache so the same ingredient name isn't re-queried every time.

Both sources return figures per 100g (or per 100ml, treated the same way here)
so they can be combined and scaled consistently later when a recipe's nutrition
is computed from its linked ingredients.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"

# Nutrient IDs used by USDA FoodData Central for the fields we care about.
# (Full list: https://fdc.nal.usda.gov/api-spec/fdc_api.html#/FDC/getFoodsList)
_USDA_NUTRIENT_IDS = {
    "calories": 1008,       # Energy (kcal)
    "protein_g": 1003,
    "fat_g": 1004,
    "carbs_g": 1005,
    "fiber_g": 1079,
    "sugar_g": 2000,
    "sodium_mg": 1093,
}

# Open Food Facts field names for the same nutrients, per 100g.
_OFF_NUTRIENT_FIELDS = {
    "calories": "energy-kcal_100g",
    "protein_g": "proteins_100g",
    "fat_g": "fat_100g",
    "carbs_g": "carbohydrates_100g",
    "fiber_g": "fiber_100g",
    "sugar_g": "sugars_100g",
    "sodium_mg": "sodium_100g",  # OFF reports sodium in g; converted below
}


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


class NutritionCache:
    """Flat JSON file cache: {"usda:chicken thigh": {...}, "off:chicken thigh": {...}}."""

    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "nutrition_cache.json"
        self._data: dict[str, Any] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str) -> Any | None:
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2))
        except OSError:
            pass  # cache is best-effort; a write failure shouldn't break a lookup


class NutritionClient:
    def __init__(self, data_dir: str, usda_api_key: str = "DEMO_KEY", timeout: float = 15.0):
        self._usda_key = usda_api_key
        self._http = httpx.AsyncClient(timeout=timeout)
        self._cache = NutritionCache(data_dir)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search_usda(self, food_name: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Search USDA FoodData Central. Returns candidates with per-100g macros,
        best matches first. Prefers 'Foundation' and 'SR Legacy' data types (whole
        foods) over 'Branded' (packaged products with marketing names)."""
        cache_key = f"usda_search:{_normalize(food_name)}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        r = await self._http.get(
            USDA_SEARCH_URL,
            params={
                "api_key": self._usda_key,
                "query": food_name,
                "pageSize": max_results,
                "dataType": ["Foundation", "SR Legacy", "Branded"],
            },
        )
        r.raise_for_status()
        foods = r.json().get("foods", [])

        results = []
        for f in foods:
            nutrients = {n.get("nutrientId"): n.get("value") for n in f.get("foodNutrients", [])}
            per_100g = {
                key: nutrients.get(nid)
                for key, nid in _USDA_NUTRIENT_IDS.items()
                if nutrients.get(nid) is not None
            }
            if not per_100g:
                continue
            results.append(
                {
                    "source": "usda",
                    "source_id": str(f.get("fdcId")),
                    "description": f.get("description"),
                    "data_type": f.get("dataType"),
                    "brand": f.get("brandOwner"),
                    "per_100g": per_100g,
                }
            )
        self._cache.set(cache_key, results)
        return results

    async def search_off(self, food_name: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Search Open Food Facts. Strongest for branded/packaged products;
        weaker for generic whole foods than USDA."""
        cache_key = f"off_search:{_normalize(food_name)}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        r = await self._http.get(
            OFF_SEARCH_URL,
            params={
                "search_terms": food_name,
                "search_simple": 1,
                "action": "process",
                "json": 1,
                "page_size": max_results,
            },
            headers={"User-Agent": "mealie-mcp-nutrition/1.0"},
        )
        r.raise_for_status()
        products = r.json().get("products", [])

        results = []
        for p in products:
            nutriments = p.get("nutriments", {})
            per_100g: dict[str, float] = {}
            for key, field in _OFF_NUTRIENT_FIELDS.items():
                val = nutriments.get(field)
                if val is None:
                    continue
                per_100g[key] = val * 1000 if key == "sodium_mg" else val  # OFF sodium is in g
            if not per_100g:
                continue
            results.append(
                {
                    "source": "off",
                    "source_id": p.get("code"),
                    "description": p.get("product_name") or food_name,
                    "brand": p.get("brands"),
                    "per_100g": per_100g,
                }
            )
        self._cache.set(cache_key, results)
        return results

    async def search_both(self, food_name: str, max_results: int = 5) -> dict[str, list[dict[str, Any]]]:
        usda_results, off_results = await asyncio.gather(
            self.search_usda(food_name, max_results),
            self.search_off(food_name, max_results),
            return_exceptions=True,
        )
        return {
            "usda": usda_results if not isinstance(usda_results, BaseException) else [],
            "off": off_results if not isinstance(off_results, BaseException) else [],
        }
