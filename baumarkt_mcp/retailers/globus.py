"""Globus Baumarkt (globus-baumarkt.de) retailer adapter.

=== Search URL — discovered empirically, 2026-08-06 ===

None of the URL patterns suggested up front (``/search?q=``, ``/suche?q=``,
``/suchergebnis?q=``, ``/de/search?q=``, ``/suche/?search=``,
``/search?ff-searchTerm=``, ``/?ff-searchTerm=``) work — each returns either
the homepage shell or a 404. The real pattern was found by driving a headed
browser to the homepage, typing a query into the site's own search box
(``input[type='search']``) and pressing Enter, then reading
``page.url`` after the site's own JS navigated:

    https://www.globus-baumarkt.de/search/result?query=<query>

confirmed both via the browser's own navigation for ``query=aluminium
flachprofil`` (landed on
``.../search/result?query=aluminium+flachprofil`` with 30 real results
rendered) and by loading that URL directly with a fresh ``page.goto()`` —
it renders results with no interaction needed, so no click-through is
required at request time. Pagination is ``&p=<n>`` (confirmed: page 1 and
``&p=2`` for ``query=schraube``, ~2400 hits, returned disjoint product-id
sets — no off-by-one, no page-1-repeated).

The site is Shopware 6 with a FactFinder (``ff-*`` custom elements) search
integration bolted on — everything under ``/search/result`` is normal
server-rendered Shopware storefront HTML once FactFinder's client-side JS
has populated it; ``/api/*`` is a separate, Cloudflare-protected surface
this adapter never touches (measured 403 there — do not use it).

=== Product identity / get_product() ===

There is no ID-only detail route (Shopware's SEO URL requires the exact
slug — a PDP URL with the right article number but a wrong slug 404s,
confirmed empirically). Instead, searching for the bare article number
(the same value as a card's ``data-product-id``, e.g. ``"0763240007"``)
against ``/search/result?query=<id>`` redirects straight to that product's
PDP when it uniquely matches — this is FactFinder/Shopware's normal
single-hit-redirects-to-detail behaviour, confirmed by observing
``page.url`` land on ``/p/alfer-flachprofil-aluminium-roh-blank-gelocht-
0763240007/`` after requesting ``?query=0763240007``. :func:`get_product`
uses exactly this lookup, with a same-page DOM fallback (matching
``data-product-id`` against the rendered search-result cards) for the rare
case a numeric query does not redirect uniquely.

=== Data source: JSON-LD on the PDP, DOM on the search listing ===

The PDP carries a ``schema.org/Product`` JSON-LD block with name, sku,
gtin13, brand, image, and a full ``offers`` block (price, priceCurrency,
availability) — this is what :func:`get_product` parses; it is far more
stable than any CSS selector. The search-*listing* page carries **no**
JSON-LD at all (checked: zero ``script[type='application/ld+json']`` tags)
and is FactFinder's own client-rendered markup, so :func:`search` parses
the DOM instead, via the ``_SELECTORS`` constant below. Consequently a
:class:`~baumarkt_mcp.models.Product` from :func:`search` has richer-than-
listing fields (``gtin``, ``store_pickup``) as ``None`` — call
:func:`get_product` for those.

=== store_pickup ===

The PDP's "In diesem Markt" widget (scoped under ``#markt-tab-pane``) is
static server-rendered HTML — no AJAX round trip needed at parse time — and
reflects whichever branch is bound to the browser context's
``_globus_markt_id`` cookie. On a fresh context (no cookie set yet) this
resolved, on this project's network, straight to store id ``212`` =
**Braunschweig** (confirmed via ``ctx.cookies()``) with no store-picker
interaction required — matching this project's Braunschweig-area
deployment, so the default is already the branch that matters here.
:func:`get_product` reads ``True``/``False`` off that widget's
``.delivery-status-indicator`` element (``bg-success`` / ``bg-danger``
class).

The adapter's ``store`` parameter is accepted for call-signature parity
with the sibling retailers but is **not wired to actual store-switching** —
the cookie pair is ``_globus_markt_id`` *and* ``_globus_markt_token``, and
the token looks server-issued (not just an id you can set unilaterally);
verifying a real switch was out of scope for this pass. Passing a non-None
``store`` logs a warning and is otherwise ignored — every result reflects
the context's default (Braunschweig) branch. Documented here rather than
silently no-op'd.

=== ProductGroup / hasVariant — checked, verified negative ===

Two sibling adapters (hornbach, obi) hit PDPs whose JSON-LD is a
``ProductGroup`` with a ``hasVariant`` list rather than a plain
``Product`` — schema.org's standard shape for a size/colour/length variant
family, and exactly the shape a cut-to-length aluminium profile (this
module's own worked example above) would plausibly use.

**Checked directly against Globus, not assumed**: every ``@type`` on every
JSON-LD block on 22 real Globus PDPs across 6 categories — aluminium
profiles (the exact worked-example category, including the 13-variant
"Ausführungen" family), wall paint (``wandfarbe``), floor/terrace tiles
(``fliesen``), screws (``schraube``), battens/boards (``dachlatte``), and
threaded rods (``gewindestange``) — was either ``BreadcrumbList`` or
``Product``. **``ProductGroup`` was never observed.** Globus's site
architecture gives every length/colour/finish variant its own fully
independent PDP (own slug, own ``data-product-id``, own direct ``Product``
JSON-LD, own price) rather than one parent page listing variants — which
also explains why the "13 Ausführungen" badge on a listing card does not
imply a ``ProductGroup`` PDP: it means 13 separate PDPs share one search
result card, not one PDP with 13 sub-entries.

Given that, `_extract_jsonld_product`/`_resolve_variant` below still handle
``ProductGroup`` defensively (should Globus ever serve one for some
category not sampled here) rather than assuming the negative holds
everywhere untested — mirroring the sibling adapters' pattern per review
feedback. Resolution matches the variant whose ``sku`` equals the
requested id and merges group-level fields *underneath* the matched
variant's own (never over it — a variant's own price must never be
shadowed by a group-level default). No sku match means `None`, never a
guessed "first variant" — a wrong variant's price is worse than reporting
not-found.

=== Listing price selector — a real, confirmed bug fixed here ===

The first version of this adapter used the descendant selector
``.product-price span``, taken via BeautifulSoup's ``select_one`` (first
match in document order). On any card with an active discount, that is
wrong: Globus renders the percentage-off badge as its *own* `<span>`
appearing *before* the actual price in the DOM —

    <div class="product-price with-list-price has-baseprice">
      <div class="list-price-wrapper">
        <span class="list-price-percentage ...">-37%</span>
        <span class="list-price" aria-label="Ursprünglicher Preis">29,99 €</span>
      </div>
      <span>19,00 €</span>
    </div>

— confirmed on a real, live card (Alpina Innenweiß Wandfarbe, found via
``query=wandfarbe``): the descendant selector matched ``"-37%"`` first,
and at the time this was found, ``parse_price("-37%")`` returned ``37.0``
— a wrong-but-entirely-plausible price (not the 19.00 EUR sale price, not
the 29.99 EUR RRP, not an error) with **no signal anything went wrong**.

``models.py``'s ``parse_price`` has since been hardened (prompted by this
exact finding) to refuse percentage strings and negative values outright
— ``parse_price("-37%")`` and ``parse_price("37%")`` now both return
``None``, as does e.g. ``parse_price("-5,00")``. That is defence-in-depth
at the shared parsing choke point, **not** a reason to relax the selector
below: a selector regression would now surface as ``search()`` silently
returning ``price=None`` for a discounted product instead of the actual
sale price — a visible gap rather than a wrong number, but still a
correctness failure this adapter should not rely on the parser alone to
catch. The selector fix (below) is what actually prevents either outcome.

Fixed by switching ``_SELECTORS["product_price"]`` to the direct-child
selector ``.product-price > span``, which is, in both the discounted and
plain card shapes actually observed, always exactly the one `<span>`
holding the current/sale price — confirmed against a broad sample (~180
cards across 7 queries plus the "Aktionen & Angebote" landing page) that
this is the only structural variant of ``.product-price`` Globus renders.
``self_check()`` at the bottom of this module pins this down with a
synthetic-HTML regression check against both shapes (see its docstring
for why it lives here instead of a proper test suite) — it asserts the
discounted card resolves to the 19.00 EUR sale price, so a selector
regression fails the check loudly rather than relying on the parser's
refusal to notice.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from patchright.async_api import Response

from baumarkt_mcp.browser import BrowserManager, fetch_page
from baumarkt_mcp.models import Product, normalize_availability, parse_price

log = logging.getLogger("baumarkt-mcp.globus")

RETAILER = "globus"
BASE_URL = "https://www.globus-baumarkt.de"


class GlobusParseError(RuntimeError):
    """globus-baumarkt.de served a page whose expected structure is missing.

    Raised when a search page never rendered its results container or
    empty-state block, a page advertised result cards but none could be
    parsed, or a resolved PDP carries no usable ``Product`` JSON-LD.
    Deliberately distinct from a genuine zero-result search (empty list) or
    an unknown article number (``None``) — a layout change must surface as
    an error, never silently read as "nothing found".
    """


# Generous but bounded — a cold FactFinder render plus Shopware storefront
# render is slower than a static page, but this must not hang a caller
# forever if the site stalls. (The per-navigation timeout itself is
# fetch_page's shared default — browser.py.)
_RESULTS_TIMEOUT_MS = 15_000

# Hard cap on how many search-result pages a single search() call will
# fetch, independent of `max_results`. Each page is a full browser
# round trip through a small, shared context pool (BM_MAX_CONCURRENT) —
# this bounds one caller's request from turning into a crawl.
_MAX_SEARCH_PAGES = 5

# --------------------------------------------------------------------------- #
# selectors / patterns — collected here so a markup change is a one-place fix
# --------------------------------------------------------------------------- #
_SELECTORS = {
    # Search listing (FactFinder-rendered Shopware storefront markup).
    "results_container": "div.cms-element-product-listing",
    # Server-rendered zero-hit page: measured live 2026-09-20, a search with
    # no matches renders this block INSTEAD of the listing container (the
    # container element does not exist at all on that page), so its presence
    # is the positive "genuinely no results" signal.
    "no_results_marker": ".no-results-cms-page",
    "product_card": "div.card.product-box.box-standard",
    "product_name": ".product-name",
    # Direct-child combinator, not a bare descendant selector — deliberate.
    # A discounted card's markup is:
    #   <div class="product-price with-list-price ...">
    #     <div class="list-price-wrapper">
    #       <span class="list-price-percentage ...">-37%</span>
    #       <span class="list-price" aria-label="Ursprünglicher Preis">29,99 €</span>
    #     </div>
    #     <span>19,00 €</span>            <-- the actual current/sale price
    #   </div>
    # A bare `.product-price span` descendant selector matches the "-37%"
    # span FIRST in document order. `parse_price` in models.py has since
    # been hardened to refuse a percentage string (`parse_price("-37%")`
    # now returns None, not the 37.0 it returned when this was found) as
    # defence-in-depth at the shared parser — but that is a backstop, not
    # a reason to relax this selector: a regression here would now make
    # `search()` silently return `price=None` for a discounted product
    # instead of its real sale price, a visible gap rather than a wrong
    # number, but still wrong. The real current price is always the one
    # `span` that is a *direct* child of `.product-price` (present, alone,
    # in both the discounted and non-discounted card shapes — confirmed
    # against live markup for both). `>` picks exactly that one and skips the
    # nested list-price-wrapper spans entirely.
    "product_price": ".product-price > span",
    "product_image_webp": "picture source[type='image/webp']",
    "product_image_fallback": "img.product-image",
    "product_link": "a[href]",
    "pagination_next": "li.page-item.page-next",
    "listing_datalayer_trigger": "[onclick*='createDataLayerForListings']",
    # PDP.
    "jsonld": "script[type='application/ld+json']",
    # Scoped under the "In diesem Markt" tab so this never picks up the
    # unrelated hidden cart-offcanvas template elsewhere on the page, which
    # reuses the same `.delivery-information`/`.delivery-status-indicator`
    # classes as an empty, JS-filled placeholder (bg-danger, "nicht
    # verfügbar", display:none ancestor) — matching on those classes
    # anywhere on the page would misread that placeholder as real data.
    "pdp_store_indicator": (
        "#markt-tab-pane .product-delivery-information .delivery-status-indicator"
    ),
}

# `onclick="createDataLayerForListings('<name>', '<brand>', ...)"` on both
# the image-wrapper and info divs of a listing card — the only place a
# listing card exposes brand. Empty string (own-brand/unbranded items) is
# treated as None by the caller, not as a parsed "".
_LISTING_BRAND_RE = re.compile(
    r"createDataLayerForListings\(\s*'[^']*'\s*,\s*'([^']*)'"
)

_PDP_PATH_RE = re.compile(r"^/p/[^/?#\s]+/?$")


def _build_search_url(query: str, page: int = 1) -> str:
    url = f"{BASE_URL}/search/result?query={quote_plus(query)}"
    if page > 1:
        url += f"&p={page}"
    return url


def _looks_like_pdp_url(url: str) -> bool:
    """True for a resolved URL on Globus's actual product-detail route."""
    if not isinstance(url, str) or url != url.strip() or any(
        char.isspace() for char in url
    ):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname != "www.globus-baumarkt.de"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        return False
    return _PDP_PATH_RE.fullmatch(parsed.path) is not None


