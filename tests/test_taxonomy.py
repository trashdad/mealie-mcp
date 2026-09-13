"""manage_taxonomy: create / update / merge / delete / batch, including Mealie's
409 conflicts and the delete-silently-unlinks-recipes trap."""

from __future__ import annotations

import json

import pytest

from fake_mealie import FakeMealie

MISSING = "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------- list / create


async def test_list_filters_and_shapes(tools, fake):
    call, _ = tools
    fake.add_food("jasmine rice", extras={"nutrition_per_100g": json.dumps({"calories": 130}), "nutrition_source": "usda"})
    fake.add_food("basmati rice")
    fake.add_food("salt")
    page = await call("manage_taxonomy", resource="foods", query="rice")
    assert page["total"] == 2
    assert [i["name"] for i in page["items"]] == ["basmati rice", "jasmine rice"]
    assert page["items"][1]["nutrition_per_100g"] == {"calories": 130.0}

    fake.add_unit("cup", standardQuantity=1, standardUnit="cup")
    units = await call("manage_taxonomy", resource="units")
    assert units["items"][0] == {**units["items"][0], "standard_quantity": 1, "standard_unit": "cup"}


async def test_create_food_and_unit(tools, fake):
    call, _ = tools
    created = await call("manage_taxonomy", resource="foods", action="create", name=" leek ", data={"plural_name": "leeks"})
    assert created["created"]["name"] == "leek" and created["created"]["plural_name"] == "leeks"
    unit = await call(
        "manage_taxonomy", resource="units", action="create", name="tablespoon",
        data={"abbreviation": "tbsp", "standard_quantity": 0.5, "standard_unit": "fluid_ounce"},
    )
    assert unit["created"]["standard_unit"] == "fluid_ounce"
    assert len(fake.foods) == 1 and len(fake.units) == 1


async def test_create_duplicate_409_explains(tools, fake):
    _, call_error = tools
    fake.add_food("salt")
    msg = await call_error("manage_taxonomy", resource="foods", action="create", name="salt")
    assert "a food named 'salt' already exists" in msg and "action='list'" in msg
    assert len(fake.foods) == 1


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"standardQuantity": 240}, "must be set together"),
        ({"standardUnit": "gram"}, "must be set together"),
        ({"standardQuantity": 0, "standardUnit": "gram"}, "must be set together"),
        ({"standardQuantity": 1, "standardUnit": "handful"}, "isn't a recognised"),
        ({"id": "abc"}, "can't set ['id']"),
        ({"name": "x"}, "can't set ['name']"),
    ],
)
async def test_create_unit_rejects_data_mealie_would_silently_drop(tools, fake, data, expected):
    _, call_error = tools
    msg = await call_error("manage_taxonomy", resource="units", action="create", name="scoop", data=data)
    assert expected in msg
    assert fake.units == {}


async def test_create_requires_name(tools):
    _, call_error = tools
    assert "create requires a name" in await call_error("manage_taxonomy", resource="foods", action="create")


# ---------------------------------------------------------------- update


async def test_update_keeps_other_fields_and_merges_extras(tools, fake):
    call, _ = tools
    rice = fake.add_food("rice", pluralName="rices", extras={"nutrition_per_100g": json.dumps({"calories": 130})})
    result = await call(
        "manage_taxonomy", resource="foods", action="update", item_id=rice["id"],
        name="jasmine rice", data={"description": "long grain", "extras": {"brand": "acme"}},
    )
    assert result["updated"] == {**result["updated"], "name": "jasmine rice", "plural_name": "rices", "description": "long grain"}
    assert fake.foods[rice["id"]]["extras"] == {"nutrition_per_100g": json.dumps({"calories": 130}), "brand": "acme"}


async def test_update_rename_conflict_409_suggests_merge(tools, fake):
    _, call_error = tools
    a = fake.add_food("Jasmine Rice")
    fake.add_food("jasmine rice")
    msg = await call_error("manage_taxonomy", resource="foods", action="update", item_id=a["id"], name="jasmine rice")
    assert "already named 'jasmine rice'" in msg and "action='merge'" in msg
    assert fake.foods[a["id"]]["name"] == "Jasmine Rice"


async def test_update_errors(tools, fake):
    _, call_error = tools
    unit = fake.add_unit("cup")
    assert "no such unit" in await call_error("manage_taxonomy", resource="units", action="update", item_id=MISSING, name="x")
    assert "requires name and/or data" in await call_error("manage_taxonomy", resource="units", action="update", item_id=unit["id"])
    assert "requires item_id" in await call_error("manage_taxonomy", resource="units", action="update", name="x")


