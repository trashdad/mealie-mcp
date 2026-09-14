"""Volume/piece -> grams from USDA household measures: parsing USDA's portion data,
the bundled reference table, matching foods to it, and set_food_nutrition fetching
the measures of the exact USDA food chosen."""

from __future__ import annotations

import json

import pytest

from mealie_sous_chef.household_measures import (
    match_reference,
    parse_usda_portions,
    portion_for_piece,
    portion_for_unit,
    reference_table,
)
from mealie_sous_chef.nutrition import FoodProfile, quantity_to_grams


def portion(modifier=None, grams=0.0, amount=1.0, unit="undetermined", description=None):
    return {
        "amount": amount,
        "modifier": modifier,
        "portionDescription": description,
        "gramWeight": grams,
        "measureUnit": {"name": unit},
    }


# Real foodPortions shapes returned by USDA FoodData Central (SR Legacy, Foundation, FNDDS)
ONION = [
    portion("cup, chopped", 160),
    portion('slice, medium (1/8" thick)', 14),
    portion('medium (2-1/2" dia)', 110),
    portion("large", 150),
    portion("rings", 60, amount=10.0),
    portion("tbsp chopped", 10),
    portion("cup, sliced", 115),
    portion("small", 70),
]
BROWN_SUGAR = [portion("cup packed", 220), portion("tsp packed", 4.6), portion("cup unpacked", 145), portion("tsp unpacked", 3)]
EGG = [
    portion("cup (4.86 large eggs)", 243),
    portion("medium", 44),
    portion("extra large", 56),
    portion("small", 38),
    portion("large", 50),
]
GARLIC = [portion("tsp", 2.8), portion("clove", 3), portion("cloves", 9, amount=3.0), portion("cup", 136)]


def test_parse_sr_legacy_measures():
    onion = parse_usda_portions(ONION, "Onions, raw")["portion_grams"]
    assert onion["cup"] == 160 and onion["cup_chopped"] == 160 and onion["cup_sliced"] == 115
    assert onion["tablespoon"] == 10 and onion["ring"] == 6  # "10 rings = 60 g" -> per ring
    assert (onion["small"], onion["medium"], onion["large"], onion["each"]) == (70, 110, 150, 110)
    garlic = parse_usda_portions(GARLIC, "Garlic, raw")
    assert garlic["portion_grams"]["clove"] == 3 and garlic["portion_grams"]["teaspoon"] == 2.8
    assert garlic["grams_per_ml"] == pytest.approx(136 / 236.588, abs=1e-3)


def test_parse_eggs_default_to_large_and_extra_sizes():
    egg = parse_usda_portions(EGG, "Egg, whole, raw, fresh")["portion_grams"]
    assert egg["each"] == 50 and egg["extra_large"] == 56 and egg["cup"] == 243


def test_parse_foundation_and_fndds_formats():
    foundation = parse_usda_portions(
        [portion("Edible", 143, unit="Onion"), portion(None, 85, unit="RACC")], "Onions, yellow, raw"
    )
    assert foundation["portion_grams"] == {"onion": 143, "each": 143}
    fndds = parse_usda_portions(
        [
            portion("64696", 130, amount=None, description="1 breast"),
            portion("90000", 130, amount=None, description="Quantity not specified"),
            portion("10049", 135, amount=None, description="1 cup, cooked, diced"),
            portion("62138", 30, amount=None, description="1 small or thin slice"),
            portion("40040", 28.35, amount=None, description="1 oz, cooked"),
        ],
        "Chicken breast, cooked",
    )["portion_grams"]
    assert fndds == {"breast": 130, "cup": 135, "cup_cooked_diced": 135, "each": 130}
    lemon = parse_usda_portions([portion('fruit (2-1/8" dia)', 84)], "Lemons, raw, without peel")["portion_grams"]
    assert lemon["each"] == 84


def test_prep_words_and_sizes_pick_the_right_measure():
    sugar = parse_usda_portions(BROWN_SUGAR)["portion_grams"]
    assert portion_for_unit(sugar, ["cup"], "packed") == ("cup_packed", 220)
    assert portion_for_unit(sugar, ["cups"], "") == ("cup", 220)
    onion = parse_usda_portions(ONION, "Onions, raw")["portion_grams"]
    assert portion_for_unit(onion, ["cup"], "thinly sliced") == ("cup_sliced", 115)
    assert portion_for_unit(onion, ["Tbsp"], "chopped") == ("tablespoon_chopped", 10)
    assert portion_for_piece(onion, "large, diced") == ("large", 150)
    assert portion_for_piece(onion, "minced") == ("each", 110)


def test_reference_table_is_usda_sourced_and_sane():
    table = reference_table()
    assert len(table) >= 100
    assert all(e["usda_fdc_id"] and e["usda_description"] and e["portion_grams"] for e in table)
    by_name = {n: e for e in table for n in e["names"]}
    # well-known kitchen weights, per USDA
    assert by_name["flour"]["portion_grams"]["cup"] == 125
    assert by_name["sugar"]["portion_grams"]["cup"] == 200
    assert by_name["butter"]["portion_grams"]["tablespoon"] == pytest.approx(14.2)
    assert by_name["salt"]["portion_grams"]["teaspoon"] == 6
    assert by_name["egg"]["portion_grams"]["each"] == 50


