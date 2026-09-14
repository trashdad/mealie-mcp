"""Regenerate mealie_sous_chef/data/household_measures.json from USDA FoodData Central.

Source: USDA FoodData Central, "SR Legacy" (Standard Reference) and "Survey (FNDDS)"
data -- the household measures (cups, spoons, pieces) USDA publishes alongside each
food, with gram weights from USDA's own measurements. Public domain.

Each entry below names the USDA food by its exact description so the pick is
deliberate, not whatever search ranks first. Run with a real API key:

    USDA_API_KEY=... python scripts/build_household_measures.py > mealie_sous_chef/data/household_measures.json

Review the diff before committing: the script prints warnings for entries it
couldn't resolve.
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mealie_sous_chef.household_measures import parse_usda_portions  # noqa: E402

API = "https://api.nal.usda.gov/fdc/v1"

# (names this entry answers to in a recipe, exact USDA description, data type)
# Names are matched against Mealie food names: every word of a name must appear.
REFERENCE: list[tuple[list[str], str, str]] = [
    # flours, starches, grains
    (["flour", "all-purpose flour", "white flour", "plain flour"], "Wheat flour, white, all-purpose, enriched, bleached", "SR Legacy"),
    (["bread flour"], "Wheat flour, white, bread, enriched", "SR Legacy"),
    (["cake flour"], "Wheat flour, white, cake, enriched", "SR Legacy"),
    (["whole wheat flour"], "Wheat flour, whole-grain", "SR Legacy"),
    (["almond flour", "almond meal"], "Nuts, almonds, blanched", "SR Legacy"),
    (["cornmeal"], "Cornmeal, whole-grain, yellow", "SR Legacy"),
    (["cornstarch", "corn starch"], "Cornstarch", "SR Legacy"),
    (["rolled oats", "oats", "oatmeal"], "Cereals, oats, regular and quick, not fortified, dry", "SR Legacy"),
    (["white rice", "rice", "jasmine rice", "basmati rice", "long grain rice"], "Rice, white, long-grain, regular, raw, enriched", "SR Legacy"),
    (["cooked white rice", "cooked rice", "cooked jasmine rice", "cooked basmati rice"], "Rice, white, long-grain, regular, enriched, cooked", "SR Legacy"),
    (["brown rice"], "Rice, brown, long-grain, raw", "SR Legacy"),
    (["cooked brown rice"], "Rice, brown, long-grain, cooked", "SR Legacy"),
    (["quinoa"], "Quinoa, uncooked", "SR Legacy"),
    (["cooked quinoa"], "Quinoa, cooked", "SR Legacy"),
    (["lentils", "red lentils", "green lentils"], "Lentils, raw", "SR Legacy"),
    (["breadcrumbs", "bread crumbs"], "Bread, crumbs, dry, grated, plain", "SR Legacy"),
    (["panko"], "Bread, crumbs, dry, grated, plain", "SR Legacy"),
    (["couscous"], "Couscous, dry", "SR Legacy"),
    # sugars, sweeteners
    (["sugar", "granulated sugar", "white sugar", "cane sugar"], "Sugars, granulated", "SR Legacy"),
    (["brown sugar", "light brown sugar", "dark brown sugar"], "Sugars, brown", "SR Legacy"),
    (["powdered sugar", "confectioners sugar", "icing sugar"], "Sugars, powdered", "SR Legacy"),
    (["honey"], "Honey", "SR Legacy"),
    (["maple syrup"], "Syrups, maple", "SR Legacy"),
    (["molasses"], "Molasses", "SR Legacy"),
    (["corn syrup"], "Syrups, corn, light", "SR Legacy"),
    # fats, dairy
    (["olive oil", "extra virgin olive oil", "extra-virgin olive oil"], "Oil, olive, salad or cooking", "SR Legacy"),
    (["vegetable oil", "canola oil", "oil", "cooking oil"], "Oil, canola", "SR Legacy"),
    (["coconut oil"], "Oil, coconut", "SR Legacy"),
    (["sesame oil"], "Oil, sesame, salad or cooking", "SR Legacy"),
    (["butter", "unsalted butter", "salted butter"], "Butter, salted", "SR Legacy"),
    (["milk", "whole milk"], "Milk, whole, 3.25% milkfat, with added vitamin D", "SR Legacy"),
    (["heavy cream", "heavy whipping cream", "whipping cream"], "Cream, fluid, heavy whipping", "SR Legacy"),
    (["half and half"], "Cream, fluid, half and half", "SR Legacy"),
    (["sour cream"], "Cream, sour, cultured", "SR Legacy"),
    (["greek yogurt"], "Yogurt, Greek, whole milk, plain", "Survey (FNDDS)"),
    (["yogurt", "plain yogurt"], "Yogurt, plain, whole milk", "SR Legacy"),
    (["cream cheese"], "Cheese, cream", "SR Legacy"),
    (["cottage cheese"], "Cheese, cottage, creamed, large or small curd", "SR Legacy"),
    (["cheddar", "cheddar cheese", "shredded cheddar"], "Cheese, Cheddar", "Survey (FNDDS)"),
    (["mozzarella", "mozzarella cheese", "shredded mozzarella"], "Cheese, mozzarella, whole milk", "SR Legacy"),
    (["parmesan", "parmesan cheese", "grated parmesan"], "Cheese, parmesan, grated", "SR Legacy"),
    (["coconut milk", "light coconut milk", "canned coconut milk"], "Nuts, coconut milk, canned (liquid expressed from grated meat and water)", "SR Legacy"),
    # baking, spices, condiments
    (["cocoa powder", "cocoa", "unsweetened cocoa powder"], "Cocoa, dry powder, unsweetened", "SR Legacy"),
    (["chocolate chips", "semisweet chocolate chips"], "Candies, semisweet chocolate", "SR Legacy"),
    (["baking soda"], "Leavening agents, baking soda", "SR Legacy"),
    (["baking powder"], "Leavening agents, baking powder, double-acting, straight phosphate", "SR Legacy"),
    (["salt", "table salt", "sea salt", "kosher salt"], "Salt, table", "SR Legacy"),
    (["black pepper", "pepper", "ground black pepper"], "Spices, pepper, black", "SR Legacy"),
    (["cinnamon", "ground cinnamon"], "Spices, cinnamon, ground", "SR Legacy"),
    (["paprika", "smoked paprika"], "Spices, paprika", "SR Legacy"),
    (["cumin", "ground cumin"], "Spices, cumin seed", "SR Legacy"),
    (["chili powder"], "Spices, chili powder", "SR Legacy"),
    (["garlic powder"], "Spices, garlic powder", "SR Legacy"),
    (["onion powder"], "Spices, onion powder", "SR Legacy"),
    (["oregano", "dried oregano"], "Spices, oregano, dried", "SR Legacy"),
    (["basil", "dried basil"], "Spices, basil, dried", "SR Legacy"),
    (["thyme", "dried thyme"], "Spices, thyme, dried", "SR Legacy"),
    (["ginger", "ground ginger"], "Spices, ginger, ground", "SR Legacy"),
    (["turmeric", "ground turmeric"], "Spices, turmeric, ground", "SR Legacy"),
    (["vanilla extract", "vanilla"], "Vanilla extract", "SR Legacy"),
    (["soy sauce"], "Soy sauce made from soy and wheat (shoyu)", "SR Legacy"),
    (["vinegar", "white vinegar", "apple cider vinegar"], "Vinegar, distilled", "SR Legacy"),
    (["ketchup"], "Catsup", "SR Legacy"),
    (["mayonnaise", "mayo"], "Salad dressing, mayonnaise, regular", "SR Legacy"),
    (["mustard", "yellow mustard", "dijon mustard"], "Mustard, prepared, yellow", "SR Legacy"),
    (["peanut butter"], "Peanut butter, smooth style, without salt", "SR Legacy"),
    (["tomato paste"], "Tomato products, canned, paste, without salt added", "SR Legacy"),
    (["tomato sauce"], "Tomato products, canned, sauce", "SR Legacy"),
    (["diced tomatoes", "canned tomatoes", "crushed tomatoes"], "Tomatoes, red, ripe, canned, packed in tomato juice", "SR Legacy"),
    (["salsa"], "Sauce, salsa, ready-to-serve", "SR Legacy"),
    (["chicken broth", "chicken stock", "broth", "stock"], "Soup, chicken broth, ready-to-serve", "SR Legacy"),
    (["water"], "Beverages, water, tap, drinking", "SR Legacy"),
    # nuts, dried fruit
    (["almonds"], "Nuts, almonds", "SR Legacy"),
    (["walnuts"], "Nuts, walnuts, english", "SR Legacy"),
    (["peanuts"], "Peanuts, all types, raw", "SR Legacy"),
    (["raisins"], "Raisins, dark, seedless", "SR Legacy"),
    (["shredded coconut", "coconut flakes"], "Nuts, coconut meat, dried (desiccated), sweetened, shredded", "SR Legacy"),
    # produce (cups chopped and whole pieces)
    (["onion", "yellow onion", "white onion"], "Onions, raw", "SR Legacy"),
    (["red onion"], "Onions, raw", "SR Legacy"),
    (["green onion", "scallion", "spring onion"], "Onions, spring or scallions (includes tops and bulb), raw", "SR Legacy"),
    (["garlic"], "Garlic, raw", "SR Legacy"),
    (["carrot", "carrots"], "Carrots, raw", "SR Legacy"),
    (["celery"], "Celery, raw", "SR Legacy"),
    (["bell pepper", "red bell pepper", "green bell pepper"], "Peppers, sweet, red, raw", "SR Legacy"),
    (["jalapeno", "jalapeno pepper", "jalapenos"], "Peppers, jalapeno, raw", "SR Legacy"),
    (["tomato", "tomatoes"], "Tomatoes, red, ripe, raw, year round average", "SR Legacy"),
    (["potato", "potatoes"], "Potatoes, flesh and skin, raw", "SR Legacy"),
    (["sweet potato", "sweet potatoes"], "Sweet potato, raw, unprepared", "SR Legacy"),
    (["broccoli"], "Broccoli, raw", "SR Legacy"),
    (["spinach", "baby spinach"], "Spinach, raw", "SR Legacy"),
    (["cabbage"], "Cabbage, raw", "SR Legacy"),
    (["mushrooms"], "Mushrooms, white, raw", "SR Legacy"),
    (["zucchini"], "Squash, summer, zucchini, includes skin, raw", "SR Legacy"),
    (["cilantro", "fresh cilantro"], "Coriander (cilantro) leaves, raw", "SR Legacy"),
    (["parsley", "fresh parsley"], "Parsley, fresh", "SR Legacy"),
    (["fresh ginger", "ginger root"], "Ginger root, raw", "SR Legacy"),
    (["lemon"], "Lemons, raw, without peel", "SR Legacy"),
    (["lemon juice"], "Lemon juice, raw", "SR Legacy"),
    (["lime"], "Limes, raw", "SR Legacy"),
    (["lime juice"], "Lime juice, raw", "SR Legacy"),
    (["banana"], "Bananas, raw", "SR Legacy"),
    (["apple"], "Apples, raw, with skin", "SR Legacy"),
    (["avocado"], "Avocados, raw, all commercial varieties", "SR Legacy"),
    (["frozen peas", "peas", "green peas"], "Peas, green, frozen, unprepared", "SR Legacy"),
    (["corn", "sweet corn", "frozen corn"], "Corn, sweet, yellow, frozen, kernels cut off cob, unprepared", "SR Legacy"),
    (["black beans"], "Beans, black, mature seeds, canned, low sodium", "SR Legacy"),
    (["chickpeas", "garbanzo beans"], "Chickpeas (garbanzo beans, bengal gram), mature seeds, canned, drained, rinsed in tap water", "SR Legacy"),
    (["white beans", "cannellini beans", "great northern beans"], "Beans, white, mature seeds, canned", "SR Legacy"),
    (["egg", "eggs"], "Egg, whole, raw, fresh", "SR Legacy"),
    (["egg white", "egg whites"], "Egg, white, raw, fresh", "SR Legacy"),
    (["egg yolk", "egg yolks"], "Egg, yolk, raw, fresh", "SR Legacy"),
]


def find(client: httpx.Client, key: str, description: str, data_type: str) -> dict | None:
    r = client.post(
        f"{API}/foods/search",
        params={"api_key": key},
        json={"query": description, "dataType": [data_type], "pageSize": 25},
    )
    r.raise_for_status()
    wanted = description.strip().lower()

    def same(found: str) -> bool:
        # USDA appends "(Includes foods for USDA's Food Distribution Program)" to some records
        found = found.strip().lower()
        return found == wanted or found.startswith(f"{wanted} (includes foods for usda")

    return next((f for f in r.json().get("foods", []) if same(f.get("description", ""))), None)


def main() -> None:
    key = os.environ["USDA_API_KEY"]
    client = httpx.Client(timeout=30)
    entries, problems = [], []
    for names, description, data_type in REFERENCE:
        match = find(client, key, description, data_type)
        if match is None:
            problems.append(f"no exact USDA match for {description!r} ({data_type})")
            continue
        detail = client.get(f"{API}/food/{match['fdcId']}", params={"api_key": key})
        detail.raise_for_status()
        portions = parse_usda_portions(detail.json().get("foodPortions") or [], match["description"])
        if not portions["portion_grams"]:
            problems.append(f"{description!r}: no usable household measures")
            continue
        entries.append(
            {
                "names": names,
                "usda_fdc_id": match["fdcId"],
                "usda_description": match["description"],
                "usda_data_type": data_type,
                **portions,
            }
        )
        time.sleep(0.2)
    print(
        json.dumps(
            {
                "source": "USDA FoodData Central household measures (SR Legacy / FNDDS), public domain",
                "generated_by": "scripts/build_household_measures.py",
                "entries": entries,
            },
            indent=1,
            ensure_ascii=False,
        )
    )
    for p in problems:
        print(f"WARNING: {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
