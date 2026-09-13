"""The original (upstream) tools: recipes, organizers, shopping lists, meal plans,
escape hatch -- happy paths and the main error paths, through the MCP layer."""

from __future__ import annotations

from datetime import date, timedelta

import pytest


@pytest.fixture
def pantry(fake):
    fake.tags.extend([{"slug": "quick", "name": "Quick"}, {"slug": "asian", "name": "Asian"}])
    fake.categories.append({"slug": "dinner", "name": "Dinner"})
    rice = fake.add_food("rice")
    cup = fake.add_unit("cup")
    bowl = fake.add_recipe(
        "Rice Bowl",
        [fake.ingredient(2, food=rice, unit=cup)],
        servings=2,
        tags=[{"slug": "quick", "name": "Quick"}],
        recipeCategory=[{"slug": "dinner", "name": "Dinner"}],
        prepTime="5 minutes",
        performTime="15 minutes",
    )
    fake.add_recipe("Noodle Soup", tags=[{"slug": "asian", "name": "Asian"}])
    groceries = fake.add_shopping_list("Groceries")
    return {"bowl": bowl, "groceries": groceries, "rice": rice}


async def test_search_and_get_recipe(tools, fake, pantry):
    call, _ = tools
    found = await call("search_recipes", query="bowl")
    assert found["total"] == 1 and found["recipes"][0]["slug"] == "rice-bowl"
    assert found["recipes"][0]["tags"] == ["Quick"] and found["recipes"][0]["cook_time"] == "15 minutes"
    tagged = await call("search_recipes", tags=["asian"])
    assert [r["name"] for r in tagged["recipes"]] == ["Noodle Soup"]
    request = next(c for c in fake.calls if c[1] == "/api/recipes")[2]
    assert request["perPage"] == ["15"] and request["orderBy"] == ["name"]

    recipe = await call("get_recipe", slug="rice-bowl")
    assert recipe["ingredients"] == ["2 cup rice"] and recipe["servings"] == 2 and recipe["categories"] == ["Dinner"]


async def test_search_clamps_page_size(tools, fake, pantry):
    call, _ = tools
    await call("search_recipes", per_page=500)
    assert next(c for c in fake.calls if c[1] == "/api/recipes")[2]["perPage"] == ["50"]


async def test_import_delete_and_tags(tools, fake, pantry):
    call, call_error = tools
    imported = await call("import_recipe_from_url", url="https://example.com/pasta")
    assert imported["source_url"] == "https://example.com/pasta"
    assert await call("delete_recipe", slug=imported["slug"]) == f"Deleted recipe {imported['slug']}"
    assert imported["slug"] not in fake.recipes
    assert await call("list_tags_and_categories") == {"tags": ["asian", "quick"], "categories": ["dinner"]}
    assert "404" in await call_error("get_recipe", slug="nope")


async def test_shopping_list_lifecycle(tools, fake, pantry):
    call, call_error = tools
    list_id = pantry["groceries"]["id"]
    assert await call("list_shopping_lists") == [{"id": list_id, "name": "Groceries"}]

    added = await call("add_shopping_items", list_id=list_id, items=["2 lemons", "  ", "olive oil"])
    assert [i["text"] for i in added["added"]] == ["2 lemons", "olive oil"]
    lemon_id = added["added"][0]["id"]

    updated = await call("update_shopping_item", item_id=lemon_id, checked=True, text="3 lemons")
    assert updated["checked"] is True and updated["text"] == "3 lemons"
    unchecked = await call("get_shopping_list", list_id=list_id)
    assert [i["text"] for i in unchecked["items"]] == ["olive oil"]
    everything = await call("get_shopping_list", list_id=list_id, include_checked=True)
    assert len(everything["items"]) == 2

    assert await call("delete_shopping_items", item_ids=[lemon_id]) == "Deleted 1 item(s)"
    assert "no items given" in await call_error("add_shopping_items", list_id=list_id, items=[" "])


async def test_add_recipe_to_shopping_list_uses_bulk_endpoint(tools, fake, pantry):
    call, _ = tools
    list_id = pantry["groceries"]["id"]
    result = await call("add_recipe_to_shopping_list", list_id=list_id, recipe_id=pantry["bowl"]["id"], scale=2)
    assert result["items"][0]["quantity"] == 4
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert posts[0][1] == f"/api/households/shopping/lists/{list_id}/recipe"
    assert posts[0][2] == [{"recipeId": pantry["bowl"]["id"], "recipeIncrementQuantity": 2}]


async def test_add_recipe_to_shopping_list_falls_back_on_older_mealie(tools, fake, pantry):
    call, call_error = tools
    fake.legacy_shopping = True
    list_id = pantry["groceries"]["id"]
    result = await call("add_recipe_to_shopping_list", list_id=list_id, recipe_id=pantry["bowl"]["id"])
    assert len(result["items"]) == 1
    # (the client also retries a 404 on the pre-2.0 /api/groups/ path, which fails too)
    paths = [c[1] for c in fake.calls if c[0] == "POST" and c[1].startswith("/api/households/")]
    assert paths == [
        f"/api/households/shopping/lists/{list_id}/recipe",
        f"/api/households/shopping/lists/{list_id}/recipe/{pantry['bowl']['id']}",
    ]
    fake.legacy_shopping = False
    fake.fail_paths[("POST", f"/api/households/shopping/lists/{list_id}/recipe")] = 500
    assert "500" in await call_error("add_recipe_to_shopping_list", list_id=list_id, recipe_id=pantry["bowl"]["id"])


async def test_meal_plan_lifecycle(tools, fake, pantry):
    call, call_error = tools
    today = date.today()
    entry = await call("add_meal_plan_entry", day=today.isoformat(), recipe_id=pantry["bowl"]["id"], note="double batch")
    assert entry["title"] == "Rice Bowl" and entry["recipe_slug"] == "rice-bowl" and entry["text"] == "double batch"
    await call("add_meal_plan_entry", day=(today + timedelta(days=10)).isoformat(), title="Takeout", entry_type="lunch")

    week = await call("get_meal_plan")
    assert [e["title"] for e in week] == ["Rice Bowl"]
    wide = await call("get_meal_plan", start_date=today.isoformat(), end_date=(today + timedelta(days=30)).isoformat())
    assert [(e["title"], e["type"]) for e in wide] == [("Rice Bowl", "dinner"), ("Takeout", "lunch")]

    assert await call("delete_meal_plan_entry", entry_id=entry["id"]) == f"Deleted meal plan entry {entry['id']}"
    assert "provide recipe_id or title" in await call_error("add_meal_plan_entry", day=today.isoformat())
    assert "must be an ISO date" in await call_error("add_meal_plan_entry", day="next tuesday", title="x")
    assert "start_date must be an ISO date" in await call_error("get_meal_plan", start_date="13/09/2026")


async def test_mealie_get_escape_hatch(tools, fake, pantry):
    call, call_error = tools
    assert (await call("mealie_get", path="/api/app/about"))["version"] == "fake"
    assert "path must start with /api/" in await call_error("mealie_get", path="/etc/passwd")


async def test_update_recipe_simple_fields_and_nothing_to_update(tools, fake, pantry):
    call, call_error = tools
    result = await call("update_recipe", slug="rice-bowl", rating=5, servings=3, cook_time="20 minutes")
    assert result["rating"] == 5 and result["servings"] == 3 and result["cook_time"] == "20 minutes"
    assert "nothing to update" in await call_error("update_recipe", slug="rice-bowl")
