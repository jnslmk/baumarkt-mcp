"""BAUHAUS (bauhaus.info) retailer adapter.

bauhaus.info sits behind a Cloudflare managed challenge (Turnstile) plus its own
branded WAF block page — see :mod:`baumarkt_mcp.browser` for the shared
headed-Chromium/patchright machinery that gets past both. This module adds two
things on top of that shared machinery that are specific to bauhaus:

1. **The Turnstile challenge has two variants, and this module only lets one
   of them clear.** :func:`baumarkt_mcp.browser.wait_for_challenge_clear`
   polls the page title/body for the interstitial to go away on its own — it
   does not interact with the widget in any way, and neither does this
   module. Measured live on 2026-08-06: bauhaus most often serves a
   *passive* "Nur einen Moment…" interstitial that clears itself with no
   interaction, typically within a few seconds — presenting as a genuine
   headed browser and being let through passively is exactly what the other
   three adapters (and the sibling geizhals-mcp project) do, and it is what
   this module relies on. Sometimes, though, bauhaus instead serves the
   interactive "Sicherheitsprüfung ihrer Verbindung" checkbox variant, and
   that one does **not** clear passively — it was left sitting unclicked
   through 60+ seconds of polling in testing. **This module deliberately
   does not click it, dispatch synthetic events at it, or otherwise attempt
   to obtain a pass token from it.** Presenting as a real browser and being
   passively let through is one thing; scripting a human-like interaction
   with the challenge widget itself to defeat it is a different thing, and
   this project does not do the second one. When the interactive variant
   appears, :func:`wait_for_challenge_clear` times out and
   :func:`_clear_challenge` re-raises :class:`baumarkt_mcp.browser.ChallengeTimeout`
   with a message that says so explicitly — see point 4 below and
   :func:`search`/:func:`get_product`'s docstrings. That is the correct,
   intended outcome for that case, not a bug to work around: it makes
   bauhaus the least reliable of the four retailers (a caller can get a
   legible "blocked" instead of results), which is an accepted trade-off,
   not an oversight.

   One operationally useful thing observed alongside this: a
   :class:`baumarkt_mcp.browser.BrowserManager` context that has already
   cleared a challenge once (of either variant) stays cleared for
   later reuse — a cold context is more likely to draw the interactive
   variant and time out than a context the pool has already warmed up. This
   is exactly why ``BrowserManager`` pools/reuses contexts instead of
   creating one per request (see its module docstring); nothing in this
   module tries to force or hasten that warm-up (no extra retries, no
   context rotation to dodge the challenge) beyond what the pool already
   does on its own.

2. **The product-search JSON API 403s on any replay, even authenticated.**
   The Next.js frontend fetches results from
   ``/api/products?productIds=...&filter=...``, which returns clean JSON —
   but re-requesting that exact URL from inside an already-cleared page (same
   cookies, same ``cf_clearance``) gets the branded 403 page back, not JSON.
   Their WAF is checking more than the cookie (most likely a same-origin
   fetch/Sec-Fetch-* signal only the page's own JS request carries). So this
   module never constructs or replays that URL: it registers a
   ``page.on("response", ...)`` handler *before* navigating and reads
   whatever the page's own JS requests, exactly as instructed. A single
   search issues more than one ``/api/products`` call (observed: one before
   the ``selectedStore`` cookie is read client-side, one after, both with an
   identical product list) and results are dict-merged as they arrive rather
   than short-circuiting after the first response.

3. **The product-detail page does not surface price via any visible network
   response at all.** The primary product's data (name, price,
   availability...) is fetched *server-side* by bauhaus's own Next.js server
   during SSR and streamed into the initial HTML as a serialized React
   Server Component payload — the only client-side ``/api/products`` call
   observed on a detail page is for its "you might also like" carousel and
   never contains the page's own product id. The exact same
   ``product_price`` JSON shape the search API returns is present verbatim
   in that embedded payload (backslash-escaped, since it is itself a
   JSON-encoded string inside the RSC stream), so :func:`get_product` regexes
   it out of ``page.content()`` — see :func:`_embedded_price_for`. Detail
   pages also carry a schema.org ``Product`` JSON-LD block (name/brand/
   sku/url/image, but no ``offers`` — no price there either), used for
   everything except price/availability/store_pickup.

Store scoping: the site keys the storefront to a ``selectedStore`` cookie
(``storeId 607`` = Braunschweig, the only store this project has measured).
:func:`get_product` additionally reads ``/api/purchasability`` responses
(also captured via the same "let the page ask, don't ask yourself" pattern)
to get a real per-store pickup signal — its ``STORE``-kind result entry,
correlated back to the request URL's own ``storeId=`` query param. Search
results have no equivalent per-store signal: the only store-shaped field
``/api/products`` exposes (``online_purchasability``/``isOnlineReservable``)
was measured identical with and without a ``storeId`` query param, so it is
not trustworthy as a store-specific signal and ``store_pickup`` is left
``None`` for every search result (see :class:`baumarkt_mcp.models.Product`
on why ``None`` — not a guess — is the correct value when a signal isn't
actually there).

4. **Bot-wall failures propagate, they are not swallowed into an empty list.**
   Both :func:`search` and :func:`get_product` let
   :class:`baumarkt_mcp.browser.CaptchaRequired` and
   :class:`baumarkt_mcp.browser.ChallengeTimeout` propagate uncaught from
   :func:`baumarkt_mcp.browser.wait_for_challenge_clear` (via
   :func:`_clear_challenge`). Callers must distinguish those from a genuine
   zero-result search or a genuine 404 detail page — collapsing "the bot
   wall never cleared" into "no results" would be a silent lie about why
   nothing came back. When the timeout is recognisably the interactive
   checkbox variant (its title contains "sicherheitsprüfung"),
   :func:`_clear_challenge` re-raises the *same* ``ChallengeTimeout``
   instance type — so an ``except ChallengeTimeout`` in a caller still works
   unchanged — with a message that says plainly that bauhaus served an
   interactive challenge this adapter does not attempt to clear, rather than
   leaving a caller to infer that from a generic timeout string.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from baumarkt_mcp.browser import (
    BrowserManager,
    ChallengeTimeout,
    wait_for_challenge_clear,
)
from baumarkt_mcp.models import Product, normalize_availability, parse_price

log = logging.getLogger("baumarkt-mcp.bauhaus")

RETAILER = "bauhaus"
BASE_URL = "https://www.bauhaus.info"
SEARCH_PATH = "/suche/produkte"

# storeId 607 = Braunschweig — the only branch this project has measured.
# Carried both as the `selectedStore` cookie (read by the storefront to pick
# which store's data its own API calls ask for) and, for get_product's
# purchasability lookup, matched against the storeId= query param the page's
# own request ends up carrying.
DEFAULT_STORE_ID = "607"

# Confirmed live 2026-08-06: `/p/<id>` (no slug) resolves the canonical
# product page directly, no redirect needed. `/<url_friendly_name>/p/<id>` is
# the fuller canonical form search results expose; both work as `Product.url`.
_PRODUCT_PATH_NO_SLUG = "/p/{id}"
_PRODUCT_PATH_WITH_SLUG = "/{slug}/p/{id}"

# Confirmed live: article_media.images[0] (an asset id, e.g. "645180") maps to
# this CDN path — matches the `image` field bauhaus's own Product JSON-LD emits.
_IMAGE_URL_TEMPLATE = "https://media.cdn.bauhaus/m/{asset_id}/prod_medium_square.webp"

_API_PRODUCTS_MARKER = "/api/products"
_API_PURCHASABILITY_MARKER = "/api/purchasability"

# How long to keep accumulating captured responses, and how long a quiet gap
# (no new response) ends the wait early. Measured live: a search settles to 2
# `/api/products` calls within ~2s of the challenge clearing; a detail page's
# `/api/purchasability` calls land within ~1-2s. These windows leave headroom
# without making every call pay the full ceiling.
_ACCUMULATE_WINDOW_S = 8.0
_ACCUMULATE_QUIET_S = 1.5
_PURCHASABILITY_WINDOW_S = 5.0
_PURCHASABILITY_QUIET_S = 1.0

# The same `product_price` shape the /api/products JSON returns, but
# embedded backslash-escaped inside the detail page's server-rendered HTML
# (a React Server Component payload) — see module docstring point 3.
_EMBEDDED_PRICE_RE = re.compile(
    r'\\"product_price\\":\{\\"prices\\":\[(?P<prices>.*?)\],'
    r'\\"product_id\\":\\"(?P<pid>\d+)\\"\}',
    re.DOTALL,
)


# --------------------------------------------------------------------------- #
# bot-wall handling
# --------------------------------------------------------------------------- #


# Title marker for the interactive checkbox variant — see module docstring
# point 1. Used only to make a resulting ChallengeTimeout's message legible,
# never to decide whether to interact with the page.
_INTERACTIVE_CHALLENGE_TITLE_MARKER = "sicherheitsprüfung"


async def _clear_challenge(page: Any) -> None:
    """Block for real content via `browser.py` — no interaction with the
    challenge widget of any kind. See module docstring point 1 for why.

    Raises :class:`baumarkt_mcp.browser.CaptchaRequired` unchanged, and
    :class:`baumarkt_mcp.browser.ChallengeTimeout` either unchanged or
    re-raised with a message that says plainly that an interactive challenge
    was encountered and not attempted, when the timeout's own message
    indicates that variant (see :data:`_INTERACTIVE_CHALLENGE_TITLE_MARKER`).
    Either way this is the same exception type, so an existing
    ``except ChallengeTimeout`` in a caller keeps working — deliberately not
    swallowed into an empty result here, see module docstring point 4.
    """
    try:
        await wait_for_challenge_clear(page)
    except ChallengeTimeout as exc:
        if _INTERACTIVE_CHALLENGE_TITLE_MARKER in str(exc).lower():
            raise ChallengeTimeout(
                "bauhaus served an interactive Cloudflare Turnstile challenge "
                "(the checkbox variant, title contains "
                f"{_INTERACTIVE_CHALLENGE_TITLE_MARKER!r}) — this adapter "
                "deliberately does not attempt to clear it (see bauhaus.py "
                f"module docstring point 1). Original: {exc}"
            ) from exc
        raise


# --------------------------------------------------------------------------- #
# response accumulation
# --------------------------------------------------------------------------- #


async def _accumulate(responses: list[Any], *, window_s: float, quiet_s: float) -> None:
    """Wait for `responses` to stop growing, up to `window_s` total.

    Ends early once `quiet_s` has passed with no new capture (and at least
    one has already arrived) rather than always paying the full window —
    matches the measured settle time instead of a fixed sleep, see module
    constants.
    """
    start = time.monotonic()
    last_len = 0
    last_change = start
    while True:
        now = time.monotonic()
        if now - start >= window_s:
            return
        if len(responses) != last_len:
            last_len = len(responses)
            last_change = now
        elif last_len > 0 and (now - last_change) >= quiet_s:
            return
        await asyncio.sleep(0.25)


# --------------------------------------------------------------------------- #
# price / availability helpers
# --------------------------------------------------------------------------- #


def _extract_regular_price(
    price_entries: list[dict[str, Any]] | None,
) -> tuple[float | None, str | None]:
    """Pick the REGULAR price entry (or the first) from a `product_price.prices` list.

    Always goes through :func:`baumarkt_mcp.models.parse_price` rather than
    reading the already-numeric `amount` field directly, so this and the
    JSON-LD fallback path both go through the one shared parser.
    """
    if not price_entries:
        return None, None
    entry = next(
        (e for e in price_entries if e.get("price_type") == "REGULAR"),
        price_entries[0],
    )
    price_obj = entry.get("price") or entry.get("base_price") or {}
    # Prefer the German-formatted text (amount_i18n) so this and the JSON-LD
    # path both exercise parse_price's string branch identically; fall back
    # to the already-numeric `amount` (parse_price accepts int/float
    # directly — no manual str() needed, see models.py).
    raw = price_obj.get("amount_i18n")
    if raw is None:
        raw = price_obj.get("amount")
    value = parse_price(raw)
    if value is None:
        return None, None
    return value, price_obj.get("currency_iso")


def _bool_to_availability(value: bool | None) -> str | None:
    if value is None:
        return None
    return normalize_availability("InStock" if value else "OutOfStock")


def _embedded_price_for(html: str, product_id: str) -> tuple[float | None, str | None]:
    """Regex out a detail page's own `product_price` block — see module docstring point 3."""
    for match in _EMBEDDED_PRICE_RE.finditer(html):
        if match.group("pid") != product_id:
            continue
        try:
            entries = json.loads("[" + match.group("prices").replace('\\"', '"') + "]")
        except json.JSONDecodeError:
            continue
        return _extract_regular_price(entries)
    return None, None


