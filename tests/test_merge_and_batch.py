"""Merge keeping names (aliases) and nutrition; the nutrition list filter; batch
set_food_nutrition and multi-name lookup_nutrition."""

from __future__ import annotations

import json

import httpx
import pytest

from mealie_sous_chef import server
from mealie_sous_chef.nutrition_sources import NutritionSources

NUTRITION = json.dumps({"calories": 130})


async def test_merge_adds_dropped_names_as_aliases_so_the_parser_still_matches(tools, fake):
    call, _ = tools
    keep = fake.add_food("rice", aliases=[{"name": "white rice"}])
    drop = fake.add_food("Basmati Rice", pluralName="basmati rices", aliases=[{"name": "White Rice"}])
    result = await call("manage_taxonomy", resource="foods", action="merge", item_id=drop["id"], merge_into=keep["id"])
    assert result["aliases_added"] == ["Basmati Rice", "basmati rices"]  # "White Rice" already known
    assert [a["name"] for a in fake.foods[keep["id"]]["aliases"]] == ["white rice", "Basmati Rice", "basmati rices"]
    assert result["into"]["aliases"] == ["white rice", "Basmati Rice", "basmati rices"]

    rows = await call("parse_ingredients", lines=["2 basmati rice"])
    assert rows[0]["food_id"] == keep["id"]


async def test_merge_units_keeps_abbreviations(tools, fake):
    call, _ = tools
    keep = fake.add_unit("tablespoon", abbreviation="tbsp")
    drop = fake.add_unit("Tbs", abbreviation="T")
    result = await call("manage_taxonomy", resource="units", action="merge", item_id=drop["id"], merge_into=keep["id"])
    assert result["aliases_added"] == ["Tbs", "T"]


async def test_merge_carries_nutrition_only_when_kept_food_has_none(tools, fake):
    call, _ = tools
    keep = fake.add_food("rice", extras={"brand": "acme"})
    drop = fake.add_food("Rice ", extras={"nutrition_per_100g": NUTRITION, "nutrition_source": "usda", "grams_per_ml": "0.85", "unrelated": "x"})
    result = await call("manage_taxonomy", resource="foods", action="merge", item_id=drop["id"], merge_into=keep["id"])
    assert result["nutrition_carried_over"] is True
    extras = fake.foods[keep["id"]]["extras"]
    assert extras == {"brand": "acme", "nutrition_per_100g": NUTRITION, "nutrition_source": "usda", "grams_per_ml": "0.85"}

    other = fake.add_food("RICE", extras={"nutrition_per_100g": json.dumps({"calories": 999})})
    result = await call("manage_taxonomy", resource="foods", action="merge", item_id=other["id"], merge_into=keep["id"])
    assert result["nutrition_carried_over"] is False and "kept the merged-into food's" in result["nutrition_conflict"]
    assert fake.foods[keep["id"]]["extras"]["nutrition_per_100g"] == NUTRITION


async def test_merge_without_alias_warns_about_discarded_nutrition(tools, fake):
    call, _ = tools
    keep, drop = fake.add_food("rice"), fake.add_food("Rice", extras={"nutrition_per_100g": NUTRITION})
    result = await call(
        "manage_taxonomy", resource="foods", action="merge", item_id=drop["id"], merge_into=keep["id"], add_alias=False
    )
    assert "discarded" in result["warning"] and fake.foods[keep["id"]]["aliases"] == []


async def test_merge_alias_save_failure_is_a_warning_not_an_error(tools, fake):
    call, _ = tools
    keep, drop = fake.add_food("rice"), fake.add_food("jasmine rice")
    fake.fail_paths[("PUT", f"/api/foods/{keep['id']}")] = 500
    result = await call("manage_taxonomy", resource="foods", action="merge", item_id=drop["id"], merge_into=keep["id"])
    assert drop["id"] not in fake.foods  # the merge itself happened
    assert "merge succeeded, but saving aliases" in result["warning"] and result["aliases_added"] == []


async def test_list_nutrition_filter(tools, fake):
    call, _ = tools
    for n in range(30):
        fake.add_food(f"food {n:02d}", extras={"nutrition_per_100g": NUTRITION} if n % 3 == 0 else {})
    missing = await call("manage_taxonomy", resource="foods", nutrition="missing", per_page=15, page=2)
    assert missing["total"] == 20 and missing["total_pages"] == 2 and len(missing["items"]) == 5
    present = await call("manage_taxonomy", resource="foods", nutrition="present", query="food 1")
    assert [i["name"] for i in present["items"]] == ["food 12", "food 15", "food 18"]
    _, call_error = tools
    assert "only applies to foods" in await call_error("manage_taxonomy", resource="units", nutrition="missing")


# ---------------------------------------------------------------- batch nutrition


async def test_set_food_nutrition_batch_isolates_failures(tools, fake):
    call, call_error = tools
    rice, egg = fake.add_food("rice"), fake.add_food("egg")
    result = await call(
        "set_food_nutrition",
        items=[
            {"food_id": rice["id"], "per_100g": {"calories": 130}, "source": "usda", "source_id": "1"},
            {"food_id": egg["id"], "per_100g": {"calories": 1430}},
            {"food_id": egg["id"], "portion_grams": {"each": 50}},
            {"per_100g": {"calories": 1}},
            {"food_id": rice["id"], "grams_per_ml": 0.85, "colour": "white"},
        ],
    )
    assert result["succeeded"] == 2 and result["failed"] == 3
    assert [r["index"] for r in result["results"]] == [0, 2]
    errors = {e["index"]: e["error"] for e in result["errors"]}
    assert "per-serving" in errors[1] and "food_id is required" in errors[3] and "unknown key(s) ['colour']" in errors[4]
    assert json.loads(fake.foods[rice["id"]]["extras"]["nutrition_per_100g"]) == {"calories": 130.0}
    assert "nutrition_per_100g" not in fake.foods[egg["id"]]["extras"]

    assert "not both" in await call_error("set_food_nutrition", food_id=rice["id"], items=[{"food_id": rice["id"]}])
    assert "items is empty" in await call_error("set_food_nutrition", items=[])
    assert "pass food_id" in await call_error("set_food_nutrition")


async def test_lookup_nutrition_multiple_names(tmp_path, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query") or request.url.params.get("q")
        seen.append(query)
        if request.url.host == "api.nal.usda.gov":
            if query == "unobtainium":
                return httpx.Response(429)
            return httpx.Response(200, json={"foods": [{"fdcId": i, "description": f"{query} {i}", "dataType": "Foundation", "foodNutrients": [{"nutrientId": 1008, "value": 100 + i}]} for i in range(5)]})
        if query == "unobtainium":
            return httpx.Response(503)
        return httpx.Response(200, json={"hits": []})

    sources = NutritionSources(str(tmp_path), "key", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_nutrition", sources)
    result = await server.mcp.call_tool("lookup_nutrition", {"food_names": ["rice", " rice ", "egg", "unobtainium"]})
    results = result.structured_content["results"]
    assert list(results) == ["rice", "egg", "unobtainium"]
    assert len(results["rice"]["usda"]) == 3  # batch default
    assert "Both nutrition sources failed" in results["unobtainium"]["error"]

    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="exactly one of food_name or food_names"):
        await server.mcp.call_tool("lookup_nutrition", {"food_name": "x", "food_names": ["y"]})
    with pytest.raises(ToolError, match="at most 10"):
        await server.mcp.call_tool("lookup_nutrition", {"food_names": [f"f{i}" for i in range(11)]})
    await sources.aclose()