# ---------------------------------------------------------------- merge


async def test_merge_repoints_recipes_and_shopping_items(tools, fake):
    call, _ = tools
    keep, drop = fake.add_food("jasmine rice"), fake.add_food("Jasmine Rice")
    fake.add_recipe("Bowl", [fake.ingredient(2, food=drop)])
    item = fake.add_shopping_item(food=drop)
    result = await call("manage_taxonomy", resource="foods", action="merge", item_id=drop["id"], merge_into=keep["id"])
    # Mealie's merge answers {"message", "error"}; the tool fetches the kept item instead of returning nulls
    assert result["merged"] == {"id": drop["id"], "name": "Jasmine Rice"}
    assert result["into"]["id"] == keep["id"] and result["into"]["name"] == "jasmine rice"
    assert drop["id"] not in fake.foods
    assert fake.recipes["bowl"]["recipeIngredient"][0]["food"]["id"] == keep["id"]
    assert fake.shopping_items[item["id"]]["foodId"] == keep["id"]
    merge_call = next(c for c in fake.calls if c[1] == "/api/foods/merge")
    assert merge_call[2] == {"fromFood": drop["id"], "toFood": keep["id"]}


async def test_merge_units_uses_unit_keys(tools, fake):
    call, _ = tools
    keep, drop = fake.add_unit("tablespoon"), fake.add_unit("tbsp")
    await call("manage_taxonomy", resource="units", action="merge", item_id=drop["id"], merge_into=keep["id"])
    assert next(c for c in fake.calls if c[1] == "/api/units/merge")[2] == {"fromUnit": drop["id"], "toUnit": keep["id"]}


async def test_merge_errors_are_specific_not_a_bare_500(tools, fake):
    _, call_error = tools
    a = fake.add_food("a")
    msg = await call_error("manage_taxonomy", resource="foods", action="merge", item_id=a["id"], merge_into=MISSING)
    assert f"merge_into {MISSING!r}: no such food" in msg
    assert "the same" in await call_error("manage_taxonomy", resource="foods", action="merge", item_id=a["id"], merge_into=a["id"])
    assert "requires item_id" in await call_error("manage_taxonomy", resource="foods", action="merge", item_id=a["id"])
    assert not any(c[1].endswith("/merge") for c in fake.calls)


# ---------------------------------------------------------------- delete


async def test_delete_unreferenced(tools, fake):
    call, _ = tools
    leek = fake.add_food("leek")
    result = await call("manage_taxonomy", resource="foods", action="delete", item_id=leek["id"])
    assert result["deleted"]["name"] == "leek" and fake.foods == {}


async def test_delete_refuses_when_recipes_use_it(tools, fake):
    _, call_error = tools
    rice = fake.add_food("rice")
    fake.add_recipe("Rice Bowl", [fake.ingredient(1, food=rice)])
    fake.add_shopping_item(food=rice)
    msg = await call_error("manage_taxonomy", resource="foods", action="delete", item_id=rice["id"])
    assert "used by 1 recipe(s) (e.g. 'Rice Bowl') and 1 shopping-list item(s)" in msg
    assert "action='merge'" in msg and "force=true" in msg
    assert rice["id"] in fake.foods
    assert fake.recipes["rice-bowl"]["recipeIngredient"][0]["food"]["id"] == rice["id"]


async def test_delete_force_unlinks_like_mealie_does(tools, fake):
    call, _ = tools
    cup = fake.add_unit("cup")
    rice = fake.add_food("rice")
    fake.add_recipe("Rice Bowl", [fake.ingredient(1, food=rice, unit=cup)])
    await call("manage_taxonomy", resource="units", action="delete", item_id=cup["id"], force=True)
    ingredient = fake.recipes["rice-bowl"]["recipeIngredient"][0]
    assert ingredient["unit"] is None and ingredient["food"]["id"] == rice["id"]