# --------------------------------------------------------------------------- #
# JSON-LD fallback
# --------------------------------------------------------------------------- #


def _ld_json_blocks(html: str) -> list[Any]:
    soup = BeautifulSoup(html, "lxml")
    blocks: list[Any] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def _search_items_from_ld_json(html: str) -> list[dict[str, Any]]:
    """The search page's `ItemList` JSON-LD block, as a flat list of `Product` items."""
    for block in _ld_json_blocks(html):
        if isinstance(block, dict) and block.get("@type") == "ItemList":
            items = block.get("itemListElement") or []
            return [
                item["item"]
                for item in items
                if isinstance(item, dict) and isinstance(item.get("item"), dict)
            ]
    return []


def _product_ld_json_block(html: str) -> dict[str, Any] | None:
    """A detail page's lone `Product` JSON-LD block, or `None` if absent."""
    for block in _ld_json_blocks(html):
        if isinstance(block, dict) and block.get("@type") == "Product":
            return block
    return None


def _product_from_ld_json_item(
    item: dict[str, Any], *, store_pickup: bool | None = None
) -> Product | None:
    """Map a schema.org `Product` object (from either JSON-LD source) to `Product`.

    Covers the search page's `ItemList` items (which carry `offers` — price
    and availability included) and a detail page's lone `Product` block
    (which does not carry `offers` at all — price/currency/availability
    naturally come back `None` here in that case, and the caller is expected
    to fill them in separately, e.g. via :func:`_embedded_price_for`).

    Returns `None` when the item carries no usable `sku` — `Product.id` is
    contractually never empty (see models.py), so a card without one cannot
    be represented; callers skip it (search) or treat it as no-product
    (get_product).
    """
    offers = item.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    # schema.org Offer.price permits either Text or Number — parse_price
    # accepts both natively (see models.py), no str() coercion needed.
    price = parse_price(offers.get("price"))
    brand = item.get("brand")
    brand_name = brand.get("name") if isinstance(brand, dict) else brand
    pid = str(item.get("sku") or "")
    if not pid:
        return None
    url = item.get("url") or (
        f"{BASE_URL}{_PRODUCT_PATH_NO_SLUG.format(id=pid)}" if pid else BASE_URL
    )
    return Product(
        retailer=RETAILER,
        id=pid,
        name=item.get("name") or "",
        brand=brand_name,
        gtin=item.get("gtin13") or item.get("gtin") or None,
        price=price,
        currency=offers.get("priceCurrency") if price is not None else None,
        availability=normalize_availability(offers.get("availability")),
        url=url,
        image=item.get("image"),
        store_pickup=store_pickup,
    )


