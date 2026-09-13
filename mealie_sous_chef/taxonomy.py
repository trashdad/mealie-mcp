"""Foods & Units management behind the manage_taxonomy tool.

Mealie behaviours this works around (verified against Mealie's source):
- Merge (`PUT /api/{foods,units}/merge`) answers `{"message", "error"}`, not the
  kept item, and a bad id comes back as a bare 500 "Failed to merge".
- Deleting a food/unit that recipes still use does NOT fail: the ORM nulls the
  link, so those ingredients silently lose their food/unit. Only a shopping-list
  reference on Postgres trips a 409. So delete checks references itself first.
- Names are unique per group (409 on a duplicate create/rename).
- A unit's standardQuantity/standardUnit are silently discarded unless both are
  set and the quantity is > 0.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .mealie import MealieClient, MealieError
from .nutrition import (
    EXTRAS_DENSITY,
    EXTRAS_NUTRITION,
    EXTRAS_PORTIONS,
    EXTRAS_SOURCE,
    EXTRAS_SOURCE_ID,
    is_known_standard_unit,
    read_food_profile,
)

_NUTRITION_EXTRAS = (EXTRAS_NUTRITION, EXTRAS_SOURCE, EXTRAS_SOURCE_ID, EXTRAS_DENSITY, EXTRAS_PORTIONS)

RESOURCES: dict[str, dict[str, Any]] = {
    "foods": {"path": "/api/foods", "merge_keys": ("fromFood", "toFood"), "label": "food", "ref": "food"},
    "units": {"path": "/api/units", "merge_keys": ("fromUnit", "toUnit"), "label": "unit", "ref": "unit"},
}
BATCH_ITEM_KEYS = {"name", "item_id", "data", "merge_into", "force", "add_alias"}
WRITE_ACTIONS = ("create", "update", "merge", "delete")
_READ_ONLY_FIELDS = {"id", "createdAt", "updatedAt", "groupId"}


def taxonomy_item(resource: str, item: dict) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": item.get("id"),
        "name": item.get("name"),
        "plural_name": item.get("pluralName"),
        "description": item.get("description") or "",
    }
    if item.get("aliases"):
        out["aliases"] = [a.get("name") for a in item["aliases"] if isinstance(a, dict)]
    if resource == "units":
        out["abbreviation"] = item.get("abbreviation") or ""
        out["standard_quantity"] = item.get("standardQuantity")
        out["standard_unit"] = item.get("standardUnit")
    else:
        out.update(read_food_profile(item).as_dict())
    return out


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _prepare_data(resource: str, data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ToolError("data must be an object")
    clean = {_camel(k): v for k, v in data.items()}
    bad = sorted(set(clean) & (_READ_ONLY_FIELDS | {"name"}))
    if bad:
        raise ToolError(f"data can't set {bad} (pass `name` as its own argument; ids are fixed)")
    if "extras" in clean and not isinstance(clean["extras"], dict):
        raise ToolError("data.extras must be an object")
    if resource == "units" and ("standardQuantity" in clean or "standardUnit" in clean):
        qty, unit = clean.get("standardQuantity"), clean.get("standardUnit")
        if qty is None and unit is None:
            pass  # explicitly clearing both
        elif not (isinstance(qty, int | float) and not isinstance(qty, bool) and qty > 0) or not unit:
            raise ToolError(
                "standardQuantity (> 0) and standardUnit must be set together -- Mealie silently discards one without the other"
            )
        elif not is_known_standard_unit(unit):
            raise ToolError(
                f"standardUnit {unit!r} isn't a recognised mass/volume unit; use one of Mealie's: "
                "gram, kilogram, ounce, pound, milliliter, liter, fluid_ounce, cup"
            )
    return clean


async def list_items(
    client: MealieClient,
    resource: str,
    query: str,
    page: int,
    per_page: int,
    nutrition: str | None = None,
) -> dict[str, Any]:
    page, per_page = max(page, 1), min(max(per_page, 1), 50)
    params: dict[str, Any] = {"page": page, "perPage": per_page, "orderBy": "name", "orderDirection": "asc"}
    if query:
        params["search"] = query
    if nutrition is None:
        data = await client.get(RESOURCES[resource]["path"], params)
        return {
            "page": data.get("page"),
            "total_pages": data.get("total_pages"),
            "total": data.get("total"),
            "items": [taxonomy_item(resource, i) for i in data.get("items", [])],
        }
    if resource != "foods":
        raise ToolError("the nutrition filter only applies to foods")
    if nutrition not in ("missing", "present"):
        raise ToolError("nutrition must be 'missing' or 'present'")
    # Mealie can't filter on extras, so fetch every food and filter here.
    data = await client.get(RESOURCES[resource]["path"], {**params, "page": 1, "perPage": -1})
    want_present = nutrition == "present"
    matching = [i for i in data.get("items", []) if bool(read_food_profile(i).per_100g) == want_present]
    start = (page - 1) * per_page
    return {
        "page": page,
        "total_pages": max(1, -(-len(matching) // per_page)),
        "total": len(matching),
        "items": [taxonomy_item(resource, i) for i in matching[start : start + per_page]],
    }


async def get_existing(client: MealieClient, resource: str, item_id: str, role: str = "item_id") -> dict:
    try:
        return await client.get(f"{RESOURCES[resource]['path']}/{item_id}")
    except MealieError as e:
        if e.status_code in (404, 422):
            raise ToolError(f"{role} {item_id!r}: no such {RESOURCES[resource]['label']}") from e
        raise


def _norm_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


async def find_by_name(client: MealieClient, resource: str, name: str) -> dict | None:
    """Exact (case/whitespace-insensitive) name match, via Mealie's search."""
    data = await client.get(RESOURCES[resource]["path"], {"search": name, "perPage": 50, "page": 1})
    target = _norm_name(name)
    return next((i for i in data.get("items", []) if _norm_name(i.get("name")) == target), None)


