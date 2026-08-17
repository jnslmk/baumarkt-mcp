# baumarkt-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM search
four German hardware-store (Baumarkt) retailers — Hornbach, BAUHAUS, Globus
Baumarkt and OBI — through one tool surface. Built for self-hosting: one
container, no API keys, no per-query credits. The point is the single
`compare_price` call: it fans out over all four retailers and answers "who
sells this cheapest, and can I collect it locally today" — every result
carries a `store_pickup` field scoped to the local (Braunschweig-area)
branches.

Companion to [jnslmk/geizhals-mcp](https://github.com/jnslmk/geizhals-mcp)
(new retail goods) and
[jnslmk/kleinanzeigen-mcp](https://github.com/jnslmk/kleinanzeigen-mcp)
(second-hand classifieds).

## Tools

| Tool | What it does |
|------|--------------|
| `search_products` | Keyword search of one retailer or all four, with an optional client-side `max_price` filter. Returns product summaries. |
| `get_product` | Full detail for one product id at one retailer. |
| `compare_price` | Fan-out over all four retailers, sorted cheapest first — the "who sells this cheapest, can I collect it locally today" answer in one call. |

The intended flow is `compare_price` (or `search_products` with
`retailer="all"`) → pick interesting ids → `get_product`. Every result is
one normalised product with the same shape:
`retailer, id, name, brand, gtin, price, currency, availability, url,
image, store_pickup`.

### search_products

```
search_products(query, retailer="all", store=None, max_price=None, max_results=20, limit=None)
```

- `retailer` — `"all"` (default) fans out over hornbach, bauhaus, globus and
  obi; or exactly one of those four names.
- `store` — optional branch/store identifier; `None` uses each retailer's
  default (Braunschweig) branch. Per-retailer semantics in "The retailers".
- `max_price` — maximum price in EUR (≥ 0), typed `str | int` because LLMs
  routinely send `"600"` as a string and the schema accepts it. Filters
  results client-side **after** fetching, so a product with no listed price
  (`price: null`) cannot be filtered and is kept.
- `max_results` — per-retailer cap, also `str | int`, hard-capped at
  `BM_MAX_RESULTS` (default 50).
- `limit` — deprecated alias for `max_results` (also per-retailer, not a
  global cap across all four). Sibling MCP servers in this project
  (aliexpress-mcp, ebay-mcp, amazon-mcp) name this knob `limit`, and a model
  that carries that name over gets a schema rejection naming no field; both
  search tools accept either name so a wrong guess costs nothing. Supplying
  both `max_results` and `limit` with different values is a `ValueError`.

Returns `{query, returned, results, errors}` — `results` is the flat list of
matching products across the requested retailers, `errors` the per-retailer
failures.

### get_product

```
get_product(retailer, product_id, store=None)
```

`retailer` must be exactly one of the four names; `product_id` is the
retailer's own id/sku as returned by a search. Returns
`{retailer, product, error}` — `product` is `null` when the id does not
resolve, or when the retailer fails. A retailer failure is additionally
reported in `error` (a non-null message) so it is distinguishable from
id-not-found, where `error` is `null`.

### compare_price

```
compare_price(query, store=None, max_results=20, limit=None)
```

Always fans out over all four retailers and sorts the merged results by
price ascending — products with no listed price sort last. Returns
`{query, returned, results, errors}`. `limit` is the same deprecated alias
for `max_results` as on `search_products` (see above).

### Failure handling

A failing retailer never fails the call. Each retailer runs independently
and a failure becomes an entry in the tool's `errors` list while the other
retailers' results are still returned. "Blocked" is never collapsed into
"no results": a bot-wall timeout reads as `blocked by a challenge: ...` (or
`blocked by a CAPTCHA: ...`, `browser pool exhausted: ...`), which is a
different answer from an empty result list.

## The bauhaus limitation

bauhaus.info sits behind a Cloudflare Turnstile challenge. The adapter
deliberately does **not** automate interaction with the challenge widget —
presenting a real browser and being passively let through is one thing,
scripting the widget itself is a different thing this project does not do.
So when bauhaus serves the interactive checkbox variant (or a hard or slow
challenge), searches through this server return **no bauhaus results** and
the tool reports a clear blocked-by-challenge error instead: `blocked by a
challenge: bauhaus served an interactive Cloudflare Turnstile challenge (the
checkbox variant, ...) — this adapter deliberately does not attempt to
clear it`.

This is intended behaviour — the deliberate cost of not defeating the
bot-control. bauhaus is the least reliable of the four retailers and may
routinely fail; the other three are unaffected, and every tool output makes
it explicit which retailer(s) failed rather than pretending bauhaus had
nothing.

## The retailers

| Retailer | Approach | Bot wall | Local store |
|----------|----------|----------|-------------|
| Hornbach | JSON-LD only (search `ItemList`, detail `Product`), headed Chromium | F5/Fastly "Client Challenge"; headless escalates to an image CAPTCHA | Braunschweig, branchCode `615` |
| BAUHAUS | Captures the page's own `/api/products` responses, never replayed; JSON-LD fallback | Cloudflare Turnstile (two variants — see above) | Braunschweig, storeId `607` |
| Globus | FactFinder-rendered listing, DOM-parsed; JSON-LD on the detail page | none observed (defensive check only) | Braunschweig, store id `212` |
| OBI | Plain HTTP (httpx), no browser | none | none — online pricing only, never pickup |

### Hornbach

hornbach.de sits behind an F5/Fastly "Client Challenge" bot wall. Plain HTTP
gets served a ~3 KB challenge body with HTTP 200 — indistinguishable from
success by status code alone. Headless Chromium escalates to an image
CAPTCHA; headed Chromium clears the challenge effectively immediately. There
is no DOM scraping: the search page (`/s/<query>`) carries a schema.org
`ItemList` JSON-LD block, and the detail page (`/p/<slug>/<sku>/`) a
`Product` block with `sku` and `gtin13`. The page carries at most one pickup
offer — for the visitor's default branch (Braunschweig, branchCode `615`);
the `store` parameter filters among whatever pickup offer the page carries
but cannot fetch another branch's real availability.

### BAUHAUS

bauhaus.info is behind a Cloudflare managed challenge (Turnstile) plus its
own branded WAF block page. The search results API
(`/api/products?productIds=...&filter=...`) returns clean JSON — but
re-requesting that exact URL from inside an already-cleared page gets the
branded 403 page back, never JSON. So the adapter **never constructs or
replays that URL**: it registers a `page.on("response", ...)` handler
*before* navigating and reads whatever the page's own JavaScript requests,
dict-merging the results as they arrive (the search page's `ItemList`
JSON-LD is the fallback). A detail page carries no price in its JSON-LD
either — the price is pulled out of the embedded server-rendered payload,
and captured `/api/purchasability` responses provide availability and a real
per-store pickup signal. The `selectedStore` cookie pins the storefront to
Braunschweig (`storeId 607`). Search results have no confirmed per-store
signal, so their `store_pickup` is `None` rather than a guess.

### Globus Baumarkt

globus-baumarkt.de is Shopware 6 with a FactFinder search integration; the
results listing is client-rendered and carries no JSON-LD, so `search` drives
a headed browser and parses the rendered cards from the DOM
(`/search/result?query=...`, paginated with `&p=<n>`, at most 5 pages). The
detail page carries a schema.org `Product` JSON-LD block (name, sku, gtin13,
brand, offers) plus a server-rendered "In diesem Markt" widget that gives a
real `store_pickup` reading. `get_product` looks an article number up by
searching for it — a unique hit redirects straight to the product detail
page. A fresh browser context resolves to the Braunschweig branch (store id
`212`); the `store` parameter is accepted for signature parity but is not
wired to store-switching and logs a warning when passed.

### OBI

obi.de has no bot wall (verified: a plain-HTTP request with a browser-shaped
User-Agent returns real search results), so this adapter is plain
[httpx](https://www.python-httpx.org/) — no browser, no Xvfb, and it never
touches the shared browser pool. `search` parses the
`window.__INITIAL_STATE__` JSON blob embedded in the search page
(`/search/<query>/`); `get_product` parses the `Product` JSON-LD on the
detail page (`/p/<id>`). OBI has no Braunschweig-area branch, so it is an
online-only price source here: every result has `store_pickup: null` and
`store` is accepted but ignored — an OBI price is never a pickup price.

## How it works

Three of the four retailers sit behind bot walls, and headless Chromium does
not clear them: on hornbach.de it gets served an image CAPTCHA, on
bauhaus.info a hard 403. The container therefore runs **headed Chromium
under Xvfb**, driven by [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
(a patched, undetected Playwright fork with the same async API). One
`BrowserManager` owns the browser and a small pool of reusable contexts
(`BM_MAX_CONCURRENT`, default 2): a context that has already cleared a wall
keeps its cookies, so reusing it is more valuable than starting cold again.
`wait_for_challenge_clear` polls the page every 250 ms and recognises both
wall shapes — hornbach's F5/Fastly challenge (raising `CaptchaRequired` on
the unrecoverable image-CAPTCHA variant) and bauhaus's Turnstile/WAF pages —
so a wall that never clears ends as an explicit `blocked by a challenge`
error, never as an empty result. obi is excluded from the pool entirely.

## Running it

```bash
docker build -t baumarkt-mcp .
docker run --rm -p 8000:8000 baumarkt-mcp
```

Streamable HTTP at `http://localhost:8000/mcp`, plain `GET /healthz` for
healthchecks (the image ships a matching Docker HEALTHCHECK). Give the
container at least 1.5 GB of memory — Chromium under Xvfb is the heavy part.

The entrypoint starts Xvfb (display `BM_DISPLAY`, default `99`, screen
`BM_SCREEN`, default `1366x900x24`), waits for the X socket to appear, then
`exec`s the server as PID 1. It deliberately does not use `xvfb-run`: its
signal handshake races as PID 1 in a container and can leave Xvfb up but the
server never binding its port.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_TRANSPORT` | `http` | `http` (streamable HTTP) or `stdio` |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8000` | Bind port |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `BM_MAX_CONCURRENT` | `2` | Browser contexts to pool — pool size and concurrency limit in one |
| `BM_MAX_RESULTS` | `50` | Hard cap on products per retailer per search |
| `BM_HEADLESS` | `0` | `1` runs headless — local development only, will not clear the hornbach/bauhaus walls |
| `BM_CHALLENGE_TIMEOUT_MS` | `25000` | How long to wait for a bot-wall challenge to clear |
| `BM_PROXY` | *(none)* | Egress proxy URL for the browser, e.g. `http://10.0.0.5:8888` |
| `BM_PROXY_USERNAME` / `BM_PROXY_PASSWORD` | *(none)* | Optional proxy auth |
| `BM_DISPLAY` | `99` | Xvfb display number (entrypoint) |
| `BM_SCREEN` | `1366x900x24` | Xvfb screen geometry (entrypoint) |
| `LOG_LEVEL` | `INFO` | Python log level |

The `BM_*` defaults are tuned for a chat agent making one request at a time.
Raise `BM_MAX_CONCURRENT` for throughput, at the cost of memory and a
greater chance of tripping these sites' bot detection.

### LibreChat

```yaml
mcpServers:
  baumarkt:
    type: streamable-http
    url: "http://baumarkt-mcp:8000/mcp"
    timeout: 180000
    chatMenu: true

mcpSettings:
  allowedAddresses:
    - "baumarkt-mcp:8000"
```

`allowedAddresses` is required — LibreChat's SSRF guard blocks MCP URLs that
resolve to private addresses, which a sibling container always does. The
timeout is generous because a cold call starts a browser and may wait out a
bot-wall challenge.

### Claude Code

```bash
claude mcp add --transport http baumarkt http://localhost:8000/mcp
```

## Development

```bash
uv venv && uv pip install -e .
patchright install chromium
BM_HEADLESS=1 MCP_TRANSPORT=stdio uv run python -m baumarkt_mcp
```

Headless is fine for a quick local check; the container default (headed
under Xvfb) is what actually clears the bot walls. `MCP_TRANSPORT=stdio`
runs the server as a local stdio server instead of HTTP.

Lint with `ruff check` and `ruff format --check`. The globus adapter ships a
dependency-free regression check runnable directly:

```bash
python -m baumarkt_mcp.retailers.globus
```

It feeds synthetic listing HTML — including the discounted-card shape that
once produced a wrong-but-plausible price — through the real parsing path and
asserts the correct sale price comes out. No network involved.

## Images

`ghcr.io/jnslmk/baumarkt-mcp` — multi-arch (`linux/amd64`, `linux/arm64`),
built by GitHub Actions on native runners for each architecture.

| Tag | Meaning |
|-----|---------|
| `latest` | Newest build of `main` |
| `sha-<full-sha>` | A specific commit |
| `v0.1.0`, `v0.1` | Release tags |

## Caveats

None of these retailers has a public API, so this server scrapes their sites
with a real browser (or, for obi, plain HTTP). It can break whenever they
change their markup or tighten their bot walls, and heavy or parallel use
may trip bot detection. Scraping is also at odds with the retailers' terms
of service. Keep it to personal-scale use — the conservative concurrency
defaults exist for exactly this reason.

## License

MIT — see [LICENSE](LICENSE).
