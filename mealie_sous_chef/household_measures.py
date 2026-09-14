"""Household measures: how much a cup, spoon or piece of a food weighs.

Source: USDA FoodData Central. Every SR Legacy / FNDDS food carries USDA's own
household measures ("1 cup, sifted = 115 g", "1 large = 50 g", "1 clove = 3 g").
They're typical values -- real kitchens vary by roughly 10-20% (how flour is
scooped, how an onion is chopped) -- which is the accepted margin for recipe
nutrition estimates.

Two ways they're used:
- a food whose nutrition came from USDA gets that exact food's measures stored on
  it (set_food_nutrition fetches them);
- any other food falls back to data/household_measures.json, a table of common
  ingredients generated from the same USDA data (scripts/build_household_measures.py),
  matched by food name.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .ingredients import _singular, fold, words

CUP_ML, TABLESPOON_ML, TEASPOON_ML, FLUID_OUNCE_ML = 236.5882365, 14.78676478125, 4.92892159375, 29.5735295625
_VOLUME_KEYS = {"cup": CUP_ML, "tablespoon": TABLESPOON_ML, "teaspoon": TEASPOON_ML, "fluid_ounce": FLUID_OUNCE_ML}
_UNIT_ALIASES = {
    "c": "cup", "cups": "cup",
    "tbsp": "tablespoon", "tbs": "tablespoon", "tablespoons": "tablespoon", "tbl": "tablespoon",
    "tsp": "teaspoon", "teaspoons": "teaspoon",
    "fl_oz": "fluid_ounce", "floz": "fluid_ounce", "fluid_ounces": "fluid_ounce",
}
SIZE_WORDS = ["extra_large", "extra_small", "jumbo", "large", "medium", "small"]
_EACH_PREFERENCE = ["medium", "large", "whole", "each", "item", "fruit", "small", "extra_large", "jumbo"]
# whole-item measures USDA uses when it lists no sizes; slices, rings, sprigs etc. are not a whole item
_WHOLE_ITEM_UNITS = ["fruit", "whole", "item", "piece", "head", "bulb", "stalk", "ear", "breast", "fillet"]
_SKIP_UNITS = {"oz", "ounce", "lb", "pound", "g", "gram", "kg", "serving", "racc", "nlea", "quantity", "package", "container"}
_FILLER = {"or", "and", "about", "approx", "yield", "after", "cooking", "with", "without", "of", "the", "a"}


def canonical_unit(name: str | None) -> str:
    key = re.sub(r"[\s\-]+", "_", (name or "").strip().lower().rstrip("."))
    key = _UNIT_ALIASES.get(key, key)
    if key.endswith("s") and len(key) > 3 and _UNIT_ALIASES.get(key[:-1], key[:-1]) in _VOLUME_KEYS:
        key = _UNIT_ALIASES.get(key[:-1], key[:-1])
    return key


def _leading_amount(text: str) -> tuple[float | None, str]:
    """'1-1/2 cup' -> (1.5, 'cup'); '1/2 cup' -> (0.5, 'cup'); 'cup' -> (None, 'cup')."""
    m = re.match(r"^\s*(\d+)(?:[\s\-]+(\d+)/(\d+))?(?:/(\d+))?\s*(.*)$", text)
    if not m:
        return None, text
    whole, num, den, simple_den, rest = m.groups()
    if simple_den:
        return int(whole) / int(simple_den), rest
    amount = float(whole)
    if num and den:
        amount += int(num) / int(den)
    return amount, rest


def parse_usda_portions(portions: list[dict[str, Any]], food_description: str | None = None) -> dict[str, Any]:
    """USDA `foodPortions` -> {"portion_grams": {key: grams per 1}, "grams_per_ml": float | None}.

    Keys: "cup", "tablespoon", "teaspoon", sizes ("large"), pieces ("clove", "slice"),
    "each" (the typical whole item), and prep variants like "cup_chopped" or
    "cup_packed" when USDA lists them. `food_description` lets a measure named after
    the food itself ("1 potato", "1 pepper") count as one whole item, and makes eggs
    default to large (the size recipes assume)."""
    grams: dict[str, float] = {}
    items: list[str] = []
    for p in portions or []:
        weight = p.get("gramWeight")
        if not isinstance(weight, int | float) or weight <= 0:
            continue
        unit_name = ((p.get("measureUnit") or {}).get("name") or "").strip()
        modifier = (p.get("modifier") or "").strip()
        if unit_name and unit_name.lower() not in ("undetermined", "racc"):
            text = f"{unit_name} {modifier}" if modifier and not modifier.isdigit() and modifier.lower() != "edible" else unit_name
            from_measure_unit = True
        elif modifier and not modifier.isdigit():
            text, from_measure_unit = modifier, False
        else:
            text, from_measure_unit = (p.get("portionDescription") or ""), False
        text = re.sub(r"\([^)]*\)", " ", text).strip()
        amount, text = _leading_amount(text)
        amount = p.get("amount") if isinstance(p.get("amount"), int | float) and p.get("amount") else (amount or 1.0)
        if not text or re.search(r"\bor\b|not specified|serving", text, re.I):
            continue
        head, _, tail = text.partition(",")
        head_words = re.findall(r"[a-z]+", head.lower())
        if not head_words:
            continue
        if head_words[:2] in (["extra", "large"], ["extra", "small"]):
            unit, rest = f"extra_{head_words[1]}", head_words[2:]
        elif head_words[:2] == ["fl", "oz"]:
            unit, rest = "fluid_ounce", head_words[2:]
        else:
            unit, rest = canonical_unit(_singular(head_words[0])), head_words[1:]
        if unit in _SKIP_UNITS:
            continue
        qualifier = [w for w in rest + re.findall(r"[a-z]+", tail.lower()) if w not in _FILLER and w not in SIZE_WORDS]
        per_one = round(float(weight) / float(amount), 3)
        grams.setdefault(unit, per_one)
        if qualifier:
            grams.setdefault(f"{unit}_{'_'.join(qualifier)}", per_one)
        if from_measure_unit and unit not in _VOLUME_KEYS and unit not in SIZE_WORDS:
            items.append(unit)  # Foundation foods name the item itself ("Onion")

    food_words = words(food_description)
    preference = list(_EACH_PREFERENCE)
    if "egg" in food_words:
        preference.remove("large")
        preference.insert(0, "large")
    each = next((grams[k] for k in preference if k in grams), None)
    if each is None:
        pick = whole_item_key(grams, food_description) or next(iter(items), None)
        each = grams[pick] if pick else None
    if each is not None:
        grams.setdefault("each", each)

    density = next((grams[k] / ml for k, ml in _VOLUME_KEYS.items() if k in grams), None)
    return {"portion_grams": grams, "grams_per_ml": round(density, 4) if density else None}


def whole_item_key(grams: dict[str, float], food_description: str | None) -> str | None:
    """A measure that means one whole item: 'fruit', 'stalk', or one named after the
    food itself ('potato' for potatoes, 'sweetpotato' for sweet potatoes)."""
    whole = [k for k in _WHOLE_ITEM_UNITS if k in grams]
    if whole:
        return whole[0]
    ordered = [_singular(w) for w in re.findall(r"[a-z]+", fold(food_description))]
    joined = "".join(ordered)
    for k in grams:
        if "_" not in k and len(k) > 2 and (k in ordered or (len(k) > 4 and k in joined)):
            return k
    return None


def portion_for_unit(portions: dict[str, float], unit_names: list[str], note: str = "") -> tuple[str, float] | None:
    """Weight of one `unit` from a portions table. A prep word in the note picks a
    matching variant ("packed" -> cup_packed); plurals and abbreviations tolerated."""
    note_words = words(note)
    for name in unit_names:
        base = canonical_unit(_singular(re.sub(r"[\s\-]+", "_", (name or "").strip().lower())))
        stems = [k for k in dict.fromkeys([base, canonical_unit(name)]) if k]
        for key in dict.fromkeys(k for stem in stems for k in (stem, f"{stem}s", f"{stem}es")):
            variants = [
                (k, v) for k, v in portions.items()
                if k.startswith(f"{key}_") and set(k[len(key) + 1 :].split("_")) <= note_words
            ]
            if variants:
                return max(variants, key=lambda kv: len(kv[0]))
            if key in portions:
                return key, portions[key]
    return None


def portion_for_piece(portions: dict[str, float], note: str = "") -> tuple[str, float] | None:
    """Weight of one whole item: a size in the note ("large") if the table has it, else 'each'."""
    note_text = "_".join(re.findall(r"[a-z]+", fold(note)))
    for size in SIZE_WORDS:
        if re.search(rf"(^|_){size}(_|$)", note_text) and size in portions:
            return size, portions[size]
    if "each" in portions:
        return "each", portions["each"]
    return None


@lru_cache(maxsize=1)
def reference_table() -> list[dict[str, Any]]:
    path = Path(__file__).parent / "data" / "household_measures.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("entries", [])
    except (OSError, json.JSONDecodeError):
        return []


def match_reference(food_name: str | None, note: str | None = None) -> dict[str, Any] | None:
    """The reference entry for a food: every word of one of the entry's names must
    appear in the food name (+ note, so 'jasmine rice' + 'cooked' finds cooked rice),
    at least one of them in the food name itself; the most specific name wins."""
    food_words = words(food_name)
    if not food_words:
        return None
    available = food_words | words(note)
    best, best_score = None, (0, False)
    for entry in reference_table():
        for name in entry.get("names", []):
            name_words = words(name)
            if not (name_words and name_words <= available and name_words & food_words):
                continue
            score = (len(name_words), name_words == food_words)  # more specific, then exact
            if score > best_score:
                best, best_score = entry, score
    return best