async def get_or_create(client: MealieClient, resource: str, name: str) -> tuple[dict, bool]:
    """(item, created). An existing item with the same name is reused, not duplicated."""
    name = name.strip()
    if not name:
        raise ToolError(f"{RESOURCES[resource]['label']} name can't be blank")
    existing = await find_by_name(client, resource, name)
    if existing:
        return existing, False
    try:
        return await client.post(RESOURCES[resource]["path"], {"name": name}), True
    except MealieError as e:
        if e.status_code == 409:  # raced, or a name that search didn't surface
            found = await find_by_name(client, resource, name)
            if found:
                return found, False
        raise


async def _absorb_into_kept(client: MealieClient, resource: str, source: dict, kept: dict) -> dict[str, Any]:
    """After a merge: keep the dropped item's names as aliases of the kept one (so
    Mealie's parser still matches them) and, for foods, carry over nutrition data
    the kept food lacks. Returns a report; a failed save becomes a warning, since
    the merge itself already happened."""
    report: dict[str, Any] = {"aliases_added": [], "nutrition_carried_over": False}
    known = {_norm_name(n) for n in (kept.get("name"), kept.get("pluralName"), kept.get("abbreviation"))}
    known |= {_norm_name(a.get("name")) for a in kept.get("aliases") or [] if isinstance(a, dict)}
    known.discard("")
    names = [source.get("name"), source.get("pluralName")]
    if resource == "units":
        names += [source.get("abbreviation"), source.get("pluralAbbreviation")]
    names += [a.get("name") for a in source.get("aliases") or [] if isinstance(a, dict)]
    new_aliases = []
    for n in names:
        if n and _norm_name(n) not in known:
            known.add(_norm_name(n))
            new_aliases.append(n.strip())

    payload = dict(kept)
    changed = False
    if new_aliases:
        payload["aliases"] = [{"name": a.get("name")} for a in kept.get("aliases") or [] if isinstance(a, dict)]
        payload["aliases"] += [{"name": n} for n in new_aliases]
        changed = True
    carry = False
    if resource == "foods" and read_food_profile(source).per_100g:
        if read_food_profile(kept).per_100g:
            report["nutrition_conflict"] = "both foods had nutrition data; kept the merged-into food's"
        else:
            kept_extras = dict(kept.get("extras") or {})
            kept_extras.update({k: v for k, v in (source.get("extras") or {}).items() if k in _NUTRITION_EXTRAS})
            payload["extras"] = kept_extras
            carry = changed = True
    if not changed:
        return report
    try:
        updated = await client.put(f"{RESOURCES[resource]['path']}/{kept['id']}", payload)
    except MealieError as e:
        report["warning"] = f"merge succeeded, but saving aliases/nutrition on the kept item failed: {e}"
        return report
    report["aliases_added"] = new_aliases
    report["nutrition_carried_over"] = carry
    report["into"] = taxonomy_item(resource, updated)
    return report


