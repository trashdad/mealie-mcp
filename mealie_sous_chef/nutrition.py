"""Per-food nutrition storage and per-recipe nutrition rollup.

Mealie has no per-ingredient nutrition: a recipe's `nutrition` is a static,
manually-entered block. This module adds it. Per-100g figures (and optional
weight hints) are stored on each Food's `extras` -- Mealie's generic key/value
store -- and summed across a recipe's linked ingredients.

Storage format on a food's `extras` (Mealie persists extras values as plain
strings, so structured values are JSON-encoded):

    nutrition_per_100g   JSON object, keys from NUTRIENTS, e.g. {"calories": 130, "protein_g": 2.7}
    nutrition_source     "usda" | "off" | "manual"
    nutrition_source_id  USDA fdcId / Open Food Facts barcode, when known
    grams_per_ml         density, used for volume units (flour ~0.53, oil ~0.92); water (1.0) assumed if unset
    portion_grams        JSON object of unit name -> grams for one of that unit, e.g.
                         {"each": 50, "clove": 5}; "each" is used when an ingredient has no unit ("2 eggs")
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

# Our nutrient key -> the matching field on Mealie's recipe `nutrition` block.
# Mealie labels calories in kcal, sodium and cholesterol in mg, everything else in g.
NUTRIENTS: dict[str, str] = {
    "calories": "calories",
    "protein_g": "proteinContent",
    "fat_g": "fatContent",
    "saturated_fat_g": "saturatedFatContent",
    "trans_fat_g": "transFatContent",
    "carbs_g": "carbohydrateContent",
    "fiber_g": "fiberContent",
    "sugar_g": "sugarContent",
    "sodium_mg": "sodiumContent",
    "cholesterol_mg": "cholesterolContent",
}

# Physical ceilings per 100g, to catch per-serving or per-package figures passed by mistake.
# (Pure fat is ~900 kcal/100g; a gram-denominated nutrient can't exceed 100g in 100g.)
_MAX_PER_100G = {key: (100_000.0 if key.endswith("_mg") else 100.0) for key in NUTRIENTS}
_MAX_PER_100G["calories"] = 950.0

EXTRAS_NUTRITION = "nutrition_per_100g"
EXTRAS_SOURCE = "nutrition_source"
EXTRAS_SOURCE_ID = "nutrition_source_id"
EXTRAS_DENSITY = "grams_per_ml"
EXTRAS_PORTIONS = "portion_grams"
PORTION_EACH = "each"

# Mass units -> grams (exact).
MASS_GRAMS: dict[str, float] = {
    "gram": 1.0, "g": 1.0,
    "kilogram": 1000.0, "kg": 1000.0,
    "milligram": 0.001, "mg": 0.001,
    "ounce": 28.349523125, "oz": 28.349523125,
    "pound": 453.59237, "lb": 453.59237, "lbs": 453.59237,
}

# Volume units -> millilitres (exact, US customary -- matches the pint definitions
# Mealie itself uses for a unit's standardUnit). Grams then need a density.
VOLUME_ML: dict[str, float] = {
    "milliliter": 1.0, "millilitre": 1.0, "ml": 1.0,
    "centiliter": 10.0, "cl": 10.0,
    "deciliter": 100.0, "dl": 100.0,
    "liter": 1000.0, "litre": 1000.0, "l": 1000.0,
    "teaspoon": 4.92892159375, "tsp": 4.92892159375,
    "tablespoon": 14.78676478125, "tbsp": 14.78676478125, "tbs": 14.78676478125,
    "fluid_ounce": 29.5735295625, "fl_oz": 29.5735295625, "floz": 29.5735295625,
    "cup": 236.5882365,
    "pint": 473.176473, "pt": 473.176473,
    "quart": 946.352946, "qt": 946.352946,
    "gallon": 3785.411784, "gal": 3785.411784,
}

WATER_GRAMS_PER_ML = 1.0


def normalize_unit_name(name: str | None) -> str:
    """'Fluid Ounces.' -> 'fluid_ounce'; '' for None."""
    s = re.sub(r"[\s\-]+", "_", (name or "").strip().lower().rstrip("."))
    return s


def _lookup_unit_table(name: str | None, table: dict[str, float]) -> float | None:
    key = normalize_unit_name(name)
    if not key:
        return None
    if key in table:
        return table[key]
    # plural forms: cups, ounces, tablespoons, liters
    if key.endswith("es") and key[:-2] in table:
        return table[key[:-2]]
    if key.endswith("s") and key[:-1] in table:
        return table[key[:-1]]
    return None


def is_known_standard_unit(name: str | None) -> bool:
    return _lookup_unit_table(name, MASS_GRAMS) is not None or _lookup_unit_table(name, VOLUME_ML) is not None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _decode_json_object(raw: Any) -> tuple[dict[str, Any], bool]:
    """Returns (object, was_malformed). Accepts a JSON string or an already-decoded dict."""
    if raw is None or raw == "":
        return {}, False
    if isinstance(raw, dict):
        return raw, False
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}, True
        return (decoded, False) if isinstance(decoded, dict) else ({}, True)
    return {}, True


# ---------------------------------------------------------------- per-food profile


@dataclass
class FoodProfile:
    per_100g: dict[str, float] = field(default_factory=dict)
    grams_per_ml: float | None = None
    portion_grams: dict[str, float] = field(default_factory=dict)
    source: str | None = None
    source_id: str | None = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.per_100g:
            out["nutrition_per_100g"] = self.per_100g
            out["nutrition_source"] = self.source
            if self.source_id:
                out["nutrition_source_id"] = self.source_id
        if self.grams_per_ml is not None:
            out["grams_per_ml"] = self.grams_per_ml
        if self.portion_grams:
            out["portion_grams"] = self.portion_grams
        if self.problems:
            out["nutrition_data_problems"] = self.problems
        return out


def read_food_profile(food: dict | None) -> FoodProfile:
    """Decode the nutrition data stored on a food's extras. Tolerates missing,
    partial and malformed data -- anything unusable is dropped and described in
    `problems` rather than raising, since one bad food shouldn't sink a rollup."""
    extras = (food or {}).get("extras") or {}
    profile = FoodProfile(source=extras.get(EXTRAS_SOURCE), source_id=extras.get(EXTRAS_SOURCE_ID))

    raw, malformed = _decode_json_object(extras.get(EXTRAS_NUTRITION))
    if malformed:
        profile.problems.append(f"{EXTRAS_NUTRITION} is not a valid JSON object")
    for key, value in raw.items():
        num = _as_number(value)
        if key not in NUTRIENTS:
            profile.problems.append(f"ignored unknown nutrient key {key!r}")
        elif num is None or num < 0:
            profile.problems.append(f"ignored non-numeric/negative value for {key}: {value!r}")
        else:
            profile.per_100g[key] = num

    if extras.get(EXTRAS_DENSITY) not in (None, ""):
        density = _as_number(extras.get(EXTRAS_DENSITY))
        if density is None or density <= 0:
            profile.problems.append(f"ignored invalid {EXTRAS_DENSITY}: {extras.get(EXTRAS_DENSITY)!r}")
        else:
            profile.grams_per_ml = density

    portions, malformed = _decode_json_object(extras.get(EXTRAS_PORTIONS))
    if malformed:
        profile.problems.append(f"{EXTRAS_PORTIONS} is not a valid JSON object")
    for unit_name, grams in portions.items():
        num = _as_number(grams)
        if num is None or num <= 0:
            profile.problems.append(f"ignored invalid portion weight for {unit_name!r}: {grams!r}")
        else:
            profile.portion_grams[normalize_unit_name(unit_name)] = num
    return profile