# --------------------------------------------------------------------------- #
# /api/products-derived mapping (search path)
# --------------------------------------------------------------------------- #


def _product_from_api(
    product_id: str,
    product: dict[str, Any],
    price_entry: dict[str, Any] | None,
    purchasability: dict[str, Any] | None,
) -> Product:
    name = product.get("frontend_name") or product.get("name") or ""
    brand = (product.get("brand") or {}).get("name")
    slug = product.get("url_friendly_name")
    url = (
        f"{BASE_URL}{_PRODUCT_PATH_WITH_SLUG.format(slug=slug, id=product_id)}"
        if slug
        else f"{BASE_URL}{_PRODUCT_PATH_NO_SLUG.format(id=product_id)}"
    )
    images = (product.get("article_media") or {}).get("images") or []
    image = _IMAGE_URL_TEMPLATE.format(asset_id=images[0]) if images else None

    price = currency = None
    if price_entry:
        entries = (price_entry.get("product_price") or {}).get("prices")
        price, currency = _extract_regular_price(entries)

    availability = None
    if purchasability is not None and "isOnlineOrderable" in purchasability:
        availability = _bool_to_availability(bool(purchasability["isOnlineOrderable"]))

    return Product(
        retailer=RETAILER,
        id=product_id,
        name=name,
        brand=brand,
        gtin=None,  # not exposed by /api/products — see Product.gtin docstring
        price=price,
        currency=currency if price is not None else None,
        availability=availability,
        url=url,
        image=image,
        # No confirmed store-specific signal at this granularity — see
        # module docstring on why this is always None for search results.
        store_pickup=None,
    )


