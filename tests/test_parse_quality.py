"""assess_parse on real parser output, needs_review in create/update, and
update_recipe keeping unchanged ingredients/steps (and their links) intact."""

from __future__ import annotations

import pytest

from mealie_sous_chef.ingredients import assess_parse, words
from parser_fixtures import REAL_PARSES, SALT, parser_response


@pytest.mark.parametrize("line", list(REAL_PARSES))
def test_assess_parse_on_real_parser_output(line):
    parsed, should_be_ok = REAL_PARSES[line]
    assert assess_parse(line, parsed).ok is should_be_ok


def test_assess_parse_reasons():
    salt = assess_parse("salt and pepper to taste", REAL_PARSES["salt and pepper to taste"][0])
    assert salt.dropped_words == ["pepper"] and salt.extra_words == []
    thighs = assess_parse("4 boneless skinless chicken thighs", REAL_PARSES["4 boneless skinless chicken thighs"][0])
    assert thighs.extra_words == ["lb"] and "words not in the line" in thighs.reason()


def test_assess_parse_accepts_matches_via_alias_or_plural():
    scallion = {"id": "f", "name": "green onion", "pluralName": "green onions", "aliases": [{"name": "scallion"}]}
    assert assess_parse("3 scallions, sliced", {"quantity": 3, "food": scallion, "note": "sliced"}).ok
    tomato = {"id": "t", "name": "tomato", "pluralName": "tomatoes", "aliases": []}
    assert assess_parse("2 tomatoes", {"quantity": 2, "food": tomato, "note": ""}).ok


def test_words_normalisation():
    assert words("1 1/2 Cups diced Tomatoes, and the berries") == {"cup", "diced", "tomato", "berry"}


# ---------------------------------------------------------------- create / update


async def test_create_recipe_keeps_lossy_lines_as_text_and_flags_them(tools, fake):
    call, _ = tools
    fake.add_food(SALT["name"], id=SALT["id"])
    fake.parser_response = parser_response
    result = await call(
        "create_recipe",
        name="Seasoned",
        ingredients=["salt and pepper to taste", "3 cups cooked basmati rice"],
        instructions=["Mix."],
    )
    stored = fake.recipes["seasoned"]["recipeIngredient"]
    assert stored[0]["food"] is None and stored[0]["note"] == "salt and pepper to taste"
    assert result["needs_review"] == [{"line": "salt and pepper to taste", "reason": "parser dropped 'pepper'"}]
    assert "review_recipe_ingredients" in result["needs_review_hint"]


@pytest.fixture
def linked_recipe(fake):
    rice = fake.add_food("rice")
    cup = fake.add_unit("cup")
    recipe = fake.add_recipe(
        "Rice",
        [
            fake.ingredient(2, food=rice, unit=cup, note="rinsed", originalText="2 cups rice, rinsed"),
            fake.ingredient(0, note="water", originalText="water"),
        ],
        steps=[{"text": "Rinse the rice.", "title": "Prep"}, {"text": "Boil."}],
    )
    refs = [i["referenceId"] for i in recipe["recipeIngredient"]]
    recipe["recipeInstructions"][0]["ingredientReferences"] = [{"referenceId": refs[0]}]
    return {"rice": rice, "cup": cup, "refs": refs}


async def test_update_recipe_keeps_identical_lines_untouched(tools, fake, linked_recipe):
    call, _ = tools
    # Claude re-sends what get_recipe showed (display text) plus one new line
    result = await call("update_recipe", slug="rice", ingredients=["2 cup rice rinsed", "water", "1 tsp salt"])
    stored = fake.recipes["rice"]["recipeIngredient"]
    assert [i["referenceId"] for i in stored[:2]] == linked_recipe["refs"]
    assert stored[0]["food"]["id"] == linked_recipe["rice"]["id"] and stored[0]["originalText"] == "2 cups rice, rinsed"
    parsed = next(c for c in fake.calls if c[1] == "/api/parser/ingredients")[2]["ingredients"]
    assert parsed == ["1 tsp salt"]  # only the new line went through the parser
    assert "warnings" not in result


async def test_update_recipe_steps_keep_ids_titles_and_links(tools, fake, linked_recipe):
    call, _ = tools
    before = fake.recipes["rice"]["recipeInstructions"][0]
    await call("update_recipe", slug="rice", instructions=["Rinse the rice.", "Boil for 12 minutes."])
    steps = fake.recipes["rice"]["recipeInstructions"]
    assert steps[0]["id"] == before["id"] and steps[0]["title"] == "Prep"
    assert steps[0]["ingredientReferences"] == [{"referenceId": linked_recipe["refs"][0]}]
    assert steps[1]["ingredientReferences"] == []


async def test_update_recipe_warns_when_step_links_are_orphaned(tools, fake, linked_recipe):
    call, _ = tools
    result = await call("update_recipe", slug="rice", ingredients=["1 cup brown rice", "water"])
    assert "step(s) [1]" in result["warnings"][0] and "edit_recipe_ingredients" in result["warnings"][0]


async def test_update_recipe_without_ingredients_skips_the_extra_read(tools, fake, linked_recipe):
    call, _ = tools
    await call("update_recipe", slug="rice", name="Plain rice")
    assert [c[0] for c in fake.calls] == ["PATCH"]
