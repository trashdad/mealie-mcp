"""set_food_nutrition and compute_recipe_nutrition end to end against the fake Mealie."""

from __future__ import annotations

import json

import pytest

MISSING = "00000000-0000-0000-0000-000000000000"


async def test_set_food_nutrition_stores_string_extras_mealie_can_save(tools, fake):
    call, _ = tools
    egg = fake.add_food("egg", extras={"unrelated": "keep"})
    result = await call(
        "set_food_nutrition",
        food_id=egg["id"],
        per_100g={"calories": 143, "protein_g": 12.6, "fat_g": 9.5},
        source="usda",
        source_id="748967",
        portion_grams={"each": 50},
    )
    extras = fake.foods[egg["id"]]["extras"]
    assert all(isinstance(v, str) for v in extras.values())  # a nested object is a 400 from Mealie
    assert extras["unrelated"] == "keep"
    assert json.loads(extras["nutrition_per_100g"]) == {"calories": 143.0, "protein_g": 12.6, "fat_g": 9.5}
    assert result["nutrition_per_100g"]["protein_g"] == 12.6
    assert result["nutrition_source"] == "usda" and result["portion_grams"] == {"each": 50.0}


async def test_set_food_nutrition_weights_only_keeps_existing_nutrition(tools, fake):
    call, _ = tools
    flour = fake.add_food("flour")
    await call("set_food_nutrition", food_id=flour["id"], per_100g={"calories": 364})
    result = await call("set_food_nutrition", food_id=flour["id"], grams_per_ml=0.53)
    assert result["nutrition_per_100g"] == {"calories": 364.0} and result["grams_per_ml"] == 0.53


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"per_100g": {"kcal": 100}}, "unknown nutrient key"),
        ({"per_100g": {"calories": 1800}}, "per-serving"),
        ({"grams_per_ml": 80}, "grams_per_ml must be a number between"),
        ({"portion_grams": {"each": -5}}, "non-negative"),
        ({}, "pass per_100g, grams_per_ml and/or portion_grams"),
    ],
)
async def test_set_food_nutrition_input_errors_reach_the_model(tools, fake, kwargs, expected):
    _, call_error = tools
    food = fake.add_food("thing")
    assert expected in await call_error("set_food_nutrition", food_id=food["id"], **kwargs)
    assert fake.foods[food["id"]]["extras"] == {}


async def test_set_food_nutrition_unknown_food(tools):
    _, call_error = tools
    assert "no such food" in await call_error("set_food_nutrition", food_id=MISSING, per_100g={"calories": 1})


# ---------------------------------------------------------------- compute


@pytest.fixture
def bowl(fake):
    """Real-world unit shapes, copied from a live Mealie instance."""
    lb = fake.add_unit("pound", abbreviation="lb", standardQuantity=1.0, standardUnit="pound")
    tsp = fake.add_unit("teaspoon", abbreviation="tsp", standardQuantity=1 / 6, standardUnit="fluid_ounce")
    chicken = fake.add_food("chicken breast", extras={"nutrition_per_100g": json.dumps({"calories": 120, "protein_g": 22.5, "fat_g": 2.6, "sodium_mg": 45})})
    salt = fake.add_food("salt", extras={"nutrition_per_100g": json.dumps({"sodium_mg": 38758}), "grams_per_ml": "1.2"})
    egg = fake.add_food("egg", extras={"nutrition_per_100g": json.dumps({"calories": 143, "protein_g": 12.6, "fat_g": 9.5}), "portion_grams": json.dumps({"each": 50})})
    recipe = fake.add_recipe(
        "Chicken Bowl",
        [
            fake.ingredient(1.5, food=chicken, unit=lb),
            fake.ingredient(1, food=salt, unit=tsp),
            fake.ingredient(2, food=egg),
            fake.ingredient(0, food=salt, note="to taste"),
        ],
        servings=4,
    )
    recipe["nutrition"].update({"calories": "429", "cholesterolContent": "95"})
    return recipe


