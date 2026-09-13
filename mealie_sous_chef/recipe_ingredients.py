"""Review and edit an existing recipe's ingredients in place.

The division of labour: these tools never guess silently. `review` shows each
ingredient's state (linked / unlinked / suspect, nutrition readiness) and, for
anything not clean, a re-parse, candidate foods and a ready-to-send edit.
Claude -- with the user where it matters -- decides. `edit` then applies the
chosen changes atomically, addressed by each ingredient's stable referenceId, so
untouched ingredients, step->ingredient links and scraped original text survive.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import uuid
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import taxonomy
from .ingredients import (
    _singular,
    _STOPWORDS,
    assess_parse,
    build_parsed_ingredient,
    fetch_by_ids,
    normalize_text,
    orphaned_step_references,
    parse_lines,
    words,
)
from .mealie import MealieClient
from .nutrition import MASS_GRAMS, VOLUME_ML, quantity_to_grams, read_food_profile

EDIT_KEYS = {
    "ref", "insert_after", "move_after", "delete", "text",
    "quantity", "food_id", "unit_id", "note", "title", "create_food", "create_unit",
}
FIELD_KEYS = {"quantity", "food_id", "unit_id", "note", "title", "create_food", "create_unit"}
MAX_CANDIDATES = 5
SEARCH_PAGE = 20
# words that describe a package size rather than a food ("1 (14 oz) can ...")
_SIZE_WORDS = {w for w in (*MASS_GRAMS, *VOLUME_ML) if "_" not in w} | {"inch", "cm", "mm", "can", "jar", "package", "pkg", "bag", "box"}
MAX_SEARCH_TERMS = 5


# ---------------------------------------------------------------- rows


def _ref_of(ing: dict) -> str:
    return str(ing.get("referenceId") or "")


def _ref_label(obj: Any) -> dict | None:
    return {"id": obj.get("id"), "name": obj.get("name")} if isinstance(obj, dict) and obj.get("id") else None


def step_usage(steps: list[dict] | None) -> dict[str, list[int]]:
    usage: dict[str, list[int]] = {}
    for n, step in enumerate(steps or [], start=1):
        for r in (step or {}).get("ingredientReferences") or []:
            if isinstance(r, dict) and r.get("referenceId"):
                usage.setdefault(str(r["referenceId"]), []).append(n)
    return usage


def _nutrition_state(ing: dict) -> tuple[str, str]:
    food = ing.get("food")
    if not (isinstance(food, dict) and food.get("id")):
        return "not_linked", "link a food first"
    quantity = ing.get("quantity") or 0
    if not quantity or quantity <= 0:
        return "no_quantity", "no quantity; ignored by compute_recipe_nutrition"
    profile = read_food_profile(food)
    if not profile.per_100g:
        return "missing_data", f"food {food.get('name')!r} has no nutrition data (lookup_nutrition -> set_food_nutrition)"
    conv = quantity_to_grams(float(quantity), ing.get("unit"), profile)
    if conv.grams is None:
        return "cannot_weigh", conv.note
    if conv.approximate:
        return "approximate", conv.note
    return "ready", conv.note


def ingredient_row(position: int, ing: dict, usage: dict[str, list[int]]) -> dict[str, Any]:
    food = ing.get("food")
    original = ing.get("originalText") or ""
    row: dict[str, Any] = {
        "ref": _ref_of(ing),
        "position": position,
        "text": ing.get("display") or ing.get("note") or original,
        "original_text": original or None,
        "quantity": ing.get("quantity"),
        "unit": _ref_label(ing.get("unit")),
        "food": _ref_label(food),
        "note": ing.get("note") or "",
        "title": ing.get("title") or None,
        "used_in_steps": usage.get(_ref_of(ing), []),
    }
    if ing.get("referencedRecipe"):
        row["status"] = "sub_recipe"
    elif not row["food"]:
        row["status"] = "unlinked"
    else:
        row["status"] = "linked"
        if original:
            # the food doesn't match what the recipe originally said
            quality = assess_parse(original, {"food": food, "unit": ing.get("unit"), "note": ing.get("note")})
            if quality.extra_words:
                row["status"] = "suspect"
                row["status_reason"] = quality.reason()
            elif quality.dropped_words:
                row["unrepresented_words"] = quality.dropped_words
    row["nutrition"], row["nutrition_note"] = _nutrition_state(ing)
    return row


def _needs_attention(row: dict) -> bool:
    return row["status"] in ("unlinked", "suspect") or row["nutrition"] in ("missing_data", "cannot_weigh")


def _summary(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {"total": len(rows)}
    for row in rows:
        out[row["status"]] = out.get(row["status"], 0) + 1
        out[f"nutrition_{row['nutrition']}"] = out.get(f"nutrition_{row['nutrition']}", 0) + 1
    return out


# ---------------------------------------------------------------- suggestions


def _ordered_words(text: str) -> list[str]:
    return [w for w in (_singular(t) for t in re.findall(r"[a-z]+", text.lower())) if w not in _STOPWORDS]


def _search_terms(name: str) -> list[str]:
    """'boneless skinless chicken thighs' -> the name, its singular form, shorter
    suffixes ('chicken thighs'), then single words ('thigh', 'chicken'). Mealie's
    search ranks poorly, so cast wide and rank locally."""
    ordered = _ordered_words(name)
    terms = [name.strip(), " ".join(ordered)]
    terms += [" ".join(ordered[i:]) for i in range(1, len(ordered))]
    terms += list(reversed(ordered))  # head noun first
    return [t for t in dict.fromkeys(t for t in terms if len(t) > 2)][:MAX_SEARCH_TERMS]


def rank_candidates(candidates: list[dict], food_name: str, line: str) -> list[dict]:
    """Best first: exact name, every word present in the line, contains the head
    noun, most overlap, fewest extra words, shortest. Unrelated items are dropped."""
    target = words(food_name) or words(line)
    line_words = words(line)
    ordered = _ordered_words(food_name or line)
    head = ordered[-1] if ordered else ""

    def key(c: dict) -> tuple:
        cw = words(c.get("name"))
        return (cw == target, bool(cw) and cw <= line_words, head in cw, len(cw & target), -len(cw - target), -len(c.get("name") or ""))

    related = [c for c in candidates if words(c.get("name")) & target]
    return sorted(related, key=key, reverse=True)[:MAX_CANDIDATES]


async def food_candidates(
    client: MealieClient, name: str, line: str, semaphore: asyncio.Semaphore, cache: dict[str, list[dict]]
) -> list[dict]:
    found: dict[str, dict] = {}
    for term in _search_terms(name):
        if term not in cache:
            async with semaphore:
                data = await client.get("/api/foods", {"search": term, "perPage": SEARCH_PAGE, "page": 1})
            cache[term] = [{"id": i["id"], "name": i.get("name")} for i in data.get("items", [])]
        for item in cache[term]:
            found.setdefault(item["id"], item)
    return rank_candidates(list(found.values()), name, line)


def _suggestion(row: dict, parsed: dict | None, line: str, candidates: list[dict]) -> dict[str, Any]:
    if parsed is None:
        return {"grade": "no_parse", "line": line, "food_candidates": candidates}
    ing = parsed["ingredient"]
    food, unit = ing.get("food") or {}, ing.get("unit") or {}
    quality = assess_parse(line, ing)
    edit: dict[str, Any] = {"ref": row["ref"], "quantity": ing.get("quantity") or 0}
    note_parts = []
    if unit.get("id"):
        edit["unit_id"] = unit["id"]
    else:
        edit["unit_id"] = None
        if unit.get("name"):
            note_parts.append(unit["name"])
    if ing.get("note"):
        note_parts.append(ing["note"])
    edit["note"] = " ".join(note_parts)

    line_words = words(line)
    # a candidate whose every word appears in the line ("coconut milk" for "light coconut milk")
    clean_candidate = next((c for c in candidates if words(c["name"]) and words(c["name"]) <= line_words), None)
    proposed: dict[str, Any] | None = edit
    if food.get("id") and (quality.ok or not quality.extra_words or clean_candidate is None):
        grade = "exact" if quality.ok else "check"
        edit["food_id"] = food["id"]
    elif clean_candidate and set(quality.dropped_words) <= _SIZE_WORDS:
        grade = "candidate"
        edit["food_id"] = clean_candidate["id"]
        # keep qualifiers the chosen food's name doesn't carry ("light", "fresh") and any package size
        qualifiers = [w for w in (food.get("name") or "").split() if words(w) and not words(w) <= words(clean_candidate["name"])]
        sizes = []
        if quality.dropped_words:
            sizes = [f"({p.strip()})" for p in re.findall(r"\(([^)]*)\)", line) if words(p) & set(quality.dropped_words)]
            sizes = sizes or quality.dropped_words
        edit["note"] = " ".join(p for p in [*qualifiers, *sizes, edit["note"]] if p)
    elif quality.dropped_words and not food.get("id"):
        # e.g. "salt and pepper to taste": more than one food, or words the parse lost
        grade = "manual"
        proposed = None
    else:
        grade = "no_match"
        if food.get("name"):
            edit["create_food"] = food["name"]
    suggestion: dict[str, Any] = {
        "grade": grade,
        "line": line,
        "parsed": {
            "quantity": ing.get("quantity"),
            "unit": unit.get("name"),
            "unit_id": unit.get("id"),
            "food": food.get("name"),
            "food_id": food.get("id"),
            "note": ing.get("note") or "",
        },
        "food_candidates": candidates,
    }
    if proposed is not None:
        suggestion["proposed_edit"] = proposed
    else:
        suggestion["hint"] = (
            "the parse lost words, so no single food fits; if the line names several foods, set this row to one "
            "(food_id or create_food) and add the others with insert_after"
        )
    if quality.dropped_words:
        suggestion["dropped_words"] = quality.dropped_words
    if quality.extra_words:
        suggestion["extra_words"] = quality.extra_words
    return suggestion


GRADE_HELP = {
    "exact": "parser matched an existing food and accounts for every word; safe to apply",
    "check": "parser matched a food but words disagree (see dropped_words/extra_words); verify or pick a candidate",
    "candidate": "parser found no food; proposed_edit uses the closest search candidate whose name is all in the line -- confirm it's the same food",
    "no_match": "no existing food fits; proposed_edit creates one (food_candidates may still hold a near-duplicate worth using)",
    "manual": "the line lost words when parsed (often several foods in one line); no edit proposed -- decide how to split it",
    "no_parse": "Mealie's parser returned nothing usable; set fields by hand",
}


async def review(client: MealieClient, slug: str, only: str = "all", suggest: bool = True) -> dict[str, Any]:
    if only not in ("all", "needs_attention"):
        raise ToolError("only must be 'all' or 'needs_attention'")
    recipe = await client.get(f"/api/recipes/{slug}")
    ingredients = [i for i in recipe.get("recipeIngredient") or [] if isinstance(i, dict)]
    usage = step_usage(recipe.get("recipeInstructions"))
    rows = [ingredient_row(n, ing, usage) for n, ing in enumerate(ingredients, start=1)]
    summary = _summary(rows)
    shown = [r for r in rows if only == "all" or _needs_attention(r)]

    to_suggest = [r for r in shown if r["status"] in ("unlinked", "suspect")] if suggest else []
    to_suggest = [r for r in to_suggest if (r["original_text"] or r["text"] or "").strip()]
    if to_suggest:
        lines = [(r["original_text"] or r["text"]).strip() for r in to_suggest]
        try:
            parses: list[dict | None] = await parse_lines(client, lines)
        except ToolError as e:
            parses = [None] * len(lines)
            summary["suggestion_error"] = f"parser failed: {e}"
        semaphore = asyncio.Semaphore(4)
        search_cache: dict[str, list[dict]] = {}

        async def candidates_for(row: dict, parse: dict | None, line: str) -> list[dict]:
            name = ((parse or {}).get("ingredient") or {}).get("food") or {}
            name = (name.get("name") if isinstance(name, dict) else None) or line
            quality = assess_parse(line, parse["ingredient"]) if parse else None
            if row["status"] == "unlinked" or (quality and not quality.ok):
                if quality and quality.dropped_words:
                    # words the parse lost may be further foods ("salt and pepper"): search for them too
                    extra = [w for w in quality.dropped_words if w not in _SIZE_WORDS]
                    name = " ".join([name, *extra])
                return await food_candidates(client, name, line, semaphore, search_cache)
            return []

        candidate_lists = await asyncio.gather(
            *(candidates_for(r, p, line) for r, p, line in zip(to_suggest, parses, lines, strict=True))
        )
        for row, parse, line, cands in zip(to_suggest, parses, lines, candidate_lists, strict=True):
            row["suggestion"] = _suggestion(row, parse, line, cands)

    out: dict[str, Any] = {
        "slug": recipe.get("slug"),
        "name": recipe.get("name"),
        "servings": recipe.get("recipeServings"),
        "summary": summary,
        "ingredients": shown,
    }
    if to_suggest:
        out["grades"] = {g: GRADE_HELP[g] for g in sorted({r["suggestion"]["grade"] for r in to_suggest})}
    return out


# ---------------------------------------------------------------- edits


def _resolve_ref(value: Any, by_ref: dict[str, int], count: int, label: str) -> int:
    """Index into the ORIGINAL ingredient list for a referenceId or 1-based position."""
    if isinstance(value, bool):
        raise ToolError(f"{label} must be a referenceId or a 1-based position")
    if isinstance(value, int):
        if not 1 <= value <= count:
            raise ToolError(f"{label} position {value} is out of range (recipe has {count} ingredients)")
        return value - 1
    if isinstance(value, str) and value in by_ref:
        return by_ref[value]
    raise ToolError(f"{label} {value!r} doesn't match any ingredient; use the ref or position from review_recipe_ingredients")


def _validate_edit(i: int, edit: Any) -> str:
    """Returns the edit kind: update | text | delete | insert | move."""
    if not isinstance(edit, dict):
        raise ToolError(f"edits[{i}] must be an object")
    unknown = set(edit) - EDIT_KEYS
    if unknown:
        raise ToolError(f"edits[{i}] has unknown key(s) {sorted(unknown)}; allowed: {sorted(EDIT_KEYS)}")
    has_ref, is_insert = "ref" in edit, "insert_after" in edit
    if has_ref == is_insert:
        raise ToolError(f"edits[{i}] needs exactly one of ref (change an ingredient) or insert_after (add one)")
    fields = set(edit) & FIELD_KEYS
    if "food_id" in edit and "create_food" in edit:
        raise ToolError(f"edits[{i}]: use food_id or create_food, not both")
    if "unit_id" in edit and "create_unit" in edit:
        raise ToolError(f"edits[{i}]: use unit_id or create_unit, not both")
    if "text" in edit and fields - {"title"}:
        raise ToolError(f"edits[{i}]: text re-parses the whole line; don't combine it with {sorted(fields - {'title'})}")
    q = edit.get("quantity")
    if q is not None and (isinstance(q, bool) or not isinstance(q, int | float) or not math.isfinite(q) or q < 0):
        raise ToolError(f"edits[{i}].quantity must be a non-negative number or null")
    for key in ("note", "title", "text", "create_food", "create_unit", "food_id", "unit_id"):
        if edit.get(key) is not None and not isinstance(edit[key], str):
            raise ToolError(f"edits[{i}].{key} must be a string or null")
    for key in ("create_food", "create_unit", "text"):
        if key in edit and not (edit[key] or "").strip():
            raise ToolError(f"edits[{i}].{key} can't be blank")
    if edit.get("delete"):
        if set(edit) - {"ref", "delete"}:
            raise ToolError(f"edits[{i}]: delete can't be combined with other changes")
        return "delete"
    if "move_after" in edit:
        return "move"  # may also carry field changes or text
    if is_insert:
        if not ("text" in edit or edit.get("food_id") or edit.get("create_food") or edit.get("note") or edit.get("title")):
            raise ToolError(f"edits[{i}]: an insert needs text, or a food_id/create_food/note/title")
        return "insert"
    if "text" in edit:
        return "text"
    if not fields:
        raise ToolError(f"edits[{i}] changes nothing")
    return "update"


def _apply_fields(target: dict, edit: dict, foods: dict[str, dict], units: dict[str, dict]) -> None:
    if "quantity" in edit:
        target["quantity"] = float(edit["quantity"] or 0)
    if "food_id" in edit:
        target["food"] = foods[edit["food_id"]] if edit["food_id"] else None
    if edit.get("create_food"):
        target["food"] = foods[f"new:{normalize_text(edit['create_food'])}"]
    if "unit_id" in edit:
        target["unit"] = units[edit["unit_id"]] if edit["unit_id"] else None
    if edit.get("create_unit"):
        target["unit"] = units[f"new:{normalize_text(edit['create_unit'])}"]
    if "note" in edit:
        target["note"] = (edit["note"] or "").strip()
    if "title" in edit:
        target["title"] = edit["title"] or None


async def edit(client: MealieClient, slug: str, edits: list[dict]) -> dict[str, Any]:
    if not edits:
        raise ToolError("edits is empty")
    recipe = await client.get(f"/api/recipes/{slug}")
    original = [i for i in recipe.get("recipeIngredient") or [] if isinstance(i, dict)]
    by_ref = {_ref_of(ing): n for n, ing in enumerate(original) if _ref_of(ing)}

    # ---- 1. validate everything that needs no network
    kinds = [_validate_edit(i, e) for i, e in enumerate(edits)]
    touched: dict[int, int] = {}  # original index -> edit index
    anchors: list[tuple[str | int, int]] = []  # (anchor, edit index) for inserts and moves
    for i, (e, kind) in enumerate(zip(edits, kinds, strict=True)):
        if "ref" in e:
            idx = _resolve_ref(e["ref"], by_ref, len(original), f"edits[{i}].ref")
            if idx in touched:
                raise ToolError(f"edits[{i}] and edits[{touched[idx]}] both change ingredient {idx + 1}; combine them")
            touched[idx] = i
        anchor_key = "insert_after" if kind == "insert" else "move_after" if kind == "move" else None
        if anchor_key:
            value = e[anchor_key]
            anchor = value if value in ("start", "end") else _resolve_ref(value, by_ref, len(original), f"edits[{i}].{anchor_key}")
            if kind == "move" and anchor == "end":
                raise ToolError(f"edits[{i}].move_after accepts a ref, a position or 'start'")
            if kind == "move" and anchor == _index_of(e, by_ref, len(original)):
                raise ToolError(f"edits[{i}] moves an ingredient after itself")
            anchors.append((anchor, i))
    _check_move_cycles(edits, kinds, by_ref, len(original))

    # ---- 2. ids that must exist, lines to parse, things to create
    food_ids = {e["food_id"] for e in edits if e.get("food_id")}
    unit_ids = {e["unit_id"] for e in edits if e.get("unit_id")}
    foods, units = await asyncio.gather(fetch_by_ids(client, "foods", food_ids), fetch_by_ids(client, "units", unit_ids))

    text_edits = [(i, e["text"].strip()) for i, e in enumerate(edits) if e.get("text")]
    parsed: dict[int, dict] = {}
    if text_edits:
        results = await parse_lines(client, [t for _, t in text_edits])
        problems = []
        for (i, line), result in zip(text_edits, results, strict=True):
            if result is None:
                problems.append(f"edits[{i}] {line!r}: parser returned nothing usable")
                continue
            quality = assess_parse(line, result["ingredient"])
            if not quality.ok:
                problems.append(f"edits[{i}] {line!r}: {quality.reason()}")
            parsed[i] = result["ingredient"]
        if problems:
            raise ToolError(
                "these lines don't parse cleanly, so nothing was changed: "
                + "; ".join(problems)
                + ". Set quantity/food_id/unit_id/note explicitly for them instead."
            )

    created: list[dict[str, Any]] = []
    for key, resource, lookup in (("create_food", "foods", foods), ("create_unit", "units", units)):
        for name in dict.fromkeys(e[key].strip() for e in edits if e.get(key)):
            item, was_created = await taxonomy.get_or_create(client, resource, name)
            lookup[f"new:{normalize_text(name)}"] = item
            created.append({"resource": resource, "id": item["id"], "name": item.get("name"), "created": was_created})

    # ---- 3. build the new list (pure)
    new_rows: dict[int, dict] = {}  # original index -> replacement row
    deleted: set[int] = set()
    inserted: dict[int, dict] = {}  # edit index -> new row
    for i, (e, kind) in enumerate(zip(edits, kinds, strict=True)):
        if kind == "delete":
            deleted.add(_index_of(e, by_ref, len(original)))
        elif kind in ("update", "text") or (kind == "move" and (set(e) & FIELD_KEYS or "text" in e)):
            idx = _index_of(e, by_ref, len(original))
            row = copy.deepcopy(original[idx])
            if "text" in e:
                built = build_parsed_ingredient(parsed[i], e["text"].strip())
                for field in ("quantity", "food", "unit", "note"):
                    row[field] = built.get(field)
                if not row.get("originalText"):
                    row["originalText"] = e["text"].strip()
                if "title" in e:
                    row["title"] = e["title"] or None
            else:
                _apply_fields(row, e, foods, units)
            row.pop("display", None)  # Mealie recomputes it
            new_rows[idx] = row
        elif kind == "insert":
            if "text" in e:
                row = build_parsed_ingredient(parsed[i], e["text"].strip())
                if e.get("title"):
                    row["title"] = e["title"]
            else:
                row = {"quantity": 0.0, "note": "", "referenceId": str(uuid.uuid4())}
                _apply_fields(row, e, foods, units)
            inserted[i] = row

    moved = {_index_of(e, by_ref, len(original)): i for i, (e, k) in enumerate(zip(edits, kinds, strict=True)) if k == "move"}
    children: dict[str | int, list[tuple[str, int]]] = {}
    for anchor, i in anchors:
        children.setdefault(anchor, []).append(("insert" if kinds[i] == "insert" else "move", i))

    result: list[dict] = []

    def emit_children(anchor: str | int) -> None:
        for kind, i in children.get(anchor, []):
            if kind == "insert":
                result.append(inserted[i])
            else:
                emit_original(_index_of(edits[i], by_ref, len(original)))

    def emit_original(idx: int) -> None:
        if idx not in deleted:
            result.append(new_rows.get(idx, original[idx]))
        emit_children(idx)

    emit_children("start")
    for idx in range(len(original)):
        if idx not in moved:
            emit_original(idx)
    emit_children("end")

    # ---- 4. one write
    saved = await client.patch(f"/api/recipes/{slug}", {"recipeIngredient": result})
    saved_ingredients = [i for i in saved.get("recipeIngredient") or [] if isinstance(i, dict)]
    usage = step_usage(saved.get("recipeInstructions"))
    rows = [ingredient_row(n, ing, usage) for n, ing in enumerate(saved_ingredients, start=1)]
    out: dict[str, Any] = {"slug": saved.get("slug"), "summary": _summary(rows), "ingredients": rows}
    if created:
        out["foods_and_units"] = created
    orphaned = orphaned_step_references(saved.get("recipeInstructions"), saved_ingredients)
    if orphaned:
        out["warnings"] = [f"step(s) {orphaned} referenced a deleted ingredient; review those steps' ingredient links in Mealie"]
    return out


def _index_of(e: dict, by_ref: dict[str, int], count: int) -> int:
    return _resolve_ref(e["ref"], by_ref, count, "ref")


def _check_move_cycles(edits: list[dict], kinds: list[str], by_ref: dict[str, int], count: int) -> None:
    after: dict[int, str | int] = {}
    for e, kind in zip(edits, kinds, strict=True):
        if kind == "move":
            value = e["move_after"]
            after[_index_of(e, by_ref, count)] = value if value == "start" else _resolve_ref(value, by_ref, count, "move_after")
    for start in after:
        seen, node = set(), start
        while node in after:
            if node in seen:
                raise ToolError("move_after edits form a cycle (A after B, B after A)")
            seen.add(node)
            node = after[node]
