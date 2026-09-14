"""Unit conversion, per-food data decoding and the per-recipe rollup math."""

from __future__ import annotations

import json

import pytest

from mealie_sous_chef.nutrition import (
    FoodProfile,
    encode_food_extras,
    estimate_recipe_nutrition,
    nutrition_patch,
    quantity_to_grams,
    read_food_profile,
    validate_per_100g,
)


def food(name: str, per_100g: dict | None = None, **extras) -> dict:
    ex = {k: (json.dumps(v) if isinstance(v, dict) else str(v)) for k, v in extras.items()}
    if per_100g is not None:
        ex["nutrition_per_100g"] = json.dumps(per_100g)
    return {"id": f"id-{name}", "name": name, "extras": ex}


def unit(name: str, std_qty=None, std_unit=None, abbreviation="") -> dict:
    return {"id": f"u-{name}", "name": name, "abbreviation": abbreviation, "standardQuantity": std_qty, "standardUnit": std_unit}


def ing(quantity, f=None, u=None, **kw) -> dict:
    return {"quantity": quantity, "food": f, "unit": u, "note": "", **kw}


# ---------------------------------------------------------------- conversion


@pytest.mark.parametrize(
    "u, qty, grams, approximate",
    [
        # Mealie's standardUnit values are pint names -- what real instances store
        (unit("pound", 1, "pound"), 1.5, 680.388555, False),
        (unit("kilo", 1, "kilogram"), 0.25, 250.0, False),
        (unit("teaspoon", 1 / 6, "fluid_ounce"), 1, 4.92892, True),
        (unit("tablespoon", 0.5, "fluid_ounce"), 2, 29.57353, True),
        (unit("cup", 1, "cup"), 0.25, 59.14706, True),
        # no standard set: fall back on the unit's name / abbreviation / plural
        (unit("grams"), 30, 30.0, False),
        (unit("Tablespoons"), 1, 14.78676, True),
        (unit("whatever", abbreviation="oz"), 2, 56.699, False),
        (unit("liter"), 1, 1000.0, True),  # the old table had liter = 1 g
        (unit("fluid ounces"), 1, 29.57353, True),
    ],
)
def test_quantity_to_grams_units(u, qty, grams, approximate):
    conv = quantity_to_grams(qty, u, FoodProfile())
    assert conv.grams == pytest.approx(grams, rel=1e-4)
    assert conv.approximate is approximate


def test_standard_unit_takes_priority_over_name():
    # a unit named "cup" but standardised as 200 g (someone's "cup of rice") wins over the table
    conv = quantity_to_grams(1, unit("cup", 200, "gram"), FoodProfile())
    assert conv.grams == 200 and not conv.approximate


def test_volume_uses_food_density_when_set():
    conv = quantity_to_grams(1, unit("cup", 1, "cup"), FoodProfile(grams_per_ml=0.53))
    assert conv.grams == pytest.approx(125.39, rel=1e-3)
    assert not conv.approximate


def test_food_portion_overrides_unit_and_handles_unitless():
    profile = FoodProfile(portion_grams={"each": 50.0, "clove": 5.0, "cup": 120.0})
    assert quantity_to_grams(2, None, profile).grams == 100
    assert quantity_to_grams(3, unit("clove"), profile).grams == 15
    assert quantity_to_grams(3, unit("cloves"), profile).grams == 15
    assert quantity_to_grams(1, unit("cup", 1, "cup"), profile).grams == 120  # beats water density


def test_unconvertible_units_explain_the_fix():
    conv = quantity_to_grams(1, unit("bunch"), FoodProfile())
    assert conv.grams is None and "standardQuantity" in conv.note and "portion_grams['bunch']" in conv.note
    conv = quantity_to_grams(2, None, FoodProfile())
    assert conv.grams is None and "portion_grams['each']" in conv.note


# ---------------------------------------------------------------- stored data


