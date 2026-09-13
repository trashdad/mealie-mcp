"""End-to-end checks against a REAL Mealie instance. Skipped unless configured:

    SOUS_CHEF_LIVE_MEALIE_URL=http://127.0.0.1:9925
    SOUS_CHEF_LIVE_MEALIE_TOKEN=<api token>

Use a scratch Mealie (e.g. a throwaway container), NOT your real one: these
tests create, merge and delete foods, units, recipes and shopping items. Every
name is prefixed with a random tag and cleaned up afterwards, but still.
"""

from __future__ import annotations

import os
import uuid

import pytest

from mcp.server.mcpserver.exceptions import ToolError

from mealie_sous_chef import server
from mealie_sous_chef.mealie import MealieClient, MealieError

URL = os.environ.get("SOUS_CHEF_LIVE_MEALIE_URL")
TOKEN = os.environ.get("SOUS_CHEF_LIVE_MEALIE_TOKEN")
pytestmark = pytest.mark.skipif(not (URL and TOKEN), reason="set SOUS_CHEF_LIVE_MEALIE_URL/TOKEN to run live tests")


@pytest.fixture
async def live(monkeypatch):
    client = MealieClient(URL, TOKEN)
    monkeypatch.setattr(server, "_mealie", client)
    tag = f"zz{uuid.uuid4().hex[:6]}"
    created: dict[str, list[str]] = {"recipes": [], "foods": [], "units": [], "shopping_lists": []}

    async def call(tool: str, /, **arguments):
        result = await server.mcp.call_tool(tool, arguments)
        content = result.structured_content
        return content.get("result", content) if set(content) == {"result"} else content

    yield client, call, tag, created

    for slug in created["recipes"]:
        try:
            await client.delete(f"/api/recipes/{slug}")
        except MealieError:
            pass
    for list_id in created["shopping_lists"]:
        try:
            await client.delete(f"/api/households/shopping/lists/{list_id}")
        except MealieError:
            pass
    for kind in ("foods", "units"):
        for item_id in created[kind]:
            try:
                await client.delete(f"/api/{kind}/{item_id}")
            except MealieError:
                pass
    await client.aclose()


async def _food(client, created, name, **fields):
    food = await client.post("/api/foods", {"name": name, **fields})
    created["foods"].append(food["id"])
    return food


async def _unit(client, created, name, **fields):
    unit = await client.post("/api/units", {"name": name, **fields})
    created["units"].append(unit["id"])
    return unit


