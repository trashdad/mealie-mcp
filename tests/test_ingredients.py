"""Ingredient payload building: parsed strings (matched / partly matched / unmatched),
pre-structured dicts, mixing the two, and parser failure modes -- checked both at
the payload level and by what actually lands in the (fake) Mealie recipe."""

from __future__ import annotations

import pytest

from mealie_sous_chef.ingredients import build_parsed_ingredient, ingredients_payload


@pytest.fixture
def pantry(fake):
    return {
        "rice": fake.add_food("jasmine rice"),
        "salt": fake.add_food("salt"),
        "cup": fake.add_unit("cup", abbreviation="c", standardQuantity=1, standardUnit="cup"),
        "gram": fake.add_unit("gram", abbreviation="g", standardQuantity=1, standardUnit="gram"),
    }


# ---------------------------------------------------------------- pure builder


def test_parsed_fully_matched_links_food_and_unit_with_names():
    food = {"id": "f1", "name": "flour", "extras": {}}
    unit = {"id": "u1", "name": "cup"}
    payload = build_parsed_ingredient({"quantity": 2, "food": food, "unit": unit, "note": "sifted"}, "2 cups flour, sifted")
    assert payload["food"] == food and payload["unit"] == unit  # id AND name: Mealie 422s on id alone
    assert payload["quantity"] == 2
    assert payload["note"] == "sifted"
    assert payload["originalText"] == "2 cups flour, sifted"
    assert "display" not in payload  # Mealie computes it; sending note-as-display hid qty/unit/food
    assert payload["referenceId"]


def test_parsed_unit_matched_food_unmatched_folds_food_into_note():
    unit = {"id": "u1", "name": "gram"}
    payload = build_parsed_ingredient(
        {"quantity": 500, "unit": unit, "food": {"id": None, "name": "mystery flour"}, "note": ""}, "500 g mystery flour"
    )
    assert payload["unit"] == unit and "food" not in payload
    assert payload["quantity"] == 500
    assert payload["note"] == "mystery flour"


def test_parsed_nothing_matched_keeps_original_line_without_quantity():
    payload = build_parsed_ingredient(
        {"quantity": 1, "unit": {"id": None, "name": "pinch"}, "food": {"id": None, "name": "saffron"}, "note": ""},
        "1 pinch of saffron",
    )
    assert "food" not in payload and "unit" not in payload
    assert payload["note"] == "1 pinch of saffron"
    assert payload["quantity"] == 0  # otherwise Mealie would print "1 1 pinch of saffron"


def test_parsed_food_matched_without_quantity():
    food = {"id": "f1", "name": "salt"}
    payload = build_parsed_ingredient({"quantity": 0, "unit": None, "food": food, "note": "to taste"}, "salt to taste")
    assert payload == {**payload, "food": food, "quantity": 0, "note": "to taste"}


def test_parsed_food_with_id_but_no_name_is_treated_as_unlinked():
    payload = build_parsed_ingredient({"quantity": 1, "food": {"id": "f1"}}, "1 thing")
    assert "food" not in payload and payload["note"] == "1 thing"


# ---------------------------------------------------------------- payload via client


async def test_mixed_strings_and_structured_preserve_order(client, fake, pantry):
    items = [
        "2 cups jasmine rice",
        {"quantity": 5, "food_id": pantry["salt"]["id"], "unit_id": pantry["gram"]["id"], "note": "flaky"},
        "1 pinch of saffron",
        {"title": "Garnish", "note": "lime wedges"},
    ]
    built = await ingredients_payload(client, items)
    payload = built.payload
    assert built.warnings == [] and built.needs_review == []
    assert [p.get("food", {}).get("name") for p in payload] == ["jasmine rice", "salt", None, None]
    assert payload[0]["unit"]["name"] == "cup" and payload[0]["quantity"] == 2
    assert payload[1] == {**payload[1], "quantity": 5, "note": "flaky"}
    assert payload[1]["food"]["name"] == "salt" and payload[1]["unit"]["name"] == "gram"
    assert payload[2]["note"] == "1 pinch of saffron" and payload[2]["quantity"] == 0
    assert payload[3]["title"] == "Garnish" and payload[3]["note"] == "lime wedges"
    parser_calls = [c for c in fake.calls if c[1] == "/api/parser/ingredients"]
    assert len(parser_calls) == 1 and parser_calls[0][2]["ingredients"] == ["2 cups jasmine rice", "1 pinch of saffron"]


async def test_duplicate_lines_are_parsed_once(client, fake, pantry):
    payload = (await ingredients_payload(client, ["2 cups jasmine rice", "2 cups jasmine rice"])).payload
    assert len(payload) == 2 and payload[0]["referenceId"] != payload[1]["referenceId"]
    parser_calls = [c for c in fake.calls if c[1] == "/api/parser/ingredients"]
    assert parser_calls[0][2]["ingredients"] == ["2 cups jasmine rice"]


async def test_empty_list_makes_no_requests(client, fake):
    built = await ingredients_payload(client, [])
    assert built.payload == [] and built.warnings == [] and built.needs_review == []
    assert fake.calls == []