def test_read_profile_tolerates_partial_malformed_and_legacy_data():
    assert read_food_profile({"extras": {}}).per_100g == {}
    assert read_food_profile(None).per_100g == {}

    partial = read_food_profile(food("oil", {"calories": 884, "fat_g": "100"}))
    assert partial.per_100g == {"calories": 884.0, "fat_g": 100.0} and partial.problems == []

    broken = read_food_profile({"extras": {"nutrition_per_100g": "{not json", "grams_per_ml": "heavy", "portion_grams": "[]"}})
    assert broken.per_100g == {} and broken.grams_per_ml is None
    assert len(broken.problems) == 3

    # a dict value (what the first version of set_food_nutrition tried to store)
    legacy = read_food_profile({"extras": {"nutrition_per_100g": {"protein_g": 3, "sodium": 5, "fat_g": -1}}})
    assert legacy.per_100g == {"protein_g": 3.0}
    assert any("sodium" in p for p in legacy.problems) and any("fat_g" in p for p in legacy.problems)


def test_encode_extras_writes_only_strings_and_preserves_other_keys():
    extras = encode_food_extras(
        {"someone_elses_key": "keep me", "portion_grams": json.dumps({"each": 50})},
        per_100g={"calories": 155.0},
        source="usda",
        source_id=171287,
        grams_per_ml=1.03,
        portion_grams={"Cloves": 5, "each": 0},
    )
    assert all(isinstance(v, str) for v in extras.values())
    assert extras["someone_elses_key"] == "keep me"
    assert json.loads(extras["nutrition_per_100g"]) == {"calories": 155.0}
    assert extras["nutrition_source"] == "usda" and extras["nutrition_source_id"] == "171287"
    assert json.loads(extras["portion_grams"]) == {"cloves": 5.0}  # "each" removed by 0
    profile = read_food_profile({"extras": extras})
    assert profile.grams_per_ml == 1.03 and quantity_to_grams(2, unit("clove"), profile).grams == 10


@pytest.mark.parametrize(
    "per_100g, message",
    [
        ({"sodium": 5}, "unknown nutrient key"),
        ({"calories": 2400}, "per-serving or per-package"),
        ({"protein_g": 130}, "impossible per 100g"),
        ({"fat_g": -2}, "non-negative"),
        ({"fat_g": "lots"}, "non-negative"),
        ({"fat_g": None}, "no values"),
    ],
)
def test_validate_per_100g_rejects_bad_input(per_100g, message):
    with pytest.raises(ValueError, match=message):
        validate_per_100g(per_100g)


# ---------------------------------------------------------------- rollup


RICE = food("rice", {"calories": 130, "protein_g": 2.7, "fat_g": 0.3, "carbs_g": 28, "sodium_mg": 1})
CHICKEN = food("chicken", {"calories": 165, "protein_g": 31, "fat_g": 3.6, "sodium_mg": 74})
GRAM = unit("gram", 1, "gram")


def test_rollup_math_and_servings_division():
    result = estimate_recipe_nutrition([ing(300, RICE, GRAM), ing(0.5, CHICKEN, unit("kg", 1, "kilogram"))], servings=4)
    # totals: rice 3x, chicken 5x
    assert result["total"]["calories"] == 390 + 825
    assert result["total"]["protein_g"] == pytest.approx(8.1 + 155)
    assert result["per_serving"]["calories"] == round(1215 / 4)
    assert result["per_serving"]["protein_g"] == round(163.1 / 4, 1)
    assert result["per_serving"]["sodium_mg"] == round((3 + 370) / 4, 1)
    assert result["servings_used"] == 4
    assert result["complete"] is True and result["unaccounted"] == [] and "warnings" not in result
    assert [c["grams"] for c in result["counted"]] == [300, 500]


