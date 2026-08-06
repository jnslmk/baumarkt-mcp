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
# separators for parse_price to sort out. Two shapes, tried in this order:
#
#  1. **Space-grouped** (DIN 5008): a 1-3 digit head, then one or more groups
#     of EXACTLY three digits, each introduced by one of the space-family
#     characters above, then an optional decimal part —
#     "1 234,56" / "12 345 678,90" / "1 234.56" / "1 234".
#  2. **Space-free**: a digit run that may carry "." and "," in any
#     arrangement, left for the decimal-separator logic in parse_price to
#     interpret — or to fail on, as "1,23,45" does —
#     "22,95" / "1.234,56" / "19.99" / "1.234".
#
# Shape 1 must come first: shape 2's final `\d` alternative would otherwise
# match just the "1" of "1 234,56" and report a price of 1.
#
# The `++` possessive quantifier and the `(?!\d)` after it are both
# load-bearing, and cost a real bug when they were missing: a permissive
# "digits, dots, commas and spaces in any order" pattern joined ANY two
# space-separated digit runs into one number, so "PSB-1800 19,99 €" parsed as
# 180019.99 and "1 23,45" as 123.45. Requiring exact three-digit groups fixes
# the join; the possessive `++` stops the engine then salvaging a *prefix* of
# a malformed run by giving groups back ("1 2345,67" must not quietly become
# 1234). Together they mean a space that does not introduce a proper
# three-digit group is not a thousands separator at all: it terminates the
# match instead of being swallowed, and only the number in front of it is
# read.
_PRICE_NUMBER_RE = re.compile(
    r"\d{1,3}(?:[" + _THOUSANDS_SPACES + r"]\d{3})++(?!\d)(?:[.,]\d+)?"
    r"|\d[\d.,]*\d"
    r"|\d"
)

# Characters that make a number negative when they sit directly in front of it.
# U+2212 MINUS SIGN turns up in typeset discount badges alongside ASCII "-".
_MINUS_SIGNS = ("-", "−")


def _is_negative(value: str, number_start: int) -> bool:
    """Is the number starting at `number_start` in `value` negated?

    True only for a minus sign directly in front of the digits (any amount of
    whitespace between the two is fine, including the no-break variants):
    ``"-5,00"``, ``"- 5,00 €"``, ``"€ -5,00"``, ``"−5,00"``.

    A hyphen glued to the end of an alphanumeric token is punctuation inside
    that token, not a sign — ``"PSB-1800"`` is a model number, not minus one
    thousand eight hundred — so it does not count.
    """
    prefix = value[:number_start].rstrip()
    if not prefix.endswith(_MINUS_SIGNS):
        return False
    before_sign = prefix[:-1]
    return not (before_sign and before_sign[-1].isalnum())


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

    That grouping is **strict**: a space only separates thousands when it is
    followed by exactly three digits, in a properly repeating group
    (``"12 345 678,90"`` -> ``12345678.9``). A space in any other position is
    not a separator at all — it ends the number, and only the part in front
    of it is read. So ``"1 23,45"`` -> ``1.0`` and ``"1 2345,67"`` -> ``1.0``
    (``"23"`` and ``"2345"`` are not thousands groups), and two unrelated
    numbers that happen to sit either side of a space are never fused:
    ``"PSB-1800 19,99 €"`` -> ``1800.0``, the first number in the string, not
    the concatenation ``180019.99`` that a permissive rule produced. Reading
    the leading number of a malformed run is the same "first number in the
    string wins" behaviour that surrounding text already gets (``"UVP
    1.234,56 €"``); inventing a value out of two spliced numbers is not.

    **A string containing ``%`` anywhere is never a price and returns
    ``None``** — ``"-37%"``, ``"-37 %"`` (with any of the three spaces
    above), ``"37%"``, ``"Sparen Sie 20%"``. No legitimate price string
    contains a percent sign, so its presence is decisive rather than
    something to parse around. This is not hypothetical: a listing-card
    selector matched a ``-37%`` discount badge ahead of the price element,
    and this function turned that into a confident ``37.0``, which was then
    reported as a product's price. A percentage reaching here means the
    caller read the wrong element; the honest answer is "no price", which
    :class:`Product`.price already defines a meaning for.

    **A negative number in a price *string* is likewise rejected rather than
    returned signed**: ``"-5,00"``, ``"- 5,00 €"``, ``"€ -5,00"`` and the
    U+2212 MINUS SIGN spelling all return ``None``. (This is a string-parsing
    rule only — the ``int``/``float`` branch above is untouched and still
    passes a JSON-sourced ``-5`` through as ``-5.0``, because a JSON Number
    is an unambiguous statement of the value rather than scraped text that
    might be the wrong element.) A retail price is never below zero, so a
    leading minus is always *something else* — a discount amount ("5 € off"),
    a price delta in a comparison table, or a badge — and every one of those
    is a different quantity that happens to be rendered near the price.
    Returning ``-5.0`` would hand a price-comparison tool a number no
    comparison can be right about (it sorts below every real price and makes
    any total nonsense), while silently dropping the sign to ``5.0`` — what
    this used to do — invents a price that was never on the page. ``None``
    is the only one of the three that does not assert something false.
    A hyphen that is part of a surrounding token is *not* a sign and is left
    alone, so a model number such as ``"PSB-1800"`` parses exactly as before;
    only a minus with a non-alphanumeric character (or nothing) in front of
    it counts. Note this makes the German "even euros" form ``"19,-"``
    unaffected — that hyphen trails the digits rather than leading them.

    Returns ``None`` when `value` is ``None``, a ``bool``, non-finite
    (``NaN``/infinity), or a string that is empty, contains a ``%``, holds a
    negative number, or contains no recognisable number at all — callers
    should treat all of these the same as "no price shown" (see
    :class:`Product`.price), not as a parse error to surface.
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
    if "%" in value:
        # A percentage is not a price, and no price string contains a "%" —
        # so this is decisive wherever it appears and whatever spacing sits
        # in front of it ("-37%", "-37 %", "-37 %", "Sparen Sie 20%").
        # Parsing it out would yield a confidently wrong price; see docstring.
        return None
    match = _PRICE_NUMBER_RE.search(value)
    if not match:
        return None
    if _is_negative(value, match.start()):
        # Negative -> not a price but a discount/delta rendered near one.
        # Rejected rather than returned as -5.0 or (worse) sign-stripped to
        # 5.0; see the docstring for why None is the only honest answer.
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