async def test_live_ingredients_taxonomy_and_nutrition(live):
    client, call, tag, created = live
    rice = await _food(client, created, f"{tag} rice")
    gram = await _unit(client, created, f"{tag}gram", abbreviation=f"{tag}g", standardQuantity=1, standardUnit="gram")

    # create_recipe with a structured ingredient, an unmatched text line and a parsed line
    recipe = await call(
        "create_recipe",
        name=f"{tag} bowl",
        ingredients=[
            {"quantity": 200, "food_id": rice["id"], "unit_id": gram["id"], "note": "rinsed"},
            f"1 pinch of {tag} saffron",
            "",
        ],
        instructions=["cook"],
        servings=2,
    )
    created["recipes"].append(recipe["slug"])
    assert recipe["warnings"] == ["dropped 1 blank ingredient line(s)"]
    stored = (await client.get(f"/api/recipes/{recipe['slug']}"))["recipeIngredient"]
    assert stored[0]["food"]["id"] == rice["id"] and stored[0]["unit"]["id"] == gram["id"]
    assert stored[0]["quantity"] == 200 and stored[0]["note"] == "rinsed"
    assert stored[1]["food"] is None and tag in stored[1]["note"]

    # unknown ids fail before anything is written
    with pytest.raises(ToolError, match="doesn't exist"):
        await call("update_recipe", slug=recipe["slug"], ingredients=[{"quantity": 1, "food_id": str(uuid.uuid4())}])

    # nutrition: extras round-trip through Mealie's string-only extras table
    saved = await call("set_food_nutrition", food_id=rice["id"], per_100g={"calories": 130, "carbs_g": 28.2}, source="usda", source_id="1")
    assert saved["nutrition_per_100g"] == {"calories": 130.0, "carbs_g": 28.2}

    partial = await call("compute_recipe_nutrition", slug=recipe["slug"])
    assert partial["saved"] is False and partial["unaccounted"][0]["reason"] == "not linked to a food"
    done = await call("compute_recipe_nutrition", slug=recipe["slug"], allow_partial=True)
    assert done["saved"] is True and done["per_serving"]["calories"] == 130
    nutrition = (await client.get(f"/api/recipes/{recipe['slug']}"))["nutrition"]
    assert nutrition["calories"] == "130" and nutrition["carbohydrateContent"] == "28.2"

    # delete refuses while the recipe uses the food (Mealie itself would just unlink it)
    with pytest.raises(ToolError, match=r"used by 1 recipe\(s\)"):
        await call("manage_taxonomy", resource="foods", action="delete", item_id=rice["id"])
    with pytest.raises(ToolError, match=r"used by 1 recipe\(s\)"):
        await call("manage_taxonomy", resource="units", action="delete", item_id=gram["id"])

    # merge a duplicate into it, in a batch with a conflicting create
    dupe = await _food(client, created, f"{tag} Rice")
    await call("update_recipe", slug=recipe["slug"], ingredients=[{"quantity": 50, "food_id": dupe["id"], "unit_id": gram["id"]}])
    batch = await call(
        "manage_taxonomy", resource="foods", action="merge", items=[{"item_id": dupe["id"], "merge_into": rice["id"]}]
    )
    assert batch["succeeded"] == 1 and batch["results"][0]["into"]["name"] == f"{tag} rice"
    stored = (await client.get(f"/api/recipes/{recipe['slug']}"))["recipeIngredient"]
    assert stored[0]["food"]["id"] == rice["id"]
    creates = await call("manage_taxonomy", resource="foods", action="create", items=[{"name": f"{tag} rice"}, {"name": f"{tag} leek"}])
    assert creates["succeeded"] == 1 and "already exists" in creates["errors"][0]["error"]
    created["foods"].append(creates["results"][0]["created"]["id"])

    # unit standardisation that Mealie would silently drop is rejected; a valid update round-trips
    with pytest.raises(ToolError, match="must be set together"):
        await call("manage_taxonomy", resource="units", action="update", item_id=gram["id"], data={"standard_quantity": 5})
    updated = await call(
        "manage_taxonomy", resource="units", action="update", item_id=gram["id"],
        name=f"{tag}grams", data={"standard_quantity": 1, "standard_unit": "gram", "plural_name": f"{tag}grams"},
    )
    assert updated["updated"] == {**updated["updated"], "name": f"{tag}grams", "standard_unit": "gram", "abbreviation": f"{tag}g"}
    renamed = await call("manage_taxonomy", resource="foods", action="update", item_id=rice["id"], data={"description": "long grain"})
    assert renamed["updated"]["nutrition_per_100g"] == {"calories": 130.0, "carbs_g": 28.2}  # extras survive a full PUT

    # force delete behaves like Mealie: link nulled
    await call("manage_taxonomy", resource="units", action="delete", item_id=gram["id"], force=True)
    stored = (await client.get(f"/api/recipes/{recipe['slug']}"))["recipeIngredient"]
    assert stored[0]["unit"] is None and stored[0]["food"]["id"] == rice["id"]


async def test_live_shopping_list_reference_check(live):
    client, call, tag, created = live
    leek = await _food(client, created, f"{tag} leek")
    shopping_list = await client.post("/api/households/shopping/lists", {"name": f"{tag} list"})
    created["shopping_lists"].append(shopping_list["id"])
    await client.post(
        "/api/households/shopping/items",
        {"shoppingListId": shopping_list["id"], "foodId": leek["id"], "quantity": 1, "note": ""},
    )
    with pytest.raises(ToolError, match=r"1 shopping-list item\(s\)"):
        await call("manage_taxonomy", resource="foods", action="delete", item_id=leek["id"])


async def test_live_parser_matches_existing_food(live):
    client, call, tag, created = live
    await _food(client, created, "zzsouschefparsnip")
    rows = await call("parse_ingredients", lines=["2 zzsouschefparsnip"])
    assert rows[0]["food_name"] == "zzsouschefparsnip" and rows[0]["food_id"] is not None