async def _rendered_search_soup(
    page: Any, *, query: str, page_num: int
) -> tuple[Any, bool]:
    """Wait until the search page reaches one of its two ready states, then
    return its parsed DOM plus an emptiness verdict.

    The page is ready when either the results container has rendered (real
    results, populated client-side by FactFinder — waiting for *visible*
    means waiting for populated) or the server-rendered
    ``.no-results-cms-page`` empty-state block is present. Measured live
    2026-09-20: a zero-hit search renders the marker INSTEAD of the
    container — the container element does not exist at all on that page —
    so waiting on the container alone made every genuine zero-hit search
    look like a render failure.

    The verdict is made in the live DOM, not in the parsed HTML: only a
    *visibly rendered* marker counts as "no results" — a hidden marker
    template sitting in the markup next to a rendered listing (or vice
    versa) must not flip the result.

    Raises :class:`GlobusParseError` when neither appears within
    `_RESULTS_TIMEOUT_MS` — that is an unrecognized page, not an empty one.
    """
    union = f'{_SELECTORS["results_container"]}, {_SELECTORS["no_results_marker"]}'
    try:
        await page.wait_for_selector(union, timeout=_RESULTS_TIMEOUT_MS)
    except Exception as exc:
        # The marker can in principle sit in the DOM without being made
        # visible (its own markup carries no hiding class, but an ancestor
        # section might stay hidden). A marker that never actually renders
        # is not positive evidence of an empty result list — only a
        # *visibly rendered* empty state counts; anything else is a render
        # failure.
        marker = await page.query_selector(_SELECTORS["no_results_marker"])
        if marker is None or not await marker.is_visible():
            raise GlobusParseError(
                f"globus search page {page_num} for {query!r} never rendered "
                "its results container or empty-state block"
            ) from exc
        return BeautifulSoup(await page.content(), "lxml"), True
    # The union wait proves one of the two became visible; decide which in
    # the live DOM rather than in the parsed HTML, where visibility is lost.
    marker = await page.query_selector(_SELECTORS["no_results_marker"])
    empty = marker is not None and await marker.is_visible()
    return BeautifulSoup(await page.content(), "lxml"), empty