# --------------------------------------------------------------------------- #
# purchasability (get_product path)
# --------------------------------------------------------------------------- #


async def _purchasability_from_responses(
    responses: list[Any], product_id: str, store_id: str
) -> tuple[str | None, bool | None]:
    """Derive (availability, store_pickup) from captured `/api/purchasability` responses.

    `availability` comes from any response's ``ONLINE``-kind result for this
    product. `store_pickup` only comes from a response whose own request URL
    carried this exact ``storeId`` — that is what ties a ``STORE``-kind
    result to *this* store rather than some other one, since the result
    object itself does not repeat the store id.
    """
    availability: str | None = None
    store_pickup: bool | None = None
    for response in responses:
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 - guard against a non-JSON body
            continue
        if not isinstance(body, dict):
            continue
        is_store_scoped = f"storeId={store_id}" in response.url
        for entry in body.get("results") or []:
            if not isinstance(entry, dict) or str(entry.get("product")) != product_id:
                continue
            purchasable = entry.get("purchasable")
            kind = entry.get("kind")
            if kind == "ONLINE" and availability is None:
                availability = _bool_to_availability(bool(purchasable))
            elif kind == "STORE" and is_store_scoped and store_pickup is None:
                store_pickup = bool(purchasable)
    return availability, store_pickup


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


