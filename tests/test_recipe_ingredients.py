"""review_recipe_ingredients / edit_recipe_ingredients: state reporting, suggestions,
and in-place edits that preserve identity (referenceId, originalText, step links)."""

from __future__ import annotations

import json

import pytest

from parser_fixtures import CAN, CLOVE, GARLIC, JUNK_THIGHS, SALT, parser_response


@pytest.fixture
def kitchen(fake):
    """A recipe shaped like the real instance's: some linked, some not, a junk
    fuzzy match, steps referencing ingredients."""
    # ids match the captured real parser output (parser_fixtures), as they would live
    foods = {
        "junk": fake.add_food(JUNK_THIGHS["name"], id=JUNK_THIGHS["id"]),
        "thigh": fake.add_food("chicken thigh", pluralName="chicken thighs"),
        "garlic": fake.add_food("garlic", id=GARLIC["id"], extras={"nutrition_per_100g": json.dumps({"calories": 149})}),
        "coconut": fake.add_food("coconut milk"),
        "salt": fake.add_food("salt", id=SALT["id"]),
    }
    units = {
        "clove": fake.add_unit("clove", id=CLOVE["id"]),
        "can": fake.add_unit("can", id=CAN["id"]),
        "cup": fake.add_unit("cup", standardQuantity=1, standardUnit="cup"),
    }
    recipe = fake.add_recipe(
        "Thigh Bowl",
        [
            fake.ingredient(4, food=foods["junk"], originalText="4 boneless skinless chicken thighs"),
            fake.ingredient(2, food=foods["garlic"], unit=units["clove"], note="minced", originalText="2 cloves garlic, minced"),
            fake.ingredient(0, note="1 (14 oz) can light coconut milk", originalText="1 (14 oz) can light coconut milk"),
            fake.ingredient(0, note="salt and pepper to taste", originalText="salt and pepper to taste"),
        ],
        servings=4,
    )
    refs = [i["referenceId"] for i in recipe["recipeIngredient"]]
    recipe["recipeInstructions"] = [
        fake._store_step({"text": "Brown the thighs.", "ingredientReferences": [{"referenceId": refs[0]}]}),
        fake._store_step({"text": "Add garlic and coconut milk.", "ingredientReferences": [{"referenceId": refs[1]}, {"referenceId": refs[2]}]}),
    ]
    return {"foods": foods, "units": units, "recipe": recipe, "refs": refs}


# ---------------------------------------------------------------- review


async def test_review_reports_status_nutrition_and_step_usage(tools, fake, kitchen):
    call, _ = tools
    result = await call("review_recipe_ingredients", slug="thigh-bowl", suggest=False)
    rows = result["ingredients"]
    assert [r["status"] for r in rows] == ["suspect", "linked", "unlinked", "unlinked"]
    assert "'lb'" in rows[0]["status_reason"]
    assert rows[0]["used_in_steps"] == [1] and rows[1]["used_in_steps"] == [2] and rows[3]["used_in_steps"] == []
    assert rows[1]["nutrition"] == "cannot_weigh" and "clove" in rows[1]["nutrition_note"]
    assert rows[0]["nutrition"] == "missing_data"
    assert rows[2]["nutrition"] == "not_linked"
    assert result["summary"] == {
        "total": 4, "suspect": 1, "linked": 1, "unlinked": 2,
        "nutrition_missing_data": 1, "nutrition_cannot_weigh": 1, "nutrition_not_linked": 2,
    }
    assert "suggestion" not in rows[0]
    assert not any(c[1] == "/api/parser/ingredients" for c in fake.calls)


async def test_review_needs_attention_filter(tools, fake, kitchen):
    call, _ = tools
    garlic = kitchen["foods"]["garlic"]
    garlic["extras"]["portion_grams"] = json.dumps({"clove": 5})
    result = await call("review_recipe_ingredients", slug="thigh-bowl", only="needs_attention", suggest=False)
    assert [r["position"] for r in result["ingredients"]] == [1, 3, 4]  # garlic is linked + weighable
    assert result["summary"]["total"] == 4