def _parse_listing_card(card: Tag) -> Product | None:
    """Build a `Product` from one search-result card, or None if malformed.

    Only the fields the listing actually exposes are populated —
    `gtin`, `availability` and `store_pickup` are legitimately `None` here
    (see module docstring); call `get_product` for those.
    """
    product_id = card.get("data-product-id")
    name_el = card.select_one(_SELECTORS["product_name"])
    name = name_el.get_text(strip=True) if name_el else None
    link_el = card.select_one(_SELECTORS["product_link"])
    href = link_el.get("href") if link_el else None
    if not product_id or not isinstance(product_id, str) or not name or not href:
        # A card missing any of these is not a usable result — skip it
        # rather than emit a Product violating the "never empty" fields.
        return None
    if not isinstance(href, str):
        return None
    url = urljoin(BASE_URL, href)

    price_el = card.select_one(_SELECTORS["product_price"])
    price = parse_price(price_el.get_text()) if price_el else None
    currency = "EUR" if price is not None else None

    image: str | None = None
    source_el = card.select_one(_SELECTORS["product_image_webp"])
    srcset = source_el.get("srcset") if source_el else None
    if isinstance(srcset, str) and srcset:
        image = srcset
    else:
        img_el = card.select_one(_SELECTORS["product_image_fallback"])
        if img_el is not None:
            candidate = img_el.get("src") or img_el.get("srcset")
            image = candidate if isinstance(candidate, str) else None

    brand: str | None = None
    onclick_el = card.select_one(_SELECTORS["listing_datalayer_trigger"])
    if onclick_el is not None:
        onclick_attr = onclick_el.get("onclick")
        if isinstance(onclick_attr, str):
            match = _LISTING_BRAND_RE.search(onclick_attr)
            if match and match.group(1).strip():
                brand = match.group(1).strip()

    return Product(
        retailer=RETAILER,
        id=product_id,
        name=name,
        brand=brand,
        gtin=None,
        price=price,
        currency=currency,
        availability=None,
        url=url,
        image=image,
        store_pickup=None,
    )


