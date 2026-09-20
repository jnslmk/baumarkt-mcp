"""OBI retailer adapter — deliberately browser-free.

obi.de has no bot wall of any kind (verified 2026-08-06: a plain-HTTP request
with a browser-shaped ``User-Agent`` returns real search results). This
adapter therefore talks to it with :class:`httpx.AsyncClient` only — it must
**never** import ``browser.py`` or anything that pulls in ``patchright``, so
it stays importable and runnable in a process with no Chromium/Xvfb at all,
and so it never consumes a slot in the shared browser context pool the other
three (browser-backed) adapters share.

Two data sources are used, one per operation:

- ``search()`` parses the ``window.__INITIAL_STATE__`` JSON blob embedded in
  the search results page (``/search/<query>/`` — note the trailing slash;
  ``?query=``/``?q=``/``/suche?q=`` all 404). There is **no** JSON-LD on the
  search page, so this is the only structured source available there, and it
  is preferred over CSS-selector scraping because it survives markup
  redesigns.
- ``get_product()`` parses the schema.org ``Product`` JSON-LD block on the
  product detail page (``/p/<id>``), which is present there and gives a
  cleaner shape (including ``gtin13`` and a proper ``offers.availability``)
  than the search blob does.

OBI has no branch in the Braunschweig area (project-wide "local store" scope)
— it is an online-only price source here. Every :class:`Product` this
adapter returns has ``store_pickup=None`` unconditionally; never infer it
from the "market availability" fields OBI's pages expose for *some other*
market, and never fabricate a branch code.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

import httpx

from baumarkt_mcp.models import Product, normalize_availability, parse_price

log = logging.getLogger("baumarkt-mcp.retailers.obi")

RETAILER = "obi"
BASE_URL = "https://www.obi.de"

# Measured-working desktop Chrome UA (2026-08-06) — obi.de serves real markup
# to this; no challenge, no CAPTCHA.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RESULTS = 20

# obi.de image URLs come as a template with a literal "[PROFIL]" placeholder
# for the size/crop variant, e.g.
#   https://bilder.obi.de/<uuid>/[PROFIL]/image.jpeg
# "prZZK" is the 1500x1500 variant used on product detail pages themselves
# (confirmed live 2026-08-06); substituting it gives a real, working image
# URL instead of the literal placeholder.
_IMAGE_PROFILE = "prZZK"

_STATE_MARKER = "window.__INITIAL_STATE__='"
# The blob is a JS single-quoted string. Observed anomaly: literal quote
# characters inside its JSON content show up double-backslash-escaped
# (`\\"`, 3 raw chars) rather than the single-backslash JSON escape (`\"`,
# 2 raw chars) — apparently an artifact of how OBI serialises this blob, not
# something byte-for-byte JSON. `\'` (the string's own delimiter) is
# escaped normally. Both are repaired before handing the text to `json.loads`.
_ESCAPED_QUOTE_RE = re.compile(r"\\\\\"")
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


class ObiError(RuntimeError):
    """obi.de could not be queried or its response could not be trusted.

    Raised for a transport failure, a non-2xx response that isn't a
    recognised "no results" page, or a 2xx page whose expected embedded data
    (the ``__INITIAL_STATE__`` blob or the ``Product`` JSON-LD block) is
    missing or unparseable. Deliberately distinct from the ordinary
    "the search matched nothing" outcome, which is an empty list / ``None``,
    not an exception — callers need to tell "OBI is blocking/broken" apart
    from "OBI genuinely has nothing".
    """


# Full browser-like header set for a plain-HTTP client — obi.de serves real
# markup to a browser-shaped request, and a bare UA-only header set is the
# classic "easy bot" tell even where it still works (measured-working UA,
# 2026-08-06; the Accept value is what desktop Chrome sends for navigations).
_BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
}

# One process-scoped client, shared by every obi call — a fresh client per
# operation paid a fresh TCP/TLS handshake each time and, worse, looked to
# the far end like a new "browser" per request. Created lazily on first use
# (so the module stays usable outside the server lifespan) and closed from
# the server lifespan's shutdown (see ``aclose``). Per-request timeouts stay
# covered by the client-level default.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=DEFAULT_TIMEOUT,
        )
    return _client


async def start() -> None:
    """Eagerly create the process-scoped client. Called once from the server
    lifespan so startup owns the lifecycle; direct adapter callers that never
    run the lifespan get the same client lazily via :func:`_get_client`."""
    _get_client()


async def aclose() -> None:
    """Close the process-scoped client. Called from the server lifespan's
    shutdown; idempotent, and the next request simply builds a new client."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _extract_initial_state(html: str) -> dict[str, Any]:
    """Parse the ``window.__INITIAL_STATE__`` blob out of a search page."""
    start = html.find(_STATE_MARKER)
    if start == -1:
        raise ObiError("obi.de: __INITIAL_STATE__ blob not found in search page")
    start += len(_STATE_MARKER)
    end = html.find("</script>", start)
    if end == -1:
        raise ObiError("obi.de: __INITIAL_STATE__ blob has no closing </script>")
    body = html[start:end]
    # The JS statement is `...'<json>';` — strip the trailing `';` (or a
    # bare `'` if there's no semicolon) before the tag.
    body = body.rstrip()
    if body.endswith("';"):
        body = body[:-2]
    elif body.endswith("'"):
        body = body[:-1]
    body = body.replace("\\'", "'")
    body = _ESCAPED_QUOTE_RE.sub('\\"', body)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ObiError(
            f"obi.de: could not parse __INITIAL_STATE__ JSON: {exc}"
        ) from exc


