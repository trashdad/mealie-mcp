# Mealie Sous Chef

Mealie Sous Chef connects Claude to your self-hosted [Mealie](https://mealie.io). It's a remote MCP server you add as a **custom connector** in Claude web, desktop or mobile, with no third-party auth service needed. Ask Claude what's for dinner, have it import a recipe from a link, fill your shopping list, plan the week or tidy up your ingredients database. It can also work out per-serving nutrition for recipes from real USDA / Open Food Facts data.

> **Where this came from.** This project started as a fork of [retr083/mealie-mcp](https://github.com/retr083/mealie-mcp), which provides the OAuth server, the Docker/proxy setup and the original recipe, shopping-list and meal-plan tools. That part is largely unchanged. On top of it, Mealie Sous Chef adds:
> - structured ingredients linked to Mealie's Foods/Units
> - Foods/Units management (merge, batch)
> - the nutrition lookup and rollup
> - a test suite
>
> It's MIT-licensed, like the original. See [Credits](#credits).

## What you get

- **Streamable HTTP** transport at `/mcp` (what Claude's connectors require).
- **Built-in OAuth 2.1 authorization server**: dynamic client registration, PKCE, refresh-token rotation and a single-password login page. There's nothing else to run.
- Tokens and clients persist to a JSON file, so restarts don't force re-authorisation.
- Only Claude's callback URLs are accepted as OAuth redirects, so nobody can register a phishing client against your login page.
- Failed logins lock out an IP for 5 minutes after 5 attempts.
- **Linked ingredients.** Plain-text ingredient lines are parsed by Mealie's own parser and linked to your existing Foods and Units. Anything that doesn't match is kept as text, never dropped.
- **Nutrition you can trust the gaps of.** Per-100g data is stored once per food and rolled up per serving. Every estimate says exactly which ingredients it couldn't count and why.

Targets the Mealie **v2/v3 API** (`/api/households/...`). The original tools were tested against Mealie **v3.22**. The ingredient, taxonomy and nutrition tools were verified end-to-end against Mealie **v3.25**, with response shapes checked against a nightly build. On Mealie 1.x the client falls back to the old `/api/groups/...` paths when it gets a 404.

## Tools

### Recipes

| Tool | What it does |
|---|---|
| `search_recipes` | Free-text search, filter by tag/category slug, paginated summaries |
| `get_recipe` | Full recipe: ingredients, steps, notes, nutrition |
| `import_recipe_from_url` | Scrape a recipe web page into Mealie |
| `create_recipe` / `update_recipe` | Ingredients can mix plain text (`"2 cups flour"`, auto-parsed and linked) and structured objects (`{"quantity", "food_id", "unit_id", "note"}`). A line is only linked when the parse accounts for every word; the rest are kept as text and listed in `needs_review`. `update_recipe` keeps lines and steps you re-send unchanged |
| `delete_recipe` | Permanently delete a recipe |
| `list_tags_and_categories` | Slugs usable as search filters |

### Ingredients: Foods & Units

| Tool | What it does |
|---|---|
| `review_recipe_ingredients` | Row-by-row state of a recipe's ingredients (linked / unlinked / suspect, nutrition readiness), with food candidates and a ready-made edit for anything that needs fixing |
| `edit_recipe_ingredients` | Change specific ingredients in place: link, relink, create-and-link a food, re-parse, insert, delete, move. Atomic; untouched ingredients and step links are preserved |
| `parse_ingredients` | Read-only preview of how text lines parse and which Foods/Units they match |
| `manage_taxonomy` | List (optionally foods missing nutrition), create, update, **merge** or delete Foods or Units, one at a time or batched via `items`. Merge keeps the dropped name as an alias; delete refuses while recipes still use an item |

### Nutrition

| Tool | What it does |
|---|---|
| `lookup_nutrition` | Per-100g candidates from USDA FoodData Central and Open Food Facts, for one food or up to 10 at once |
| `set_food_nutrition` | Store per-100g nutrition on a food (or many, via `items`), plus optional density and portion weights |
| `compute_recipe_nutrition` | Roll up per-serving nutrition from linked foods, report the gaps, optionally save it to the recipe |

### Shopping lists & meal plans

| Tool | What it does |
|---|---|
| `list_shopping_lists` / `get_shopping_list` | Lists and their (unchecked) items |
| `add_shopping_items` / `update_shopping_item` / `delete_shopping_items` | Add free-text items; tick, rename or change quantity; remove |
| `add_recipe_to_shopping_list` | Push a recipe's ingredients onto a list (scalable) |
| `get_meal_plan` / `add_meal_plan_entry` / `delete_meal_plan_entry` | Meal planning by date |

### Escape hatch

| Tool | What it does |
|---|---|
| `mealie_get` | Read-only GET against any `/api/...` path |

## Requirements

- Mealie reachable from wherever this runs (LAN is fine).
- A Mealie **API token**: in Mealie, open your user → *API Tokens* → create one (long-lived).
- A **public HTTPS hostname** pointing at this server, reachable by Claude's servers. If you already run a reverse proxy (Nginx Proxy Manager, Caddy, Traefik) with ports 80/443 forwarded, just add a host (Option B). Otherwise the compose file includes a Cloudflare Tunnel (Option A).
- Optional: a free [USDA FoodData Central API key](https://fdc.nal.usda.gov/api-key-signup.html) for nutrition lookups. Without one you get the shared `DEMO_KEY`, which allows about 10 requests per hour.

## Deploy with Docker

On any Docker host (a VM or LXC on Proxmox, a NAS, etc.):

```bash
git clone https://github.com/trashdad/mealie-mcp.git && cd mealie-mcp
cp .env.example .env
nano .env        # MEALIE_URL, MEALIE_API_TOKEN, PUBLIC_URL, MCP_LOGIN_PASSWORD (+ USDA_API_KEY)
```

The compose file pulls the pre-built multi-arch image `ghcr.io/trashdad/mealie-mcp:latest` (amd64 + arm64), published by this repo's GitHub Actions workflow. To build from source instead, uncomment `build: .` in `docker-compose.yml` and add `--build` to the commands below.

**Unraid:** add a container with Repository `ghcr.io/trashdad/mealie-mcp:latest`, map container port `8000`, map a host path to `/data`, and add the same variables as `.env.example`. Then continue with Option A or B for the public hostname.

### Option A: Cloudflare Tunnel (no port forwarding)

1. In Cloudflare Zero Trust, go to *Networks → Tunnels → Create a tunnel* (Cloudflared). Copy the token into `.env` as `TUNNEL_TOKEN=...`.
2. In the tunnel's *Public Hostname* tab add: `mealie-mcp.yourdomain.com` → Service `HTTP` → `mealie-mcp:8000`.
3. Set `PUBLIC_URL=https://mealie-mcp.yourdomain.com` in `.env` and remove the `ports:` block from `docker-compose.yml` (it isn't needed). Then:

```bash
docker compose --profile cloudflared up -d
```

### Option B: Nginx Proxy Manager (or any reverse proxy)

```bash
docker compose up -d
```

1. DNS: add an `A`/`CNAME` record for `mealie-mcp.yourdomain.com` pointing at your public IP (same as your other NPM hosts).
2. In NPM, go to *Hosts → Proxy Hosts → Add Proxy Host*:
   - **Domain Names:** `mealie-mcp.yourdomain.com`
   - **Scheme:** `http`
   - **Forward Hostname/IP:** the Docker host's LAN IP, or the container name `mealie-mcp` if NPM is on the same Docker network
   - **Forward Port:** `8000`
   - **Block Common Exploits:** on
   - **Websockets Support:** on (harmless, not required)
   - **SSL tab:** request a Let's Encrypt certificate, turn on **Force SSL** and **HTTP/2**
   - **Advanced tab**, paste:
     ```nginx
     proxy_buffering off;
     proxy_read_timeout 300s;
     proxy_send_timeout 300s;
     ```
     This stops nginx buffering streamed responses, and keeps long tool calls (e.g. importing a slow recipe site) from being cut off.
3. Do **not** put an NPM Access List (IP allow-list) on this host. The login page has to be reachable from your phone/browser, not just from Anthropic.

`PUBLIC_URL` in `.env` must be `https://mealie-mcp.yourdomain.com`. It's what the OAuth metadata advertises, and Claude rejects the connector if it doesn't match the URL you enter.

Caddy equivalent, if you ever switch:

```
mealie-mcp.yourdomain.com {
    reverse_proxy 192.168.1.20:8000
}
```

### Check it

- `https://mealie-mcp.yourdomain.com/` → a one-line banner
- `https://mealie-mcp.yourdomain.com/healthz` → `{"ok": true, "mealie": "reachable"}`
- `https://mealie-mcp.yourdomain.com/.well-known/oauth-authorization-server` → JSON metadata

## Connect Claude

**Claude mobile / web / desktop:**

1. Go to Settings → *Connectors* → *Add custom connector*.
2. Enter the URL `https://mealie-mcp.yourdomain.com/mcp` and leave the OAuth client ID/secret fields empty.
3. Click *Add*. Claude opens your login page; enter `MCP_LOGIN_PASSWORD`.

Then enable the connector in a chat and ask it what's for dinner. Custom connectors are added per account, so once it's added on the web it appears on mobile too.

**Claude Code:**

```bash
claude mcp add --transport http mealie https://mealie-mcp.yourdomain.com/mcp
```

Then run `/mcp` inside Claude Code to trigger the login.

## Structured ingredients & nutrition

Mealie itself does **not** calculate nutrition from ingredients. A recipe's nutrition block is a static set of numbers, usually scraped from the source site or typed in by hand. Everything below is logic this project adds on top, using data it stores in Mealie's own database.

### How it fits together

1. **Link ingredients.** `create_recipe` / `update_recipe` accept plain lines like `"2 cups jasmine rice"`. They run all lines through Mealie's parser in one call and link each one to an existing Food and Unit where one matches. Lines that don't match are saved as free text, exactly as Mealie stores unparsed ingredients.
   - A line is only linked when the parse accounts for every word of it. Mealie's parser sometimes drops part of a line ("salt and pepper to taste" → just *salt*) or fuzzy-matches a food whose name says something else ("chicken thighs" → a junk food named "lbs boneless skinless chicken thighs"). Those lines are kept as text and listed in `needs_review`, rather than being linked wrongly.
   - To check or override matches first, call `parse_ingredients`. Then pass structured objects such as `{"quantity": 2, "food_id": "…", "unit_id": "…", "note": "rinsed"}`; you can mix them with plain strings in one call.
   - Structured ids are verified up front. If you pass a food id that doesn't exist, Mealie would silently save the ingredient with no food, so this is reported as an error instead.
   - If Mealie's parser is down or returns garbage, the lines are saved as free text and the reply includes a `warnings` list.
2. **Fix what's left, in place.** For an existing recipe, `review_recipe_ingredients(slug)` lists every ingredient with its state, and for anything unlinked or suspect gives a re-parse, matching foods from your database and a `proposed_edit`. Claude (asking you when a match is a judgement call) sends the chosen edits to `edit_recipe_ingredients`, which changes only those ingredients. It can also create a missing food (`create_food`, reusing an existing one with the same name).
3. **Look up nutrition** with `lookup_nutrition("jasmine rice")`. It searches USDA FoodData Central and Open Food Facts and returns per-100g candidates:
   - USDA is best for generic foods; its Foundation and SR Legacy entries are listed first.
   - Open Food Facts is best for branded products.
   - If one source is rate-limited or down, you still get the other, plus an `errors` entry saying why.
   - Results are cached on disk for 30 days.
4. **Store it** with `set_food_nutrition(food_id, per_100g=…, source="usda", source_id=…)`. This is done once per food and reused by every recipe that uses it.
5. **Roll it up** with `compute_recipe_nutrition(slug)`. It weighs each linked ingredient, sums, divides by servings and writes the result to the recipe.

Steps 3–4 only happen once per distinct food, and both batch: `lookup_nutrition(food_names=[…])` and `set_food_nutrition(items=[…])`. `manage_taxonomy(resource="foods", nutrition="missing")` lists foods still without data. After your staples are set up, most new recipes parse straight into foods that already have nutrition.

### Editing ingredients without breaking things

Each Mealie ingredient has a hidden `referenceId`. Instruction steps use it to link to ingredients, and each ingredient keeps the recipe's original scraped line in `originalText`.

- **`edit_recipe_ingredients`** addresses ingredients by that `ref` (or by position), validates every edit before writing, and saves once. Untouched ingredients, step links and original text survive byte-for-byte. If any edit is invalid, nothing changes. Available edits:
  - update fields (`food_id`, `unit_id`, `quantity`, `note`, `title`; `null` clears)
  - `create_food` / `create_unit`
  - `text` (re-parse the line; rejected if lossy)
  - `delete`
  - `insert_after`
  - `move_after`
- **`update_recipe`** still replaces whole lists, but a line identical to an existing ingredient (its original or displayed text) keeps that ingredient as-is, and an identical step keeps its id, title and links. If step links do get orphaned, the reply says which steps.
- **`review_recipe_ingredients` grades each suggestion:**
  - `exact`: the parser matched and every word is accounted for.
  - `check`: the parser matched, but words disagree.
  - `candidate`: no parser match, so the closest search result is proposed; any qualifier such as "light" or "fresh" is kept in the note.
  - `no_match`: `proposed_edit` would create the food.

### Nutrients

`calories` (kcal), `protein_g`, `fat_g`, `saturated_fat_g`, `trans_fat_g`, `carbs_g`, `fiber_g`, `sugar_g`, `sodium_mg`, `cholesterol_mg`. These map onto the matching fields of Mealie's nutrition block. Unsaturated fat isn't computed.

### Weighing ingredients

Nutrition is per 100 g, so every ingredient has to become grams. Recipes mostly say "1 cup" or "2 eggs", not grams, so the conversion uses **USDA's official household measures**. USDA FoodData Central publishes, for each food, how much 1 cup, 1 tablespoon, 1 large, 1 clove and so on weighs (flour: 1 cup = 125 g; brown sugar: 1 cup packed = 220 g; egg: 1 large = 50 g). These are typical values; real kitchens vary by roughly 10–20% depending on how you scoop or chop, which is the accepted margin for recipe estimates.

The most specific source wins:

1. **This food's own measures.**
   - When you store nutrition from a USDA record (`set_food_nutrition(source="usda", source_id=…)`), that exact record's household measures are saved on the food too.
   - You can also set weights yourself with `portion_grams` (e.g. `{"each": 50, "clove": 5}`) and `grams_per_ml`.
2. **A built-in USDA reference table** of ~110 common ingredients: flours, sugars, oils, dairy, rice, spices, produce and more. It's matched by food name, plus the note, so "jasmine rice" with note "cooked" uses cooked rice. The table is generated from USDA data by `scripts/build_household_measures.py`, and every entry records its USDA FDC id.
3. **Mass units** (g, kg, oz, lb, or a unit's Mealie standard) convert exactly.
4. **Volume units** go through the food's density from (1) or (2). Only if nothing is known is water assumed, and those ingredients are listed under `approximate`.

Details matter where USDA lists them:
- A prep word in the note picks the right measure: a "packed" cup of brown sugar, a "chopped" vs "sliced" cup of onion.
- A size word picks the right piece: "1 large onion" = 150 g, a plain "1 onion" = the medium 110 g, and eggs default to large.

Mealie unit standards (`standardQuantity` + `standardUnit`, e.g. tablespoon = 0.5 `fluid_ounce`) still work and can be set with `manage_taxonomy(resource="units", action="update", …)`. Units with no known weight for a food (a "bunch" of something not in the table) are reported, with the setting that would fix them.

### What the result tells you

- `per_serving` / `total`: unknown nutrients are `null`, never 0.
- `unaccounted`: ingredients that contributed nothing, each with a reason and fix: not linked to a food, food has no nutrition data, unit can't be weighed, or it references another recipe.
- `incomplete_nutrients`: counted foods that lack a particular nutrient. USDA rarely lists trans fat, for example.
- `approximate`: volume weighed as water.
- `skipped`: linked ingredients with no quantity ("salt, to taste").
- `previous_nutrition`: what the recipe had before.

`save=true` (the default) only writes when **every** ingredient was accounted for. That stops publisher-supplied nutrition being overwritten by an undercount; pass `allow_partial=true` to save anyway. Nutrients with no data keep whatever value the recipe already had. `servings=` overrides the recipe's own serving count. If the recipe has none set, the totals are for the whole recipe and a warning says so.

### Cleaning up duplicates

Imports tend to create near-duplicates ("jasmine rice" vs "Jasmine Rice"). Use `manage_taxonomy(action="merge", item_id=<drop>, merge_into=<keep>)`. It repoints every recipe ingredient and shopping-list item, then deletes the duplicate.

- The dropped item's names become **aliases** of the kept one, so Mealie's parser keeps matching lines that use them. For units this includes abbreviations.
- If the kept food has no nutrition data and the dropped one did, the data is carried over.
- `add_alias=false` turns both off.
- `items=[{…}, {…}]` batches merges (or any write action); one failure doesn't stop the rest.

Don't use `delete` for this. Mealie happily deletes a food that recipes still use and just strips it from those ingredients: "2 cups jasmine rice" becomes "2". So `delete` checks for references first and refuses, unless you pass `force=true`. The shopping-list part of that check only sees your own household's lists. On Postgres, Mealie itself also refuses to delete anything a shopping list uses, and the error suggests merging instead.

### Where the data lives

Nutrition data is stored in each Food's `extras`, Mealie's generic key/value store, as the keys `nutrition_per_100g` (JSON), `nutrition_source`, `nutrition_source_id`, `grams_per_ml` and `portion_grams` (JSON). It's included in Mealie's own backups and survives replacing this server. Mealie's UI doesn't show it; use `manage_taxonomy(action="list")` to see it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MEALIE_URL` | — | Mealie base URL, as seen from this container |
| `MEALIE_API_TOKEN` | — | Mealie API token (the server acts as that user) |
| `PUBLIC_URL` | — | Public HTTPS origin of this server, no trailing slash, no `/mcp` |
| `MCP_LOGIN_PASSWORD` | — | Password for the connect-time login page (min 12 chars) |
| `USDA_API_KEY` | `DEMO_KEY` | USDA FoodData Central key for `lookup_nutrition` (free; the demo key allows ~10 requests/hour) |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Bind address |
| `MCP_DATA_DIR` | `/data` | Where `auth_state.json` and `nutrition_cache.json` live |
| `MCP_ACCESS_TOKEN_TTL` | `3600` | Access token lifetime (s). Claude refreshes automatically. |
| `MCP_REFRESH_TOKEN_TTL` | `2592000` | Refresh token lifetime (s): how long before you must log in again |
| `MCP_ALLOWED_REDIRECT_URIS` | claude.ai callback + loopback | Comma-separated allowlist for OAuth clients |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Browser shows `ERR_SSL_UNRECOGNIZED_NAME_ALERT`, or `http://` gives NPM's "Congratulations" page | NPM has no proxy host matching that exact hostname (typo, not saved, or disabled). Fix the proxy host and request the certificate. |
| Claude: "Failed to start MCP authorization" and **nothing** in `docker logs` | Claude never reached you. The hostname must resolve (from the public internet) to a public **IPv4** address: no private/CGNAT ranges, no AAAA-only. Check with `nslookup <host> 8.8.8.8` from mobile data. |
| Claude: "Your account was authorized, but no MCP server was found at the provided URL" | Login worked but the connector URL is wrong: it must end in **`/mcp`**. Delete and re-add the connector with `https://<host>/mcp`. |
| Claude: "Authorization with the MCP server failed" | `PUBLIC_URL` doesn't match the URL you entered (scheme/host must be identical, no trailing slash), or `/token` took >10 s. Check `docker logs mealie-mcp`. |
| `/healthz` returns `503` | The container can't reach `MEALIE_URL`, or `MEALIE_API_TOKEN` is wrong. |
| Tool calls time out on slow recipe imports | Add the `proxy_read_timeout` lines from Option B to your proxy config. |
| `lookup_nutrition` reports a USDA rate limit | You're on the shared `DEMO_KEY`. Set `USDA_API_KEY` and restart. |
| Nutrition looks too low | Check `unaccounted` and `skipped` in the `compute_recipe_nutrition` reply; each entry says what to fix. |

Every failure toast in Claude includes an `ofid_…` reference. If you file an issue with [anthropics/claude-ai-mcp](https://github.com/anthropics/claude-ai-mcp/issues), include it along with your `docker logs` lines from the attempt.

## Limitations (read before exposing it)

- **Single user, single password.** Anyone who knows the password gets full access to the Mealie account behind the API token. This is designed for a personal homelab, not multi-tenant use.
- **Tokens are stored unhashed** in `data/auth_state.json`. They're random 48-byte secrets, but treat that file like a password file; it lives in a Docker volume for that reason.
- **No account/session UI.** To revoke everything, delete `data/auth_state.json` and restart.
- **Nutrition is an estimate.** It's only as good as the source data you pick, the gram conversions and the parser's matches. Known gaps:
  - Cup and spoon weights are USDA typical values (±10–20% in a real kitchen); foods not in the reference table and not linked to a USDA record are weighed as water.
  - Sub-recipes aren't rolled up.
  - Nothing checks raw vs cooked weights (pick the matching USDA entry).
  - `oz` always means weight, never fluid ounces.
- Pinned to `mcp==2.2.0`. The official SDK's server API still changes between releases (e.g. `FastMCP` → `MCPServer`), so upgrades need a look, not just a bump.

## Security notes

- Everything Claude can do, it does **as the Mealie user who owns the API token**. Create a dedicated Mealie user if you want to limit blast radius.
- The MCP endpoint is only reachable with a valid bearer token. The only unauthenticated surfaces are the login page and the OAuth metadata/registration endpoints, which are public by design.
- For belt-and-braces you can restrict the hostname at your proxy/tunnel to Anthropic's egress range `160.79.104.0/21`. Note that *you* also need to reach `/login` from your phone/browser during connect, so allow that too, or only enforce the IP rule on `/mcp`, `/token` and `/register`.
- `lookup_nutrition` sends food names (nothing else) to USDA and Open Food Facts.
- To revoke access at any time, delete `data/auth_state.json` and restart. Rotating `MCP_LOGIN_PASSWORD` alone isn't enough: existing tokens keep working until they expire, so delete the file too.

## Development

```bash
python -m venv .venv && .venv/Scripts/activate      # or: source .venv/bin/activate
pip install -e ".[test]"
pytest
```

The unit tests run against an in-memory fake of Mealie's API. It reproduces the Mealie behaviours this code has to handle: 422s on nameless food links, silent unlinking on delete, 409 name conflicts, string-only extras, and the merge response shape. They need no network. Parser-quality tests use real parser output captured from a Mealie instance (`tests/parser_fixtures.py`). `tests/test_auth.py` drives the full OAuth + PKCE flow and authenticated MCP calls against the real Starlette app.

`tests/test_live_mealie.py` runs the same flows against a **real** Mealie. Point it at a throwaway instance, **never your real one**: it creates, merges and deletes data.

```bash
SOUS_CHEF_LIVE_MEALIE_URL=http://127.0.0.1:9925 SOUS_CHEF_LIVE_MEALIE_TOKEN=... pytest tests/test_live_mealie.py
```

To run the server locally:

```bash
MEALIE_URL=http://mealie.lan:9925 MEALIE_API_TOKEN=... PUBLIC_URL=http://127.0.0.1:8000 \
MCP_LOGIN_PASSWORD=correct-horse-battery MCP_DATA_DIR=./data python -m mealie_sous_chef
```

Layout:

| Module | Contents |
|---|---|
| `server.py` | Tool definitions |
| `auth.py` | OAuth provider and login page |
| `mealie.py` | HTTP client |
| `ingredients.py` | Parse, quality-check and link ingredients |
| `recipe_ingredients.py` | Review and in-place editing of a recipe's ingredients |
| `taxonomy.py` | Foods/Units management |
| `nutrition.py` | Storage format, unit conversion, rollup |
| `household_measures.py` | USDA cup/spoon/piece weights and the bundled reference table (`data/household_measures.json`) |
| `nutrition_sources.py` | USDA/OFF client and cache |

Built on the official [`mcp`](https://github.com/modelcontextprotocol/python-sdk) Python SDK (2.x). The SDK provides the `/authorize`, `/token`, `/register`, `/revoke` and `.well-known` endpoints; this project supplies the provider, login page, Mealie client and tools.

## Credits

- [retr083/mealie-mcp](https://github.com/retr083/mealie-mcp) by retr083 is the original server this project is forked from: OAuth provider, deployment setup, and the recipe, shopping-list and meal-plan tools. Its MIT license and copyright notice are kept in [LICENSE](LICENSE).
- Two design choices were borrowed from [mgummich/mcp-mealie](https://github.com/mgummich/mcp-mealie): parsing ingredients transparently inside `create_recipe`/`update_recipe`, and one `manage_taxonomy` tool rather than separate tools per Foods/Units action.
- Nutrition data comes from [USDA FoodData Central](https://fdc.nal.usda.gov/) (public domain) and [Open Food Facts](https://world.openfoodfacts.org/) (ODbL). Household measures (cup, spoon and piece weights) are USDA FoodData Central SR Legacy / FNDDS data.