async def _parse_store_pickup(page) -> bool | None:
    el = await page.query_selector(_SELECTORS["pdp_store_indicator"])
    if el is None:
        return None
    class_attr = (await el.get_attribute("class")) or ""
    classes = class_attr.split()
    if "bg-success" in classes:
        return True
    if "bg-danger" in classes:
        return False
    return None


def _resolve_variant(group: dict, product_id: str) -> dict | None:
    """Pick the `hasVariant` entry matching `product_id` out of a ProductGroup.

    Deliberately does **not** fall back to "just take the first variant"
    when no sku matches — that risks silently returning a different
    variant's price as if it were the requested one, which is worse than
    reporting not-found (flagged in review against a sibling adapter that
    tried this). Group-level fields (name, brand, image, ...) are merged
    in *underneath* the matched variant's own fields, so a variant's own
    value always wins and a group-level default can never overwrite a
    variant's own price/offers/etc.
    """
    variants = group.get("hasVariant")
    if isinstance(variants, dict):
        variants = [variants]
    if not isinstance(variants, list):
        return None
    match = next(
        (
            v
            for v in variants
            if isinstance(v, dict) and str(v.get("sku")) == str(product_id)
        ),
        None,
    )
    if match is None:
        log.warning(
            "globus PDP: ProductGroup (sku=%r) has no hasVariant entry "
            "matching requested id=%r — refusing to guess which variant "
            "was requested",
            group.get("sku"),
            product_id,
        )
        return None
    merged = {**group, **match}
    merged.pop("hasVariant", None)
    merged["@type"] = "Product"
    return merged


