"""USDA / Open Food Facts client: parsing, ranking, rate limits, outages, cache."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from mealie_sous_chef import server
from mealie_sous_chef.nutrition_sources import NutritionCache, NutritionSources, SourceError


def usda_food(fdc_id, description, data_type, **nutrients):
    return {
        "fdcId": fdc_id,
        "description": description,
        "dataType": data_type,
        "foodNutrients": [{"nutrientId": int(nid), "value": v} for nid, v in nutrients.items()],
    }


USDA_OK = {
    "foods": [
        usda_food(1, "RICE CRACKERS", "Branded", **{"1008": 416, "1003": 10}),
        usda_food(2, "Rice, white, cooked", "SR Legacy", **{"1008": 130, "1003": 2.69, "1093": 1, "1253": 0}),
        usda_food(3, "Rice, jasmine, raw", "Foundation", **{"2048": 360, "1003": 7.1, "1004": 0.6}),
        usda_food(4, "Rice cake", "Foundation", **{"1062": 1590, "1005": 80.1}),
        usda_food(5, "Nothing useful", "Foundation"),
        usda_food(6, "Typo'd", "Branded", **{"1008": 4160, "1003": 250}),
    ]
}
OFF_OK = {
    "hits": [
        {
            "code": "35400349",
            "product_name": "Coconut Milk",
            "brands": ["Country Barn", "Other"],
            "nutriments": {"energy-kcal_100g": 75, "proteins_100g": 0, "sodium_100g": 0.0187, "cholesterol_100g": "0.005", "saturated-fat_100g": 5.62},
        },
        {"code": "1", "product_name": "No nutriments", "nutriments": {}},
        {"code": "2", "product_name": "kJ only", "brands": "Acme", "nutriments": {"energy_100g": 418.4}},
    ]
}
OFF_OUTAGE_HTML = "<!DOCTYPE html><title>Page temporarily unavailable - Open Food Facts</title>"


class Upstream:
    """Scriptable responses per host; counts requests."""

    def __init__(self, **routes):
        self.routes = routes  # host -> callable(request) -> Response, or Response
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = {"api.nal.usda.gov": "usda", "search.openfoodfacts.org": "off", "world.openfoodfacts.org": "off_legacy"}[request.url.host]
        route = self.routes.get(key, httpx.Response(404))
        return route(request) if callable(route) else route


@pytest.fixture
def make_sources(tmp_path):
    created = []

    def make(upstream: Upstream, key: str = "DEMO_KEY") -> NutritionSources:
        src = NutritionSources(str(tmp_path), key, transport=httpx.MockTransport(upstream.handler))
        created.append(src)
        return src

    yield make


async def test_usda_parsing_ranking_and_energy_fallbacks(make_sources):
    up = Upstream(usda=httpx.Response(200, json=USDA_OK), off=httpx.Response(200, json={"hits": []}))
    results = await make_sources(up).search_usda("rice", max_results=10)
    # Foundation, Foundation, SR Legacy, Branded; no-data and impossible-values entries dropped
    assert [r["source_id"] for r in results] == ["3", "4", "2", "1"]
    assert results[0]["per_100g"] == {"calories": 360, "protein_g": 7.1, "fat_g": 0.6}  # Atwater energy id
    assert results[1]["per_100g"]["calories"] == pytest.approx(380.02, abs=0.01)  # from kJ
    assert results[2]["per_100g"] == {"calories": 130, "protein_g": 2.69, "sodium_mg": 1, "cholesterol_mg": 0}
    request = up.requests[0]
    assert request.url.params["api_key"] == "DEMO_KEY"
    assert request.url.params.get_list("dataType") == ["Foundation", "SR Legacy", "Survey (FNDDS)", "Branded"]


async def test_off_parsing_units_and_brands(make_sources):
    up = Upstream(off=httpx.Response(200, json=OFF_OK))
    results = await make_sources(up).search_off("coconut milk")
    assert len(results) == 2
    assert results[0] == {
        "source": "off",
        "source_id": "35400349",
        "description": "Coconut Milk",
        "brand": "Country Barn, Other",
        "per_100g": {"calories": 75, "protein_g": 0, "saturated_fat_g": 5.62, "sodium_mg": 18.7, "cholesterol_mg": 5.0},
    }
    assert results[1]["per_100g"] == {"calories": 100.0} and results[1]["brand"] == "Acme"


async def test_off_falls_back_to_legacy_search_on_outage(make_sources):
    up = Upstream(
        off=httpx.Response(200, text=OFF_OUTAGE_HTML, headers={"content-type": "text/html"}),
        off_legacy=httpx.Response(200, json={"products": OFF_OK["hits"][:1]}),
    )
    results = await make_sources(up).search_off("coconut milk")
    assert results[0]["source_id"] == "35400349"
    assert [r.url.host for r in up.requests] == ["search.openfoodfacts.org", "world.openfoodfacts.org"]


async def test_usda_rate_limit_degrades_to_off_with_actionable_error(make_sources):
    up = Upstream(
        usda=httpx.Response(429, json={"error": {"code": "OVER_RATE_LIMIT"}}, headers={"X-RateLimit-Limit": "10"}),
        off=httpx.Response(200, json=OFF_OK),
    )
    out = await make_sources(up).search("coconut milk")
    assert out["usda"] == [] and len(out["off"]) == 2
    assert "rate limit" in out["errors"]["usda"] and "10 requests/hour" in out["errors"]["usda"]
    assert "USDA_API_KEY" in out["errors"]["usda"]
    assert "off" not in out["errors"]


async def test_rate_limit_with_own_key_does_not_blame_demo_key(make_sources):
    up = Upstream(usda=httpx.Response(429), off=httpx.Response(200, json=OFF_OK))
    out = await make_sources(up, key="my-real-key").search("x")
    assert out["errors"]["usda"] == "USDA rate limit reached"


async def test_both_sources_failing_raises_and_tool_reports_it(make_sources, monkeypatch):
    up = Upstream(
        usda=httpx.Response(403),
        off=httpx.Response(503, text=OFF_OUTAGE_HTML),
        off_legacy=lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow", request=r)),
    )
    sources = make_sources(up)
    with pytest.raises(SourceError, match="Both nutrition sources failed"):
        await sources.search("rice")

    monkeypatch.setattr(server, "_nutrition", sources)
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await server.mcp.call_tool("lookup_nutrition", {"food_name": "rice"})
    message = str(exc.value)
    assert "usda: USDA rejected the request (403); check USDA_API_KEY" in message
    assert "off: Open Food Facts timed out" in message


async def test_malformed_but_200_responses_are_errors_not_empty_results(make_sources):
    up = Upstream(usda=httpx.Response(200, json={"totalHits": 0}), off=httpx.Response(200, text="not json"),
                  off_legacy=httpx.Response(200, json={"count": 0}))
    with pytest.raises(SourceError) as exc:
        await make_sources(up).search("rice")
    assert "no 'foods' list" in str(exc.value) and "no 'products' list" in str(exc.value)


async def test_zero_results_is_a_hint_not_an_error_and_is_not_cached(make_sources):
    up = Upstream(usda=httpx.Response(200, json={"foods": []}), off=httpx.Response(200, json={"hits": []}))
    sources = make_sources(up)
    out = await sources.search("boneless skinless organic free range chicken thighs")
    assert out["usda"] == [] and out["off"] == [] and "errors" not in out and "simpler" in out["hint"]
    await sources.search("boneless skinless organic free range chicken thighs")
    assert len(up.requests) == 4  # retried, since nothing was cached


async def test_cache_hits_slicing_and_expiry(make_sources, tmp_path):
    up = Upstream(usda=httpx.Response(200, json=USDA_OK), off=httpx.Response(200, json=OFF_OK))
    sources = make_sources(up)
    first = await sources.search("  Rice ", max_results=2)
    again = await sources.search("rice", max_results=4)
    assert len(up.requests) == 2  # second call served from cache, despite different spacing/case
    assert len(first["usda"]) == 2 and len(again["usda"]) == 4

    # entries older than the TTL are refetched
    cache_file = tmp_path / "nutrition_cache.json"
    data = json.loads(cache_file.read_text())
    for entry in data.values():
        entry["at"] = time.time() - 40 * 24 * 3600
    cache_file.write_text(json.dumps(data))
    fresh = make_sources(up)
    await fresh.search("rice")
    assert len(up.requests) == 4


def test_corrupt_cache_file_is_ignored(tmp_path):
    (tmp_path / "nutrition_cache.json").write_text("{truncated")
    cache = NutritionCache(str(tmp_path))
    assert cache.get("usda:rice") is None
    cache.set("usda:rice", [{"x": 1}])
    assert NutritionCache(str(tmp_path)).get("usda:rice") == [{"x": 1}]


async def test_empty_query_rejected(make_sources):
    with pytest.raises(SourceError, match="empty"):
        await make_sources(Upstream()).search("   ")