async def test_live_review_edit_preserves_identity_and_step_links(live):
    client, call, tag, created = live
    thigh = await _food(client, created, f"{tag} chicken thigh")
    created_recipe = await call(
        "create_recipe",
        name=f"{tag} thighs",
        ingredients=[f"4 {tag} chicken thighs", f"salt and {tag}pepper to taste", "1 cup water"],
        instructions=["Season.", "Cook."],
    )
    slug = created_recipe["slug"]
    created["recipes"].append(slug)
    recipe = await client.get(f"/api/recipes/{slug}")
    refs = [i["referenceId"] for i in recipe["recipeIngredient"]]
    steps = recipe["recipeInstructions"]
    steps[0]["ingredientReferences"] = [{"referenceId": refs[0]}, {"referenceId": refs[1]}]
    await client.patch(f"/api/recipes/{slug}", {"recipeInstructions": steps})

    review = await call("review_recipe_ingredients", slug=slug)
    rows = review["ingredients"]
    assert rows[0]["used_in_steps"] == [1]
    edits = []
    for row in rows:
        suggestion = row.get("suggestion")
        if suggestion and suggestion["grade"] in ("exact", "candidate") and suggestion["proposed_edit"].get("food_id") == thigh["id"]:
            edits.append(suggestion["proposed_edit"])
    if not edits:  # parser didn't surface it; Claude would pick from candidates
        edits = [{"ref": rows[0]["ref"], "food_id": thigh["id"], "quantity": 4}]
    result = await call("edit_recipe_ingredients", slug=slug, edits=[*edits, {"insert_after": "end", "note": "lime wedges"}])
    assert result["ingredients"][0]["food"]["id"] == thigh["id"]

    after = await client.get(f"/api/recipes/{slug}")
    assert [i["referenceId"] for i in after["recipeIngredient"]][:3] == refs
    assert [r["referenceId"] for r in after["recipeInstructions"][0]["ingredientReferences"]] == refs[:2]
    assert after["recipeIngredient"][3]["note"] == "lime wedges"

    # an update_recipe that re-sends the same lines keeps them (and the step links)
    same = [i["display"] or i["note"] for i in after["recipeIngredient"]]
    await call("update_recipe", slug=slug, ingredients=same)
    again = await client.get(f"/api/recipes/{slug}")
    assert [i["referenceId"] for i in again["recipeIngredient"]] == [i["referenceId"] for i in after["recipeIngredient"]]


async def test_live_merge_keeps_names_as_aliases_for_the_parser(live):
    client, call, tag, created = live
    keep = await _food(client, created, f"{tag}rice")
    drop = await _food(client, created, f"{tag}basmati")
    result = await call("manage_taxonomy", resource="foods", action="merge", item_id=drop["id"], merge_into=keep["id"])
    assert result["aliases_added"] == [f"{tag}basmati"]
    kept = await client.get(f"/api/foods/{keep['id']}")
    assert [a["name"] for a in kept["aliases"]] == [f"{tag}basmati"]
    rows = await call("parse_ingredients", lines=[f"2 cups {tag}basmati"])
    assert rows[0]["food_id"] == keep["id"]


async def test_live_create_recipe_never_links_a_lossy_parse(live):
    """Invariant on real parser output: every ingredient create_recipe links
    accounts for all words of its line; everything else is flagged."""
    from mealie_sous_chef.ingredients import assess_parse

    client, call, tag, created = live
    lines = [
        "4 boneless skinless chicken thighs",
        "2 cloves garlic, minced",
        "1 (14 oz) can light coconut milk",
        "1/2 cup chopped fresh cilantro",
        "salt and pepper to taste",
        "2 tbsp extra-virgin olive oil",
        "1 large yellow onion, diced",
        "3 cups cooked basmati rice",
        "1 1/2 lbs ground beef",
        "juice of 1 lime",
    ]
    result = await call("create_recipe", name=f"{tag} realistic", ingredients=lines, instructions=["x"])
    created["recipes"].append(result["slug"])
    stored = (await client.get(f"/api/recipes/{result['slug']}"))["recipeIngredient"]
    flagged = {n["line"] for n in result.get("needs_review", [])}
    for line, ing in zip(lines, stored, strict=True):
        if ing.get("food") or ing.get("unit"):
            assert assess_parse(line, ing).dropped_words == [], (line, ing)
        else:
            assert ing["note"] == line or line not in flagged
    print("needs_review:", result.get("needs_review"))
