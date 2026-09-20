"""Headed-browser lifecycle shared by every bot-walled Baumarkt retailer.

Three of the four retailers this project scrapes (hornbach, bauhaus, globus)
sit behind a bot wall that headless Chromium simply does not clear. Measured
on 2026-08-06: headless Chromium got served an image CAPTCHA on hornbach.de
and a hard 403 on bauhaus.info; headed Chromium under Xvfb, on the same
machine, cleared both. Headless is therefore a hard fail for this project —
``BM_HEADLESS=1`` exists for local development only and will not clear either
wall from a real deployment.

We use **patchright** — a patched, undetected Playwright fork with the same
async API surface as vanilla Playwright — rather than vanilla Playwright,
which both walls fingerprint trivially. The fourth retailer, obi, has no bot
wall at all and its adapter talks plain HTTP via httpx; it does not touch this
module.

Adapted from geizhals-mcp's ``browser.py`` (same problem: a real browser
behind a JS/CAPTCHA wall), but the context pool here is not a
semaphore-gated "fresh context per request" pool — it is a reusable pool
guarded by an ``asyncio.Condition``, matching the design kleinanzeigen-mcp's
``SafePlaywrightManager`` had to be rebuilt into after a real outage there.
That upstream pool's ``get_context`` recursed into itself while still holding
the pool lock once every context was busy; ``asyncio.Lock`` (and therefore
``asyncio.Condition``'s underlying lock) is *not* reentrant, so the recursion
self-deadlocked the whole pool, and its ``release_context`` did slow context
cleanup under that same lock — so any release stuck behind the deadlock leaked
its context out of the pool permanently. Every later request, related or not,
then hung until FastMCP's own call deadline, and only a container restart
recovered it. This module avoids both mistakes: waiters block on
``self._condition.wait()``, which releases the lock while waiting (so it
cannot self-deadlock), and ``release_context`` returns the context to the
pool and wakes waiters *before* it does any page cleanup, so a slow or failed
cleanup can never leak a context out of the pool.

Released contexts are kept (not recreated) for reuse: a context that has
already cleared a wall carries the cookies/tokens that proved it, so reusing
it is more valuable than starting cold again next time.

Environment variables (all read in this module):

- ``BM_MAX_CONCURRENT`` (default ``2``) — pool size *and* concurrency limit.
  Each context is a full browser profile; this bounds both how many run at
  once and how many idle ones are kept warm for reuse.
- ``BM_HEADLESS`` (default ``0``, i.e. headed) — set to ``"1"`` to run
  headless. Local development only; will not clear either wall.
- ``BM_CHALLENGE_TIMEOUT_MS`` (default ``25000``) — default timeout for
  :func:`wait_for_challenge_clear`, in milliseconds.
- ``BM_PROXY`` / ``BM_PROXY_USERNAME`` / ``BM_PROXY_PASSWORD`` (optional) —
  route the browser through an egress proxy, e.g. ``http://host:port``.
- ``LOG_LEVEL`` (default ``INFO``) — level for this module's logger.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TypeVar

# patchright mirrors playwright's async API surface exactly, so this import is
# a straight swap for `from playwright.async_api import ...`.
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

T = TypeVar("T")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
log = logging.getLogger("baumarkt-mcp.browser")
log.setLevel(LOG_LEVEL)

# Pool size / concurrency. Each context is a real browser profile, so this
# stays small on purpose — a chat agent issues requests a handful at a time,
# not en masse, and hammering these sites in parallel is the fastest way to
# get flagged.
MAX_CONCURRENT = int(os.getenv("BM_MAX_CONCURRENT", "2"))

# Headed-under-Xvfb by default — see module docstring. BM_HEADLESS=1 is local
# dev only and will not clear hornbach's or bauhaus's wall.
HEADLESS = os.getenv("BM_HEADLESS", "0") == "1"

# Default timeout for wait_for_challenge_clear(). Measured behaviour
# (2026-08-06): hornbach's F5/Fastly challenge clears effectively
# immediately; bauhaus's Cloudflare Turnstile takes ~5s. 25s leaves headroom
# for a slow run without making a genuinely stuck page hang the caller long.
CHALLENGE_TIMEOUT_MS = int(os.getenv("BM_CHALLENGE_TIMEOUT_MS", "25000"))

# Optional egress proxy — same shape as geizhals-mcp's GH_PROXY*.
PROXY_SERVER = os.getenv("BM_PROXY") or None
PROXY_USERNAME = os.getenv("BM_PROXY_USERNAME") or None
PROXY_PASSWORD = os.getenv("BM_PROXY_PASSWORD") or None

# How long a caller waits for a pooled context to free up before giving up.
_POOL_WAIT_TIMEOUT_S = 120

# --------------------------------------------------------------------------- #
# pacing + backoff — applied at the navigation boundary (see fetch_page)
# --------------------------------------------------------------------------- #
#
# A chat agent issues requests a handful at a time, but nothing so far spaced
# consecutive navigations out at all: a compare_price fan-out drives four
# searches back to back, and globus pagination fires up to _MAX_SEARCH_PAGES
# navigations in a tight loop. This is the fastest way to look like a bot to
# the very walls this module exists to clear, so every navigation pays a
# jittered inter-request delay first, and transient navigation failures
# (timeout, or a 429/503 from the edge) are retried with a capped exponential
# backoff that honours Retry-After where the response exposes one.
#
# Deliberately plain constants, not env knobs — nothing here needs tuning per
# deployment, and an untuned bot-wall scraper should err on the patient side.
_MIN_PACING_S = 1.0
_PACING_JITTER_S = 1.0  # uniform(0, jitter) on top of the minimum
_BACKOFF_BASE_S = 2.0
_BACKOFF_CAP_S = 30.0  # ceiling for a single backoff sleep
_NAV_ATTEMPTS = 3  # initial attempt + 2 retries
# Statuses the edge serves while throttling; anything else is either fine or
# a hard failure the caller's parse step should surface.
_RETRYABLE_STATUS = frozenset({429, 503})

_pacing_lock = asyncio.Lock()
_last_nav_monotonic = 0.0


async def _pace() -> None:
    """Space navigations at least _MIN_PACING_S + uniform(0, jitter) apart.

    The lock serialises the read-add-sleep-mark sequence so two concurrent
    navigations cannot both observe "last navigation was long ago" and skip
    the delay — the pool allows up to MAX_CONCURRENT pages in flight, and
    pacing is about the *site's* view of request rate, not per-context.
    """
    global _last_nav_monotonic
    async with _pacing_lock:
        now = time.monotonic()
        earliest = _last_nav_monotonic + _MIN_PACING_S + random.uniform(
            0.0, _PACING_JITTER_S
        )
        delay = earliest - now
        if delay > 0:
            await asyncio.sleep(delay)
        _last_nav_monotonic = time.monotonic()


def _retry_after_seconds(value: str) -> float | None:
    """Seconds to wait for one `Retry-After` header value, or None.

    Handles both RFC 7231 forms: delta-seconds (``"120"``) and HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``, resolved against the wall clock —
    HTTP dates are wall-clock by definition). A past date means "retry now"
    and yields ``0.0``. Unparseable values return None so the caller falls
    back to its own backoff.
    """
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        # Naive timestamp — no basis to compare against a clock; ignore.
        return None
    now = datetime.now(timezone.utc)
    return max(0.0, (retry_at - now).total_seconds())


def _retry_delay_s(attempt: int, response: Response | None) -> float:
    """How long to wait before navigation retry `attempt` (0-based).

    Honours the `Retry-After` header when the response carries one — both
    its delta-seconds and its HTTP-date form (capped at `_BACKOFF_CAP_S`) —
    otherwise capped exponential backoff with uniform jitter.
    """
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after is not None:
        delay = _retry_after_seconds(retry_after)
        if delay is not None:
            return min(_BACKOFF_CAP_S, delay)
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2**attempt) + random.uniform(
        0.0, _BACKOFF_BASE_S
    )


def _proxy_config() -> dict[str, str] | None:
    if not PROXY_SERVER:
        return None
    proxy: dict[str, str] = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    return proxy


class ChallengeError(RuntimeError):
    """Base class for bot-wall failures raised by ``wait_for_challenge_clear``."""


class ChallengeTimeout(ChallengeError):
    """The interstitial never handed off to real content within the timeout."""


class CaptchaRequired(ChallengeError):
    """Hornbach served an unrecoverable image CAPTCHA instead of a challenge.

    Distinguished from :class:`ChallengeTimeout` because no amount of waiting
    fixes this — it is worth its own error so a caller can report "this
    request hit a CAPTCHA" honestly instead of a misleading "timed out".
    """


class PoolExhausted(RuntimeError):
    """No pooled context freed up within the pool-wait timeout."""


# --------------------------------------------------------------------------- #
# challenge detection
# --------------------------------------------------------------------------- #

# Hornbach (F5/Fastly "Client Challenge"). The image-CAPTCHA variant is
# unrecoverable and is checked first so it never gets misreported as a
# timeout.
_HORNBACH_CAPTCHA_MARKER = "characters seen in the image"
_HORNBACH_TITLE_MARKER = "client challenge"

# Bauhaus (Cloudflare Turnstile interstitial, or the branded WAF block page).
_BAUHAUS_TITLE_MARKERS = ("moment", "sicherheitsprüfung")
_BAUHAUS_BODY_MARKER = "zugriff verweigert"


async def _visible_text(page: Page) -> str:
    """Rendered body text, falling back to raw HTML if that fails.

    ``page.content()`` is the full serialized document, including every
    inline ``<script>``/``<style>`` body and attribute value — a marker like
    "characters seen in the image" can legitimately sit past any fixed
    prefix of that (e.g. behind a large inline fingerprinting script in
    ``<head>``), so slicing ``content()`` to a fixed length before matching
    is unreliable: a marker past the cut is invisible, and a genuine wall
    then gets misreported as a timeout instead of its distinguishable error.
    ``inner_text()`` returns only rendered text — smaller (no tags,
    scripts, attributes) and a more faithful match target for
    human-readable markers, and unlike a truncated slice, whether it
    contains a marker does not depend on where in the document that marker
    happens to sit. Falls back to ``content()`` for the rare page that
    isn't far enough along in rendering for ``inner_text`` to resolve.

    Both calls reach into the page's DOM/execution context and can fail
    together mid-navigation (e.g. a redirect tears down the context between
    the two calls) — the fallback is guarded too, so this never raises. On
    a double failure it returns ``""`` for that poll tick rather than
    propagating: the caller's poll loop just tries again next tick, and a
    page that never recovers still ends in the normal, catchable
    :class:`ChallengeTimeout` instead of an uncaught exception breaking out
    of :func:`wait_for_challenge_clear`.
    """
    try:
        return await page.inner_text("body")
    except Exception:  # noqa: BLE001 - best-effort, content() below covers it
        pass
    try:
        return await page.content()
    except Exception:  # noqa: BLE001 - both DOM calls failed; let the poll loop retry
        return ""


async def _current_wall(page: Page) -> str | None:
    """Which known bot wall (if any) `page` is showing *right now*.

    Single poll, no waiting: ``"hornbach-captcha"`` is the unrecoverable
    image-CAPTCHA variant, ``"challenge"`` a clearable interstitial (either
    vendor's), ``None`` real content. Both DOM reads are best-effort (see
    `_visible_text`) — a page mid-render can read as `None`, which callers
    treat as "not a wall *yet*".
    """
    title = (await page.title() or "").lower()
    text = (await _visible_text(page)).lower()

    # The image CAPTCHA is checked first so it never gets misreported as a
    # clearable challenge.
    if _HORNBACH_CAPTCHA_MARKER in text:
        return "hornbach-captcha"
    if (
        _HORNBACH_TITLE_MARKER in title
        or any(m in title for m in _BAUHAUS_TITLE_MARKERS)
        or _BAUHAUS_BODY_MARKER in text
    ):
        return "challenge"
    return None


async def wait_for_challenge_clear(page: Page, timeout_ms: int | None = None) -> bool:
    """Block until `page` is real content, not a bot-wall interstitial.

    Shared by every browser-driven retailer adapter (hornbach, bauhaus,
    globus) so each does not reinvent wall detection. Recognises both wall
    shapes this project has seen:

    - Hornbach's F5/Fastly challenge: page title contains "Client Challenge".
      If it also serves an image CAPTCHA ("characters seen in the image" in
      the body), that is unrecoverable — raises :class:`CaptchaRequired`
      immediately rather than waiting out the timeout.
    - Bauhaus's Cloudflare Turnstile: title contains "Moment" or
      "Sicherheitsprüfung"; or the branded WAF block page, body contains
      "Zugriff verweigert".

    Polls every 250ms rather than sleeping a fixed duration — measured
    clear times are very different between the two walls (hornbach:
    effectively immediate, bauhaus: ~5s), so a fixed sleep would either be
    too slow for hornbach or too impatient for bauhaus.

    Raises :class:`CaptchaRequired` on the unrecoverable hornbach CAPTCHA, or
    :class:`ChallengeTimeout` if a wall is still showing when `timeout_ms`
    (default :data:`CHALLENGE_TIMEOUT_MS`) elapses. Returns ``True`` when a
    wall was present and cleared, ``False`` when the page never showed one —
    :func:`fetch_page` uses that to know when a non-2xx goto response is a
    stale interstitial rather than the page's real status.
    """
    deadline_ms = CHALLENGE_TIMEOUT_MS if timeout_ms is None else timeout_ms
    step_ms = 250
    waited_ms = 0
    saw_wall = False
    while True:
        wall = await _current_wall(page)
        if wall is None:
            return saw_wall
        saw_wall = True
        if wall == "hornbach-captcha":
            raise CaptchaRequired(
                "Hornbach served an image CAPTCHA instead of a challenge — "
                "this cannot be cleared automatically."
            )
        if waited_ms >= deadline_ms:
            raise ChallengeTimeout(
                f"Bot-wall challenge did not clear within {deadline_ms}ms "
                f"(title={(await page.title() or '').lower()!r})"
            )
        await page.wait_for_timeout(step_ms)
        waited_ms += step_ms


# --------------------------------------------------------------------------- #
# shared page fetch — navigation + challenge + cleanup in one place
# --------------------------------------------------------------------------- #

# Default per-navigation timeout for fetch_page. Every adapter previously
# rolled its own (hornbach/globus: 30s, bauhaus: none at all — a stalled
# navigation hung on playwright's own much larger default); one shared
# default keeps them honest.
DEFAULT_NAV_TIMEOUT_MS = 30_000


async def fetch_page(
    ctx: BrowserContext,
    url: str,
    parse: Callable[[Page, Response | None], Awaitable[T]],
    *,
    prepare: Callable[[Page], Awaitable[None]] | None = None,
    clear_challenge: Callable[[Page], Awaitable[None]] = wait_for_challenge_clear,
    timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
) -> T:
    """Navigate one pooled-context page to `url`, clear the bot wall, parse.

    Single owner of the goto/challenge/cleanup sequence every browser-backed
    adapter used to duplicate (bauhaus even omitted the explicit navigation
    timeout): opens a page on `ctx`, runs `prepare` (e.g. registering
    response-capture handlers, which must happen *before* navigation),
    navigates with an explicit timeout, clears the wall, hands the page plus
    the goto response to `parse`, and closes the page in a finally.

    Also owns pacing and transient-failure retry: every navigation pays the
    shared jittered pacing delay first. A navigation timeout is retried up
    to `_NAV_ATTEMPTS` times with capped exponential backoff (honouring
    Retry-After in both its delta-seconds and HTTP-date forms). A 429/503 is
    retried the same way *only when the page is not a bot wall* — walls
    serving those statuses are handed to `clear_challenge` and then
    re-navigated, never retried into. After clearing, a stale non-2xx
    interstitial response is refreshed with one re-navigation so `parse`
    sees the page's real status. Challenge failures proper (an interstitial
    that never clears) are deliberately NOT retried — `clear_challenge`
    decides what a wall means (bauhaus's interactive variant must
    propagate, not be retried into).

    `parse` receives the goto `Response` so a caller can classify statuses
    itself (e.g. hornbach's 404-on-unknown-sku) instead of trusting that a
    resolved navigation means a healthy page.
    """
    page = await ctx.new_page()
    try:
        if prepare is not None:
            await prepare(page)
        response: Response | None = None
        for attempt in range(_NAV_ATTEMPTS):
            await _pace()
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout_ms
                )
            except PlaywrightTimeoutError:
                if attempt == _NAV_ATTEMPTS - 1:
                    raise
                delay = _retry_delay_s(attempt, None)
                log.warning(
                    "navigation to %s timed out after %dms (attempt %d/%d), "
                    "retrying in %.1fs",
                    url,
                    timeout_ms,
                    attempt + 1,
                    _NAV_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            status = response.status if response is not None else 0
            if status in _RETRYABLE_STATUS and attempt < _NAV_ATTEMPTS - 1:
                # A 429/503 is only "throttling" if the page is not a bot
                # wall — several walls serve their interstitial with exactly
                # these statuses, and those must never be blind-retried.
                wall = await _current_wall(page)
                if wall == "hornbach-captcha":
                    break  # unrecoverable — clear_challenge raises it below
                if wall is not None:
                    # The throttling status IS the interstitial: let the
                    # challenge machinery resolve it, then re-navigate for a
                    # real response instead of retrying into the wall.
                    await clear_challenge(page)
                    continue
                delay = _retry_delay_s(attempt, response)
                log.warning(
                    "navigation to %s got HTTP %d (attempt %d/%d), "
                    "retrying in %.1fs",
                    url,
                    status,
                    attempt + 1,
                    _NAV_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break
        saw_wall = await clear_challenge(page)
        if saw_wall and response is not None and response.status >= 400:
            # The wall's interstitial arrived with a non-2xx status (bauhaus's
            # Turnstile serves 403) and has since cleared into the real
            # document, so the stale response no longer describes the page —
            # re-navigate once now that the wall is down, so `parse` sees the
            # page's true status instead of the challenge's.
            await _pace()
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            await clear_challenge(page)
        return await parse(page, response)
    finally:
        await page.close()


# --------------------------------------------------------------------------- #
# browser + context pool
# --------------------------------------------------------------------------- #


class BrowserManager:
    """Owns one Chromium instance and vends a pool of reusable contexts.

    Public API consumed by retailer adapters and by the MCP server's
    lifespan:

    - ``await manager.start()`` / ``await manager.close()`` — process-wide
      lifecycle, called once each from the server's lifespan.
    - ``manager.ready`` — bool, whether the browser is up.
    - ``async with manager.context() as ctx:`` — borrow a pooled
      ``BrowserContext``, use it (typically ``page = await ctx.new_page()``),
      and it is returned to the pool on exit. Prefer this over calling
      ``get_context``/``release_context`` directly.
    - ``await manager.get_context()`` / ``await manager.release_context(ctx)``
      — the primitives ``context()`` wraps, exposed directly in case a
      caller needs the context to outlive a single ``async with`` block.

    Concurrency and pool size are both governed by ``max_concurrent``
    (``BM_MAX_CONCURRENT``): at most that many contexts exist at once, and at
    most that many are held in ``_in_use`` concurrently. A caller past that
    limit waits on an ``asyncio.Condition`` — see the module docstring for
    why a Condition and not a Semaphore, and why release must free the pool
    slot before it does any cleanup.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._max_concurrent = max_concurrent
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._condition = asyncio.Condition()
        self._pool: list[BrowserContext] = []
        self._in_use: set[BrowserContext] = set()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        proxy = _proxy_config()
        self._browser = await self._playwright.chromium.launch(
            headless=HEADLESS,
            proxy=proxy,
            args=[
                # Required in a container: Chromium's own sandbox needs
                # privileged caps or user namespaces we do not grant.
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        log.info(
            "Chromium ready (headless=%s, max_concurrent=%s, proxy=%s)",
            HEADLESS,
            self._max_concurrent,
            PROXY_SERVER or "none",
        )

    async def close(self) -> None:
        async with self._condition:
            contexts = [*self._pool, *self._in_use]
            self._pool.clear()
            self._in_use.clear()
        for ctx in contexts:
            try:
                await ctx.close()
            except BaseException:  # noqa: BLE001 - shutdown must never raise
                pass
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def ready(self) -> bool:
        return self._browser is not None

    async def _new_context(self) -> BrowserContext:
        assert self._browser is not None
        return await self._browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            # Matches entrypoint.sh's default Xvfb screen (BM_SCREEN,
            # default 1366x900) and is a realistic desktop size — locale and
            # viewport both feed bot-detection heuristics on these sites.
            viewport={"width": 1366, "height": 900},
            extra_http_headers={
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
                # Set explicitly rather than relying on the engine default so
                # the header trio (UA + Accept + Accept-Language) is one
                # documented, deterministic unit. The version is taken from
                # the actual engine, so the UA can never drift ahead of (or
                # behind) the browser's real behaviour — a hard-coded version
                # would go stale with the next patchright bump and is a
                # classic fingerprint mismatch.
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{self._browser.version} "
                    "Safari/537.36"
                ),
                # What desktop Chrome sends for a navigation request.
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8,"
                    "application/signed-exchange;v=b3;q=0.7"
                ),
            },
        )

    async def get_context(self) -> BrowserContext:
        """Borrow a context: reuse a pooled one, create one, or wait.

        Never call this while already holding a context you intend to
        release later in the same task without awaiting something in
        between — with `max_concurrent` contexts all in use, that is a
        single-task self-deadlock no different from the pool-lock recursion
        this design exists to avoid.
        """
        if self._browser is None:
            raise RuntimeError("Browser manager is not running")
        while True:
            async with self._condition:
                if self._pool:
                    ctx = self._pool.pop()
                    self._in_use.add(ctx)
                    return ctx
                if len(self._in_use) < self._max_concurrent:
                    ctx = await self._new_context()
                    self._in_use.add(ctx)
                    return ctx
                # Every slot is busy: wait for a release. Condition.wait()
                # releases the lock while waiting, so concurrent releases
                # can still run — this is what makes it safe, unlike a plain
                # Lock, which a recursive re-entry into get_context() would
                # deadlock on permanently.
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=_POOL_WAIT_TIMEOUT_S
                    )
                except TimeoutError as exc:
                    raise PoolExhausted(
                        f"All {self._max_concurrent} browser contexts stayed "
                        f"busy for {_POOL_WAIT_TIMEOUT_S}s — one of these "
                        "sites may be throttling this server"
                    ) from exc

    async def release_context(self, context: BrowserContext) -> None:
        """Return `context` to the pool, then clean up its pages.

        The pool slot is freed and waiters are woken *before* any cleanup
        runs, so a slow or failed cleanup can never leak the slot out of the
        pool — see the module docstring for the outage this avoids.
        """
        async with self._condition:
            if context not in self._in_use:
                return
            self._in_use.discard(context)
            self._pool.append(context)
            # Snapshot the pages BEFORE waking waiters: a waiter that pops
            # this context can open a new page immediately, and the cleanup
            # below must only close pages that existed at release time.
            pages = list(context.pages)
            self._condition.notify_all()
        # Best-effort cleanup outside the lock: close any pages left open so
        # the next borrower starts from a blank tab. Cookies are kept
        # deliberately — see module docstring on reusing cleared-wall state.
        try:
            for page in pages:
                await page.close()
        except BaseException:  # noqa: BLE001 - cleanup must never raise
            pass

    @asynccontextmanager
    async def context(self) -> AsyncIterator[BrowserContext]:
        """Borrow a pooled context for the duration of the `async with` block."""
        ctx = await self.get_context()
        try:
            yield ctx
        finally:
            await self.release_context(ctx)