def validate_per_100g(per_100g: dict[str, Any]) -> dict[str, float]:
    """Strict validation for values being written. Raises ValueError with a
    message meant for the model (wrong key names, per-serving numbers, etc.)."""
    clean: dict[str, float] = {}
    unknown = [k for k in per_100g if k not in NUTRIENTS]
    if unknown:
        raise ValueError(f"unknown nutrient key(s) {unknown}; valid keys are {list(NUTRIENTS)}")
    for key, value in per_100g.items():
        if value is None:
            continue
        num = _as_number(value)
        if num is None or num < 0:
            raise ValueError(f"{key} must be a non-negative number, got {value!r}")
        if num > _MAX_PER_100G[key]:
            raise ValueError(
                f"{key}={num} is impossible per 100g -- is this a per-serving or per-package figure? "
                "Convert it to per 100g first."
            )
        clean[key] = num
    if not clean:
        raise ValueError("per_100g has no values")
    return clean


def encode_food_extras(
    extras: dict[str, Any] | None,
    *,
    per_100g: dict[str, float] | None = None,
    source: str | None = None,
    source_id: str | None = None,
    grams_per_ml: float | None = None,
    portion_grams: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return a copy of a food's extras with nutrition fields updated, every value
    a string (Mealie's extras table stores strings; a nested object fails to save).
    Unrelated extras keys are preserved."""
    out = dict(extras or {})
    if per_100g is not None:
        out[EXTRAS_NUTRITION] = json.dumps(per_100g, sort_keys=True)
        out[EXTRAS_SOURCE] = source or "manual"
        if source_id:
            out[EXTRAS_SOURCE_ID] = str(source_id)
        else:
            out.pop(EXTRAS_SOURCE_ID, None)
    if grams_per_ml is not None:
        if grams_per_ml <= 0:
            out.pop(EXTRAS_DENSITY, None)
        else:
            out[EXTRAS_DENSITY] = repr(float(grams_per_ml))
    if portion_grams is not None:
        merged, _ = _decode_json_object(out.get(EXTRAS_PORTIONS))
        for unit_name, grams in portion_grams.items():
            key = normalize_unit_name(unit_name)
            if not key:
                raise ValueError("portion_grams keys must be unit names (use 'each' for unitless ingredients)")
            num = _as_number(grams)
            if num is None or num < 0:
                raise ValueError(f"portion_grams[{unit_name!r}] must be a non-negative number (0 removes it)")
            if num == 0:
                merged.pop(key, None)
            else:
                merged[key] = num
        if merged:
            out[EXTRAS_PORTIONS] = json.dumps(merged, sort_keys=True)
        else:
            out.pop(EXTRAS_PORTIONS, None)
    return out


# ---------------------------------------------------------------- unit conversion


@dataclass
class Conversion:
    grams: float | None
    approximate: bool = False
    note: str = ""  # how it was converted, or why it couldn't be


def _unit_names(unit: dict) -> list[str]:
    names = [unit.get("name"), unit.get("pluralName"), unit.get("abbreviation"), unit.get("pluralAbbreviation")]
    names += [a.get("name") for a in unit.get("aliases") or [] if isinstance(a, dict)]
    return [n for n in names if n]


def _portion_sources(profile: FoodProfile, food_name: str | None, note: str) -> list[tuple[dict[str, float], float | None, str]]:
    """Where cup/spoon/piece weights can come from, most specific first."""
    from .household_measures import match_reference

    sources = []
    if profile.portion_grams or profile.grams_per_ml is not None:
        sources.append((profile.portion_grams, profile.grams_per_ml, "this food's measures"))
    reference = match_reference(food_name, note)
    if reference:
        sources.append(
            (
                reference.get("portion_grams") or {},
                reference.get("grams_per_ml"),
                f"USDA household measure for {reference['usda_description']!r} (FDC {reference['usda_fdc_id']})",
            )
        )
    return sources


def quantity_to_grams(
    quantity: float, unit: dict | None, profile: FoodProfile, note: str = "", food_name: str | None = None
) -> Conversion:
    """Convert an ingredient amount to grams, most specific source first:
    1. cup/spoon/piece weights -- this food's own (portion_grams, e.g. from its USDA
       record), then USDA's household measures for a matching common ingredient.
       A prep word in the note picks a variant ("packed" brown sugar, "chopped"
       onion); a size word picks a piece ("large" egg).
    2. mass units (exact), from the unit's Mealie standard or its name
    3. volume units, via density from the same sources as 1; water only as a last
       resort (flagged approximate)."""
    from .household_measures import portion_for_piece, portion_for_unit

    sources = _portion_sources(profile, food_name, note)
    if not unit:
        for portions, _, label in sources:
            hit = portion_for_piece(portions, note)
            if hit:
                key, grams = hit
                return Conversion(quantity * grams, False, f"1 {key.replace('_', ' ')} = {grams:g} g ({label})")
        return Conversion(
            None,
            note="no unit and no known weight for one of this item: set portion_grams['each'] on this food",
        )

    names = _unit_names(unit)
    ml, how = None, ""
    std_qty, std_unit = _as_number(unit.get("standardQuantity")), unit.get("standardUnit")
    if std_qty and std_qty > 0 and std_unit:
        mass = _lookup_unit_table(std_unit, MASS_GRAMS)
        if mass is not None:
            return Conversion(quantity * std_qty * mass, False, f"unit standard {std_qty:g} {std_unit}")
        vol = _lookup_unit_table(std_unit, VOLUME_ML)
        if vol is not None:
            ml, how = quantity * std_qty * vol, f"unit standard {std_qty:g} {std_unit}"
    if ml is None:
        for name in names:
            mass = _lookup_unit_table(name, MASS_GRAMS)
            if mass is not None:
                return Conversion(quantity * mass, False, f"'{name}' as a mass unit")
            vol = _lookup_unit_table(name, VOLUME_ML)
            if vol is not None:
                ml, how = quantity * vol, f"'{name}' as a volume unit"
                break

    # Food-specific data beats the generic reference: this food's weight for the
    # unit, then its density, then the same two from the USDA reference table.
    for portions, density, label in sources:
        hit = portion_for_unit(portions, names, note)
        if hit:
            key, grams = hit
            return Conversion(quantity * grams, False, f"1 {key.replace('_', ' ')} = {grams:g} g ({label})")
        if ml is not None and density:
            return Conversion(ml * density, False, f"{how}, {density:.3g} g/ml ({label})")

    if ml is not None:
        return Conversion(
            ml * WATER_GRAMS_PER_ML,
            True,
            f"{how}, assumed water density -- no measures known for this food (set grams_per_ml on it)",
        )

    label = unit.get("name") or "?"
    return Conversion(
        None,
        note=(
            f"unit {label!r} has no known weight for this food: set the unit's standardQuantity/standardUnit, "
            f"or portion_grams[{normalize_unit_name(label)!r}] on this food"
        ),
    )


# ---------------------------------------------------------------- recipe rollup


def _ingredient_label(ing: dict) -> str:
    food = ing.get("food") or {}
    text = ing.get("display") or ing.get("originalText") or ing.get("note") or food.get("name")
    return (text or "(unnamed ingredient)").strip()


def _round_nutrient(key: str, value: float) -> float:
    return float(round(value)) if key == "calories" else round(value, 1)


def estimate_recipe_nutrition(ingredients: list[dict], servings: float | None) -> dict[str, Any]:
    """Sum per-100g nutrition across a recipe's linked ingredients, scaled by
    quantity/unit, divided by servings (Mealie's nutrition block is per serving).

    Honesty rules -- the result says what it doesn't know rather than guessing:
    - an ingredient that can't be weighed or has no nutrition data is listed in
      `unaccounted` and contributes nothing;
    - a nutrient no counted ingredient has data for is None (unknown), not 0;
    - a counted ingredient missing some nutrient keys is listed under
      `incomplete_nutrients` for that key;
    - volume->weight via assumed water density is listed under `approximate`;
    - food-linked ingredients with no quantity ("salt, to taste") are listed in
      `skipped`; unlinked free text is always `unaccounted`, since its amount
      lives in the text.
    """
    totals: dict[str, float] = {}
    has_data: dict[str, bool] = dict.fromkeys(NUTRIENTS, False)
    counted: list[dict[str, Any]] = []
    unaccounted: list[dict[str, str]] = []
    approximate: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    incomplete: dict[str, list[str]] = {}
    data_problems: list[dict[str, Any]] = []

    for ing in ingredients or []:
        if not isinstance(ing, dict):
            continue
        label = _ingredient_label(ing)
        food = ing.get("food")
        if ing.get("referencedRecipe"):
            unaccounted.append({"ingredient": label, "reason": "references another recipe (sub-recipes aren't rolled up)"})
            continue
        quantity = _as_number(ing.get("quantity")) or 0.0
        linked = isinstance(food, dict) and bool(food.get("id"))
        if not linked:
            if quantity <= 0 and not (ing.get("note") or ing.get("display") or ing.get("originalText")):
                skipped.append({"ingredient": label, "reason": "empty ingredient row"})
            else:
                # Unparsed free text keeps its amount inside the note ("1 pinch of saffron"),
                # so a zero quantity here doesn't mean "negligible".
                unaccounted.append({"ingredient": label, "reason": "not linked to a food"})
            continue
        if quantity <= 0:
            skipped.append({"ingredient": label, "reason": "no quantity"})
            continue

        profile = read_food_profile(food)
        if profile.problems:
            data_problems.append({"food": food.get("name"), "problems": profile.problems})
        if not profile.per_100g:
            unaccounted.append({"ingredient": label, "reason": f"food {food.get('name')!r} has no nutrition data"})
            continue

        conv = quantity_to_grams(quantity, ing.get("unit"), profile, ing.get("note") or "", food.get("name"))
        if conv.grams is None:
            unaccounted.append({"ingredient": label, "reason": conv.note})
            continue
        if conv.approximate:
            approximate.append({"ingredient": label, "reason": conv.note})

        factor = conv.grams / 100.0
        for key in NUTRIENTS:
            if key in profile.per_100g:
                totals[key] = totals.get(key, 0.0) + profile.per_100g[key] * factor
                has_data[key] = True
            else:
                incomplete.setdefault(key, []).append(food.get("name") or label)
        counted.append({"ingredient": label, "grams": round(conv.grams, 1), "conversion": conv.note})

    servings_used = servings if servings and servings > 0 else 1.0
    total = {k: (_round_nutrient(k, totals[k]) if has_data[k] else None) for k in NUTRIENTS}
    per_serving = {k: (_round_nutrient(k, totals[k] / servings_used) if has_data[k] else None) for k in NUTRIENTS}
    # A key nobody had is already None; only report partial coverage for keys we do have.
    incomplete = {k: v for k, v in incomplete.items() if has_data[k]}

    result: dict[str, Any] = {
        "per_serving": per_serving,
        "total": total,
        "servings_used": servings_used,
        "counted": counted,
        "unaccounted": unaccounted,
        "approximate": approximate,
        "incomplete_nutrients": incomplete,
        "skipped": skipped,
        # Every ingredient with a quantity was weighed and had data. Missing individual
        # nutrient keys (common: USDA rarely lists trans fat) don't make it incomplete.
        "complete": not unaccounted,
    }
    if not servings or servings <= 0:
        result["warnings"] = ["recipe has no servings set; totals are for the whole recipe (servings_used=1)"]
    if data_problems:
        result["data_problems"] = data_problems
    return result


def nutrition_patch(per_serving: dict[str, float | None], existing: dict[str, Any] | None) -> tuple[dict, list[str]]:
    """Build Mealie's recipe `nutrition` block from computed per-serving values.
    Fields we computed are overwritten; fields we have no data for keep their
    existing value (returned as the second element) rather than being zeroed."""
    patch = dict(existing or {})
    left_unchanged = []
    for key, mealie_field in NUTRIENTS.items():
        value = per_serving.get(key)
        if value is None:
            left_unchanged.append(key)
            continue
        patch[mealie_field] = str(int(value)) if key == "calories" else f"{value:g}"
    return patch, left_unchanged