def _extract_jsonld_product(raw_blocks: list[str], product_id: str) -> dict | None:
    """Find the PDP's product data among its JSON-LD `<script>` blocks.

    Handles two schema.org shapes seen across this project's retailers (see
    module docstring for what Globus itself was actually observed to
    serve): a plain ``Product`` block, used as-is; and a ``ProductGroup``
    block with a ``hasVariant`` list (schema.org's standard shape for a
    size/colour/length variant family) — resolved via `_resolve_variant`
    against the specific variant that was requested.
    """
    for text in raw_blocks:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        type_ = data.get("@type")
        if type_ == "Product":
            return data
        if type_ == "ProductGroup":
            return _resolve_variant(data, product_id)
    return None


async def _parse_pdp(page: Any, fallback_id: str) -> Product:
    """Parse the resolved PDP's Product JSON-LD (plus store-pickup widget).

    Raises :class:`GlobusParseError` when the resolved detail page carries no
    parseable ``Product``/``ProductGroup`` JSON-LD, or JSON-LD without the
    fields a `Product` cannot do without — a resolved PDP always carries
    them, so their absence is a structural failure, never "not found"
    (not-found is decided by the caller from the rendered search page before
    this is reached).
    """
    script_texts: list[str] = []
    for el in await page.query_selector_all(_SELECTORS["jsonld"]):
        try:
            script_texts.append(await el.inner_text())
        except Exception:  # noqa: BLE001 - a single bad script tag shouldn't abort parsing
            continue

    ld = _extract_jsonld_product(script_texts, fallback_id)
    if ld is None:
        raise GlobusParseError(
            f"globus PDP {page.url}: no parseable schema.org Product JSON-LD found"
        )

    name = ld.get("name")
    product_id = ld.get("sku") or fallback_id
    if not name or not product_id:
        raise GlobusParseError(
            f"globus PDP {page.url}: Product JSON-LD lacks name/sku "
            f"(name={name!r}, sku={ld.get('sku')!r})"
        )

    offer = ld.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    # offers.price is a JSON Number in this site's schema.org markup (seen:
    # 6.49, not "6,49 €") — parse_price() accepts int/float directly and
    # passes them straight through (no locale guessing needed), so hand it
    # the raw value rather than round-tripping through str().
    price = parse_price(offer.get("price"))
    currency = offer.get("priceCurrency") if price is not None else None

    brand: str | None = None
    brand_field = ld.get("brand")
    if isinstance(brand_field, dict):
        brand = brand_field.get("name") or None
    elif isinstance(brand_field, str):
        brand = brand_field or None

    gtin = ld.get("gtin13") or ld.get("gtin") or ld.get("gtin12") or ld.get("gtin8")

    image: str | None = None
    image_field = ld.get("image")
    if isinstance(image_field, dict):
        image = image_field.get("url")
    elif isinstance(image_field, str):
        image = image_field
    elif isinstance(image_field, list) and image_field:
        first = image_field[0]
        image = first.get("url") if isinstance(first, dict) else first

    store_pickup = await _parse_store_pickup(page)

    return Product(
        retailer=RETAILER,
        id=str(product_id),
        name=str(name),
        brand=brand,
        gtin=str(gtin) if gtin else None,
        price=price,
        currency=currency,
        availability=normalize_availability(offer.get("availability")),
        url=ld.get("url") or page.url,
        image=image,
        store_pickup=store_pickup,
    )


