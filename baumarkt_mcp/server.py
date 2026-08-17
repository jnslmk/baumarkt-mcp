"""FastMCP server exposing the four Baumarkt retailer adapters as tools.

Owns the process-wide browser lifecycle — one :class:`BrowserManager` started
once in the lifespan and closed on shutdown, never per request — plus the
tool surface. All scraping lives in ``baumarkt_mcp.retailers.*``; this module
only routes calls to it.

Tools:

- ``search_products`` — keyword search, either one retailer or a fan-out
  over all four.
- ``get_product`` — one product by id.
- ``compare_price`` — fan-out over all four, side by side, cheapest first
  (the "who sells this cheapest, and can I collect it locally today" tool;
  every result carries a ``store_pickup`` field).

Fan-out contract (learned the hard way in the sibling geizhals-mcp /
kleinanzeigen-mcp servers): a failing retailer must NEVER fail the call.
Every fan-out returns the retailers that succeeded plus a per-retailer
``errors`` entry for each that failed — and a challenge/error is never
collapsed into an empty result list, so "bauhaus is behind its Cloudflare
wall" reads as an explicit error entry, not as "bauhaus has nothing".
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from baumarkt_mcp import __version__
from baumarkt_mcp.browser import (
    MAX_CONCURRENT,
    BrowserManager,
    CaptchaRequired,
    ChallengeTimeout,
    PoolExhausted,
)
from baumarkt_mcp.models import Product
from baumarkt_mcp.retailers import bauhaus, globus, hornbach, obi

log = logging.getLogger("baumarkt-mcp")

# Search results go straight into an LLM context window, and a fan-out over
# all four retailers multiplies the count — cap how much a single call can
# ask each retailer for.
MAX_RESULTS = int(os.getenv("BM_MAX_RESULTS", "50"))

_browser: BrowserManager | None = None
_browser_gate: asyncio.Semaphore | None = None


def _manager() -> BrowserManager:
    if _browser is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Browser manager is not running")
    return _browser


def _gate() -> asyncio.Semaphore:
    if _browser_gate is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Concurrency gate is not initialised")
    return _browser_gate


# --------------------------------------------------------------------------- #
# retailer registry + per-retailer degradation
# --------------------------------------------------------------------------- #

_RETAILERS: dict[str, Any] = {
    "hornbach": hornbach,
    "bauhaus": bauhaus,
    "globus": globus,
    "obi": obi,
}

# Browser-backed adapters share the one BrowserManager pool and run under the
# process-wide gate; obi is httpx-only and must never be gated — serialising
# the one cheap retailer behind the browser pool would just slow it down.
_BROWSER_RETAILERS = frozenset({"hornbach", "bauhaus", "globus"})


def _error_for(retailer: str, exc: BaseException) -> str:
    """Human-readable failure for one retailer.

    "Blocked" must never read as "no results": a challenge/error entry is a
    different answer from an empty shelf, and this wording keeps it that way
    in the tool output.
    """
    if isinstance(exc, CaptchaRequired):
        return f"blocked by a CAPTCHA: {exc}"
    if isinstance(exc, ChallengeTimeout):
        return f"blocked by a challenge: {exc}"
    if isinstance(exc, PoolExhausted):
        return f"browser pool exhausted: {exc}"
    return str(exc) or exc.__class__.__name__


async def _degrade(retailer: str, call: Coroutine[Any, Any, Any]) -> dict[str, Any]:
    """Run one retailer call; a failure becomes an error entry, never a raised batch."""
    try:
        products = await call
    except Exception as exc:  # noqa: BLE001 - per-retailer degradation is the contract
        log.warning("retailer %s failed: %s", retailer, exc)
        return {
            "retailer": retailer,
            "error": _error_for(retailer, exc),
            "products": [],
        }
    return {
        "retailer": retailer,
        "error": None,
        "products": [p.to_dict() for p in products],
    }


async def _collect(
    calls: list[tuple[str, Coroutine[Any, Any, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Fan out, then split the outcomes into flat results and per-retailer errors."""
    outcomes = await asyncio.gather(*(_degrade(r, c) for r, c in calls))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for outcome in outcomes:
        if outcome["error"] is not None:
            errors.append({"retailer": outcome["retailer"], "error": outcome["error"]})
        else:
            results.extend(outcome["products"])
    return results, errors


async def _search_one(
    retailer: str, query: str, store: str | None, max_results: int
) -> list[Product]:
    """Search one retailer; browser-backed ones run under the concurrency gate."""
    module = _RETAILERS[retailer]
    if retailer in _BROWSER_RETAILERS:
        async with _gate():
            return await module.search(
                query, store=store, max_results=max_results, manager=_manager()
            )
    return await module.search(query, store=store, max_results=max_results)


