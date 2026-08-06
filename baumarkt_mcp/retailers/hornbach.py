"""Hornbach retailer adapter.

hornbach.de sits behind an F5/Fastly "Client Challenge" bot wall. Plain HTTP
(``httpx``) gets served a ~3 KB challenge body with an HTTP 200 — indistinguishable
from success by status code alone, but carrying no product data. Headless
Chromium escalates to an image CAPTCHA; headed patchright clears the challenge
effectively immediately (measured 2026-08-06). This adapter therefore always
drives a real, headed :class:`~baumarkt_mcp.browser.BrowserManager` context —
see that module's docstring for why headed and why patchright.

**No DOM scraping.** Both page types this adapter needs carry a schema.org
JSON-LD block:

- Search — ``https://www.hornbach.de/s/<url-encoded query>`` — an ``ItemList``
  whose ``itemListElement[].item`` are ``Product`` objects (search-result
  shape; no ``sku`` field of its own, only a canonical ``url`` with the sku as
  its trailing path segment).
- Detail — ``https://www.hornbach.de/p/<slug>/<sku>/`` — a single ``Product``
  object with ``sku``/``gtin13`` populated directly.

Every page actually observed live (2026-08-06) — including dimension/length
variant families that would be an obvious candidate for it — renders a flat
``Product``, never a ``ProductGroup``/``hasVariant`` wrapper (see
:func:`_resolve_product_object`'s docstring for what was checked). Handling
for that shape exists anyway, defensively, since a sibling adapter (obi) did
hit it live for the same kind of variant family.

Calling convention (matches every other retailer adapter in this project):
both public functions are coroutines, take the browser manager as a mandatory
**keyword-only** ``manager: BrowserManager`` argument, and borrow a pooled
context via ``async with manager.context() as ctx`` for the duration of the
call — never longer, so the pool slot is returned promptly.

``CaptchaRequired`` and ``ChallengeTimeout`` (both from
:mod:`baumarkt_mcp.browser`) are **not** caught here — they propagate to the
caller so a bot-wall failure is never silently reported as "no results". Only
``search()``/``get_product()`` should be awaited directly by callers that want
to distinguish those from an empty result.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from ..browser import BrowserManager, CaptchaRequired, ChallengeTimeout, wait_for_challenge_clear
from ..models import Product, normalize_availability, parse_price

log = logging.getLogger("baumarkt-mcp.retailers.hornbach")

RETAILER = "hornbach"
BASE_URL = "https://www.hornbach.de"


# --------------------------------------------------------------------------- #
# JSON-LD extraction — shared by search and detail pages
# --------------------------------------------------------------------------- #


async def _extract_ldjson_objects(page: Any) -> list[dict]:
    """Return every JSON-LD object on `page`, flattened out of ``@graph``.

    A page can carry several ``<script type="application/ld+json">`` blocks
    (BreadcrumbList, Organization, WebSite, as well as the one we want) and
    any one of them can be malformed or wrap its real payload in ``@graph``.
    Each script tag is parsed independently and a bad one is skipped with a
    warning rather than raising — one malformed block must never take down
    the whole search or product fetch.
    """
    try:
        raw_texts = await page.locator(
            'script[type="application/ld+json"]'
        ).all_text_contents()
    except Exception:  # noqa: BLE001 - defensive; page state can be odd mid-render
        log.warning("hornbach: could not read JSON-LD script tags", exc_info=True)
        return []

    objects: list[dict] = []
    for text in raw_texts:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("hornbach: skipping malformed JSON-LD block")
            continue

        candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                objects.extend(g for g in graph if isinstance(g, dict))
            else:
                objects.append(candidate)
    return objects


def _has_type(obj: dict, type_name: str) -> bool:
    """True if `obj`'s ``@type`` is or includes `type_name` (str or list form)."""
    t = obj.get("@type")
    if isinstance(t, list):
        return type_name in t
    return t == type_name


def _find_first_of_type(objects: list[dict], type_name: str) -> dict | None:
    for obj in objects:
        if _has_type(obj, type_name):
            return obj
    return None