def _quote(value: str) -> str:
    return '"' + re.sub(r'(["\\])', r"\\\1", value) + '"'


async def find_references(client: MealieClient, resource: str, item_id: str) -> dict[str, Any]:
    """Recipes and shopping-list items that use this food/unit."""
    ref = RESOURCES[resource]["ref"]
    recipes = await client.get(
        "/api/recipes",
        {"perPage": 5, "page": 1, "queryFilter": f"recipeIngredient.{ref}.id = {_quote(item_id)}"},
    )
    shopping = await client.get(
        "/api/households/shopping/items",
        {"perPage": 1, "page": 1, "queryFilter": f"{ref}Id = {_quote(item_id)}"},
    )
    return {
        "recipe_count": recipes.get("total") or 0,
        "recipe_examples": [r.get("name") for r in recipes.get("items", [])],
        "shopping_item_count": shopping.get("total") or 0,
    }


async def apply(
    client: MealieClient,
    resource: str,
    action: str,
    *,
    name: str | None = None,
    item_id: str | None = None,
    data: dict[str, Any] | None = None,
    merge_into: str | None = None,
    force: bool = False,
    add_alias: bool = True,
) -> dict[str, Any]:
    cfg = RESOURCES[resource]
    path, label = cfg["path"], cfg["label"]

    if action == "create":
        if not name or not name.strip():
            raise ToolError("create requires a name")
        body = {**_prepare_data(resource, data), "name": name.strip()}
        try:
            return {"created": taxonomy_item(resource, await client.post(path, body))}
        except MealieError as e:
            if e.status_code == 409:
                raise ToolError(
                    f"a {label} named {name.strip()!r} already exists -- find it with action='list', query={name.strip()!r}"
                ) from e
            raise

    if action == "update":
        if not item_id:
            raise ToolError("update requires item_id (from action='list')")
        changes = _prepare_data(resource, data)
        if name is None and not changes:
            raise ToolError("update requires name and/or data")
        current = await get_existing(client, resource, item_id)
        if "extras" in changes:  # merge, so nutrition data isn't wiped by an unrelated extras edit
            changes["extras"] = {**(current.get("extras") or {}), **changes["extras"]}
        payload = {**current, **changes}
        if name is not None:
            if not name.strip():
                raise ToolError("name can't be blank")
            payload["name"] = name.strip()
        try:
            return {"updated": taxonomy_item(resource, await client.put(f"{path}/{item_id}", payload))}
        except MealieError as e:
            if e.status_code == 409:
                raise ToolError(
                    f"another {label} is already named {payload['name']!r}. To combine them use "
                    f"action='merge', item_id={item_id!r}, merge_into=<that {label}'s id>."
                ) from e
            raise

    if action == "merge":
        if not item_id or not merge_into:
            raise ToolError("merge requires item_id (the one folded away) and merge_into (the one kept)")
        if item_id == merge_into:
            raise ToolError("item_id and merge_into are the same")
        source = await get_existing(client, resource, item_id)
        await get_existing(client, resource, merge_into, role="merge_into")
        from_key, to_key = cfg["merge_keys"]
        await client.put(f"{path}/merge", {from_key: item_id, to_key: merge_into})
        kept = await client.get(f"{path}/{merge_into}")
        result: dict[str, Any] = {"merged": {"id": item_id, "name": source.get("name")}, "into": taxonomy_item(resource, kept)}
        if add_alias:
            result.update(await _absorb_into_kept(client, resource, source, kept))
        elif resource == "foods" and read_food_profile(source).per_100g:
            result["warning"] = "the merged-away food's nutrition data was discarded (add_alias=false skips carry-over)"
        return result

    if action == "delete":
        if not item_id:
            raise ToolError("delete requires item_id (from action='list')")
        current = await get_existing(client, resource, item_id)
        if not force:
            try:
                refs = await find_references(client, resource, item_id)
            except MealieError as e:
                raise ToolError(
                    f"couldn't check whether {label} {current.get('name')!r} is still in use ({e}). "
                    "Pass force=true to delete anyway."
                ) from e
            if refs["recipe_count"] or refs["shopping_item_count"]:
                examples = ", ".join(repr(n) for n in refs["recipe_examples"])
                raise ToolError(
                    f"{label} {current.get('name')!r} is used by {refs['recipe_count']} recipe(s)"
                    f"{f' (e.g. {examples})' if examples else ''} and {refs['shopping_item_count']} shopping-list "
                    f"item(s). Deleting would silently strip it from those ingredients. Merge it instead: "
                    f"action='merge', item_id={item_id!r}, merge_into=<the {label} to keep>. "
                    "Or pass force=true to delete anyway."
                )
        try:
            await client.delete(f"{path}/{item_id}")
        except MealieError as e:
            if e.status_code == 409:
                raise ToolError(
                    f"Mealie refused to delete {label} {current.get('name')!r} because something still references it "
                    f"(409). Merge it into another {label} instead: action='merge', item_id={item_id!r}, "
                    f"merge_into=<the {label} to keep>."
                ) from e
            raise
        return {"deleted": taxonomy_item(resource, current)}

    raise ToolError(f"unknown action {action!r}")


