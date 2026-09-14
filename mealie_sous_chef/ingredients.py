"""Turning create_recipe/update_recipe ingredient input into Mealie's recipeIngredient shape.

Input is a mix of plain strings ("2 cups flour") and structured dicts
({"quantity": 2, "food_id": "...", "unit_id": "...", "note": "sifted"}).

Things Mealie's API is strict or silent about, which this module handles:
- A linked food/unit must be sent with both `id` and `name` -- `{"id": ...}` alone
  fails request validation (422).
- A food/unit id that doesn't exist is silently dropped on save (the ingredient
  just loses its link), so structured ids are verified up front.
- A food/unit with a name but no id isn't created on the fly; it errors. Parser
  results that didn't match an existing food/unit are folded into `note` instead.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .mealie import MealieClient, MealieError

STRUCTURED_KEYS = {"quantity", "food_id", "unit_id", "note", "title", "original_text"}


def _linked(value: Any) -> dict | None:
    """The food/unit object to send if it references an existing row, else None."""
    if isinstance(value, dict) and value.get("id") and value.get("name"):
        return value
    return None


def _unresolved_name(value: Any) -> str | None:
    if isinstance(value, dict) and not value.get("id"):
        return (value.get("name") or "").strip() or None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _quantity(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        q = float(value)
    except (TypeError, ValueError):
        return 0.0
    return q if math.isfinite(q) and q > 0 else 0.0


# ---------------------------------------------------------------- parse quality

_STOPWORDS = {"and", "or", "of", "a", "an", "the", "to", "for", "plus", "about"}


def _singular(word: str) -> str:
    if len(word) <= 2:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes") or word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def fold(text: str | None) -> str:
    """Lower-case ASCII: 'Jalapeños' -> 'jalapenos', 'crème' -> 'creme'."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def words(text: str | None) -> set[str]:
    """Comparable content words: lower-case letters only (accents folded; numbers,
    fractions and punctuation dropped), singularised, stopwords removed."""
    return {_singular(w) for w in re.findall(r"[a-z]+", fold(text))} - _STOPWORDS