async def test_review_suggestions_grade_and_propose_edits(tools, fake, kitchen):
    call, _ = tools
    fake.parser_response = parser_response
    result = await call("review_recipe_ingredients", slug="thigh-bowl")
    rows = {r["position"]: r for r in result["ingredients"]}

    # junk fuzzy match: the clean candidate "chicken thigh" is proposed instead
    thighs = rows[1]["suggestion"]
    assert thighs["grade"] == "candidate"
    assert thighs["proposed_edit"]["food_id"] == kitchen["foods"]["thigh"]["id"]
    assert thighs["extra_words"] == ["lb"]
    assert kitchen["foods"]["thigh"]["id"] in [c["id"] for c in thighs["food_candidates"]]

    # unmatched "light coconut milk": candidate found, qualifier kept in note, unit kept
    coconut = rows[3]["suggestion"]
    assert coconut["grade"] == "candidate"
    assert coconut["proposed_edit"] == {
        "ref": kitchen["refs"][2],
        "quantity": 1,
        "unit_id": kitchen["units"]["can"]["id"],
        "note": "light (14 oz)",
        "food_id": kitchen["foods"]["coconut"]["id"],
    }
    assert coconut["dropped_words"] == ["oz"]

    # "salt and pepper": parser linked salt but dropped pepper -> check
    salt = rows[4]["suggestion"]
    assert salt["grade"] == "check" and salt["dropped_words"] == ["pepper"]
    assert set(result["grades"]) == {"candidate", "check"}
    assert "suggestion" not in rows[2]  # linked and clean


async def test_review_lossy_unmatched_line_gets_no_guess(tools, fake):
    call, _ = tools
    fake.add_food("salt cod")
    fake.add_food("bell pepper")
    fake.parser_response = lambda lines: [
        {"input": line, "ingredient": {"quantity": 0, "unit": None, "food": {"id": None, "name": "salt"}, "note": "to taste"}}
        for line in lines
    ]
    fake.add_recipe("Seasoning", [fake.ingredient(0, note="salt and pepper to taste", originalText="salt and pepper to taste")])
    suggestion = (await call("review_recipe_ingredients", slug="seasoning"))["ingredients"][0]["suggestion"]
    assert suggestion["grade"] == "manual" and "proposed_edit" not in suggestion
    assert "insert_after" in suggestion["hint"]
    assert {c["name"] for c in suggestion["food_candidates"]} == {"salt cod", "bell pepper"}


def test_candidate_ranking_prefers_head_noun_and_exact_words():
    from mealie_sous_chef.recipe_ingredients import rank_candidates

    cands = [{"id": str(i), "name": n} for i, n in enumerate(
        ["boneless chicken", "chicken taco seasoning", "chicken thigh", "turkey thigh", "thickened cream"]
    )]
    ranked = [c["name"] for c in rank_candidates(cands, "boneless skinless chicken thighs", "4 boneless skinless chicken thighs")]
    assert ranked[:2] == ["chicken thigh", "boneless chicken"]
    assert "thickened cream" not in ranked
    onions = [{"id": str(i), "name": n} for i, n in enumerate(["onion gravy", "green olive", "yellow onion", "onion"])]
    assert rank_candidates(onions, "green onions", "2 green onions, sliced")[0]["name"] == "onion"


async def test_review_no_match_proposes_create(tools, fake):
    call, _ = tools
    fake.add_unit("teaspoon", abbreviation="tsp")
    fake.add_recipe("Odd", [fake.ingredient(0, note="1 tsp sumac", originalText="1 tsp sumac")])
    result = await call("review_recipe_ingredients", slug="odd")
    suggestion = result["ingredients"][0]["suggestion"]
    assert suggestion["grade"] == "no_match" and suggestion["food_candidates"] == []
    assert suggestion["proposed_edit"]["create_food"] == "sumac"


async def test_review_survives_parser_failure(tools, fake, kitchen):
    call, _ = tools
    fake.parser_status = 500
    result = await call("review_recipe_ingredients", slug="thigh-bowl")
    assert "parser failed" in result["summary"]["suggestion_error"]
    assert result["ingredients"][2]["suggestion"]["grade"] == "no_parse"