def _resolve_product_object(obj: dict, target_sku: str | None) -> dict | None:
    """Normalise `obj` to a flat ``Product``-shaped dict, resolving a
    ``ProductGroup``/``hasVariant`` wrapper if that's what it is.

    Every hornbach page actually observed live (2026-08-06, including
    dimension/length-variant families like "Flachstange Alu ... 50x3 mm" at
    every length hornbach sells, paint colours, screw sizes, tiles) renders a
    flat ``Product`` — each variant gets its own sku *and* its own detail
    page, never a ``ProductGroup`` wrapper. This handling exists defensively
    in case hornbach ever does render one (the sibling OBI adapter hit this
    shape live), so a future product page doesn't silently come back as
    "not found".

    When `obj` is already a ``Product``, it is returned unchanged. When it's
    a ``ProductGroup``, the variant in ``hasVariant`` whose ``sku`` matches
    `target_sku` is selected. If no `target_sku` was given at all (the
    search-result case, where there is nothing yet to match against), the
    **first** variant is used as a representative, so a caller still gets *a*
    product for a group that genuinely exists rather than `None`.

    **A given `target_sku` that matches no variant returns `None` — it does
    NOT fall back to the first variant.** Silently substituting a different
    product's price/sku/availability for the one the caller explicitly asked
    for would be the wrong-price-labelled-as-the-right-product failure
    ``Product.price``'s own docstring in models.py warns against: a page
    that can't be resolved to the requested product must come back as "not
    found", never as a different product wearing the requested id. The
    first-variant fallback is only ever a reasonable default when there was
    no specific request to honour in the first place.

    The chosen variant's own fields (`sku`, `offers`, ...) win over the
    group's shared fields (`name`, `brand`, `image`, `url`, ...), which fill
    in whatever the variant leaves unset — this is schema.org's documented
    `ProductGroup`/`hasVariant` field-inheritance pattern.

    Returns ``None`` when `obj` is neither shape, is a ``ProductGroup`` with
    no usable variants, or is a ``ProductGroup`` with a `target_sku` that
    doesn't match any variant.
    """
    if _has_type(obj, "Product"):
        return obj
    if not _has_type(obj, "ProductGroup"):
        return None

    raw_variants = obj.get("hasVariant")
    variants = [v for v in raw_variants if isinstance(v, dict)] if isinstance(raw_variants, list) else []
    if not variants:
        return None

    if target_sku:
        target = target_sku.strip()
        chosen = None
        for variant in variants:
            sku = variant.get("sku")
            if isinstance(sku, str) and sku.strip() == target:
                chosen = variant
                break
        if chosen is None:
            # A specific product was requested and none of this group's
            # variants are it — report "not found", not a different variant.
            return None
    else:
        # No specific sku was requested (the search-result case): any
        # variant is a reasonable representative of the group.
        chosen = variants[0]

    merged = {k: v for k, v in obj.items() if k not in ("hasVariant", "@type")}
    merged["@type"] = "Product"
    merged.update(chosen)
    return merged


# --------------------------------------------------------------------------- #
# Product-object -> Product dataclass
# --------------------------------------------------------------------------- #


def _abs_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return BASE_URL + url
    return url


def _sku_from_url(url: str | None) -> str | None:
    """Hornbach product URLs end ``/p/<slug>/<sku>/`` — pull the sku segment."""
    if not url:
        return None
    segments = [s for s in url.split("/") if s]
    return segments[-1] if segments else None


def _extract_brand(obj: dict) -> str | None:
    brand = obj.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    if isinstance(brand, str) and brand.strip():
        return brand.strip()
    return None


def _extract_gtin(obj: dict) -> str | None:
    for key in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_image(obj: dict) -> str | None:
    image = obj.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        url = image.get("url")
        return _abs_url(url) if isinstance(url, str) else None
    if isinstance(image, str) and image.strip():
        return _abs_url(image)
    return None


def _normalize_offers(obj: dict) -> list[dict]:
    """schema.org allows ``offers`` to be a bare object, not just a list."""
    offers = obj.get("offers")
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    if isinstance(offers, dict):
        return [offers]
    return []


def _delivery_method(offer: dict) -> str:
    method = offer.get("availableDeliveryMethod")
    if not isinstance(method, str):
        return ""
    return method.rsplit("/", 1)[-1]


def _pick_representative_offer(offers: list[dict]) -> dict | None:
    """Choose the offer that best represents "the" price/availability.

    Prefers the parcel-delivery offer (applies regardless of a caller's
    location) over an on-site-pickup offer, since pickup is branch-specific
    and surfaced separately via `store_pickup`. Falls back to the first offer
    with a price, then the first offer at all.
    """
    for offer in offers:
        if _delivery_method(offer) == "ParcelService":
            return offer
    for offer in offers:
        if offer.get("price") is not None:
            return offer
    return offers[0] if offers else None


