"""Real /api/parser/ingredients results, captured from a live Mealie (nightly, ~250
foods) and trimmed to the fields the code reads. Ids are the instance's real ids."""

CUP = {"id": "720faacf-02d6-4eb7-8f41-7e8961fe6c14", "name": "cup", "pluralName": None, "abbreviation": "cup", "aliases": []}
CLOVE = {"id": "0e767cae-df8b-49b9-a5e3-4db6dbb1232d", "name": "clove", "pluralName": None, "abbreviation": "", "aliases": []}
CAN = {"id": "ec0fc665-6b95-4572-9126-71429a4b005a", "name": "can", "pluralName": None, "abbreviation": "", "aliases": []}
TBSP = {"id": "64aa54e2-0012-4942-97ab-632a8dcd8733", "name": "tablespoon", "pluralName": None, "abbreviation": "tbsp", "aliases": []}

JUNK_THIGHS = {"id": "f4e0abb3-39f2-46fc-bf80-2590b20604f8", "name": "lbs boneless skinless chicken thighs", "pluralName": None, "aliases": []}
GARLIC = {"id": "33b8a7ab-e16f-4f67-b2b5-be04947160f2", "name": "garlic", "pluralName": None, "aliases": []}
SALT = {"id": "a9ae58ec-45a7-4049-8964-37ed54b91b1c", "name": "salt", "pluralName": None, "aliases": []}
BASMATI = {"id": "2db510f1-6a34-46aa-8a5b-c1c14710d99f", "name": "basmati rice", "pluralName": None, "aliases": []}


def unmatched(name):
    return {"id": None, "name": name}


# line -> (parsed ingredient, should it be linked automatically?)
REAL_PARSES = {
    "4 boneless skinless chicken thighs": ({"quantity": 4, "unit": None, "food": JUNK_THIGHS, "note": ""}, False),
    "2 cloves garlic, minced": ({"quantity": 2, "unit": CLOVE, "food": GARLIC, "note": "minced"}, True),
    "1 (14 oz) can light coconut milk": ({"quantity": 1, "unit": CAN, "food": unmatched("light coconut milk"), "note": ""}, False),
    "1/2 cup chopped fresh cilantro": ({"quantity": 0.5, "unit": CUP, "food": unmatched("fresh cilantro"), "note": "chopped"}, True),
    "salt and pepper to taste": ({"quantity": 0, "unit": None, "food": SALT, "note": "to taste"}, False),
    "2 tbsp extra-virgin olive oil": ({"quantity": 2, "unit": TBSP, "food": unmatched("extra-virgin olive oil"), "note": ""}, True),
    "1 large yellow onion, diced": ({"quantity": 1, "unit": None, "food": unmatched("yellow onion"), "note": "large, diced"}, True),
    "3 cups cooked basmati rice": ({"quantity": 3, "unit": CUP, "food": BASMATI, "note": "cooked"}, True),
}


def parser_response(lines):
    return [{"input": line, "confidence": {"average": 0.9}, "ingredient": REAL_PARSES[line][0]} for line in lines]