# ---------------------------------------------------------------- edit


async def test_edit_updates_only_targeted_rows_and_keeps_identity(tools, fake, kitchen):
    call, _ = tools
    before = json.loads(json.dumps(fake.recipes["thigh-bowl"]["recipeIngredient"]))
    result = await call(
        "edit_recipe_ingredients",
        slug="thigh-bowl",
        edits=[
            {"ref": kitchen["refs"][0], "food_id": kitchen["foods"]["thigh"]["id"], "note": "boneless skinless"},
            {"ref": 3, "quantity": 1, "unit_id": kitchen["units"]["can"]["id"], "food_id": kitchen["foods"]["coconut"]["id"], "note": "light, 14 oz"},
        ],
    )
    after = fake.recipes["thigh-bowl"]["recipeIngredient"]
    assert [i["referenceId"] for i in after] == kitchen["refs"]
    assert after[0]["food"]["id"] == kitchen["foods"]["thigh"]["id"] and after[0]["quantity"] == 4
    assert after[0]["originalText"] == "4 boneless skinless chicken thighs"
    assert after[2]["unit"]["name"] == "can" and after[2]["note"] == "light, 14 oz"
    for untouched in (1, 3):
        assert {k: after[untouched][k] for k in ("food", "unit", "note", "quantity")} == {
            k: before[untouched][k] for k in ("food", "unit", "note", "quantity")
        }
    assert fake.recipes["thigh-bowl"]["recipeInstructions"][1]["ingredientReferences"][1]["referenceId"] == kitchen["refs"][2]
    assert [r["status"] for r in result["ingredients"]] == ["linked", "linked", "linked", "unlinked"]
    assert "warnings" not in result


async def test_edit_create_food_reuses_existing_name(tools, fake, kitchen):
    call, _ = tools
    result = await call(
        "edit_recipe_ingredients",
        slug="thigh-bowl",
        edits=[
            {"ref": 4, "create_food": "Salt", "note": "to taste"},
            {"insert_after": 4, "create_food": "black pepper", "note": "to taste"},
        ],
    )
    names = {(f["name"], f["created"]) for f in result["foods_and_units"]}
    assert names == {("salt", False), ("black pepper", True)}
    assert sum(1 for f in fake.foods.values() if f["name"].lower() == "salt") == 1
    after = fake.recipes["thigh-bowl"]["recipeIngredient"]
    assert [i["food"]["name"] if i["food"] else None for i in after][3:] == ["salt", "black pepper"]


async def test_edit_text_reparse_rejects_lossy_lines_atomically(tools, fake, kitchen):
    call, call_error = tools
    fake.parser_response = parser_response
    before = json.loads(json.dumps(fake.recipes["thigh-bowl"]))
    msg = await call_error(
        "edit_recipe_ingredients",
        slug="thigh-bowl",
        edits=[
            {"ref": 2, "text": "2 cloves garlic, minced"},  # clean
            {"ref": 4, "text": "salt and pepper to taste"},  # drops pepper
        ],
    )
    assert "nothing was changed" in msg and "'pepper'" in msg
    assert fake.recipes["thigh-bowl"] == before
    assert not any(c[0] == "PATCH" for c in fake.calls)

    await call("edit_recipe_ingredients", slug="thigh-bowl", edits=[{"ref": 2, "text": "2 cloves garlic, minced"}])
    assert fake.recipes["thigh-bowl"]["recipeIngredient"][1]["referenceId"] == kitchen["refs"][1]


async def test_edit_delete_insert_move_ordering(tools, fake, kitchen):
    call, _ = tools
    r = kitchen["refs"]
    result = await call(
        "edit_recipe_ingredients",
        slug="thigh-bowl",
        edits=[
            {"ref": r[3], "delete": True},
            {"insert_after": "start", "title": "Sauce", "note": "chilli crisp"},
            {"ref": r[0], "move_after": r[2]},
            {"insert_after": r[3], "note": "black pepper"},  # anchored to a deleted row: lands where it was
            {"insert_after": "end", "note": "lime wedges"},
        ],
    )
    texts = [i["note"] or (i["food"] or {}).get("name") for i in fake.recipes["thigh-bowl"]["recipeIngredient"]]
    assert texts == [
        "chilli crisp", "minced", "1 (14 oz) can light coconut milk",
        "lbs boneless skinless chicken thighs", "black pepper", "lime wedges",
    ]
    assert fake.recipes["thigh-bowl"]["recipeIngredient"][3]["referenceId"] == r[0]
    assert fake.recipes["thigh-bowl"]["recipeIngredient"][0]["title"] == "Sauce"
    assert "warnings" not in result  # the deleted row wasn't referenced by a step