def test_rollup_reports_every_kind_of_unaccounted_ingredient():
    no_data = food("mystery spice")
    result = estimate_recipe_nutrition(
        [
            ing(100, RICE, GRAM),
            ing(1, None, None, note="lime wedges", display="1 lime wedges"),
            ing(2, no_data, GRAM, display="2 gram mystery spice"),
            ing(1, CHICKEN, unit("bunch"), display="1 bunch chicken"),
            ing(1, None, None, referencedRecipe={"id": "r2"}, display="1 pizza dough"),
        ],
        servings=2,
    )
    reasons = {u["ingredient"]: u["reason"] for u in result["unaccounted"]}
    assert reasons["1 lime wedges"] == "not linked to a food"
    assert "has no nutrition data" in reasons["2 gram mystery spice"]
    assert "unit 'bunch' has no known weight" in reasons["1 bunch chicken"]
    assert "sub-recipes" in reasons["1 pizza dough"]
    assert result["complete"] is False
    assert result["per_serving"]["calories"] == 65  # only the rice counted


def test_rollup_partial_nutrient_data_is_unknown_not_zero():
    result = estimate_recipe_nutrition([ing(100, RICE, GRAM), ing(100, CHICKEN, GRAM)], servings=1)
    assert result["per_serving"]["fiber_g"] is None  # nobody has fiber
    assert result["per_serving"]["carbs_g"] == 28  # chicken lacks carbs: counted as partial
    assert result["incomplete_nutrients"] == {"carbs_g": ["chicken"]}
    assert "fiber_g" not in result["incomplete_nutrients"]
    assert result["complete"] is True  # every ingredient was weighed and had data


def test_rollup_skips_zero_quantity_and_flags_approximations():
    salt = food("salt", {"sodium_mg": 38758})
    grain = food("mystery grain", {"calories": 350})  # nothing in the USDA reference table matches
    result = estimate_recipe_nutrition(
        [ing(0, salt, None, note="to taste", display="salt to taste"), ing(1, grain, unit("cup", 1, "cup"))],
        servings=1,
    )
    assert result["skipped"] == [{"ingredient": "salt to taste", "reason": "no quantity"}]
    assert len(result["approximate"]) == 1 and "water density" in result["approximate"][0]["reason"]
    assert result["complete"] is True


@pytest.mark.parametrize("servings", [0, None, -3])
def test_rollup_without_servings_warns_and_uses_one(servings):
    result = estimate_recipe_nutrition([ing(200, RICE, GRAM)], servings=servings)
    assert result["servings_used"] == 1 and result["per_serving"]["calories"] == 260
    assert "no servings" in result["warnings"][0]


def test_rollup_empty_and_garbage_ingredients():
    result = estimate_recipe_nutrition([], servings=2)
    assert result["counted"] == [] and all(v is None for v in result["per_serving"].values())
    assert estimate_recipe_nutrition([None, "text", {}], servings=1)["skipped"] == [
        {"ingredient": "(unnamed ingredient)", "reason": "empty ingredient row"}
    ]


def test_rollup_unlinked_free_text_is_unaccounted_even_without_quantity():
    # how an unparsed line is stored: amount inside the note, quantity 0
    result = estimate_recipe_nutrition(
        [ing(100, RICE, GRAM), ing(0, None, None, note="1 pinch of saffron", display="1 pinch of saffron")],
        servings=1,
    )
    assert result["unaccounted"] == [{"ingredient": "1 pinch of saffron", "reason": "not linked to a food"}]
    assert result["skipped"] == [] and result["complete"] is False


def test_rollup_surfaces_malformed_food_data():
    bad = {"id": "x", "name": "bad", "extras": {"nutrition_per_100g": "oops"}}
    result = estimate_recipe_nutrition([ing(100, bad, GRAM)], servings=1)
    assert result["data_problems"] == [{"food": "bad", "problems": ["nutrition_per_100g is not a valid JSON object"]}]
    assert "no nutrition data" in result["unaccounted"][0]["reason"]


def test_nutrition_patch_keeps_existing_values_for_unknown_nutrients():
    existing = {"calories": "400", "fiberContent": "3", "unsaturatedFatContent": "2"}
    per_serving = {"calories": 312.0, "protein_g": 20.5, "fiber_g": None}
    patch, unchanged = nutrition_patch(per_serving, existing)
    assert patch["calories"] == "312" and patch["proteinContent"] == "20.5"
    assert patch["fiberContent"] == "3" and patch["unsaturatedFatContent"] == "2"
    assert "fiber_g" in unchanged and "protein_g" not in unchanged