def _map_online_availability(code: str | None) -> str | None:
    """Map OBI's own ``anzeigeOnlineBestellbarkeit`` codes to the shared vocabulary."""
    if not code:
        return None
    mapped = {
        "BESTELLBAR": "InStock",
        "NICHT_BESTELLBAR": "OutOfStock",
    }.get(code, code)
    return normalize_availability(mapped)


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return BASE_URL + url


def _product_from_search_item(item: dict[str, Any]) -> Product | None:
    """Build a Product from one entry of the search blob's ``ergebnisListe``.

    Returns ``None`` (rather than raising) for a malformed entry — callers
    skip it and keep the rest of the result list rather than let one bad
    item sink the whole search.
    """
    if not isinstance(item, dict):
        return None

    product_id = item.get("artikelNummer")
    name = item.get("artikelName")
    relative_url = item.get("detailseitenUrl")
    if not product_id or not name or not relative_url:
        return None
    product_id = str(product_id).strip()
    name = str(name).strip()
    if not product_id or not name:
        return None

    url = _absolute_url(str(relative_url))
    if not url:
        return None

    brand = item.get("markenName")
    brand = brand.strip() if isinstance(brand, str) and brand.strip() else None

    price_text = item.get("jsonLdPreis")
    price = parse_price(str(price_text)) if price_text is not None else None

    currency = None
    if price is not None:
        preise = item.get("preise") or {}
        primary = preise.get("preisPrimary") or {}
        currency = primary.get("waehrungsIsoCode") or "EUR"

    availability = _map_online_availability(item.get("anzeigeOnlineBestellbarkeit"))

    image = None
    bilder = item.get("bilder") or []
    if bilder and isinstance(bilder[0], dict):
        url_schema = bilder[0].get("urlSchema")
        if url_schema:
            image = url_schema.replace("[PROFIL]", _IMAGE_PROFILE)

    return Product(
        retailer=RETAILER,
        id=product_id,
        name=name,
        brand=brand,
        gtin=None,
        price=price,
        currency=currency,
        availability=availability,
        url=url,
        image=image,
        store_pickup=None,
    )