async def search(
    query: str,
    *,
    store: str | int | None = None,
    max_results: int = 10,
    manager: BrowserManager,
) -> list[Product]:
    """Search bauhaus.info for `query`, returning up to `max_results` products.

    `store` selects the branch used for the `selectedStore` cookie (default
    :data:`DEFAULT_STORE_ID`, Braunschweig — the only branch measured for
    this project); it does not affect any field on the returned `Product`s
    (see module docstring — search results carry no confirmed per-store
    signal, so `store_pickup` is always `None` here regardless of `store`).

    Primary path: capture the page's own `/api/products` responses (never
    constructed or replayed directly — see module docstring point 2).
    Falls back to the search page's `ItemList` JSON-LD block if no
    `/api/products` response is captured within the accumulation window.

    Raises :class:`baumarkt_mcp.browser.CaptchaRequired` or
    :class:`baumarkt_mcp.browser.ChallengeTimeout` if bauhaus's bot wall
    never clears — these propagate uncaught, see module docstring point 4.
    In particular, if bauhaus serves the interactive Turnstile checkbox
    variant (module docstring point 1), this always raises
    `ChallengeTimeout` with an explicit "interactive challenge, not
    attempted" message rather than falling back to an empty result — that is
    the intended behaviour for that case, not a bug. Never raises on a
    genuine zero-result search; returns `[]` for that.
    """
    store_id = str(store) if store is not None else DEFAULT_STORE_ID
    # `text` is a query-string value, not a path segment — quote_plus (not
    # quote()'s default safe='/') so a query containing a slash (e.g. "1/2
    # zoll") is actually percent-encoded instead of silently producing a
    # malformed query value.
    search_url = f"{BASE_URL}{SEARCH_PATH}?text={urllib.parse.quote_plus(query)}"

    captured: list[Any] = []

    async with manager.context() as ctx:
        await ctx.add_cookies(
            [
                {
                    "name": "selectedStore",
                    "value": store_id,
                    "domain": ".bauhaus.info",
                    "path": "/",
                }
            ]
        )
        page = await ctx.new_page()
        try:
            page.on(
                "response",
                lambda r: captured.append(r) if _API_PRODUCTS_MARKER in r.url else None,
            )
            await page.goto(search_url, wait_until="domcontentloaded")
            await _clear_challenge(page)
            await _accumulate(
                captured, window_s=_ACCUMULATE_WINDOW_S, quiet_s=_ACCUMULATE_QUIET_S
            )

            if captured:
                return await _products_from_captured(captured, max_results)

            log.info(
                "bauhaus: no /api/products response captured for %r within %.1fs, "
                "falling back to JSON-LD",
                query,
                _ACCUMULATE_WINDOW_S,
            )
            html = await page.content()
            items = _search_items_from_ld_json(html)
            results = []
            for item in items[:max_results]:
                product = _product_from_ld_json_item(item)
                if product is not None:
                    results.append(product)
            return results
        finally:
            await page.close()