async def run(
    client: MealieClient,
    resource: str,
    action: str,
    *,
    query: str = "",
    page: int = 1,
    per_page: int = 25,
    name: str | None = None,
    item_id: str | None = None,
    data: dict[str, Any] | None = None,
    merge_into: str | None = None,
    force: bool = False,
    add_alias: bool = True,
    nutrition: str | None = None,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if resource not in RESOURCES:
        raise ToolError(f"resource must be one of {', '.join(RESOURCES)}")

    if action == "list":
        if items is not None:
            raise ToolError("items batches write actions, not list")
        return await list_items(client, resource, query, page, per_page, nutrition)

    if action not in WRITE_ACTIONS:
        raise ToolError(f"action must be one of list, {', '.join(WRITE_ACTIONS)}")

    if items is None:
        return await apply(
            client,
            resource,
            action,
            name=name,
            item_id=item_id,
            data=data,
            merge_into=merge_into,
            force=force,
            add_alias=add_alias,
        )

    if any(v is not None for v in (name, item_id, data, merge_into)):
        raise ToolError("pass either items (batch) or name/item_id/data/merge_into (single), not both")
    if not items:
        raise ToolError("items is empty")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for i, batch_item in enumerate(items):
        try:
            if not isinstance(batch_item, dict):
                raise ToolError("each batch item must be an object")
            unknown = set(batch_item) - BATCH_ITEM_KEYS
            if unknown:
                raise ToolError(f"unknown key(s) {sorted(unknown)}; batch items accept {sorted(BATCH_ITEM_KEYS)}")
            results.append(
                {
                    "index": i,
                    **await apply(
                        client,
                        resource,
                        action,
                        name=batch_item.get("name"),
                        item_id=batch_item.get("item_id"),
                        data=batch_item.get("data"),
                        merge_into=batch_item.get("merge_into"),
                        force=bool(batch_item.get("force", force)),
                        add_alias=bool(batch_item.get("add_alias", add_alias)),
                    ),
                }
            )
        except ToolError as e:
            errors.append({"index": i, "item": batch_item, "error": str(e)})
        except Exception as e:  # noqa: BLE001 -- one bad item mustn't abort the batch
            errors.append({"index": i, "item": batch_item, "error": f"{e.__class__.__name__}: {e}"})
    return {"succeeded": len(results), "failed": len(errors), "results": results, "errors": errors}