async def search(
    query: str,
    *,
    store: str | None = None,
    max_results: int = 20,
    manager: BrowserManager,
) -> list[Product]:
    """Search Globus Baumarkt for `query`, returning up to `max_results` products.

    Drives a pooled browser context to
    ``/search/result?query=<query>`` (and ``&p=<n>`` for further pages,
    fetched only as needed to satisfy `max_results`, capped at
    `_MAX_SEARCH_PAGES` pages total). See the module docstring for how this
    URL was discovered and why the listing is DOM-parsed rather than
    JSON-LD (the listing carries none).

    `store` is accepted for signature parity with the sibling adapters but
    is currently a no-op here — see the module docstring's "store_pickup"
    section. Every result reflects the browser context's default branch.

    Returns an empty list only for a positively-rendered zero-result search
    (the visibly rendered `.no-results-cms-page` block). A result container
    with no cards is a parse error on the first page; an empty later page is
    accepted only as the end of an already paginated result set. Raises
    :class:`GlobusParseError` when a search page never reaches a recognized
    ready state, or advertises result cards that then all fail to parse — a
    markup change must never read as "no results" —
    and `CaptchaRequired`/`ChallengeTimeout` if a bot-wall interstitial is
    detected and does not clear (see
    `baumarkt_mcp.browser.wait_for_challenge_clear`) — Globus's own HTML was
    not observed to be walled (see module docstring), so this is a defensive
    path, not the expected one.
    """
    if store is not None:
        log.warning(
            "globus.search: store=%r requested but store-switching is not "
            "implemented — results reflect the default (Braunschweig) branch",
            store,
        )

    def parse_search_page(
        page_num: int, remaining: int
    ) -> Callable[[Any, Response | None], Awaitable[tuple[list[Product], bool]]]:
        async def parse(page: Any, response: Response | None) -> tuple[list[Product], bool]:
            if response is not None and response.status >= 400:
                raise GlobusParseError(
                    f"globus search page {page_num} for {query!r} returned "
                    f"HTTP {response.status}"
                )
            soup, empty = await _rendered_search_soup(
                page, query=query, page_num=page_num
            )
            cards = soup.select(_SELECTORS["product_card"])
            if empty:
                return [], False  # visibly rendered zero-hit page
            if not cards:
                if page_num > 1:
                    # The prior page advertised another page; an empty
                    # later listing is the site's pagination terminator.
                    return [], False
                raise GlobusParseError(
                    f"globus search page {page_num} for {query!r} rendered "
                    "an empty results container without a visible "
                    "no-results marker"
                )

            products: list[Product] = []
            skipped = 0
            for card in cards:
                if len(products) >= remaining:
                    break
                try:
                    product = _parse_listing_card(card)
                except Exception:  # noqa: BLE001 - one bad card must not kill the page
                    skipped += 1
                    log.warning(
                        "globus: skipping malformed search card", exc_info=True
                    )
                    continue
                if product is None:
                    skipped += 1
                    continue
                products.append(product)
            if skipped:
                log.warning(
                    "globus: search page %d for %r: skipped %d unparseable "
                    "result cards",
                    page_num,
                    query,
                    skipped,
                )
            if not products:
                # The page advertised cards and not one of them parsed —
                # that is a markup change (or a degraded page), not "no
                # results".
                raise GlobusParseError(
                    f"globus search page {page_num} for {query!r} rendered "
                    f"{len(cards)} result cards but none parsed"
                )

            next_li = soup.select_one(_SELECTORS["pagination_next"])
            next_classes = next_li.get("class") or [] if next_li else []
            has_next = next_li is not None and "disabled" not in next_classes
            return products, has_next

        return parse

    results: list[Product] = []
    async with manager.context() as ctx:
        for page_num in range(1, _MAX_SEARCH_PAGES + 1):
            if len(results) >= max_results:
                break
            products, has_next = await fetch_page(
                ctx,
                _build_search_url(query, page_num),
                parse_search_page(page_num, max_results - len(results)),
            )
            results.extend(products)
            if not has_next:
                break

    return results[:max_results]