@pytest.mark.parametrize(
    "food, note, expected",
    [
        ("coconut milk", "", "coconut milk"),
        ("milk", "", "milk"),
        ("peanut butter", "", "peanut butter"),
        ("jasmine rice", "cooked (or basmati)", "cooked jasmine rice"),
        ("jasmine rice", "", "jasmine rice"),
        ("ginger", "fresh grated", "fresh ginger"),
        ("ginger", "", "ginger"),
        ("red bell pepper", "", "red bell pepper"),
        ("pepper", "", "pepper"),
        ("chicken breasts", "", None),
        ("dragonfruit", "", None),
    ],
)
def test_reference_matching_prefers_the_most_specific_name(food, note, expected):
    entry = match_reference(food, note)
    assert (expected in entry["names"]) if expected else entry is None


@pytest.mark.parametrize(
    "quantity, unit, note, food, grams",
    [
        (2, {"name": "cup", "standardQuantity": 1, "standardUnit": "cup"}, "", "all-purpose flour", 250),
        (1, {"name": "cup"}, "packed", "brown sugar", 220),
        (1, {"name": "tablespoon", "abbreviation": "tbsp"}, "", "brown sugar", 220 / 236.588 * 14.787),  # via density
        (2, None, "", "eggs", 100),
        (1, None, "large, diced", "yellow onion", 150),
        (3, {"name": "clove"}, "minced", "garlic", 9),
        (0.5, {"name": "teaspoon", "standardQuantity": 1 / 6, "standardUnit": "fluid_ounce"}, "kosher", "salt", 3),
        (1, {"name": "pound", "standardQuantity": 1, "standardUnit": "pound"}, "", "flour", 453.59),  # mass stays exact
    ],
)
def test_conversion_uses_usda_measures(quantity, unit, note, food, grams):
    conv = quantity_to_grams(quantity, unit, FoodProfile(), note, food)
    assert conv.grams == pytest.approx(grams, rel=0.01)
    assert conv.approximate is False


def test_food_specific_data_beats_the_reference_and_water_is_last_resort():
    own = FoodProfile(grams_per_ml=0.6)  # e.g. sifted flour measured by the user
    conv = quantity_to_grams(1, {"name": "cup"}, own, "", "flour")
    assert conv.grams == pytest.approx(141.95, rel=0.01) and "this food's measures" in conv.note
    unknown = quantity_to_grams(1, {"name": "cup"}, FoodProfile(), "", "mystery grain")
    assert unknown.approximate is True and "water density" in unknown.note
    ref = quantity_to_grams(1, {"name": "cup"}, FoodProfile(), "", "flour")
    assert "USDA household measure" in ref.note and "FDC" in ref.note


# ---------------------------------------------------------------- set_food_nutrition fetch


async def test_set_food_nutrition_stores_the_usda_records_measures(tools, fake, usda):
    call, _ = tools
    usda.foods["171287"] = {"description": "Egg, whole, raw, fresh", "foodPortions": EGG}
    egg = fake.add_food("egg", extras={"portion_grams": json.dumps({"each": 55})})
    result = await call(
        "set_food_nutrition",
        food_id=egg["id"],
        per_100g={"calories": 143},
        source="usda",
        source_id="171287",
        portion_grams={"jumbo": 70},
    )
    stored = json.loads(fake.foods[egg["id"]]["extras"]["portion_grams"])
    assert stored["each"] == 55  # already on the food: kept
    assert stored["jumbo"] == 70  # passed explicitly: wins
    assert stored["large"] == 50 and stored["cup"] == 243  # added from USDA
    report = result["household_measures"]
    assert report["source"].startswith("USDA FDC 171287") and "large" in report["portion_grams_added"]
    assert "each" not in report["portion_grams_added"]
    assert report["grams_per_ml_added"] == pytest.approx(1.0271, abs=1e-3)

    # cached: a second food with the same record doesn't refetch
    other = fake.add_food("eggs")
    await call("set_food_nutrition", food_id=other["id"], per_100g={"calories": 143}, source="usda", source_id="171287")
    assert len(usda.requests) == 1


async def test_set_food_nutrition_measures_failures_are_warnings(tools, fake, usda):
    call, _ = tools
    rice = fake.add_food("rice")
    result = await call("set_food_nutrition", food_id=rice["id"], per_100g={"calories": 130}, source="usda", source_id="999")
    assert "couldn't fetch USDA household measures" in result["household_measures"]["warning"]
    assert result["nutrition_per_100g"] == {"calories": 130.0}  # nutrition still saved

    skipped = await call(
        "set_food_nutrition",
        food_id=rice["id"],
        per_100g={"calories": 130},
        source="usda",
        source_id="999",
        fetch_measures=False,
    )
    assert "household_measures" not in skipped
    manual = await call("set_food_nutrition", food_id=rice["id"], per_100g={"calories": 130}, source="manual")
    assert "household_measures" not in manual
    assert len(usda.requests) == 1