async def test_blank_lines_dropped_with_warning(client, fake, pantry):
    built = await ingredients_payload(client, ["  ", "2 cups jasmine rice", ""])
    assert len(built.payload) == 1
    assert built.warnings == ["dropped 2 blank ingredient line(s)"]


# ---------------------------------------------------------------- end to end through the tools


async def test_update_recipe_links_ingredients_in_mealie(tools, fake, pantry):
    call, _ = tools
    fake.add_recipe("Rice Bowl")
    await call(
        "update_recipe",
        slug="rice-bowl",
        ingredients=["2 cups jasmine rice", {"quantity": 3, "food_id": pantry["salt"]["id"], "unit_id": pantry["gram"]["id"]}],
    )
    stored = fake.recipes["rice-bowl"]["recipeIngredient"]
    assert stored[0]["food"]["id"] == pantry["rice"]["id"] and stored[0]["unit"]["id"] == pantry["cup"]["id"]
    assert stored[1]["food"]["id"] == pantry["salt"]["id"] and stored[1]["quantity"] == 3


async def test_structured_unknown_food_id_fails_loudly_and_creates_nothing(tools, fake, pantry):
    _, call_error = tools
    missing = "00000000-0000-0000-0000-000000000000"
    msg = await call_error(
        "create_recipe", name="Ghost", ingredients=[{"quantity": 1, "food_id": missing}], instructions=["x"]
    )
    assert f"food_id {missing!r} doesn't exist" in msg
    assert fake.recipes == {}  # validated before the recipe was created


async def test_structured_non_uuid_id_fails_loudly(tools, fake):
    _, call_error = tools
    fake.add_recipe("Soup")
    msg = await call_error("update_recipe", slug="soup", ingredients=[{"quantity": 1, "unit_id": "cup", "note": "stock"}])
    assert "unit_id 'cup' doesn't exist" in msg


@pytest.mark.parametrize(
    "item, expected",
    [
        ({"quantity": 1, "food": "rice"}, "unknown key(s) ['food']"),
        ({"quantity": -1, "note": "x"}, "must be a non-negative number"),
        ({"quantity": "2", "note": "x"}, "must be a non-negative number"),
        ({"quantity": 2}, "needs at least a food_id, note or title"),
        ({"note": 5}, "note must be a string"),
    ],
)
async def test_structured_input_errors_reach_the_model(tools, fake, item, expected):
    _, call_error = tools
    fake.add_recipe("Soup")
    msg = await call_error("update_recipe", slug="soup", ingredients=[item])
    assert expected in msg
    assert fake.recipes["soup"]["recipeIngredient"] == []


async def test_parser_http_failure_degrades_to_free_text(tools, fake, pantry):
    call, _ = tools
    fake.parser_status = 500
    fake.add_recipe("Soup")
    result = await call("update_recipe", slug="soup", ingredients=["2 cups jasmine rice", "salt"])
    assert "ingredient parser failed" in result["warnings"][0]
    stored = fake.recipes["soup"]["recipeIngredient"]
    assert [(i["note"], i["food"], i["quantity"]) for i in stored] == [("2 cups jasmine rice", None, 0), ("salt", None, 0)]


@pytest.mark.parametrize("bad_response", [{"detail": "nope"}, "garbage", None])
async def test_parser_non_list_response_degrades_to_free_text(tools, fake, pantry, bad_response):
    call, _ = tools
    fake.parser_response = bad_response if bad_response is not None else 42
    fake.add_recipe("Soup")
    result = await call("update_recipe", slug="soup", ingredients=["2 cups jasmine rice"])
    assert "expected a list" in result["warnings"][0]
    assert fake.recipes["soup"]["recipeIngredient"][0]["note"] == "2 cups jasmine rice"


async def test_parser_short_or_malformed_items_only_affect_those_lines(tools, fake, pantry):
    call, _ = tools
    good = fake._parse_line("2 cups jasmine rice")
    fake.parser_response = [good, {"input": "salt", "confidence": {}}]  # 2nd has no "ingredient"; 3rd missing
    fake.add_recipe("Soup")
    result = await call("update_recipe", slug="soup", ingredients=["2 cups jasmine rice", "salt", "pepper"])
    assert "no usable result for ['salt', 'pepper']" in result["warnings"][0]
    stored = fake.recipes["soup"]["recipeIngredient"]
    assert stored[0]["food"]["name"] == "jasmine rice"
    assert [i["note"] for i in stored[1:]] == ["salt", "pepper"]


async def test_parse_ingredients_tool_reports_matches_and_bad_items(tools, fake, pantry):
    call, _ = tools
    rows = await call("parse_ingredients", lines=["2 cups jasmine rice", "1 pinch of saffron", "  "])
    assert rows[0] == {**rows[0], "food_id": pantry["rice"]["id"], "unit_id": pantry["cup"]["id"], "quantity": 2}
    assert rows[1]["food_id"] is None and rows[1]["unit_id"] is None and rows[1]["food_name"] == "saffron"
    assert len(rows) == 2

    fake.parser_response = [{"input": "x"}]
    rows = await call("parse_ingredients", lines=["x"])
    assert rows == [{"input": "x", "error": "parser returned no usable result for this line"}]