async def get_product(
    product_id: str,
    *,
    store: str | None = None,
    manager: BrowserManager,
) -> Product | None:
    """Fetch one product by its Globus article number (e.g. ``"0763240007"``).

    This is the same value as a search result's `Product.id` /
    a listing card's ``data-product-id``. Resolves it to a detail page via
    the single-hit search redirect described in the module docstring, then
    parses the PDP's schema.org JSON-LD (plus the store-pickup DOM widget,
    which JSON-LD does not carry).

    `store` is accepted for signature parity but currently a no-op — see
    the module docstring.

    Returns `None` only when the id *positively* does not resolve to a
    product: the result page renders its empty-state block, or renders
    non-empty result cards none of which carries the id. A page that fails to
    reach a recognized state, has an empty unmarked result container, or a
    resolved PDP with no usable Product JSON-LD, raises
    :class:`GlobusParseError` instead — "page did not render its product data"
    is a failure, not "no such product".
    """
    if store is not None:
        log.warning(
            "globus.get_product: store=%r requested but store-switching is "
            "not implemented — result reflects the default (Braunschweig) branch",
            store,
        )

    async def resolve_pdp(page: Any, response: Response | None) -> str | None:
        if response is not None and response.status >= 400:
            raise GlobusParseError(
                f"globus lookup for {product_id!r} returned HTTP {response.status}"
            )
        if _looks_like_pdp_url(page.url):
            # Unique-hit redirect: navigation already landed on the PDP.
            return page.url
        # Not a unique-hit redirect — look the id up in this page's own
        # rendered result cards (same ready-state rules as search()).
        soup, empty = await _rendered_search_soup(page, query=product_id, page_num=1)
        if empty:
            return None
        cards = soup.select(_SELECTORS["product_card"])
        if not cards:
            raise GlobusParseError(
                f"globus lookup for {product_id!r} rendered an empty results "
                "container without a visible no-results marker"
            )
        card = next(
            (
                c
                for c in cards
                if c.get("data-product-id") == product_id
            ),
            None,
        )
        if card is None:
            # Non-empty results rendered and this id simply isn't in them.
            return None
        link_el = card.select_one(_SELECTORS["product_link"])
        raw_href = link_el.get("href") if link_el else None
        href = raw_href if isinstance(raw_href, str) else ""
        if not href or href != href.strip() or any(char.isspace() for char in href):
            raise GlobusParseError(
                f"globus result card for {product_id!r} carries no usable "
                f"product link (href={href!r})"
            )
        pdp_url = urljoin(BASE_URL, href)
        if not _looks_like_pdp_url(pdp_url):
            # The card matching the requested id is there but its link is
            # not a usable product page ("#", "javascript:", a non-PDP
            # route) — a structural failure of a resolved match, not
            # "no such product".
            raise GlobusParseError(
                f"globus result card for {product_id!r} carries no usable "
                f"product link (href={href!r})"
            )
        return pdp_url

    async def parse_pdp(page: Any, response: Response | None) -> Product:
        if response is not None and response.status >= 400:
            raise GlobusParseError(
                f"globus PDP for {product_id!r} returned HTTP {response.status}"
            )
        return await _parse_pdp(page, product_id)

    async with manager.context() as ctx:
        pdp_url = await fetch_page(ctx, _build_search_url(product_id), resolve_pdp)
        if pdp_url is None:
            return None
        return await fetch_page(ctx, pdp_url, parse_pdp)