async def search(
    query: str,
    *,
    store: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[Product]:
    """Search obi.de for `query` and return up to `max_results` products.

    OBI has no Braunschweig-area branch (see module docstring), so it is an
    online-only source here: `store` is accepted only for signature
    symmetry with the browser-backed sibling adapters (t7 fans out over all
    four uniformly) and is otherwise ignored — every returned Product has
    `store_pickup=None`.

    An empty list means the search genuinely matched nothing (OBI itself
    distinguishes this with a dedicated "no results" page). Raises
    :class:`ObiError` when the page could not be fetched, or fetched but not
    trusted — a transport error, an unrecognised non-2xx response, or a 2xx
    page whose `__INITIAL_STATE__` blob is missing/unparseable/of
    unexpected shape, or a result list whose entries all failed to
    normalise. Malformed individual entries are skipped (and logged), never
    fatal while others parse.
    """
    query = query.strip()
    if not query or max_results <= 0:
        return []

    url = f"{BASE_URL}/search/{urllib.parse.quote(query, safe='')}/"
    client = _get_client()
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ObiError(f"obi.de: search request failed: {exc}") from exc

    if response.status_code == 404:
        # OBI serves a real HTTP 404 for a query with zero matches (not a
        # transport problem) — its own state blob names this explicitly.
        # Only trust that reading on this specific, recognised shape;
        # anything else on a 404 is treated as a genuine error.
        state = _extract_initial_state(response.text)
        try:
            error = state["pinia"]["suche"].get("error") or {}
        except (KeyError, TypeError) as exc:
            raise ObiError(
                f"obi.de: unexpected __INITIAL_STATE__ shape: {exc}"
            ) from exc
        if error.get("name") == "NullErgebnisSeiteError":
            return []
        raise ObiError(
            "obi.de: search returned HTTP 404 without a recognised no-results marker"
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ObiError(
            f"obi.de: search returned HTTP {response.status_code}"
        ) from exc

    state = _extract_initial_state(response.text)
    try:
        suche = state["pinia"]["suche"]
    except (KeyError, TypeError) as exc:
        raise ObiError(f"obi.de: unexpected __INITIAL_STATE__ shape: {exc}") from exc

    try:
        result_list = suche["suchergebnis"]["ergebnisListe"]
    except (KeyError, TypeError) as exc:
        raise ObiError(f"obi.de: unexpected __INITIAL_STATE__ shape: {exc}") from exc

    if not isinstance(result_list, list):
        raise ObiError("obi.de: ergebnisListe was not a list")

    products: list[Product] = []
    skipped = 0
    for item in result_list:
        if len(products) >= max_results:
            break
        try:
            product = _product_from_search_item(item)
        except Exception:  # noqa: BLE001 - one bad item must not sink the list
            skipped += 1
            log.warning("obi: skipping malformed search item", exc_info=True)
            continue
        if product is None:
            skipped += 1
            continue
        products.append(product)
    if skipped:
        log.warning(
            "obi: search for %r: skipped %d unparseable result entries",
            query,
            skipped,
        )
    if not products and result_list:
        # OBI returned entries and not one normalised — that is an
        # unexpected payload shape, not a successful empty search.
        raise ObiError(
            f"obi.de: search for {query!r} returned {len(result_list)} "
            "entries but none could be normalised"
        )
    return products


def _iter_ld_json_objects(html: str) -> list[Any]:
    objects: list[Any] = []
    for raw in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            objects.extend(data)
        elif isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                objects.extend(graph)
            else:
                objects.append(data)
    return objects


def _extract_product_ld_json(html: str, product_id: str) -> dict[str, Any] | None:
    """Find the `Product` JSON-LD block for `product_id` on a detail page.

    Most detail pages carry a top-level `@type: "Product"` block directly.
    Some (anything OBI sells in multiple sizes/variants, e.g. cut-to-length
    profiles) instead carry a `@type: "ProductGroup"` whose actual per-SKU
    data lives in its `hasVariant` list — the URL still resolves to one
    specific variant (`/p/<id>` redirects to that variant's canonical
    slug), so the matching entry is the one whose own `sku` equals
    `product_id`.

    `product_id` is always given by `get_product()` — there is no "just get
    me something off this page" caller — so a block whose `sku` does not
    match it is *never* an acceptable substitute: returning it would hand
    the caller a different product's price/availability/sku labelled as the
    one they asked for, silently. Returns ``None`` when nothing on the page
    matches, rather than guessing.
    """
    for obj in _iter_ld_json_objects(html):
        if not isinstance(obj, dict):
            continue
        if obj.get("@type") == "Product":
            if str(obj.get("sku", "")).strip() == product_id:
                return obj
        elif obj.get("@type") == "ProductGroup":
            has_variant = obj.get("hasVariant")
            if not isinstance(has_variant, list):
                continue
            for variant in has_variant:
                if (
                    isinstance(variant, dict)
                    and str(variant.get("sku", "")).strip() == product_id
                ):
                    return variant
    return None


def _first_image_url(image: Any) -> str | None:
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        url = image.get("url")
        return url if isinstance(url, str) and url else None
    if isinstance(image, str) and image:
        return image
    return None


def _first_offer(offers: Any) -> dict[str, Any]:
    if isinstance(offers, list):
        return offers[0] if offers and isinstance(offers[0], dict) else {}
    if isinstance(offers, dict):
        return offers
    return {}


def _product_from_ld_json(data: dict[str, Any], *, fallback_url: str) -> Product | None:
    product_id = data.get("sku")
    name = data.get("name")
    if not product_id or not name:
        return None
    product_id = str(product_id).strip()
    name = str(name).strip()
    if not product_id or not name:
        return None

    brand = data.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    brand = brand.strip() if isinstance(brand, str) and brand.strip() else None

    gtin = data.get("gtin13") or data.get("gtin") or data.get("gtin14")
    gtin = str(gtin).strip() or None if gtin else None

    offer = _first_offer(data.get("offers"))
    price_value = offer.get("price")
    price = parse_price(str(price_value)) if price_value is not None else None
    currency = offer.get("priceCurrency") if price is not None else None

    availability = normalize_availability(offer.get("availability"))

    image = _first_image_url(data.get("image"))

    url = _absolute_url(data.get("url") or offer.get("url")) or fallback_url

    return Product(
        retailer=RETAILER,
        id=product_id,
        name=name,
        brand=brand,
        gtin=gtin,
        price=price,
        currency=currency,
        availability=availability,
        url=url,
        image=image,
        store_pickup=None,
    )


async def get_product(product_id: str, *, store: str | None = None) -> Product | None:
    """Fetch a single OBI product by its article number.

    `product_id` is OBI's article number as exposed on a `Product` this
    adapter returned from `search()` (its `id`, equal to the page's `sku`).
    The URL slug after the id is cosmetic — OBI resolves `/p/<id>` to the
    canonical product page regardless of what (if anything) follows it, so
    no slug needs to be known ahead of time.

    `store` is accepted only for signature symmetry with the browser-backed
    sibling adapters and is otherwise ignored: OBI has no Braunschweig-area
    branch (see module docstring), so there is no store to scope this to,
    and the returned Product always has `store_pickup=None`.

    Returns ``None`` when OBI has no such product (its detail-page route
    answers a bad/unknown numeric id with HTTP 400 or 404, observed
    2026-08-06, and never with a `Product` JSON-LD block in that case).
    Raises :class:`ObiError` for a transport failure or an unrecognised
    non-2xx response, or for a 2xx page with no `Product` JSON-LD matching
    this id, or one that fails to build a usable `Product` -- distinct from
    "no such product".
    """
    product_id = product_id.strip()
    if not product_id:
        return None

    url = f"{BASE_URL}/p/{urllib.parse.quote(product_id, safe='')}"
    client = _get_client()
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ObiError(f"obi.de: product request failed: {exc}") from exc

    if response.status_code in (400, 404):
        # The recognised "no such product" answers — see docstring.
        return None
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ObiError(
            f"obi.de: product page returned HTTP {response.status_code}"
        ) from exc

    data = _extract_product_ld_json(response.text, product_id)
    if data is None:
        # A 2xx detail page that resolves this id always carries a matching
        # Product(ProductGroup-variant) JSON-LD block — its absence means a
        # layout change or a degraded page, which must surface as an error
        # rather than read as "no such product".
        raise ObiError(
            f"obi.de: product page for {product_id!r} returned HTTP "
            f"{response.status_code} without a matching Product JSON-LD block"
        )
    product = _product_from_ld_json(data, fallback_url=str(response.url))
    if product is None:
        raise ObiError(
            f"obi.de: Product JSON-LD for {product_id!r} lacks usable "
            "sku/name fields"
        )
    return product
