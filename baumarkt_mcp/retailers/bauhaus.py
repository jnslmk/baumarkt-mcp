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
from patchright.async_api import Response

from baumarkt_mcp.browser import (
    BrowserManager,
    ChallengeTimeout,
    fetch_page,
    wait_for_challenge_clear,
)
from baumarkt_mcp.models import Product, normalize_availability, parse_price

log = logging.getLogger("baumarkt-mcp.bauhaus")

RETAILER = "bauhaus"
BASE_URL = "https://www.bauhaus.info"
SEARCH_PATH = "/suche/produkte"


class BauhausParseError(RuntimeError):
    """bauhaus.info served a page whose expected data is missing.

    Raised when a search page neither captured its ``/api/products``
    responses nor carries an ``ItemList`` JSON-LD block nor shows its
    "Kein Ergebnis" empty state, when captured responses exist but none can
    be parsed, or when a resolved detail page lacks its ``Product`` JSON-LD
    or its embedded price payload. Deliberately distinct from a genuine
    zero-result search (empty list) or an unknown product id (``None``) — a
    layout change or a degraded page must surface as an error, never
    silently read as "nothing found".
    """


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

# Measured live 2026-09-20: a zero-hit search page renders "Kein Ergebnis
# für "<query>"" in the body and carries no ItemList JSON-LD and issues no
# /api/products request — this phrase is the positive "genuinely no results"
# signal, checked before an empty fallback result is returned.
_EMPTY_RESULTS_MARKER = "Kein Ergebnis"


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

    Returns :func:`baumarkt_mcp.browser.wait_for_challenge_clear`'s flag:
    ``True`` when a wall was present and cleared, ``False`` when the page
    never showed one (fetch_page uses this to refresh stale interstitial
    responses).
    """
    try:
        return await wait_for_challenge_clear(page)
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

    Responses that are not JSON or not the expected object shape are
    recorded (warning + count) rather than silently skipped. Raises
    :class:`BauhausParseError` when responses were captured but *none* of
    them parses — a response that cannot be read is a failure, not a
    missing signal. A parsed response that simply carries no matching
    entry leaves both values `None`.
    """
    availability: str | None = None
    store_pickup: bool | None = None
    parse_failures = 0
    for response in responses:
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 - guard against a non-JSON body
            parse_failures += 1
            log.warning(
                "bauhaus: captured /api/purchasability response to %s is "
                "not JSON — skipped",
                response.url,
            )
            continue
        if not isinstance(body, dict):
            parse_failures += 1
            log.warning(
                "bauhaus: captured /api/purchasability response to %s has "
                "unexpected shape (%s) — skipped",
                response.url,
                type(body).__name__,
            )
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
    if parse_failures == len(responses) and responses:
        raise BauhausParseError(
            f"bauhaus: captured {len(responses)} /api/purchasability "
            "responses but none could be parsed"
        )
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
    the intended behaviour for that case, not a bug.

    Returns `[]` only for a positively-recognized zero-result search — the
    page renders its "Kein Ergebnis" empty state (or the JSON-LD fallback
    finds an ItemList with no usable items). Any other failure to produce
    results — no `/api/products` captured, no ItemList, no empty-state
    marker, or captured responses that are all unparseable — raises
    :class:`BauhausParseError`: a page that resolved but produced neither
    data nor a recognized empty state is a structural failure, not "no
    results".
    """
    store_id = str(store) if store is not None else DEFAULT_STORE_ID
    # `text` is a query-string value, not a path segment — quote_plus (not
    # quote()'s default safe='/') so a query containing a slash (e.g. "1/2
    # zoll") is actually percent-encoded instead of silently producing a
    # malformed query value.
    search_url = f"{BASE_URL}{SEARCH_PATH}?text={urllib.parse.quote_plus(query)}"

    captured: list[Any] = []

    async def prepare(page: Any) -> None:
        # Response capture must be registered *before* navigation — see
        # module docstring point 2.
        page.on(
            "response",
            lambda r: captured.append(r) if _API_PRODUCTS_MARKER in r.url else None,
        )

    async def parse(page: Any, response: Response | None) -> list[Product]:
        if response is not None and not 200 <= response.status <= 299:
            # fetch_page refreshes stale interstitial responses, so this is
            # the page's real status — anything outside 2xx (error pages,
            # unexpected redirects) must never reach the empty-state
            # classification below and read as "no results".
            raise BauhausParseError(
                f"bauhaus search page for {query!r} returned HTTP {response.status}"
            )
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
        skipped = 0
        for item in items[:max_results]:
            product = _product_from_ld_json_item(item)
            if product is None:
                skipped += 1
                continue
            results.append(product)
        if skipped:
            log.warning(
                "bauhaus: search for %r: skipped %d unparseable JSON-LD items",
                query,
                skipped,
            )
        if items:
            if not results:
                # The page advertised ItemList items and not one parsed —
                # a markup change, not "no results".
                raise BauhausParseError(
                    f"bauhaus search page for {query!r} listed "
                    f"{len(items)} JSON-LD items but none parsed"
                )
            return results
        # No captured data and no ItemList: only a positively rendered
        # empty state may read as "no results".
        text = await page.inner_text("body")
        if _EMPTY_RESULTS_MARKER in text:
            log.info("bauhaus: confirmed zero hits for %r", query)
            return []
        raise BauhausParseError(
            f"bauhaus search page for {query!r} resolved but carries neither "
            f"/api/products data, an ItemList JSON-LD block, nor its "
            f"{_EMPTY_RESULTS_MARKER!r} empty state"
        )

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
        return await fetch_page(
            ctx, search_url, parse, prepare=prepare, clear_challenge=_clear_challenge
        )


async def _products_from_captured(
    captured: list[Any], max_results: int
) -> list[Product]:
    """Merge captured `/api/products` responses into `Product`s, in search order.

    Responses that are not JSON, not the expected object shape, or lack a
    ``products`` mapping are recorded (warning + count) rather than silently
    skipped. Raises :class:`BauhausParseError` when *no* captured response
    is a well-formed payload — the page advertised its product data and none
    of it is readable, which is a failure, not an empty result. A payload
    that explicitly carries an empty ``products`` mapping is a
    positively-empty result and yields `[]`.
    """
    products: dict[str, Any] = {}
    prices: dict[str, Any] = {}
    purchasabilities: dict[str, Any] = {}
    order: list[str] = []
    parse_failures = 0

    for response in captured:
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 - guard against a non-JSON body
            parse_failures += 1
            log.warning(
                "bauhaus: captured /api/products response to %s is not "
                "JSON — skipped",
                response.url,
            )
            continue
        if not isinstance(body, dict):
            parse_failures += 1
            log.warning(
                "bauhaus: captured /api/products response to %s has "
                "unexpected shape (%s) — skipped",
                response.url,
                type(body).__name__,
            )
            continue
        payload = body.get("products")
        if not isinstance(payload, dict):
            # A dict without a products mapping is not a "successful empty
            # search" — the real empty payload carries `products: {}` — so
            # anything else is an unparseable shape, not an empty one.
            parse_failures += 1
            log.warning(
                "bauhaus: captured /api/products response to %s carries no "
                "products mapping (%s) — skipped",
                response.url,
                type(payload).__name__,
            )
            continue
        if not order:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            ids_param = qs.get("productIds", [""])[0]
            order = [pid for pid in ids_param.split(",") if pid]
        products.update(payload)
        if isinstance(body.get("prices"), dict):
            prices.update(body["prices"])
        if isinstance(body.get("purchasabilities"), dict):
            purchasabilities.update(body["purchasabilities"])

    if parse_failures == len(captured):
        raise BauhausParseError(
            f"bauhaus: captured {len(captured)} /api/products responses but "
            "none could be parsed"
        )

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

    Returns `None` only when the id *positively* does not resolve to a
    product: the HTTP 404 bauhaus answers some unknown ids with, or —
    measured live 2026-09-20 — the redirect that takes an unknown
    ``/p/<id>`` off the PDP route onto a campaign landing page
    (``/ad/produkte``, HTTP 200). A page that stays on the PDP route but
    lacks its `Product` JSON-LD block or its embedded price payload raises
    :class:`BauhausParseError`: a resolved PDP always carries them, so
    their absence is a structural failure, never "no such product".

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

    async def prepare(page: Any) -> None:
        # Response capture must be registered *before* navigation — see
        # module docstring points 2 and 3.
        def _on_response(r: Any) -> None:
            if _API_PRODUCTS_MARKER in r.url:
                api_products_responses.append(r)
            elif _API_PURCHASABILITY_MARKER in r.url:
                purchasability_responses.append(r)

        page.on("response", _on_response)

    async def parse(page: Any, response: Response | None) -> Product | None:
        if response is not None and response.status == 404:
            return None
        if response is not None and not 200 <= response.status <= 299:
            # fetch_page refreshes stale interstitial responses, so this is
            # the page's real status — anything outside 2xx (error pages,
            # unexpected redirects) is a failure, not "no such product".
            raise BauhausParseError(
                f"bauhaus detail page for id={product_id} returned HTTP "
                f"{response.status}"
            )
        if "/p/" not in page.url:
            # Unknown ids redirect off the PDP route onto a campaign landing
            # page (measured live 2026-09-20: /p/<garbage> -> /ad/produkte,
            # HTTP 200) — a positive "no such product", not a parse failure.
            log.info(
                "bauhaus: id=%s did not resolve to a PDP (landed on %s)",
                product_id,
                page.url,
            )
            return None

        await _accumulate(
            purchasability_responses,
            window_s=_PURCHASABILITY_WINDOW_S,
            quiet_s=_PURCHASABILITY_QUIET_S,
        )

        availability, store_pickup = await _purchasability_from_responses(
            purchasability_responses, product_id, store_id
        )

        api_product = api_price_entry = api_purch = None
        for api_response in api_products_responses:
            try:
                body = await api_response.json()
            except Exception:  # noqa: BLE001 - guard against a non-JSON body
                log.warning(
                    "bauhaus: captured /api/products response to %s is not "
                    "JSON — skipped",
                    api_response.url,
                )
                continue
            if not isinstance(body, dict):
                log.warning(
                    "bauhaus: captured /api/products response to %s has "
                    "unexpected shape (%s) — skipped",
                    api_response.url,
                    type(body).__name__,
                )
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

        html = await page.content()
        if api_product is None:
            ld_block = _product_ld_json_block(html)
            if ld_block is None:
                raise BauhausParseError(
                    f"bauhaus PDP for id={product_id} ({page.url}) resolved "
                    "but carries no Product JSON-LD block"
                )
            log.info(
                "bauhaus: get_product id=%s from Product JSON-LD + embedded payload",
                product_id,
            )
            result = _product_from_ld_json_item(ld_block, store_pickup=store_pickup)
            if result is None:
                raise BauhausParseError(
                    f"bauhaus Product JSON-LD for id={product_id} has no "
                    "usable sku"
                )
        # else: result was already built from the captured /api/products
        # response above; both paths converge on the price requirement below.

        if result.price is None:
            price, currency = _embedded_price_for(html, product_id)
            if price is not None:
                result = _replace(result, price=price, currency=currency)
        if result.price is None:
            raise BauhausParseError(
                f"bauhaus PDP for id={product_id} resolved but carries no "
                "price in either /api/products or its embedded payload"
            )
        if availability is not None:
            result = _replace(result, availability=availability)
        return result

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
        return await fetch_page(
            ctx, product_url, parse, prepare=prepare, clear_challenge=_clear_challenge
        )


def _replace(product: Product, **changes: Any) -> Product:
    """Thin `dataclasses.replace` alias, used to patch a `Product` built from one
    source (JSON-LD / `/api/products`) with a field captured from another
    (`/api/purchasability`) without re-deriving the whole object."""
    return dataclasses.replace(product, **changes)