def _pick_pickup_offer(offers: list[dict], store: str | None) -> dict | None:
    """Find the on-site-pickup offer, optionally matching a specific branch.

    This only *filters among the pickup offers already present in the page's
    own JSON-LD* — nothing here sends a store cookie or query parameter to
    hornbach, so it cannot fetch or switch to another branch's real
    availability. In practice hornbach's server-rendered page carries at
    most one pickup offer (the visitor's default local branch — Braunschweig,
    branch code ``"615"``, for this project's residential connection).

    `store`, when given, is matched case-insensitively against either the
    branch's ``branchCode`` (e.g. ``"615"``) or its ``name`` (e.g.
    ``"HORNBACH Braunschweig"``). If it doesn't match the one pickup offer
    the page happens to carry, no offer is returned — the caller ends up
    with `store_pickup=None` ("unknown"), not a real lookup of that other
    branch's stock. When `store` is ``None``, the first (and, in practice,
    only) pickup offer on the page is used.
    """
    pickup_offers = [o for o in offers if _delivery_method(o) == "OnSitePickup"]
    if not store:
        return pickup_offers[0] if pickup_offers else None

    store_lower = store.strip().lower()
    for offer in pickup_offers:
        place = offer.get("availableAtOrFrom")
        if not isinstance(place, dict):
            continue
        code = str(place.get("branchCode") or "").lower()
        name = str(place.get("name") or "").lower()
        if store_lower == code or store_lower in name:
            return offer
    return None


def _derive_store_pickup(offers: list[dict], store: str | None) -> bool | None:
    """See ``Product.store_pickup`` in models.py: bool when explicitly stated,
    ``None`` when there is no pickup signal at all (no pickup offer present,
    e.g. the query asked for a branch Hornbach doesn't expose one for).
    """
    pickup_offer = _pick_pickup_offer(offers, store)
    if pickup_offer is None:
        return None
    availability = normalize_availability(pickup_offer.get("availability"))
    return availability == "InStock"


def _product_from_ldjson(obj: dict, *, fallback_url: str | None, store: str | None) -> Product | None:
    """Build a `Product` from one schema.org ``Product`` JSON-LD object.

    Returns ``None`` (rather than raising) when the object is missing a field
    a `Product` cannot do without (`name` or a resolvable `url`) — the caller
    is expected to skip and continue rather than let one bad item kill an
    entire search.
    """
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    url = _abs_url(obj.get("url") or obj.get("@id")) or _abs_url(fallback_url)
    if not url:
        return None

    sku = obj.get("sku")
    if not (isinstance(sku, str) and sku.strip()):
        sku = _sku_from_url(url)
    if not sku:
        return None

    offers = _normalize_offers(obj)
    representative = _pick_representative_offer(offers)

    # parse_price accepts str | int | float | None directly (models.py) —
    # schema.org permits Offer.price to be a JSON Number as well as Text, and
    # parse_price's numeric branch handles that exactly via float(); a str()
    # coercion here would route a number back through the text regex instead
    # and mis-parse anything that stringifies to scientific notation (e.g.
    # str(1e16) == "1e+16" -> the regex's first match is just "1" -> 1.0).
    price = parse_price(representative.get("price")) if representative else None
    currency = None
    if representative and price is not None:
        raw_currency = representative.get("priceCurrency")
        currency = raw_currency.strip() if isinstance(raw_currency, str) else None

    availability = (
        normalize_availability(representative.get("availability"))
        if representative
        else None
    )

    return Product(
        retailer=RETAILER,
        id=sku,
        name=name.strip(),
        brand=_extract_brand(obj),
        gtin=_extract_gtin(obj),
        price=price,
        currency=currency,
        availability=availability,
        url=url,
        image=_extract_image(obj),
        store_pickup=_derive_store_pickup(offers, store),
    )


# --------------------------------------------------------------------------- #
# public adapter API
# --------------------------------------------------------------------------- #


