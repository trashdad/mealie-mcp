"""An in-memory stand-in for the parts of Mealie's API these tools use, served
through httpx.MockTransport so the real MealieClient (status handling included)
is exercised.

Behaviours are modelled on Mealie's source (mealie-recipes/mealie, v3.x), not
guessed -- each quirk that matters is noted where it's implemented:
  - food/unit `name` is required on create/update and on linked ingredient objects (422)
  - food/unit names are unique per group (409)
  - extras values are stored in a string column; a nested object fails to save (400)
  - a unit's standardQuantity/standardUnit are silently nulled unless both are valid
  - PUT /merge returns {"message", "error"}; a bad id is a bare 500
  - DELETE of a food used by recipes succeeds and nulls the ingredient links;
    a shopping-list reference is a 409 on Postgres (`postgres=True`)
  - an ingredient food/unit `id` that doesn't exist is silently dropped on save
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx


def _err(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": {"message": message, "error": True, "exception": None}})


def _paginate(items: list[dict], params: dict[str, list[str]]) -> dict:
    per_page = int(params.get("perPage", ["50"])[0])
    page = int(params.get("page", ["1"])[0])
    total = len(items)
    if per_page == -1:
        per_page = max(total, 1)
    start = (page - 1) * per_page
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, -(-total // per_page)),
        "items": items[start : start + per_page],
    }


class FakeMealie:
    def __init__(self, postgres: bool = False):
        self.postgres = postgres
        self.foods: dict[str, dict] = {}
        self.units: dict[str, dict] = {}
        self.recipes: dict[str, dict] = {}  # slug -> recipe
        self.shopping_items: dict[str, dict] = {}
        self.shopping_lists: dict[str, dict] = {}
        self.mealplans: dict[str, dict] = {}
        self.tags: list[dict] = []
        self.categories: list[dict] = []
        self.calls: list[tuple[str, str, Any]] = []
        self.legacy_shopping = False  # True: no bulk add-recipe endpoint (older Mealie)
        # knobs for failure-mode tests
        self.parser_response: Any = None  # override the parser's JSON body
        self.parser_status: int | None = None
        self.fail_paths: dict[tuple[str, str], int] = {}  # (method, path) -> status

    # ------------------------------------------------------------ seeding

    def add_food(self, name: str, **fields: Any) -> dict:
        food = {
            "id": str(uuid.uuid4()),
            "name": name,
            "pluralName": None,
            "description": "",
            "extras": {},
            "labelId": None,
            "aliases": [],
            "substitutions": [],
            "householdsWithIngredientFood": [],
            "label": None,
            **fields,
        }
        self.foods[food["id"]] = food
        return food

    def add_unit(self, name: str, **fields: Any) -> dict:
        unit = {
            "id": str(uuid.uuid4()),
            "name": name,
            "pluralName": None,
            "description": "",
            "extras": {},
            "fraction": True,
            "abbreviation": "",
            "pluralAbbreviation": "",
            "useAbbreviation": False,
            "aliases": [],
            "standardQuantity": None,
            "standardUnit": None,
            **fields,
        }
        self._normalize_standard(unit)
        self.units[unit["id"]] = unit
        return unit

    def add_recipe(
        self,
        name: str,
        ingredients: list[dict] | None = None,
        servings: float = 0,
        steps: list[dict] | None = None,
        **fields: Any,
    ) -> dict:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        recipe = {
            "id": str(uuid.uuid4()),
            "name": name,
            "slug": slug,
            "description": "",
            "recipeServings": float(servings),
            "recipeYieldQuantity": 0.0,
            "recipeIngredient": [],
            "recipeInstructions": [],
            "nutrition": {
                k: None
                for k in (
                    "calories", "carbohydrateContent", "cholesterolContent", "fatContent", "fiberContent",
                    "proteinContent", "saturatedFatContent", "sodiumContent", "sugarContent",
                    "transFatContent", "unsaturatedFatContent",
                )
            },
            "tags": [],
            "recipeCategory": [],
            "tools": [],
            "notes": [],
            "extras": {},
            **fields,
        }
        self.recipes[slug] = recipe
        if ingredients:
            recipe["recipeIngredient"] = [self._store_ingredient(i) for i in ingredients]
        if steps:
            recipe["recipeInstructions"] = [self._store_step(st) for st in steps]
        return recipe

    def add_shopping_list(self, name: str) -> dict:
        shopping_list = {"id": str(uuid.uuid4()), "name": name}
        self.shopping_lists[shopping_list["id"]] = shopping_list
        return shopping_list

    @staticmethod
    def _store_step(step: dict) -> dict:
        return {
            "id": step.get("id") or str(uuid.uuid4()),
            "title": step.get("title") or "",
            "summary": step.get("summary"),
            "text": step["text"],
            "ingredientReferences": [
                {"referenceId": r.get("referenceId")} for r in step.get("ingredientReferences") or []
            ],
            "noteReferences": [],
        }

    def ingredient(
        self, quantity: float, food: dict | None = None, unit: dict | None = None, note: str = "", **fields: Any
    ) -> dict:
        return {"quantity": quantity, "food": food, "unit": unit, "note": note, **fields}

    def add_shopping_item(
        self, food: dict | None = None, unit: dict | None = None, note: str = "", list_id: str | None = None
    ) -> dict:
        item = {
            "id": str(uuid.uuid4()),
            "shoppingListId": list_id,
            "foodId": food["id"] if food else None,
            "unitId": unit["id"] if unit else None,
            "note": note,
            "quantity": 1.0,
            "display": note,
            "checked": False,
        }
        self.shopping_items[item["id"]] = item
        return item

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _normalize_standard(unit: dict) -> None:
        # CreateIngredientUnit.validate_standardization_fields
        if not unit.get("standardUnit") or not ((unit.get("standardQuantity") or 0) > 0):
            unit["standardQuantity"] = unit["standardUnit"] = None

    @staticmethod
    def _extras_ok(extras: Any) -> bool:
        return extras is None or (isinstance(extras, dict) and all(isinstance(v, str | None) for v in extras.values()))

    def _display(self, ing: dict) -> str:
        parts = []
        if ing.get("quantity"):
            q = ing["quantity"]
            parts.append(str(int(q)) if float(q).is_integer() else str(q))
        if ing.get("quantity") and ing.get("unit"):
            parts.append(ing["unit"]["name"])
        if ing.get("food"):
            parts.append(ing["food"]["name"])
        if ing.get("note"):
            parts.append(ing["note"])
        return " ".join(parts)

    def _store_ingredient(self, ing: dict) -> dict:
        """RecipeIngredient validation + RecipeIngredientModel auto_init lookup."""
        stored = {
            "quantity": float(ing.get("quantity") or 0),
            "unit": None,
            "food": None,
            "referencedRecipe": None,
            "note": ing.get("note") or "",
            "title": ing.get("title"),
            "originalText": ing.get("originalText"),
            "substitutions": [],
            "referenceId": ing.get("referenceId") or str(uuid.uuid4()),
        }
        for key, table in (("food", self.foods), ("unit", self.units)):
            ref = ing.get(key)
            if ref is None:
                continue
            if not isinstance(ref, dict) or not ref.get("name"):
                raise ValueError(f"422:{key}.name field required")
            if not ref.get("id"):
                raise ValueError(f"400:Expected 'id' to be provided for {key}")
            # MANYTOONE lookup by id: an unknown id silently becomes None
            row = table.get(ref["id"])
            stored[key] = copy.deepcopy(row) if row else None
        stored["display"] = self._display(stored)
        return stored

    def _recipe_out(self, recipe: dict) -> dict:
        out = copy.deepcopy(recipe)
        for ing in out["recipeIngredient"]:
            # relationships are loaded fresh on read
            if ing.get("food"):
                ing["food"] = copy.deepcopy(self.foods.get(ing["food"]["id"]))
            if ing.get("unit"):
                ing["unit"] = copy.deepcopy(self.units.get(ing["unit"]["id"]))
            ing["display"] = self._display(ing)
        return out

    # ------------------------------------------------------------ parser

    @staticmethod
    def _names(item: dict, plural_s: bool = False) -> set[str]:
        names = {item.get("name"), item.get("pluralName"), item.get("abbreviation")}
        names |= {a.get("name") for a in item.get("aliases") or []}
        names = {n.lower() for n in names if n}
        if plural_s:
            names |= {f"{n}s" for n in names}
        return names

    def _parse_line(self, line: str) -> dict:
        m = re.match(r"^\s*(\d+(?:\.\d+)?|\d+/\d+)?\s*(.*)$", line)
        qty_text, rest = m.group(1), m.group(2)
        quantity = 0.0
        if qty_text:
            if "/" in qty_text:
                a, b = qty_text.split("/")
                quantity = int(a) / int(b)
            else:
                quantity = float(qty_text)
        note = ""
        if "," in rest:
            rest, note = (p.strip() for p in rest.split(",", 1))
        words = rest.split()
        unit = None
        if quantity and words:
            candidate = words[0].lower()
            match = next((u for u in self.units.values() if candidate in self._names(u, plural_s=True)), None)
            if match:
                unit, words = copy.deepcopy(match), words[1:]
            elif candidate in {"pinch", "pinches", "handful", "sprig"}:
                unit, words = {"id": None, "name": candidate.rstrip("es") if candidate.endswith("es") else candidate}, words[1:]
            if words and words[0] == "of":
                words = words[1:]
        food_name = " ".join(words)
        food_match = next((f for f in self.foods.values() if food_name.lower() in self._names(f)), None)
        food = copy.deepcopy(food_match) if food_match else ({"id": None, "name": food_name} if food_name else None)
        return {
            "input": line,
            "confidence": {"average": 0.9},
            "ingredient": {"quantity": quantity, "unit": unit, "food": food, "note": note, "display": ""},
        }

    # ------------------------------------------------------------ transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path, params = url.path, parse_qs(url.query)
        body = json.loads(request.content) if request.content else None
        method = request.method
        self.calls.append((method, path, body if body is not None else params))
        if (method, path) in self.fail_paths:
            return _err(self.fail_paths[(method, path)], "injected failure")
        try:
            return self._route(method, path, params, body)
        except ValueError as e:
            status, _, msg = str(e).partition(":")
            return _err(int(status), msg)

    def _route(self, method: str, path: str, params: dict, body: Any) -> httpx.Response:
        if path == "/api/parser/ingredients" and method == "POST":
            if self.parser_status:
                return _err(self.parser_status, "parser exploded")
            if callable(self.parser_response):
                return httpx.Response(200, json=self.parser_response(body["ingredients"]))
            if self.parser_response is not None:
                return httpx.Response(200, json=self.parser_response)
            return httpx.Response(200, json=[self._parse_line(line) for line in body["ingredients"]])

        m = re.fullmatch(r"/api/(foods|units)(?:/([^/]+))?", path)
        if m:
            return self._taxonomy(method, m.group(1), m.group(2), params, body)

        if path == "/api/recipes" and method == "POST":
            recipe = self.add_recipe(body["name"])
            return httpx.Response(201, json=recipe["slug"])
        if path == "/api/app/about" and method == "GET":
            return httpx.Response(200, json={"version": "fake"})
        if path == "/api/recipes/create/url" and method == "POST":
            recipe = self.add_recipe(f"Imported {len(self.recipes) + 1}", orgURL=body["url"])
            return httpx.Response(201, json=recipe["slug"])
        if path in ("/api/organizers/tags", "/api/organizers/categories") and method == "GET":
            items = self.tags if path.endswith("tags") else self.categories
            return httpx.Response(200, json=_paginate(items, params))
        if path == "/api/recipes" and method == "GET":
            items = list(self.recipes.values())
            if params.get("search"):
                needle = params["search"][0].lower()
                items = [r for r in items if needle in r["name"].lower()]
            for key, field in (("tags", "tags"), ("categories", "recipeCategory")):
                if params.get(key):
                    items = [r for r in items if set(params[key]) <= {t["slug"] for t in r[field]}]
            qf = params.get("queryFilter", [""])[0]
            fm = re.fullmatch(r'recipeIngredient\.(food|unit)\.id = "([^"]+)"', qf)
            if qf and not fm:
                return _err(400, f"unsupported queryFilter in fake: {qf}")
            if fm:
                kind, ref_id = fm.groups()
                items = [
                    r for r in items
                    if any((i.get(kind) or {}).get("id") == ref_id for i in r["recipeIngredient"])
                ]
            return httpx.Response(200, json=_paginate([self._recipe_out(r) for r in items], params))
        m = re.fullmatch(r"/api/recipes/([^/]+)", path)
        if m:
            recipe = self.recipes.get(m.group(1))
            if recipe is None:
                return _err(404, "Recipe not found.")
            if method == "GET":
                return httpx.Response(200, json=self._recipe_out(recipe))
            if method == "DELETE":
                del self.recipes[recipe["slug"]]
                return httpx.Response(200, json=self._recipe_out(recipe))
            if method == "PATCH":
                updates = dict(body)
                if "recipeIngredient" in updates:
                    updates["recipeIngredient"] = [self._store_ingredient(i) for i in updates["recipeIngredient"]]
                if "recipeInstructions" in updates:
                    updates["recipeInstructions"] = [self._store_step(st) for st in updates["recipeInstructions"]]
                if "nutrition" in updates:
                    # Nutrition model: all fields optional strings; the object is replaced wholesale
                    nutrition = dict.fromkeys(recipe["nutrition"])
                    for k, v in (updates["nutrition"] or {}).items():
                        if v is not None and not isinstance(v, str | int | float):
                            raise ValueError(f"422:nutrition.{k} must be a string")
                        nutrition[k] = None if v is None else str(v)
                    updates["nutrition"] = nutrition
                recipe.update(updates)
                return httpx.Response(200, json=self._recipe_out(recipe))

        response = self._shopping_and_plans(method, path, params, body)
        if response is not None:
            return response

        if path == "/api/households/shopping/items" and method == "GET":
            qf = params.get("queryFilter", [""])[0]
            fm = re.fullmatch(r'(food|unit)Id = "([^"]+)"', qf)
            items = list(self.shopping_items.values())
            if fm:
                items = [i for i in items if i[f"{fm.group(1)}Id"] == fm.group(2)]
            return httpx.Response(200, json=_paginate(items, params))

        return _err(404, f"fake has no route for {method} {path}")

    def _list_out(self, shopping_list: dict) -> dict:
        items = [i for i in self.shopping_items.values() if i.get("shoppingListId") == shopping_list["id"]]
        return {**shopping_list, "listItems": copy.deepcopy(items)}

    def _shopping_and_plans(self, method: str, path: str, params: dict, body: Any) -> httpx.Response | None:
        if path == "/api/households/shopping/lists" and method == "GET":
            return httpx.Response(200, json=_paginate(list(self.shopping_lists.values()), params))
        m = re.fullmatch(r"/api/households/shopping/lists/([^/]+)(/recipe(?:/([^/]+))?)?", path)
        if m:
            shopping_list = self.shopping_lists.get(m.group(1))
            if shopping_list is None:
                return _err(404, "Not found.")
            if m.group(2) is None and method == "GET":
                return httpx.Response(200, json=self._list_out(shopping_list))
            if m.group(2) and method == "POST":
                if m.group(3) is None:
                    if self.legacy_shopping:
                        return _err(404, "Not Found")
                    requests = [(r["recipeId"], r.get("recipeIncrementQuantity", 1)) for r in body]
                else:
                    requests = [(m.group(3), body.get("recipeIncrementQuantity", 1))]
                for recipe_id, scale in requests:
                    recipe = next((r for r in self.recipes.values() if r["id"] == recipe_id), None)
                    if recipe is None:
                        return _err(404, "recipe not found")
                    for ing in recipe["recipeIngredient"]:
                        item = self.add_shopping_item(food=ing.get("food"), unit=ing.get("unit"), note=ing.get("note") or "", list_id=shopping_list["id"])
                        item["quantity"] = (ing.get("quantity") or 1) * scale
                return httpx.Response(200, json=self._list_out(shopping_list))
        if path == "/api/households/shopping/items/create-bulk" and method == "POST":
            created = []
            for entry in body:
                item = self.add_shopping_item(note=entry["note"], list_id=entry["shoppingListId"])
                created.append(copy.deepcopy(item))
            return httpx.Response(201, json={"createdItems": created, "updatedItems": [], "deletedItems": []})
        m = re.fullmatch(r"/api/households/shopping/items/([^/]+)", path)
        if m:
            item = self.shopping_items.get(m.group(1))
            if item is None:
                return _err(404, "Not found.")
            if method == "GET":
                return httpx.Response(200, json=copy.deepcopy(item))
            if method == "PUT":
                item.update({k: v for k, v in body.items() if k != "id"})
                return httpx.Response(200, json=copy.deepcopy(item))
            if method == "DELETE":
                del self.shopping_items[item["id"]]
                return httpx.Response(200, json=copy.deepcopy(item))
        if path == "/api/households/mealplans":
            if method == "GET":
                start, end = params["start_date"][0], params["end_date"][0]
                items = sorted((e for e in self.mealplans.values() if start <= e["date"] <= end), key=lambda e: e["date"])
                return httpx.Response(200, json=_paginate(items, params))
            if method == "POST":
                recipe = next((r for r in self.recipes.values() if r["id"] == body.get("recipeId")), None)
                if body.get("recipeId") and recipe is None:
                    return _err(404, "recipe not found")
                entry = {
                    "id": str(uuid.uuid4()), **body,
                    "recipe": {"name": recipe["name"], "slug": recipe["slug"]} if recipe else None,
                }
                self.mealplans[entry["id"]] = entry
                return httpx.Response(201, json=entry)
        m = re.fullmatch(r"/api/households/mealplans/([^/]+)", path)
        if m and method == "DELETE":
            if self.mealplans.pop(m.group(1), None) is None:
                return _err(404, "Not found.")
            return httpx.Response(200, json={})
        return None

    def _taxonomy(self, method: str, kind: str, item_id: str | None, params: dict, body: Any) -> httpx.Response:
        table = self.foods if kind == "foods" else self.units
        label = kind[:-1]

        if item_id is None:
            if method == "GET":
                items = sorted(table.values(), key=lambda i: i["name"].lower())
                if params.get("search"):
                    needle = params["search"][0].lower()
                    items = [i for i in items if any(needle in n for n in self._names(i))]
                return httpx.Response(200, json=_paginate(copy.deepcopy(items), params))
            if method == "POST":
                if not body.get("name"):
                    return _err(422, "name: field required")
                if not self._extras_ok(body.get("extras")):
                    return _err(400, "Error binding parameter: unsupported type")
                if any(i["name"] == body["name"] for i in table.values()):
                    return _err(409, "This item already exists.")
                fields = {k: v for k, v in body.items() if k != "name"}
                item = (self.add_food if kind == "foods" else self.add_unit)(body["name"], **fields)
                return httpx.Response(201, json=copy.deepcopy(item))

        if item_id == "merge" and method == "PUT":
            from_key, to_key = ("fromFood", "toFood") if kind == "foods" else ("fromUnit", "toUnit")
            src, dst = body.get(from_key), body.get(to_key)
            if src not in table or dst not in table:
                return _err(500, f"Failed to merge {kind}")
            for recipe in self.recipes.values():
                for ing in recipe["recipeIngredient"]:
                    if (ing.get(label) or {}).get("id") == src:
                        ing[label] = copy.deepcopy(table[dst])
            for item in self.shopping_items.values():
                if item[f"{label}Id"] == src:
                    item[f"{label}Id"] = dst
            del table[src]
            return httpx.Response(200, json={"message": f"Successfully merged {kind}", "error": False})

        if not re.fullmatch(r"[0-9a-f-]{36}", item_id):
            return _err(422, "item_id: value is not a valid uuid")
        item = table.get(item_id)
        if item is None:
            return _err(404, "Not found.")

        if method == "GET":
            return httpx.Response(200, json=copy.deepcopy(item))
        if method == "PUT":
            if not body.get("name"):
                return _err(422, "name: field required")
            if not self._extras_ok(body.get("extras")):
                return _err(400, "Error binding parameter: unsupported type")
            if any(i["name"] == body["name"] and i["id"] != item_id for i in table.values()):
                return _err(409, "This item already exists.")
            updated = {**item, **{k: v for k, v in body.items() if k in item}, "id": item_id}
            if kind == "units":
                self._normalize_standard(updated)
            table[item_id] = updated
            return httpx.Response(200, json=copy.deepcopy(updated))
        if method == "DELETE":
            if self.postgres and any(s[f"{label}Id"] == item_id for s in self.shopping_items.values()):
                return _err(409, "integrity error: foreign key violation")
            for recipe in self.recipes.values():
                for ing in recipe["recipeIngredient"]:
                    if (ing.get(label) or {}).get("id") == item_id:
                        ing[label] = None  # ORM nulls the FK; no error
            del table[item_id]
            return httpx.Response(200, json=copy.deepcopy(item))
        return _err(405, "Method Not Allowed")