def _coerce_int(
    value: str | int | None, field: str, *, ge: int | None = None
) -> int | None:
    """Coerce the numeric strings LLMs routinely send for int parameters.

    FastMCP validates tool input against the JSON schema before the function
    runs, so a parameter typed ``int`` rejects the string ``"600"`` outright
    (the same bug kleinanzeigen-mcp 0.1.1 fixed for its price params).
    Accepting ``str | int`` in the schema and normalising here keeps the
    model-facing contract lenient while the adapters still see a real int.
    """
    if value is None or isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} must be an integer, got {value!r}") from exc
    else:
        raise ValueError(f"{field} must be an integer, got {value!r}")
    if ge is not None and result is not None and result < ge:
        raise ValueError(f"{field} must be >= {ge}, got {result}")
    return result


def _resolve_max_results(
    max_results: str | int | None, limit: str | int | None
) -> int:
    """Accept either name for the per-retailer result cap, on both search tools.

    Across this project's sibling MCP servers the same knob has two different
    names — ``max_results`` here and in geizhals-mcp, ``limit`` in
    aliexpress-mcp, ebay-mcp and amazon-mcp — and a single chat conversation
    can put one LLM in front of all six. FastMCP emits
    ``additionalProperties: false``, so a model that carries ``limit`` over
    from a sibling server gets a hard schema rejection here — and the MCP
    client's rejection ("Additional properties are not allowed ('limit' was
    unexpected)") names no field for the model to fix, only that something is
    wrong, so it can only guess. This exact shape burned six consecutive
    rejected calls in one production conversation before the model happened
    to guess right.

    Both search tools now take both names, for the same reason
    `_resolve_page_count` does in kleinanzeigen-mcp: an LLM handed two names
    for one thing will reach for the wrong one on a sibling server, and
    silently swallowing the unknown key would be worse than rejecting it — it
    would hand back the default 20 while the model believed it had asked for
    more. ``max_results`` stays canonical; its meaning here is "per
    retailer", not a global cap, and ``limit``'s schema description repeats
    that so an LLM cannot infer otherwise from the shorter name alone.
    """
    if max_results is not None and limit is not None:
        resolved = _coerce_int(max_results, "max_results", ge=1)
        alias = _coerce_int(limit, "limit", ge=1)
        if resolved != alias:
            raise ValueError(
                "max_results and limit are two names for the same parameter "
                f"but were given different values ({resolved} and {alias}); "
                "pass max_results only"
            )
    elif limit is not None:
        resolved = _coerce_int(limit, "limit", ge=1)
    else:
        resolved = _coerce_int(max_results, "max_results", ge=1)
    return min(resolved or 20, MAX_RESULTS)


# Shared by both search tools so the pair can never drift apart again. The
# alias is declared in the schema rather than silently swallowed — see
# `_resolve_max_results` for why a quietly-ignored `limit` would be worse.
_MaxResults = Annotated[
    str | int | None,
    Field(description="Maximum products per retailer to return (default 20)"),
]
_LimitAlias = Annotated[
    str | int | None,
    Field(
        description="Deprecated alias for `max_results` (maximum products "
        "PER RETAILER, not a global cap across all four); prefer "
        "`max_results`"
    ),
]


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[None]:
    """One shared browser (and its concurrency gate) for the process lifetime."""
    global _browser, _browser_gate
    _browser = BrowserManager()
    _browser_gate = asyncio.Semaphore(MAX_CONCURRENT)
    await _browser.start()
    try:
        yield
    finally:
        await _browser.close()
        _browser = None
        _browser_gate = None


mcp = FastMCP(
    name="baumarkt",
    version=__version__,
    lifespan=lifespan,
    instructions=(
        "Search German hardware/DIY stores (hornbach, bauhaus, globus, obi) "
        "for products and compare prices. `compare_price` shows all four "
        "retailers side by side, cheapest first; `search_products` searches "
        "one retailer or all four; `get_product` fetches one product by the "
        "id returned by a search. Prices are in EUR. Every result carries a "
        "`store_pickup` field for whether the item can be collected at the "
        "retailer's local (Braunschweig-area) branch. A retailer that fails "
        "(e.g. bauhaus behind its bot-wall challenge) is reported in the "
        "`errors` list, never as an empty result."
    ),
)


