# Structured ingredients + nutrition — what this fork adds

This adds 8 new tools (and revises `create_recipe`/`update_recipe`) on top of
the original retr083/mealie-mcp, closing the gap [issue #152](https://github.com/2fst4u/mealie-mcp/issues/152)
describes on a different fork. It went through a second pass after reviewing
several other Mealie MCP implementations, borrowing two design decisions from
[mgummich/mcp-mealie](https://github.com/mgummich/mcp-mealie) (its own
nutrition/parser coverage is thin, but its ergonomics for this specific piece
are cleaner than what we started with): ingredients parse **transparently**
inside `create_recipe`/`update_recipe` rather than needing a separate step,
and Foods/Units management collapsed into one `manage_taxonomy` tool instead
of nine separate ones.

**Important context, unchanged from before:** Mealie itself does **not**
auto-calculate nutrition from linked ingredients — its nutrition field is a
static, manually-entered number per recipe. Everything nutrition-related
here (`lookup_nutrition`, `set_food_nutrition`, `compute_recipe_nutrition`)
is new logic this fork adds on top, not a hidden Mealie feature switched on.

## The workflow, end to end

For a recipe with free-text ingredients (most of your 24 existing recipes):

1. **Just call `update_recipe`** with plain ingredient strings, e.g.
   `["2 cups jasmine rice", "8 oz chicken thighs, boneless skinless"]`. Behind
   the scenes this batch-parses them through Mealie's own NLP parser in one
   call and links each to an existing Food/Unit when one matches (your
   instance already has 253 foods, 22 units from prior imports) — nothing
   extra to call first. Lines with no match are kept as free text, not
   dropped.
   - Want to see matches before committing, or override the parser's guess?
     Call `parse_ingredients` first (read-only) and build the ingredient list
     yourself with `{"quantity": ..., "food_id": ..., "unit_id": ..., "note": ...}`
     dicts — `update_recipe` accepts a mix of plain strings and these dicts in
     the same call.
2. **`manage_taxonomy(resource="foods", action="create", name="...")`** — only
   for ingredients the parser truly couldn't match to anything existing.
3. **`lookup_nutrition`** — searches USDA FoodData Central and Open Food
   Facts for a food name, returns per-100g candidates from both.
4. **`set_food_nutrition`** — pick the best candidate (or `source="manual"`
   for numbers off a label yourself) and attach it to the food. This is
   stored once per food and reused by every recipe using it.
5. **`compute_recipe_nutrition`** — sums each linked food's nutrition ×
   quantity, converts units to grams (exactly if the unit has
   `standardQuantity`/`standardUnit` set via `manage_taxonomy`, approximately
   otherwise), divides by servings, writes it into the recipe, and reports
   anything it couldn't account for so the total is honest about its gaps.

Steps 2-4 only need to happen once per distinct food — after your pantry's
staples are built out the first time, most new recipes parse straight into
already-known, already-priced-nutrition foods.

## Cleaning up duplicates

Your 253 existing foods almost certainly have near-duplicates ("jasmine
rice" vs "Jasmine Rice") from past imports. `manage_taxonomy` has a native
`merge` action for this: `action="merge", item_id=<the one to drop>,
merge_into=<the one to keep>` repoints every recipe/shopping-item using the
dropped one and removes it — safer than `delete`, which just fails if
anything still references the item. `items` batches this (or any action)
across many entries in one call.

## Unit accuracy

`compute_recipe_nutrition` is only as accurate as its gram conversions.
Mealie's default units generally don't have `standardQuantity` set, so the
fork falls back to rough, water-density volume estimates (a cup ~= 240g) for
anything not explicitly configured. Worth spending a few minutes on your
most-used units (cup, tbsp, tsp, oz -- especially for dense stuff like flour
or rice where 240g/cup is off) via `manage_taxonomy(resource="units",
action="update", item_id=..., data={"standardQuantity": ..., "standardUnit": "g"})`
if you want tighter numbers; one-time cost per unit, not per recipe.

## New/changed tools

| Tool | What it does |
|---|---|
| `create_recipe` / `update_recipe` | *(revised)* ingredients now accept a mix of plain strings (auto-parsed + linked) and structured dicts |
| `manage_taxonomy` | Foods/Units: list, create, update, merge, delete -- single or batched via `items` |
| `parse_ingredients` | Read-only preview of how text will parse, before saving |
| `lookup_nutrition` | Search USDA + Open Food Facts, per-100g candidates |
| `set_food_nutrition` | Attach per-100g nutrition to a food (cached, reused across recipes) |
| `compute_recipe_nutrition` | Roll up + write a recipe's per-serving nutrition |