async def search(
    query: str,
    *,
    store: str | None = None,
    max_results: int = 20,
    manager: BrowserManager,
) -> list[Product]:
    """Search hornbach.de for `query` and return up to `max_results` products.

    `store`, if given, filters `Product.store_pickup` to a specific branch
    among whatever pickup offer the page's own JSON-LD happens to carry — it
    does not fetch that branch's real availability. See
    :func:`_pick_pickup_offer` for exactly what this can and can't do.

    Raises :class:`~baumarkt_mcp.browser.CaptchaRequired` or
    :class:`~baumarkt_mcp.browser.ChallengeTimeout` if the bot wall does not
    clear — deliberately not caught here, so a caller can tell "hit a bot
    wall" apart from "no results found" (an empty list).

    Returns an empty list, not an error, when the page loads cleanly but
    simply has no ``ItemList``/no matches for `query`.
    """
    # safe="" so a literal "/" in the query (ordinary hardware vocabulary,
    # e.g. "1/2 zoll rohr") is percent-encoded rather than surviving as an
    # extra path segment — quote()'s default safe="/" would silently split
    # the URL and turn a real query into an empty/garbage result.
    url = f"{BASE_URL}/s/{quote(query, safe='')}"
    async with manager.context() as ctx:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await wait_for_challenge_clear(page)

            objects = await _extract_ldjson_objects(page)
            item_list = _find_first_of_type(objects, "ItemList")
            if item_list is None:
                log.info("hornbach: no ItemList JSON-LD for query %r", query)
                return []

            elements = item_list.get("itemListElement")
            if not isinstance(elements, list):
                return []

            results: list[Product] = []
            for element in elements:
                if len(results) >= max_results:
                    break
                if not isinstance(element, dict):
                    continue
                item = element.get("item")
                if not isinstance(item, dict):
                    # Some ItemList shapes put the Product directly in the
                    # ListItem rather than nested under "item".
                    item = element
                # Resolves a Product as-is, or a ProductGroup down to its
                # first variant (no target sku to match against here — see
                # _resolve_product_object). None for anything else (e.g. a
                # plain dict that isn't either shape).
                resolved = _resolve_product_object(item, target_sku=None)
                if resolved is None:
                    continue
                try:
                    product = _product_from_ldjson(resolved, fallback_url=None, store=store)
                except Exception:  # noqa: BLE001 - one bad item must not kill the search
                    log.warning("hornbach: skipping malformed search result item", exc_info=True)
                    continue
                if product is not None:
                    results.append(product)
            return results
        finally:
            await page.close()


async def get_product(
    product_id: str,
    *,
    store: str | None = None,
    manager: BrowserManager,
) -> Product | None:
    """Fetch a single hornbach product by its sku (`product_id`).

    `product_id` is the retailer sku as returned in `Product.id` (e.g.
    ``"6179061"``); a full ``https://www.hornbach.de/...`` detail URL is also
    accepted and used as-is. A bare sku is resolved via
    ``https://www.hornbach.de/p/-/<sku>/``, which hornbach redirects to the
    canonical slugged detail URL (verified live 2026-08-06) without needing
    the slug at all.

    `store`, if given, filters `Product.store_pickup` to a specific branch
    among whatever pickup offer the page's own JSON-LD happens to carry — it
    does not fetch that branch's real availability. See
    :func:`_pick_pickup_offer` for exactly what this can and can't do.

    Raises :class:`~baumarkt_mcp.browser.CaptchaRequired` or
    :class:`~baumarkt_mcp.browser.ChallengeTimeout` if the bot wall does not
    clear — not caught here, same reasoning as `search`.

    Returns ``None`` (not an error) for a sku hornbach doesn't recognise
    (404, a page with no ``Product`` JSON-LD, or one whose JSON-LD fails to
    parse into a `Product`) rather than raising.
    """
    if product_id.startswith("http://") or product_id.startswith("https://"):
        url = product_id
    else:
        # safe="" — same reasoning as search(): a product_id is expected to
        # be a bare sku, but a non-numeric one must not silently produce a
        # malformed URL if it ever contains a "/".
        url = f"{BASE_URL}/p/-/{quote(product_id.strip(), safe='')}/"

    async with manager.context() as ctx:
        page = await ctx.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await wait_for_challenge_clear(page)

            if response is not None and response.status == 404:
                return None

            objects = await _extract_ldjson_objects(page)
            product_obj = _find_first_of_type(objects, "Product")
            if product_obj is None:
                # Not seen live on hornbach (see _resolve_product_object's
                # docstring), but fall back to a ProductGroup wrapper
                # defensively rather than reporting a real product as
                # missing.
                group_obj = _find_first_of_type(objects, "ProductGroup")
                if group_obj is not None:
                    target_sku = (
                        _sku_from_url(product_id) if url == product_id else product_id.strip()
                    )
                    product_obj = _resolve_product_object(group_obj, target_sku)
            if product_obj is None:
                log.info("hornbach: no Product/ProductGroup JSON-LD for product_id %r", product_id)
                return None

            try:
                return _product_from_ldjson(product_obj, fallback_url=page.url, store=store)
            except (CaptchaRequired, ChallengeTimeout):
                # Cannot actually be raised by _product_from_ldjson (it never
                # touches the page), but re-raised explicitly rather than
                # falling into the broad except below so a future change
                # can't accidentally start swallowing a bot-wall failure
                # into a misleading "not found".
                raise
            except Exception:  # noqa: BLE001 - must not raise; see docstring
                log.warning(
                    "hornbach: failed to parse Product JSON-LD for product_id %r",
                    product_id,
                    exc_info=True,
                )
                return None
        finally:
            await page.close()