@mcp.tool
async def search_products(
    query: Annotated[
        str,
        Field(description="Search terms, e.g. 'Bohrmaschine' or 'Bosch Akkuschrauber'"),
    ],
    retailer: Annotated[
        str,
        Field(
            description="Which retailer: 'all' (default) fans out over hornbach, "
            "bauhaus, globus and obi; or exactly one of those four names"
        ),
    ] = "all",
    store: Annotated[
        str | None,
        Field(
            description="Optional branch/store identifier to scope the search to; "
            "None uses each retailer's default (Braunschweig) branch"
        ),
    ] = None,
    max_price: Annotated[
        str | int | None,
        Field(
            description="Maximum price in EUR. Filters results client-side AFTER "
            "fetching; a product with no listed price (price=null) cannot be "
            "filtered and is kept"
        ),
    ] = None,
    max_results: _MaxResults = None,
    limit: _LimitAlias = None,
) -> dict[str, Any]:
    """Search one retailer (or all four) for products matching a keyword.

    With ``retailer="all"`` the fan-out degrades per retailer: a retailer
    that fails (e.g. bauhaus behind its Cloudflare challenge) is reported in
    ``errors`` while the others' results are still returned — a failure is
    never confused with "no results". Each result is one retailer's
    ``Product`` shape (fields: retailer, id, name, brand, gtin, price,
    currency, availability, url, image, store_pickup). ``max_price`` filters
    client-side after fetching; products without a listed price cannot be
    filtered and are kept.
    """
    retailer = retailer.strip().lower()
    if retailer != "all" and retailer not in _RETAILERS:
        raise ValueError(
            f"retailer must be 'all' or one of {', '.join(_RETAILERS)}; got {retailer!r}"
        )
    max_price = _coerce_int(max_price, "max_price", ge=0)
    max_results = _resolve_max_results(max_results, limit)

    targets = list(_RETAILERS) if retailer == "all" else [retailer]
    results, errors = await _collect(
        [(_t, _search_one(_t, query, store, max_results)) for _t in targets]
    )
    if max_price is not None:
        results = [r for r in results if r["price"] is None or r["price"] <= max_price]

    return {
        "query": query,
        "returned": len(results),
        "results": results,
        "errors": errors,
    }


@mcp.tool
async def get_product(
    retailer: Annotated[
        str,
        Field(description="Which retailer: one of hornbach, bauhaus, globus, obi"),
    ],
    product_id: Annotated[
        str,
        Field(description="The retailer's product id/SKU, as returned by a search"),
    ],
    store: Annotated[
        str | None,
        Field(
            description="Optional branch/store identifier; None uses the retailer's "
            "default (Braunschweig) branch"
        ),
    ] = None,
) -> dict[str, Any]:
    """Fetch one product's details by its retailer id.

    Returns ``product`` as the retailer's ``Product`` shape, or ``None`` when
    the id does not resolve. A retailer failure (challenge, transport error)
    is reported in ``error`` — never as ``product: null``, which means "no
    such product".
    """
    retailer = retailer.strip().lower()
    if retailer not in _RETAILERS:
        raise ValueError(
            f"retailer must be one of {', '.join(_RETAILERS)}; got {retailer!r}"
        )
    product_id = product_id.strip()
    if not product_id:
        raise ValueError("product_id must not be empty")

    module = _RETAILERS[retailer]
    try:
        if retailer in _BROWSER_RETAILERS:
            async with _gate():
                product = await module.get_product(
                    product_id, store=store, manager=_manager()
                )
        else:
            product = await module.get_product(product_id, store=store)
    except Exception as exc:  # noqa: BLE001 - per-retailer degradation is the contract
        log.warning("retailer %s get_product failed: %s", retailer, exc)
        return {
            "retailer": retailer,
            "product": None,
            "error": _error_for(retailer, exc),
        }

    return {
        "retailer": retailer,
        "product": product.to_dict() if product is not None else None,
        "error": None,
    }


@mcp.tool
async def compare_price(
    query: Annotated[
        str,
        Field(description="Search terms, e.g. 'Bohrmaschine' or 'Bosch Akkuschrauber'"),
    ],
    store: Annotated[
        str | None,
        Field(
            description="Optional branch/store identifier to scope the search to; "
            "None uses each retailer's default (Braunschweig) branch"
        ),
    ] = None,
    max_results: _MaxResults = None,
    limit: _LimitAlias = None,
) -> dict[str, Any]:
    """Compare `query` across all four retailers, side by side, cheapest first.

    The one-call answer to "who sells this cheapest, and can I collect it
    locally today": results are sorted by price ascending (products with no
    listed price sort last), and each result's ``store_pickup`` field says
    whether the item is available for pickup at the retailer's local branch.
    A retailer that fails (e.g. bauhaus behind its Cloudflare challenge) is
    reported in ``errors`` while the others' results are still returned.
    """
    max_results = _resolve_max_results(max_results, limit)

    results, errors = await _collect(
        [(r, _search_one(r, query, store, max_results)) for r in _RETAILERS]
    )
    results.sort(
        key=lambda r: (
            r["price"] is None,
            r["price"] if r["price"] is not None else math.inf,
        )
    )

    return {
        "query": query,
        "returned": len(results),
        "results": results,
        "errors": errors,
    }


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Container healthcheck.

    Deliberately touches neither the browser context pool nor any retailer:
    it must not borrow a context (that would queue behind BM_MAX_CONCURRENT
    and flap under load) — ``ready`` is a plain attribute check, green even
    while every pool slot is busy.
    """
    if _browser is None or not _browser.ready:
        return JSONResponse({"status": "starting"}, status_code=503)
    return JSONResponse({"status": "ok"})


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = os.getenv("MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host=os.getenv("MCP_HOST", "0.0.0.0"),  # noqa: S104 - containerised
            port=int(os.getenv("MCP_PORT", "8000")),
            path=os.getenv("MCP_PATH", "/mcp"),
        )


if __name__ == "__main__":
    main()