async def test_edit_warns_when_deleting_a_step_referenced_ingredient(tools, fake, kitchen):
    call, _ = tools
    result = await call("edit_recipe_ingredients", slug="thigh-bowl", edits=[{"ref": 1, "delete": True}])
    assert "step(s) [1]" in result["warnings"][0]


async def test_edit_move_with_field_change_and_unlink(tools, fake, kitchen):
    call, _ = tools
    await call(
        "edit_recipe_ingredients",
        slug="thigh-bowl",
        edits=[{"ref": 1, "move_after": 4, "food_id": None, "note": "4 chicken thighs"}],
    )
    after = fake.recipes["thigh-bowl"]["recipeIngredient"]
    assert after[-1]["referenceId"] == kitchen["refs"][0] and after[-1]["food"] is None
    assert after[-1]["note"] == "4 chicken thighs"


@pytest.mark.parametrize(
    "edits, expected",
    [
        ([], "edits is empty"),
        ([{"food_id": "x"}], "exactly one of ref"),
        ([{"ref": 1, "insert_after": "end", "note": "x"}], "exactly one of ref"),
        ([{"ref": 9, "note": "x"}], "position 9 is out of range"),
        ([{"ref": "not-a-ref", "note": "x"}], "doesn't match any ingredient"),
        ([{"ref": 1, "note": "a"}, {"ref": 1, "quantity": 2}], "both change ingredient 1"),
        ([{"ref": 1, "food_id": "a", "create_food": "b"}], "food_id or create_food"),
        ([{"ref": 1, "text": "2 cups rice", "note": "x"}], "don't combine it with ['note']"),
        ([{"ref": 1, "delete": True, "note": "x"}], "delete can't be combined"),
        ([{"ref": 1}], "changes nothing"),
        ([{"insert_after": "end"}], "an insert needs"),
        ([{"ref": 1, "quantity": -1}], "non-negative"),
        ([{"ref": 1, "colour": "red"}], "unknown key(s) ['colour']"),
        ([{"ref": 1, "move_after": 2}, {"ref": 2, "move_after": 1}], "cycle"),
        ([{"ref": 1, "move_after": 1}], "after itself"),
        ([{"ref": 1, "food_id": "00000000-0000-0000-0000-000000000000"}], "doesn't exist"),
    ],
)
async def test_edit_validation_errors_change_nothing(tools, fake, kitchen, edits, expected):
    _, call_error = tools
    before = json.loads(json.dumps(fake.recipes["thigh-bowl"]))
    food_count = len(fake.foods)
    msg = await call_error("edit_recipe_ingredients", slug="thigh-bowl", edits=edits)
    assert expected in msg
    assert fake.recipes["thigh-bowl"] == before and len(fake.foods) == food_count


async def test_review_then_apply_proposed_edits_end_to_end(tools, fake, kitchen):
    """The intended loop: review -> Claude picks proposed edits -> edit."""
    call, _ = tools
    fake.parser_response = parser_response
    review = await call("review_recipe_ingredients", slug="thigh-bowl", only="needs_attention")
    chosen = [r["suggestion"]["proposed_edit"] for r in review["ingredients"] if r.get("suggestion", {}).get("grade") == "candidate"]
    assert len(chosen) == 2
    result = await call("edit_recipe_ingredients", slug="thigh-bowl", edits=chosen)
    assert [r["status"] for r in result["ingredients"]] == ["linked", "linked", "linked", "unlinked"]
    assert result["ingredients"][0]["food"]["name"] == "chicken thigh"
    assert result["ingredients"][2]["food"]["name"] == "coconut milk"