async def _products_from_captured(
    captured: list[Any], max_results: int
) -> list[Product]:
    """Merge captured `/api/products` responses into `Product`s, in search order."""
    products: dict[str, Any] = {}
    prices: dict[str, Any] = {}
    purchasabilities: dict[str, Any] = {}
    order: list[str] = []

    for response in captured:
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 - guard against a non-JSON body
            continue
        if not isinstance(body, dict):
            continue
        if not order:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            ids_param = qs.get("productIds", [""])[0]
            order = [pid for pid in ids_param.split(",") if pid]
        if isinstance(body.get("products"), dict):
            products.update(body["products"])
        if isinstance(body.get("prices"), dict):
            prices.update(body["prices"])
        if isinstance(body.get("purchasabilities"), dict):
            purchasabilities.update(body["purchasabilities"])

    ids = order or list(products.keys())
    results: list[Product] = []
    for pid in ids:
        product = products.get(pid)
        if product is None:
            continue
        results.append(
            _product_from_api(pid, product, prices.get(pid), purchasabilities.get(pid))
        )
        if len(results) >= max_results:
            break
    return results


async def get_product(
    product_id: str,
    *,
    store: str | int | None = None,
    manager: BrowserManager,
) -> Product | None:
    """Fetch a single bauhaus product by its id (the SKU shown on the site).

    `store` selects the branch for the `selectedStore` cookie and for
    `store_pickup` (default :data:`DEFAULT_STORE_ID`, Braunschweig).

    Navigates directly to ``/p/<product_id>`` (confirmed live to resolve the
    canonical product page with no slug needed). Data is assembled from
    several sources — see module docstring point 3 for why the detail page
    needs more than one:

    - name/brand/url/image/gtin: the page's `Product` JSON-LD block.
    - price/currency: regexed out of the embedded server-rendered payload
      (:func:`_embedded_price_for`), since JSON-LD carries no `offers` here.
    - availability/store_pickup: captured `/api/purchasability` responses
      (never constructed directly — same pattern as `/api/products`).
    - If a captured `/api/products` response happens to include this exact
      id (observed in practice to carry only cross-sell ids, but not
      guaranteed to stay that way), that structured data is preferred over
      the JSON-LD + regex combination above.

    Returns `None` if the id does not resolve to a real product page (no
    `Product` JSON-LD block found at all) rather than raising.

    Raises :class:`baumarkt_mcp.browser.CaptchaRequired` or
    :class:`baumarkt_mcp.browser.ChallengeTimeout` if bauhaus's bot wall
    never clears — these propagate uncaught, see module docstring point 4.
    As with :func:`search`, the interactive Turnstile checkbox variant
    (module docstring point 1) always raises `ChallengeTimeout` with an
    explicit "interactive challenge, not attempted" message rather than
    returning `None` — do not treat that case as "no such product".
    """
    store_id = str(store) if store is not None else DEFAULT_STORE_ID
    product_url = f"{BASE_URL}{_PRODUCT_PATH_NO_SLUG.format(id=product_id)}"

    api_products_responses: list[Any] = []
    purchasability_responses: list[Any] = []

    async with manager.context() as ctx:
        await ctx.add_cookies(
            [
                {
                    "name": "selectedStore",
                    "value": store_id,
                    "domain": ".bauhaus.info",
                    "path": "/",
                }
            ]
        )
        page = await ctx.new_page()
        try:

            def _on_response(r: Any) -> None:
                if _API_PRODUCTS_MARKER in r.url:
                    api_products_responses.append(r)
                elif _API_PURCHASABILITY_MARKER in r.url:
                    purchasability_responses.append(r)

            page.on("response", _on_response)
            await page.goto(product_url, wait_until="domcontentloaded")
            await _clear_challenge(page)
            await _accumulate(
                purchasability_responses,
                window_s=_PURCHASABILITY_WINDOW_S,
                quiet_s=_PURCHASABILITY_QUIET_S,
            )

            availability, store_pickup = await _purchasability_from_responses(
                purchasability_responses, product_id, store_id
            )

            api_product = api_price_entry = api_purch = None
            for response in api_products_responses:
                try:
                    body = await response.json()
                except Exception:  # noqa: BLE001 - guard against a non-JSON body
                    continue
                if not isinstance(body, dict):
                    continue
                products = body.get("products") or {}
                if product_id in products:
                    api_product = products[product_id]
                    api_price_entry = (body.get("prices") or {}).get(product_id)
                    api_purch = (body.get("purchasabilities") or {}).get(product_id)
                    break

            if api_product is not None:
                log.info(
                    "bauhaus: get_product id=%s from captured /api/products response",
                    product_id,
                )
                result = _product_from_api(
                    product_id, api_product, api_price_entry, api_purch
                )
                # Prefer the dedicated purchasability signal (real per-store
                # data) over /api/products's unconfirmed-store-specific one.
                if availability is not None:
                    result = _replace(result, availability=availability)
                if store_pickup is not None:
                    result = _replace(result, store_pickup=store_pickup)
                return result

            html = await page.content()
            ld_block = _product_ld_json_block(html)
            if ld_block is None:
                log.info(
                    "bauhaus: no Product JSON-LD found for id=%s — no such product",
                    product_id,
                )
                return None

            log.info(
                "bauhaus: get_product id=%s from Product JSON-LD + embedded payload",
                product_id,
            )
            result = _product_from_ld_json_item(ld_block, store_pickup=store_pickup)
            if result is None:
                log.info(
                    "bauhaus: Product JSON-LD for id=%s has no sku — no usable product",
                    product_id,
                )
                return None
            if result.price is None:
                price, currency = _embedded_price_for(html, product_id)
                if price is not None:
                    result = _replace(result, price=price, currency=currency)
            if availability is not None:
                result = _replace(result, availability=availability)
            return result
        finally:
            await page.close()


def _replace(product: Product, **changes: Any) -> Product:
    """Thin `dataclasses.replace` alias, used to patch a `Product` built from one
    source (JSON-LD / `/api/products`) with a field captured from another
    (`/api/purchasability`) without re-deriving the whole object."""
    return dataclasses.replace(product, **changes)
