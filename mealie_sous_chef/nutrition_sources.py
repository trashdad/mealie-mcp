"""Nutrition lookups against USDA FoodData Central and Open Food Facts, with a
small on-disk cache so the same ingredient name isn't re-queried every time.

Both sources are normalised to per-100g figures using the nutrient keys in
`nutrition.NUTRIENTS`, so a candidate can be passed straight to set_food_nutrition.

Failure policy: a source that errors (rate limit, outage, garbage response) is
reported in `errors` next to the other source's results; the lookup only fails
outright when every source errored. Zero results is not an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .nutrition import NUTRIENTS

log = logging.getLogger(__name__)

USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"  # search-a-licious, OFF's current search API
OFF_LEGACY_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
USER_AGENT = "mealie-sous-chef/0.2 (self-hosted Mealie MCP server)"

CACHE_TTL_SECONDS = 30 * 24 * 3600
MAX_RESULTS = 25
KJ_PER_KCAL = 4.184

# USDA FoodData Central nutrient ids. Energy has several: 1008 (kcal) is standard,
# Foundation foods sometimes only carry the Atwater variants 2048/2047, or kJ (1062).
_USDA_NUTRIENT_IDS = {
    "protein_g": 1003,
    "fat_g": 1004,
    "saturated_fat_g": 1258,
    "trans_fat_g": 1257,
    "carbs_g": 1005,
    "fiber_g": 1079,
    "sugar_g": 2000,
    "sodium_mg": 1093,
    "cholesterol_mg": 1253,
}
_USDA_KCAL_IDS = (1008, 2048, 2047)
_USDA_KJ_ID = 1062
# Whole/generic foods first; branded products (marketing names, per-label data) last.
_USDA_TYPE_RANK = {"Foundation": 0, "SR Legacy": 1, "Survey (FNDDS)": 2, "Branded": 3}
# Types requested. USDA's gateway answers HTTP 400 when all four are requested at
# once (verified 2026-09); branded products are Open Food Facts' strength anyway.
USDA_DATA_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]

# Open Food Facts nutriment fields (per 100g). Sodium and cholesterol are reported in g.
_OFF_FIELDS = {
    "protein_g": ("proteins_100g", 1),
    "fat_g": ("fat_100g", 1),
    "saturated_fat_g": ("saturated-fat_100g", 1),
    "trans_fat_g": ("trans-fat_100g", 1),
    "carbs_g": ("carbohydrates_100g", 1),
    "fiber_g": ("fiber_100g", 1),
    "sugar_g": ("sugars_100g", 1),
    "sodium_mg": ("sodium_100g", 1000),
    "cholesterol_mg": ("cholesterol_100g", 1000),
}


class SourceError(Exception):
    """A nutrition source failed; the message is written for the model/user."""


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _plausible(per_100g: dict[str, float]) -> dict[str, float]:
    """Drop physically impossible per-100g values (crowd-sourced data has typos)."""
    out = {}
    for key, value in per_100g.items():
        ceiling = 950 if key == "calories" else (100_000 if key.endswith("_mg") else 100)
        if 0 <= value <= ceiling:
            out[key] = round(value, 3)
    return out


def _ordered(per_100g: dict[str, float]) -> dict[str, float]:
    return {k: per_100g[k] for k in NUTRIENTS if k in per_100g}


class NutritionCache:
    """JSON file cache: {"usda:chicken thigh": {"at": 1700000000, "results": [...]}}."""

    def __init__(self, data_dir: str, ttl_seconds: int = CACHE_TTL_SECONDS):
        self._path = Path(data_dir) / "nutrition_cache.json"
        self._ttl = ttl_seconds
        self._data: dict[str, Any] = {}
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                log.warning("Ignoring unreadable nutrition cache at %s", self._path)

    def get(self, key: str) -> list[dict[str, Any]] | None:
        entry = self._data.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("results"), list):
            return None
        if time.time() - float(entry.get("at", 0)) > self._ttl:
            return None
        return entry["results"]

    def set(self, key: str, results: list[dict[str, Any]]) -> None:
        self._data[key] = {"at": int(time.time()), "results": results}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            log.warning("Could not write nutrition cache to %s", self._path)  # best-effort


class NutritionSources:
    def __init__(
        self,
        data_dir: str,
        usda_api_key: str = "DEMO_KEY",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._usda_key = usda_api_key or "DEMO_KEY"
        self._http = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, transport=transport)
        self._cache = NutritionCache(data_dir)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_json(self, source: str, url: str, params: dict[str, Any]) -> Any:
        try:
            r = await self._http.get(url, params=params)
        except httpx.TimeoutException as e:
            raise SourceError(f"{source} timed out") from e
        except httpx.HTTPError as e:
            raise SourceError(f"{source} unreachable: {e.__class__.__name__}") from e
        if r.status_code == 429:
            limit = r.headers.get("x-ratelimit-limit")
            msg = f"{source} rate limit reached"
            if source == "USDA" and self._usda_key == "DEMO_KEY":
                msg += (
                    f" -- the shared DEMO_KEY allows only {limit or 'a few'} requests/hour per IP. "
                    "Set USDA_API_KEY (free: https://fdc.nal.usda.gov/api-key-signup.html)"
                )
            raise SourceError(msg)
        if r.status_code in (401, 403):
            raise SourceError(f"{source} rejected the request ({r.status_code}); check USDA_API_KEY")
        if r.status_code >= 400:
            raise SourceError(f"{source} returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise SourceError(f"{source} returned a non-JSON response (likely a temporary outage page)") from e

    # ---------- USDA ----------

    async def search_usda(self, food_name: str, max_results: int = 5) -> list[dict[str, Any]]:
        key = f"usda:{_normalize(food_name)}"
        cached = self._cache.get(key)
        if cached is None:
            data = await self._get_json(
                "USDA",
                USDA_SEARCH_URL,
                {
                    "api_key": self._usda_key,
                    "query": food_name,
                    "pageSize": MAX_RESULTS,
                    "dataType": USDA_DATA_TYPES,
                },
            )
            foods = data.get("foods") if isinstance(data, dict) else None
            if not isinstance(foods, list):
                raise SourceError("USDA response had no 'foods' list")
            cached = self._parse_usda(foods)
            if cached:
                self._cache.set(key, cached)
        return cached[:max_results]

    @staticmethod
    def _parse_usda(foods: list[Any]) -> list[dict[str, Any]]:
        results = []
        for f in foods:
            if not isinstance(f, dict):
                continue
            values: dict[int, float] = {}
            for n in f.get("foodNutrients") or []:
                if isinstance(n, dict) and isinstance(n.get("nutrientId"), int):
                    v = _num(n.get("value"))
                    if v is not None:
                        values[n["nutrientId"]] = v
            per_100g = {key: values[nid] for key, nid in _USDA_NUTRIENT_IDS.items() if nid in values}
            kcal = next((values[i] for i in _USDA_KCAL_IDS if i in values), None)
            if kcal is None and _USDA_KJ_ID in values:
                kcal = values[_USDA_KJ_ID] / KJ_PER_KCAL
            if kcal is not None:
                per_100g["calories"] = kcal
            per_100g = _plausible(per_100g)
            if not per_100g:
                continue
            results.append(
                {
                    "source": "usda",
                    "source_id": str(f.get("fdcId")),
                    "description": f.get("description"),
                    "data_type": f.get("dataType"),
                    "brand": f.get("brandOwner") or f.get("brandName"),
                    "per_100g": _ordered(per_100g),
                }
            )
        # stable sort keeps USDA's relevance order within each data type
        results.sort(key=lambda r: _USDA_TYPE_RANK.get(r.get("data_type") or "", 9))
        return results

    # ---------- Open Food Facts ----------

    async def search_off(self, food_name: str, max_results: int = 5) -> list[dict[str, Any]]:
        key = f"off:{_normalize(food_name)}"
        cached = self._cache.get(key)
        if cached is None:
            fields = "code,product_name,brands,nutriments"
            try:
                data = await self._get_json(
                    "Open Food Facts",
                    OFF_SEARCH_URL,
                    {"q": food_name, "page_size": MAX_RESULTS, "fields": fields},
                )
                products = data.get("hits") if isinstance(data, dict) else None
                if not isinstance(products, list):
                    raise SourceError("Open Food Facts response had no 'hits' list")
            except SourceError as primary_error:
                log.info("OFF search API failed (%s); trying legacy search", primary_error)
                data = await self._get_json(
                    "Open Food Facts",
                    OFF_LEGACY_SEARCH_URL,
                    {
                        "search_terms": food_name,
                        "search_simple": 1,
                        "action": "process",
                        "json": 1,
                        "page_size": MAX_RESULTS,
                        "fields": fields,
                    },
                )
                products = data.get("products") if isinstance(data, dict) else None
                if not isinstance(products, list):
                    raise SourceError("Open Food Facts response had no 'products' list") from primary_error
            cached = self._parse_off(products)
            if cached:
                self._cache.set(key, cached)
        return cached[:max_results]

    @staticmethod
    def _parse_off(products: list[Any]) -> list[dict[str, Any]]:
        results = []
        for p in products:
            if not isinstance(p, dict):
                continue
            nutriments = p.get("nutriments") if isinstance(p.get("nutriments"), dict) else {}
            per_100g: dict[str, float] = {}
            kcal = _num(nutriments.get("energy-kcal_100g"))
            if kcal is None and _num(nutriments.get("energy_100g")) is not None:
                kcal = _num(nutriments.get("energy_100g")) / KJ_PER_KCAL  # energy_100g is kJ
            if kcal is not None:
                per_100g["calories"] = kcal
            for key, (field, multiplier) in _OFF_FIELDS.items():
                v = _num(nutriments.get(field))
                if v is not None:
                    per_100g[key] = v * multiplier
            per_100g = _plausible(per_100g)
            if not per_100g:
                continue
            brands = p.get("brands")
            if isinstance(brands, list):
                brands = ", ".join(str(b) for b in brands)
            results.append(
                {
                    "source": "off",
                    "source_id": p.get("code"),
                    "description": p.get("product_name") or None,
                    "brand": brands or None,
                    "per_100g": _ordered(per_100g),
                }
            )
        return results

    # ---------- both ----------

    async def search(self, food_name: str, max_results: int = 5) -> dict[str, Any]:
        """Query both sources concurrently. Raises SourceError only if both fail."""
        if not food_name.strip():
            raise SourceError("food_name is empty")
        max_results = min(max(max_results, 1), MAX_RESULTS)
        usda, off = await asyncio.gather(
            self.search_usda(food_name, max_results),
            self.search_off(food_name, max_results),
            return_exceptions=True,
        )
        out: dict[str, Any] = {"query": food_name, "usda": [], "off": []}
        errors: dict[str, str] = {}
        for name, result in (("usda", usda), ("off", off)):
            if isinstance(result, SourceError):
                errors[name] = str(result)
            elif isinstance(result, BaseException):
                log.exception("Unexpected %s lookup failure", name, exc_info=result)
                errors[name] = f"unexpected error: {result.__class__.__name__}"
            else:
                out[name] = result
        if len(errors) == 2:
            raise SourceError("Both nutrition sources failed -- " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        if errors:
            out["errors"] = errors
        if not out["usda"] and not out["off"]:
            out["hint"] = "No candidates. Try a simpler, generic name (e.g. 'chicken thigh' rather than 'boneless skinless chicken thighs')."
        return out