async def test_compute_complete_recipe_saves_per_serving(tools, fake, bowl):
    call, _ = tools
    result = await call("compute_recipe_nutrition", slug="chicken-bowl")
    chicken_g = 1.5 * 453.59237
    salt_g = 4.92892159375 * 1.2
    assert result["saved"] is True and result["complete"] is True
    assert result["per_serving"]["calories"] == round((chicken_g * 1.2 + 100 * 1.43) / 4)
    assert result["per_serving"]["sodium_mg"] == round((chicken_g * 0.45 + salt_g * 387.58) / 4, 1)
    assert result["approximate"] == []  # salt has a density; pound is a mass unit
    assert result["skipped"][0]["reason"] == "no quantity"
    assert result["previous_nutrition"] == {"calories": "429", "cholesterolContent": "95"}

    saved = fake.recipes["chicken-bowl"]["nutrition"]
    assert saved["calories"] == str(int(result["per_serving"]["calories"]))
    assert saved["proteinContent"] == f"{result['per_serving']['protein_g']:g}"
    assert saved["cholesterolContent"] == "95"  # nothing computed for it -> left alone
    assert "cholesterolContent" in result["left_unchanged"]
    assert saved["fiberContent"] is None  # not "0"


async def test_compute_incomplete_recipe_does_not_overwrite_by_default(tools, fake, bowl):
    call, _ = tools
    bunch = fake.add_unit("bunch")
    cilantro = fake.add_food("cilantro", extras={"nutrition_per_100g": json.dumps({"calories": 23})})
    bowl["recipeIngredient"].append(fake._store_ingredient({"quantity": 1, "food": cilantro, "unit": bunch}))
    bowl["recipeIngredient"].append(fake._store_ingredient({"quantity": 1, "note": "lime wedges"}))

    result = await call("compute_recipe_nutrition", slug="chicken-bowl")
    assert result["saved"] is False and "2 ingredient(s) unaccounted" in result["not_saved_reason"]
    assert {u["ingredient"] for u in result["unaccounted"]} == {"1 bunch cilantro", "1 lime wedges"}
    assert fake.recipes["chicken-bowl"]["nutrition"]["calories"] == "429"

    result = await call("compute_recipe_nutrition", slug="chicken-bowl", allow_partial=True)
    assert result["saved"] is True and fake.recipes["chicken-bowl"]["nutrition"]["calories"] != "429"


async def test_compute_preview_and_servings_override(tools, fake, bowl):
    call, _ = tools
    four = await call("compute_recipe_nutrition", slug="chicken-bowl", save=False)
    two = await call("compute_recipe_nutrition", slug="chicken-bowl", save=False, servings=2)
    assert four["saved"] is False and four["not_saved_reason"] == "save=false"
    assert two["servings_used"] == 2 and two["per_serving"]["protein_g"] == pytest.approx(four["per_serving"]["protein_g"] * 2, abs=0.1)
    assert not any(c[0] == "PATCH" for c in fake.calls)


async def test_compute_nothing_countable_never_saves(tools, fake):
    call, _ = tools
    fake.add_recipe("Plain", [fake.ingredient(1, note="a thing")], servings=1)
    result = await call("compute_recipe_nutrition", slug="plain", allow_partial=True)
    assert result["saved"] is False and result["not_saved_reason"] == "no ingredient could be counted"


async def test_compute_errors(tools, fake):
    _, call_error = tools
    fake.add_recipe("Empty")
    assert "has no ingredients" in await call_error("compute_recipe_nutrition", slug="empty")
    assert "404" in await call_error("compute_recipe_nutrition", slug="nope")
    fake.add_recipe("One", [fake.ingredient(1, note="x")])
    assert "servings must be > 0" in await call_error("compute_recipe_nutrition", slug="one", servings=0)


async def test_full_workflow_from_text_to_saved_nutrition(tools, fake):
    """create_recipe with plain text -> set_food_nutrition -> compute_recipe_nutrition."""
    call, _ = tools
    fake.add_unit("gram", abbreviation="g", standardQuantity=1, standardUnit="gram")
    rice = fake.add_food("rice")
    created = await call(
        "create_recipe", name="Rice", ingredients=["200 g rice", "1 pinch of saffron"], instructions=["Cook."], servings=2
    )
    assert created["ingredients"] == ["200 gram rice", "1 pinch of saffron"]
    await call("set_food_nutrition", food_id=rice["id"], per_100g={"calories": 130, "carbs_g": 28})
    result = await call("compute_recipe_nutrition", slug="rice", allow_partial=True)
    assert result["per_serving"]["calories"] == 130 and result["per_serving"]["carbs_g"] == 28
    assert result["unaccounted"] == [{"ingredient": "1 pinch of saffron", "reason": "not linked to a food"}]
    assert fake.recipes["rice"]["nutrition"]["calories"] == "130"