def _names(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return [obj] if isinstance(obj, str) else []
    names = [obj.get("name"), obj.get("pluralName"), obj.get("abbreviation"), obj.get("pluralAbbreviation")]
    names += [a.get("name") for a in obj.get("aliases") or [] if isinstance(a, dict)]
    return [n for n in names if n]


@dataclass
class ParseQuality:
    ok: bool
    dropped_words: list[str] = field(default_factory=list)  # in the line, missing from the result
    extra_words: list[str] = field(default_factory=list)  # in the matched food, missing from the line

    def reason(self) -> str:
        parts = []
        if self.dropped_words:
            parts.append(f"parser dropped {', '.join(repr(w) for w in self.dropped_words)}")
        if self.extra_words:
            parts.append(f"matched food has words not in the line: {', '.join(repr(w) for w in self.extra_words)}")
        return "; ".join(parts)


def assess_parse(line: str, ingredient: dict) -> ParseQuality:
    """Is linking this parse lossless? Catches the parser silently dropping part of
    a line ("salt and pepper" -> salt) and fuzzy matches to a food whose name says
    something the line doesn't ("chicken thighs" -> "lbs boneless ... thighs")."""
    food, unit = ingredient.get("food"), ingredient.get("unit")
    line_words = words(line)
    covered: set[str] = words(ingredient.get("note"))
    for obj in (food, unit):
        for name in _names(obj):
            covered |= words(name)
    dropped = sorted(line_words - covered)

    extra: list[str] = []
    if isinstance(food, dict) and food.get("id"):
        # any one of the food's names/aliases fully present in the line is a clean match
        candidates = [words(n) for n in _names(food) if words(n)]
        if candidates and not any(c <= line_words for c in candidates):
            extra = sorted(min((c - line_words for c in candidates), key=len))
    return ParseQuality(ok=not dropped and not extra, dropped_words=dropped, extra_words=extra)


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def build_parsed_ingredient(parsed: dict, original: str) -> dict[str, Any]:
    """One parser result (ParsedIngredient.ingredient) -> recipeIngredient payload.
    Matched food/unit are linked; unmatched ones fold into free-text `note` so
    nothing from the original line is lost."""
    food, unit = _linked(parsed.get("food")), _linked(parsed.get("unit"))
    unresolved = [n for n in (_unresolved_name(parsed.get("unit")), _unresolved_name(parsed.get("food"))) if n]
    parser_note = (parsed.get("note") or "").strip()

    payload: dict[str, Any] = {"quantity": _quantity(parsed.get("quantity"))}
    if food is None and unit is None:
        # Nothing linked: the original line reads better than a reassembled
        # fragment ("pinch saffron"), and keeping the parsed quantity would
        # print it twice ("500 500 g flour").
        payload["quantity"] = 0.0
        payload["note"] = original.strip()
    else:
        if food is not None:
            payload["food"] = food
        if unit is not None:
            payload["unit"] = unit
        payload["note"] = " ".join(p for p in [*unresolved, parser_note] if p)
    payload["originalText"] = original
    # Older Mealie versions reject a null referenceId; newer ones mint one anyway.
    payload["referenceId"] = str(uuid.uuid4())
    return payload


def free_text_ingredient(text: str) -> dict[str, Any]:
    return {"quantity": 0.0, "note": text.strip(), "originalText": text, "referenceId": str(uuid.uuid4())}


def build_structured_ingredient(item: dict, foods: dict[str, dict], units: dict[str, dict]) -> dict[str, Any]:
    payload: dict[str, Any] = {"quantity": _quantity(item.get("quantity")), "note": (item.get("note") or "").strip()}
    if item.get("food_id"):
        payload["food"] = foods[item["food_id"]]
    if item.get("unit_id"):
        payload["unit"] = units[item["unit_id"]]
    if item.get("title"):
        payload["title"] = item["title"]
    if item.get("original_text"):
        payload["originalText"] = item["original_text"]
    payload["referenceId"] = str(uuid.uuid4())
    return payload


def _validate_structured(index: int, item: dict) -> None:
    unknown = set(item) - STRUCTURED_KEYS
    if unknown:
        raise ToolError(
            f"ingredients[{index}] has unknown key(s) {sorted(unknown)}; structured ingredients accept "
            f"{sorted(STRUCTURED_KEYS)} (use food_id/unit_id from parse_ingredients or manage_taxonomy)"
        )
    q = item.get("quantity")
    if q is not None and (isinstance(q, bool) or not isinstance(q, int | float) or not math.isfinite(q) or q < 0):
        raise ToolError(f"ingredients[{index}].quantity must be a non-negative number, got {q!r}")
    for key in ("food_id", "unit_id", "note", "title", "original_text"):
        if item.get(key) is not None and not isinstance(item[key], str):
            raise ToolError(f"ingredients[{index}].{key} must be a string")
    if not (item.get("food_id") or item.get("note") or item.get("title")):
        raise ToolError(f"ingredients[{index}] needs at least a food_id, note or title")


async def fetch_by_ids(client: MealieClient, kind: str, ids: set[str]) -> dict[str, dict]:
    async def one(item_id: str) -> tuple[str, dict]:
        try:
            return item_id, await client.get(f"/api/{kind}/{item_id}")
        except MealieError as e:
            if e.status_code in (404, 422):  # 422: not even a UUID
                raise ToolError(
                    f"{kind[:-1]}_id {item_id!r} doesn't exist in Mealie. Look it up with "
                    f"manage_taxonomy(resource='{kind}', action='list', query=...) or parse_ingredients."
                ) from e
            raise

    return dict(await asyncio.gather(*(one(i) for i in sorted(ids))))


async def parse_lines(client: MealieClient, lines: list[str], parser: str = "nlp") -> list[dict | None]:
    """Run Mealie's parser. Returns one ParsedIngredient dict per line, or None for
    a line the response didn't cover properly. Raises MealieError on HTTP failure,
    ToolError if the response isn't a list at all."""
    results = await client.post("/api/parser/ingredients", {"parser": parser, "ingredients": lines})
    if not isinstance(results, list):
        raise ToolError(f"Mealie's ingredient parser returned {type(results).__name__}, expected a list")
    out: list[dict | None] = []
    for i in range(len(lines)):
        r = results[i] if i < len(results) else None
        out.append(r if isinstance(r, dict) and isinstance(r.get("ingredient"), dict) else None)
    return out


@dataclass
class IngredientsResult:
    payload: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    # lines saved as unlinked text because linking them would have lost information
    needs_review: list[dict[str, Any]] = field(default_factory=list)
    reused: int = 0  # lines matched to an existing ingredient and kept as-is


def _existing_index(existing: list[dict] | None) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for ing in existing or []:
        if not isinstance(ing, dict):
            continue
        for key in {normalize_text(ing.get("originalText")), normalize_text(ing.get("display"))} - {""}:
            index.setdefault(key, []).append(ing)
    return index


async def ingredients_payload(
    client: MealieClient, items: list[Any], existing: list[dict] | None = None
) -> IngredientsResult:
    """Build the recipeIngredient list.

    Fails loudly (ToolError) on input the caller got wrong: unknown dict keys,
    bad quantities, food/unit ids that don't exist. Degrades gracefully otherwise:
    - parser failure: affected lines are saved as unlinked free text, with a warning;
    - a parse that would lose information (assess_parse) is saved as unlinked text
      and listed in `needs_review` rather than linked to a wrong/partial food.
    With `existing` (the recipe's current ingredients), a string identical to an
    existing ingredient's original text or display keeps that ingredient untouched
    -- same referenceId (so step links survive), same link, no re-parse."""
    result = IngredientsResult(payload=[])
    for i, item in enumerate(items):
        if isinstance(item, dict):
            _validate_structured(i, item)
        elif not isinstance(item, str):
            raise ToolError(f"ingredients[{i}] must be a string or an object, got {type(item).__name__}")

    blank = sum(1 for s in items if isinstance(s, str) and not s.strip())
    if blank:
        result.warnings.append(f"dropped {blank} blank ingredient line(s)")

    index = _existing_index(existing)
    used_refs: set[str] = set()
    reuse: dict[int, dict] = {}
    for i, item in enumerate(items):
        if isinstance(item, str) and item.strip():
            for candidate in index.get(normalize_text(item), []):
                ref = str(candidate.get("referenceId"))
                if ref not in used_refs:
                    used_refs.add(ref)
                    reuse[i] = candidate
                    break

    strings = [s for i, s in enumerate(items) if isinstance(s, str) and s.strip() and i not in reuse]
    parsed: dict[str, dict | None] = {}
    if strings:
        unique = list(dict.fromkeys(strings))
        try:
            for line, parse in zip(unique, await parse_lines(client, unique), strict=True):
                parsed[line] = parse
        except ToolError as e:  # MealieError included
            result.warnings.append(
                f"Mealie's ingredient parser failed ({e}); saved {len(unique)} line(s) as unlinked free text"
            )
            parsed = dict.fromkeys(unique)
        else:
            bad = [line for line, r in parsed.items() if r is None]
            if bad:
                result.warnings.append(f"parser returned no usable result for {bad}; saved as unlinked free text")

    dicts = [d for d in items if isinstance(d, dict)]
    foods, units = await asyncio.gather(
        fetch_by_ids(client, "foods", {d["food_id"] for d in dicts if d.get("food_id")}),
        fetch_by_ids(client, "units", {d["unit_id"] for d in dicts if d.get("unit_id")}),
    )

    flagged: set[str] = set()
    for i, item in enumerate(items):
        if isinstance(item, dict):
            result.payload.append(build_structured_ingredient(item, foods, units))
        elif i in reuse:
            result.payload.append(copy.deepcopy(reuse[i]))
            result.reused += 1
        elif item.strip():
            parse = parsed.get(item)
            if not parse:
                result.payload.append(free_text_ingredient(item))
                continue
            quality = assess_parse(item, parse["ingredient"])
            links_something = bool(_linked(parse["ingredient"].get("food")) or _linked(parse["ingredient"].get("unit")))
            if links_something and not quality.ok:
                result.payload.append(free_text_ingredient(item))
                if item not in flagged:
                    flagged.add(item)
                    result.needs_review.append({"line": item, "reason": quality.reason()})
            else:
                result.payload.append(build_parsed_ingredient(parse["ingredient"], item))
    return result


def instructions_payload(texts: list[str], existing: list[dict] | None = None) -> tuple[list[dict], int]:
    """Steps from text. A step whose text matches an existing step keeps that step
    (id, title, ingredient references). Returns (steps, reused_count)."""
    pool: dict[str, list[dict]] = {}
    for step in existing or []:
        if isinstance(step, dict):
            pool.setdefault(normalize_text(step.get("text")), []).append(step)
    steps, reused = [], 0
    for text in texts:
        matches = pool.get(normalize_text(text))
        if matches:
            steps.append(copy.deepcopy(matches.pop(0)))
            reused += 1
        else:
            steps.append({"text": text})
    return steps, reused


def orphaned_step_references(steps: list[dict] | None, ingredients: list[dict]) -> list[int]:
    """1-based numbers of steps that reference an ingredient no longer in the list."""
    refs = {str(i.get("referenceId")) for i in ingredients if isinstance(i, dict)}
    orphaned = []
    for n, step in enumerate(steps or [], start=1):
        step_refs = [str(r.get("referenceId")) for r in (step or {}).get("ingredientReferences") or [] if isinstance(r, dict)]
        if any(r not in refs for r in step_refs if r and r != "None"):
            orphaned.append(n)
    return orphaned


def parsed_ingredient_summary(p: dict | None, line: str) -> dict[str, Any]:
    if p is None:
        return {"input": line, "error": "parser returned no usable result for this line"}
    ing = p.get("ingredient") or {}
    food = ing.get("food") if isinstance(ing.get("food"), dict) else {}
    unit = ing.get("unit") if isinstance(ing.get("unit"), dict) else {}
    return {
        "input": p.get("input") or line,
        "quantity": ing.get("quantity"),
        "unit_name": unit.get("name"),
        "unit_id": unit.get("id"),  # present only if it matched an existing unit
        "food_name": food.get("name"),
        "food_id": food.get("id"),  # present only if it matched an existing food
        "note": ing.get("note") or "",
        "confidence": (p.get("confidence") or {}).get("average"),
    }
