"""The single normalised product shape every retailer adapter returns.

Four retailer adapters (hornbach, bauhaus, globus, obi) are built against this
module in parallel, so it has to be complete and unambiguous on its own —
nothing here should require reading an adapter to understand.

Kept **stdlib-only, no project imports**: obi's adapter talks plain HTTP via
httpx and must not be forced to pull in patchright (and, transitively, a
browser) just to build a :class:`Product`.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# the product model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Product:
    """One product, normalised from whatever shape a retailer's site returns.

    Fields:

    - ``retailer``: short lowercase source tag, e.g. ``"hornbach"``,
      ``"bauhaus"``, ``"globus"``, ``"obi"``. Never empty.
    - ``id``: the retailer's own product identifier (article/SKU number as
      that retailer exposes it — format varies per retailer, treat as an
      opaque string). Never empty; this is what `get_product`-style tools key
      on.
    - ``name``: product title as shown on the retailer's site. Never empty.
    - ``brand``: manufacturer/brand name, or ``None`` when the retailer does
      not expose one for this product (common for own-brand or unbranded
      items).
    - ``gtin``: the product's GTIN/EAN barcode, or ``None``. **Often absent
      outside hornbach** — most retailers here do not surface it on listing
      or even detail pages; do not treat a missing GTIN as a data-quality bug.
    - ``price``: current price as a float in ``currency``, or ``None`` when
      the retailer's page shows **no price at all** (e.g. "price on
      request", a discontinued listing, or a page that failed to render the
      price block). ``None`` means "no price was shown" — never coerce a
      missing price to ``0.0``, which would read as "free".
    - ``currency``: ISO 4217 code for `price`, e.g. ``"EUR"``. ``None`` when
      `price` is ``None`` (no price shown, so no currency to report either).
    - ``availability``: normalised stock status, typically the tail of a
      schema.org ``ItemAvailability`` value — e.g. ``"InStock"``,
      ``"OutOfStock"``, ``"LimitedAvailability"`` — see
      :func:`normalize_availability`. ``None`` when the retailer's page does
      not expose an availability signal at all.
    - ``url``: canonical/absolute URL of the product page. Never empty.
    - ``image``: absolute URL of the primary product image, or ``None`` when
      the retailer has none for this product.
    - ``store_pickup``: **local branch** availability — whether this specific
      product can be picked up at the retailer's relevant local branch
      (project-wide, that means the Braunschweig-area store where each
      retailer has one). ``True``/``False`` when the retailer's page states
      this explicitly; ``None`` whenever it does not — including, and
      especially, when the retailer **has no branch there at all** (OBI has
      no Braunschweig branch). ``None`` here means "unknown / not
      applicable", never "not available" — do not write ``False`` for a
      retailer that simply has no local branch to check; that would claim
      false negative stock information instead of admitting no local
      pickup signal exists.

    ``retailer``, ``id``, ``name`` and ``url`` are always populated by a
    correct adapter; every other field is legitimately optional and its
    ``None`` means exactly what its bullet above says — not "the adapter
    forgot to fill it in".
    """

    retailer: str
    id: str
    name: str
    brand: str | None
    gtin: str | None
    price: float | None
    currency: str | None
    availability: str | None
    url: str
    image: str | None
    store_pickup: bool | None

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form, e.g. for JSON serialisation in a tool response."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# shared parsing helpers
# --------------------------------------------------------------------------- #

# DIN 5008 permits a space as a German thousands-grouping separator, and
# rendered German price text commonly uses U+00A0 (NO-BREAK SPACE) or U+202F
# (NARROW NO-BREAK SPACE) instead of a plain ASCII space for exactly that —
# all three are recognised as grouping separators below.
_THOUSANDS_SPACES = "   "
_THOUSANDS_SPACE_TABLE = str.maketrans("", "", _THOUSANDS_SPACES)

# Matches the numeric portion of a price string, keeping any thousands/decimal
# separators (including the space family above) for parse_price to sort out:
# "22,95" / "1.234,56 €" / "19.99" / "1 234,56" / "12 345 678,90".
_PRICE_NUMBER_RE = re.compile(r"\d[\d.," + _THOUSANDS_SPACES + r"]*\d|\d")


def parse_price(value: str | int | float | None) -> float | None:
    """Parse a retailer price into a float, handling German string formatting
    and passing a JSON-sourced numeric price straight through.

    Accepts three shapes because retailers hand this three shapes:

    - ``str``: parsed as German-formatted text, in full below.
    - ``int``/``float``: returned as ``float(value)`` directly — no regex, no
      locale guessing, because a number that already came out of a JSON
      payload (schema.org's ``Offer.price`` permits a JSON Number as well as
      Text, and adapters that read a captured API response — e.g. Bauhaus —
      routinely get one) is already unambiguous; running it through the
      string parser below would be pure risk for zero benefit. ``NaN`` and
      infinity return ``None`` rather than a nonsense price. ``bool`` is
      rejected (returns ``None``) even though it is an ``int`` subclass in
      Python — ``parse_price(True)`` must not silently become ``1.0``.
    - ``None``: returns ``None`` (see below).

    String parsing: German notation uses ``,`` as the decimal separator and
    ``.`` as the thousands separator (``"1.234,56 €"`` -> ``1234.56``); a
    bare ``"22,95"`` -> ``22.95``. Currency symbols, whitespace and
    surrounding text are ignored — pass the whole price string, e.g.
    ``"22,95 €"`` or ``"UVP 1.234,56 €"``, not a pre-stripped number.

    All four retailers here are German sites, but English notation
    (``"1,234.56"``) is also handled correctly rather than silently
    misparsed, in case a retailer ever localises differently: when both
    ``,`` and ``.`` appear, whichever occurs **last** in the string is the
    decimal separator (its digits are the fractional part) and every
    earlier occurrence of either character is a thousands-grouping
    separator, dropped. This is deliberately by-position rather than "comma
    present -> assume German": treating both as always-German silently
    turned ``"1,234.56"`` into ``1.23456`` (1000x off) by stripping the
    decimal point along with the grouping comma — a wrong float with no
    signal that it's wrong, which is worse here than returning no price at
    all (see :class:`Product`.price).

    Also handles a space-grouped thousands separator (DIN 5008), whether
    written as a plain ASCII space, a NO-BREAK SPACE (U+00A0), or a NARROW
    NO-BREAK SPACE (U+202F) — rendered German price text commonly uses one
    of the latter two instead of an ASCII space: ``"1 234,56"`` -> ``1234.56``
    regardless of which of the three that space actually is.

    Returns ``None`` when `value` is ``None``, a ``bool``, non-finite
    (``NaN``/infinity), or a string that is empty or contains no
    recognisable number at all — callers should treat all of these the same
    as "no price shown" (see :class:`Product`.price), not as a parse error
    to surface.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int in Python - reject explicitly so
        # parse_price(True) doesn't silently become 1.0.
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if not isinstance(value, str):
        return None
    match = _PRICE_NUMBER_RE.search(value)
    if not match:
        return None
    # Space-family characters are always a thousands-grouping separator,
    # never a decimal one, so they can be dropped unconditionally before
    # the comma/dot decimal-separator logic below ever sees them.
    raw = match.group(0).translate(_THOUSANDS_SPACE_TABLE)
    has_comma = "," in raw
    has_dot = "." in raw
    if has_comma and has_dot:
        # Both separators present: the last one to occur is the decimal
        # separator; anything earlier is thousands grouping and gets
        # dropped. Covers German "1.234,56" (comma last) and English
        # "1,234.56" (dot last) alike.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif has_comma:
        # Only commas: German decimal comma, e.g. "22,95".
        raw = raw.replace(",", ".")
    elif has_dot and (
        raw.count(".") > 1 or len(raw.rsplit(".", 1)[-1]) not in (1, 2)
    ):
        # Only dots, and more than one, or a single dot not followed by 1-2
        # digits: cannot be a decimal point, so it's German thousands
        # grouping ("1.234" with no decimal part) — drop it. A single dot
        # followed by 1-2 digits (e.g. "19.99") is left alone as a decimal
        # point.
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


_SCHEMA_ORG_PREFIXES = ("https://schema.org/", "http://schema.org/", "schema:")


def normalize_availability(value: str | None) -> str | None:
    """Normalise a schema.org ``ItemAvailability`` value to its short form.

    ``"https://schema.org/InStock"`` -> ``"InStock"``; also accepts the
    ``http://`` and ``schema:`` variants some markup uses. A value that is
    not schema.org-prefixed is returned trimmed and as-is, so a retailer that
    exposes its own plain-text status (e.g. ``"Auf Lager"``) round-trips
    unchanged rather than being dropped.

    Returns ``None`` for ``None`` or an empty/whitespace-only string.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    for prefix in _SCHEMA_ORG_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value
