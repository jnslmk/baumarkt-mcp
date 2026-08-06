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
import re
import urllib.parse
from typing import Any

import httpx

from baumarkt_mcp.models import Product, normalize_availability, parse_price

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


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=DEFAULT_TIMEOUT,
    )


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
    unexpected shape. Never lets one malformed result item fail the whole
    call — such items are skipped.
    """
    query = query.strip()
    if not query or max_results <= 0:
        return []

    url = f"{BASE_URL}/search/{urllib.parse.quote(query, safe='')}/"
    async with _make_client() as client:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ObiError(f"obi.de: search request failed: {exc}") from exc

    if response.status_code not in (200, 404):
        raise ObiError(f"obi.de: search returned HTTP {response.status_code}")

    state = _extract_initial_state(response.text)
    try:
        suche = state["pinia"]["suche"]
    except (KeyError, TypeError) as exc:
        raise ObiError(f"obi.de: unexpected __INITIAL_STATE__ shape: {exc}") from exc

    if response.status_code == 404:
        # OBI serves a real HTTP 404 for a query with zero matches (not a
        # transport problem) — its own state blob names this explicitly.
        # Only trust that reading on this specific, recognised shape;
        # anything else on a 404 is treated as a genuine error.
        error = suche.get("error") or {}
        if error.get("name") == "NullErgebnisSeiteError":
            return []
        raise ObiError(
            "obi.de: search returned HTTP 404 without a recognised "
            "no-results marker"
        )

    try:
        result_list = suche["suchergebnis"]["ergebnisListe"]
    except (KeyError, TypeError) as exc:
        raise ObiError(f"obi.de: unexpected __INITIAL_STATE__ shape: {exc}") from exc

    if not isinstance(result_list, list):
        raise ObiError("obi.de: ergebnisListe was not a list")

    products: list[Product] = []
    for item in result_list:
        if len(products) >= max_results:
            break
        try:
            product = _product_from_search_item(item)
        except Exception:  # noqa: BLE001 - one bad item must not sink the list
            continue
        if product is not None:
            products.append(product)
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
    `product_id`. Falls back to the first variant if none matches by sku
    (a defensive fallback, not expected to trigger given the URL already
    canonicalised to this id) rather than returning nothing for a page that
    plainly has product data.
    """
    plain_products: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    for obj in _iter_ld_json_objects(html):
        if not isinstance(obj, dict):
            continue
        if obj.get("@type") == "Product":
            plain_products.append(obj)
        elif obj.get("@type") == "ProductGroup":
            has_variant = obj.get("hasVariant")
            if isinstance(has_variant, list):
                variants.extend(v for v in has_variant if isinstance(v, dict))

    for product in plain_products:
        if str(product.get("sku", "")).strip() == product_id:
            return product
    for variant in variants:
        if str(variant.get("sku", "")).strip() == product_id:
            return variant

    if plain_products:
        return plain_products[0]
    if variants:
        return variants[0]
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


def _product_from_ld_json(
    data: dict[str, Any], *, fallback_url: str
) -> Product | None:
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


async def get_product(
    product_id: str, *, store: str | None = None
) -> Product | None:
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
    non-2xx response, or a 2xx page with no parseable `Product` JSON-LD at
    all -- distinct from "no such product".
    """
    product_id = product_id.strip()
    if not product_id:
        return None

    url = f"{BASE_URL}/p/{urllib.parse.quote(product_id, safe='')}"
    async with _make_client() as client:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ObiError(f"obi.de: product request failed: {exc}") from exc

    if response.status_code in (400, 404):
        return None
    if response.status_code != 200:
        raise ObiError(f"obi.de: product page returned HTTP {response.status_code}")

    data = _extract_product_ld_json(response.text, product_id)
    if data is None:
        return None
    return _product_from_ld_json(data, fallback_url=str(response.url))