# --------------------------------------------------------------------------- #
# synthetic-HTML regression check
# --------------------------------------------------------------------------- #
#
# This project has no test suite (no `tests/` directory, no pytest
# dependency in pyproject.toml) and this adapter's declared write-set is
# this single file — adding either would be scope creep beyond that
# boundary. `self_check()` is the closest available substitute: a
# dependency-free, in-module regression guard against the two live-markup
# shapes that produced a real bug during review (see the module docstring's
# "Listing price selector" section), runnable directly with
#
#     python -m baumarkt_mcp.retailers.globus
#
# It intentionally does not touch the network — pure synthetic HTML fed
# through the same `_parse_listing_card`/`_SELECTORS["product_price"]` path
# `search()` uses, so a future markup-selector change that reintroduces the
# "-37%" bug (or an equivalent) fails loudly here instead of silently
# shipping a wrong price. If this project ever grows a real test suite,
# this belongs there instead of here.
def self_check() -> None:
    discounted_card_html = """
    <div class="card product-box box-standard" data-product-id="0765051896">
      <div class="card-body">
        <a href="/p/alpina-innenweiss-wandfarbe-weiss-matt-10-l-0765051896/">
          <div class="product-info" onclick="createDataLayerForListings(
              'Alpina Innenweiß Wandfarbe weiß matt 10 L', 'Alpina', 'Sortiment',
              'Farben', 'Wandfarbe', '', '')">
            <span class="product-name">Alpina Innenweiß Wandfarbe weiß matt 10 L</span>
            <div class="product-price-info d-flex">
              <div class="product-price with-list-price has-baseprice">
                <div class="list-price-wrapper">
                  <span class="list-price-percentage text-highlight"> -37%</span>
                  <span aria-label="Ursprünglicher Preis" class="list-price">29,99 &euro;</span>
                </div>
                <span>19,00 &euro;</span>
              </div>
              <p class="product-price-unit has-baseprice">
                <span class="product-unit-price">1,90 &euro;/l</span>
              </p>
            </div>
          </div>
        </a>
      </div>
    </div>
    """
    plain_card_html = """
    <div class="card product-box box-standard" data-product-id="0763240007">
      <div class="card-body">
        <a href="/p/alfer-flachprofil-aluminium-roh-blank-gelocht-0763240007/">
          <div class="product-info" onclick="createDataLayerForListings(
              'alfer Flachprofil Aluminium roh blank gelocht', 'alfer', 'Sortiment',
              'Werkzeug', 'Profile', '', '')">
            <span class="product-name">alfer Flachprofil Aluminium roh blank gelocht</span>
            <div class="product-price-info d-flex">
              <div class="product-price">
                <span>2,59 &euro;</span>
              </div>
            </div>
          </div>
        </a>
      </div>
    </div>
    """

    discounted_card = BeautifulSoup(discounted_card_html, "lxml").select_one(
        _SELECTORS["product_card"]
    )
    plain_card = BeautifulSoup(plain_card_html, "lxml").select_one(
        _SELECTORS["product_card"]
    )
    assert discounted_card is not None and plain_card is not None

    discounted_product = _parse_listing_card(discounted_card)
    assert discounted_product is not None, "discounted card failed to parse at all"
    assert discounted_product.price == 19.00, (
        f"discounted card: expected the 19,00 EUR sale price, got "
        f"{discounted_product.price!r} — the price selector is matching "
        f"the wrong <span> again. models.py's parse_price() now refuses a "
        f"percentage string, so a regressed selector matching the '-37%' "
        f"badge span would surface as price=None here rather than the "
        f"37.0 it used to produce; the 29,99 EUR list price is the other "
        f"wrong-but-plausible answer to watch for if it matches that span "
        f"instead"
    )

    plain_product = _parse_listing_card(plain_card)
    assert plain_product is not None, "plain (undiscounted) card failed to parse"
    assert plain_product.price == 2.59, (
        f"plain card: expected 2,59 EUR, got {plain_product.price!r}"
    )

    # ProductGroup/hasVariant resolution: variant's own field wins over the
    # group-level default, and a non-matching id returns None rather than
    # guessing.
    group = {
        "@type": "ProductGroup",
        "sku": "GROUP1",
        "name": "Group-level name (should never win)",
        "brand": {"@type": "Brand", "name": "GroupBrand"},
        "hasVariant": [
            {
                "@type": "Product",
                "sku": "VARIANT1",
                "name": "Variant One",
                "offers": {"price": 6.49, "priceCurrency": "EUR"},
            },
            {
                "@type": "Product",
                "sku": "VARIANT2",
                "name": "Variant Two",
                "offers": {"price": 99.99, "priceCurrency": "EUR"},
            },
        ],
    }
    resolved = _resolve_variant(group, "VARIANT1")
    assert resolved is not None
    assert resolved["name"] == "Variant One", (
        "variant's own name must win over group name"
    )
    assert resolved["offers"]["price"] == 6.49, (
        f"variant's own price must win over any group-level default, got "
        f"{resolved['offers']['price']!r} (99.99 would mean VARIANT2's "
        f"price leaked in; a group-level price would mean the merge "
        f"direction is backwards)"
    )
    assert resolved["brand"]["name"] == "GroupBrand", (
        "group-level fields the variant doesn't override should still come through"
    )
    assert _resolve_variant(group, "NO-SUCH-SKU") is None, (
        "a non-matching id must return None, never a guessed first variant"
    )

    print("globus.self_check(): all assertions passed")


if __name__ == "__main__":
    self_check()