async def test_delete_409_conflict_suggests_merge():
    """Postgres enforces the shopping-list foreign key: Mealie answers 409."""
    from mealie_sous_chef import server
    from mealie_sous_chef.mealie import MealieClient
    from mcp.server.mcpserver.exceptions import ToolError

    fake = FakeMealie(postgres=True)
    salt = fake.add_food("salt")
    fake.add_shopping_item(food=salt)
    client = MealieClient("http://mealie.test", "t", transport=fake.transport())
    old, server._mealie = server._mealie, client
    try:
        with pytest.raises(ToolError) as exc:
            await server.mcp.call_tool(
                "manage_taxonomy", {"resource": "foods", "action": "delete", "item_id": salt["id"], "force": True}
            )
    finally:
        server._mealie = old
        await client.aclose()
    assert "Mealie refused to delete food 'salt'" in str(exc.value) and "(409)" in str(exc.value)
    assert "action='merge'" in str(exc.value)
    assert salt["id"] in fake.foods


async def test_delete_reference_check_failure_refuses(tools, fake):
    _, call_error = tools
    rice = fake.add_food("rice")
    fake.fail_paths[("GET", "/api/recipes")] = 400
    msg = await call_error("manage_taxonomy", resource="foods", action="delete", item_id=rice["id"])
    assert "couldn't check whether food 'rice' is still in use" in msg and "force=true" in msg
    assert rice["id"] in fake.foods


async def test_delete_other_errors_are_not_mislabelled_as_conflicts(tools, fake):
    _, call_error = tools
    assert "no such food" in await call_error("manage_taxonomy", resource="foods", action="delete", item_id=MISSING)
    rice = fake.add_food("rice")
    fake.fail_paths[("DELETE", f"/api/foods/{rice['id']}")] = 500
    msg = await call_error("manage_taxonomy", resource="foods", action="delete", item_id=rice["id"])
    assert "500" in msg and "still referenced" not in msg and "refused" not in msg


# ---------------------------------------------------------------- batch


async def test_batch_create_continues_past_conflicts(tools, fake):
    call, _ = tools
    fake.add_food("salt")
    result = await call(
        "manage_taxonomy", resource="foods", action="create",
        items=[{"name": "pepper"}, {"name": "salt"}, {"name": "cumin", "data": {"pluralName": "cumins"}}, {"nme": "typo"}],
    )
    assert result["succeeded"] == 2 and result["failed"] == 2
    assert [r["created"]["name"] for r in result["results"]] == ["pepper", "cumin"]
    assert [r["index"] for r in result["results"]] == [0, 2]
    errors = {e["index"]: e["error"] for e in result["errors"]}
    assert "already exists" in errors[1]
    assert "unknown key(s) ['nme']" in errors[3]
    assert sorted(f["name"] for f in fake.foods.values()) == ["cumin", "pepper", "salt"]


async def test_batch_merge_and_delete(tools, fake):
    call, _ = tools
    keep = fake.add_food("rice")
    dupes = [fake.add_food(n) for n in ("Rice", "RICE")]
    fake.add_recipe("Bowl", [fake.ingredient(1, food=dupes[0]), fake.ingredient(1, food=dupes[1])])
    merged = await call(
        "manage_taxonomy", resource="foods", action="merge",
        items=[{"item_id": d["id"], "merge_into": keep["id"]} for d in dupes],
    )
    assert merged["succeeded"] == 2 and list(fake.foods) == [keep["id"]]
    assert {i["food"]["id"] for i in fake.recipes["bowl"]["recipeIngredient"]} == {keep["id"]}

    spare = fake.add_food("spare")
    deleted = await call(
        "manage_taxonomy", resource="foods", action="delete",
        items=[{"item_id": spare["id"]}, {"item_id": keep["id"]}, {"item_id": keep["id"], "force": True}],
    )
    assert deleted["succeeded"] == 2 and deleted["failed"] == 1
    assert "used by 1 recipe(s)" in deleted["errors"][0]["error"]
    assert fake.foods == {}


async def test_batch_misuse(tools, fake):
    _, call_error = tools
    assert "items is empty" in await call_error("manage_taxonomy", resource="foods", action="create", items=[])
    assert "not list" in await call_error("manage_taxonomy", resource="foods", items=[{"name": "x"}])
    assert "not both" in await call_error("manage_taxonomy", resource="foods", action="create", name="x", items=[{"name": "y"}])
    assert fake.foods == {}


async def test_batch_survives_mealie_outage_per_item(tools, fake):
    call, _ = tools
    fake.fail_paths[("POST", "/api/foods")] = 503
    result = await call("manage_taxonomy", resource="foods", action="create", items=[{"name": "a"}, {"name": "b"}])
    assert result["failed"] == 2 and all("503" in e["error"] for e in result["errors"])
