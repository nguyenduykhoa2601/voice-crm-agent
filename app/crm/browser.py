"""Shared headless Chromium for driving the EspoCRM demo.

One `CrmBrowser` owns a single Playwright + Chromium process. Each reviewer
session gets its own `CrmSession` (isolated BrowserContext + Page, logged in),
with an optional CDP screencast that streams JPEG frames to a callback.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.config import settings

log = logging.getLogger(__name__)

FrameCallback = Callable[[bytes], Awaitable[None]]

VIEWPORT = {"width": 1280, "height": 800}
ACTION_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 45_000

# Flags that keep Chromium small and working inside an unprivileged container.
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--renderer-process-limit=4",
    "--js-flags=--max-old-space-size=256",
]

# LOW_MEMORY=1 (set on Render's 512 MB free tier): one renderer process, no
# site-isolation helpers, a smaller V8 heap and fewer background features.
# Later flags override the defaults above (Chromium keeps the last value).
LOW_MEMORY_ARGS = [
    "--no-zygote",
    "--renderer-process-limit=1",
    "--disable-site-isolation-trials",
    "--disable-features=site-per-process,IsolateOrigins,Translate,BackForwardCache,"
    "MediaRouter,OptimizationHints,AudioServiceOutOfProcess",
    "--disable-breakpad",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-client-side-phishing-detection",
    "--disk-cache-size=33554432",
    "--js-flags=--max-old-space-size=192",
]


def chromium_args() -> list[str]:
    if settings.low_memory:
        return CHROMIUM_ARGS + LOW_MEMORY_ARGS
    return list(CHROMIUM_ARGS)

# Third-party requests that only slow the demo down.
BLOCKED_HOSTS = ("google-analytics.com", "googletagmanager.com", "doubleclick.net")

HIGHLIGHT_CSS = """
.vca-highlight {
  outline: 3px solid #f59e0b !important;
  outline-offset: 2px !important;
  box-shadow: 0 0 0 6px rgba(245, 158, 11, 0.25) !important;
  border-radius: 4px;
  transition: outline-color 0.2s ease;
}
"""


class CrmError(RuntimeError):
    """Raised when the CRM cannot be reached or logged into."""


class CrmSession:
    """One isolated, logged-in browser tab on the CRM."""

    def __init__(self, owner: "CrmBrowser", context: BrowserContext, page: Page):
        self.owner = owner
        self.context = context
        self.page = page
        self.base_url = owner.base_url
        # Tools must not run concurrently on the same page.
        self.lock = asyncio.Lock()
        self._cdp: Optional[CDPSession] = None
        self._on_frame: Optional[FrameCallback] = None
        self._frame_busy = False
        self._pending_frame: Optional[bytes] = None
        self._frame_tasks: set[asyncio.Task] = set()
        self.frames_sent = 0
        self.closed = False

    # ------------------------------------------------------------------ navigation
    def url_for(self, hash_route: str) -> str:
        route = hash_route if hash_route.startswith("#") else "#" + hash_route
        return f"{self.base_url}/{route}"

    async def goto(self, hash_route: str, wait_selector: Optional[str] = None,
                   retries: int = 2) -> None:
        """Navigate to a hash route (e.g. "#Contact/create") with retries.

        `wait_selector` is awaited on the *newly rendered* view only: the old view's
        DOM is marked before navigating so a stale match cannot satisfy the wait.
        Navigation is idempotent, so it is safe to retry. If the login page shows
        up (expired session), log in again and repeat.
        """
        target = self.url_for(hash_route)
        fragment = target.split("#", 1)[1]
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                await self.mark_stale()
                same_app = self.page.url.startswith(self.base_url) and await self.page.locator("#main").count() > 0
                if same_app and attempt == 0:
                    current = await self.page.evaluate("() => window.location.hash.replace(/^#/, '')")
                    if current == fragment:
                        # Same route: reload so the view is re-fetched from the server.
                        await self.page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    else:
                        await self.page.evaluate("(h) => { window.location.hash = h; }", fragment)
                else:
                    await self.page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                if wait_selector:
                    await self.wait_fresh(wait_selector, or_login=True)
                if await self.ensure_logged_in():
                    # Session had expired: now logged in again, navigate once more.
                    await self.page.evaluate("(h) => { window.location.hash = h; }", fragment)
                    if wait_selector:
                        await self.wait_fresh(wait_selector)
                return
            except (PlaywrightTimeout, PlaywrightError) as exc:
                last_exc = exc
                log.warning("goto %s failed (attempt %d): %s", hash_route, attempt + 1, exc)
                if attempt < retries:
                    await asyncio.sleep(1.0 + attempt)
        raise CrmError(f"Could not open {hash_route}: {last_exc}")

    async def mark_stale(self) -> None:
        """Tag the currently rendered main view so waits only match a re-rendered one."""
        try:
            await self.page.evaluate(
                "() => document.querySelectorAll('#main > *').forEach(e => e.setAttribute('data-vca-stale', '1'))")
        except PlaywrightError:
            pass

    async def wait_fresh(self, selector: str, timeout_ms: int = ACTION_TIMEOUT_MS,
                         or_login: bool = False) -> None:
        """Wait for a visible element matching `selector` that is not part of a stale view.

        With `or_login`, the login page appearing also ends the wait (expired session).
        """
        await self.page.wait_for_function(
            """([sel, orLogin]) => {
                const ok = el => !el.closest('[data-vca-stale]') && el.getClientRects().length > 0;
                if (orLogin) { const b = document.querySelector('#btn-login'); if (b && ok(b)) return true; }
                return [...document.querySelectorAll(sel)].some(ok);
            }""",
            arg=[selector, or_login], timeout=timeout_ms, polling=150)

    async def is_login_page(self) -> bool:
        try:
            return await self.page.locator("#btn-login").is_visible()
        except PlaywrightError:
            return False

    async def login(self) -> None:
        """Log in as the preselected demo user. Retries on slow loads."""
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                if not await self.is_login_page():
                    await self.page.goto(self.base_url + "/", wait_until="domcontentloaded",
                                         timeout=NAV_TIMEOUT_MS)
                login_btn = self.page.locator("#btn-login")
                try:
                    await login_btn.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
                except PlaywrightTimeout:
                    # Already logged in (cookie still valid)?
                    if await self.page.locator("#navbar").count():
                        return
                    raise
                await login_btn.click()
                await self.page.wait_for_selector("#navbar", state="attached", timeout=NAV_TIMEOUT_MS)
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeout:
                    pass
                return
            except (PlaywrightTimeout, PlaywrightError) as exc:
                last_exc = exc
                log.warning("login failed (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise CrmError(f"Could not log in to the CRM: {last_exc}")

    async def ensure_logged_in(self) -> bool:
        """Log in again if the session expired. Returns True if a login happened."""
        if await self.is_login_page():
            log.info("CRM session expired, logging in again")
            await self.login()
            return True
        return False

    # ------------------------------------------------------------------ API helpers
    async def api(self, method: str, path: str, body: Any = None) -> Any:
        """Call the CRM REST API (`/api/v1/<path>`) from inside the logged-in page.

        Used for metadata, eval seeding and cleanup, never for user-facing actions
        (those go through the visible UI).
        """
        result = await self.page.evaluate(
            """async ([method, path, body]) => {
                const opts = {method, headers: {'Content-Type': 'application/json',
                                                'Espo-Authorization-By-Token': 'true'}};
                if (body !== null && body !== undefined) opts.body = JSON.stringify(body);
                const res = await fetch('api/v1/' + path.replace(/^\\//, ''), opts);
                const text = await res.text();
                let data = null;
                try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
                return {status: res.status, data};
            }""",
            [method.upper(), path, body],
        )
        if result["status"] == 401:
            await self.ensure_logged_in()
            raise CrmError("API call unauthorized (session expired); retry")
        if result["status"] >= 400:
            raise CrmError(f"API {method} {path} failed with HTTP {result['status']}: {result['data']}")
        return result["data"]

    # ------------------------------------------------------------------ visuals
    async def highlight(self, target: Locator, duration_ms: int = 1200) -> None:
        """Briefly outline the element being acted on (visible in the screencast)."""
        try:
            await target.evaluate(
                """(el, args) => {
                    const [css, ms] = args;
                    if (!document.getElementById('vca-highlight-style')) {
                        const s = document.createElement('style');
                        s.id = 'vca-highlight-style';
                        s.textContent = css;
                        document.head.appendChild(s);
                    }
                    // Selectize hides the native control; outline the visible widget instead.
                    let node = el;
                    if (el.tagName === 'SELECT' && el.nextElementSibling &&
                        el.nextElementSibling.classList.contains('selectize-control')) {
                        node = el.nextElementSibling;
                    }
                    node.scrollIntoView({block: 'nearest', inline: 'nearest'});
                    node.classList.add('vca-highlight');
                    clearTimeout(node._vcaTimer);
                    node._vcaTimer = setTimeout(() => node.classList.remove('vca-highlight'), ms);
                }""",
                [HIGHLIGHT_CSS, duration_ms],
                timeout=3000,
            )
        except PlaywrightError:
            pass  # Highlighting is cosmetic; never fail an action because of it.

    async def screenshot(self, quality: int = 60) -> bytes:
        return await self.page.screenshot(type="jpeg", quality=quality, timeout=10_000)

    # ------------------------------------------------------------------ screencast
    async def start_screencast(self, on_frame: FrameCallback) -> None:
        """Stream JPEG frames of the page to `on_frame(jpeg_bytes)`.

        Chromium only emits frames when the page repaints, so an idle page sends
        nothing; an initial full screenshot is pushed right away. Frames are
        acknowledged immediately; if the callback is still busy with the previous
        frame, only the newest pending frame is kept (no unbounded queue).
        On LOW_MEMORY instances frames are smaller and rarer to save CPU.
        """
        if settings.low_memory:
            quality, every_nth_frame, max_width = 50, 3, 960
        else:
            quality, every_nth_frame, max_width = 60, 2, VIEWPORT["width"]
        max_height = VIEWPORT["height"] * max_width // VIEWPORT["width"]
        await self.stop_screencast()
        self._on_frame = on_frame
        self._cdp = await self.context.new_cdp_session(self.page)
        self._cdp.on("Page.screencastFrame", self._handle_frame)
        await self._cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": quality,
            "maxWidth": max_width,
            "maxHeight": max_height,
            "everyNthFrame": every_nth_frame,
        })
        try:
            self._deliver(await self.screenshot(quality))
        except PlaywrightError:
            pass

    async def stop_screencast(self) -> None:
        cdp, self._cdp = self._cdp, None
        self._on_frame = None
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            except PlaywrightError:
                pass

    def _handle_frame(self, params: dict) -> None:
        cdp = self._cdp
        if cdp is None:
            return
        task = asyncio.ensure_future(
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        )
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        try:
            self._deliver(base64.b64decode(params["data"]))
        except (ValueError, KeyError):
            pass

    def _deliver(self, jpeg: bytes) -> None:
        if self._on_frame is None:
            return
        if self._frame_busy:
            self._pending_frame = jpeg  # Drop older frames under backpressure.
            return
        self._frame_busy = True
        task = asyncio.ensure_future(self._frame_loop(jpeg))
        self._frame_tasks.add(task)
        task.add_done_callback(self._frame_tasks.discard)

    async def _frame_loop(self, jpeg: bytes) -> None:
        try:
            current: Optional[bytes] = jpeg
            while current is not None and self._on_frame is not None:
                try:
                    await self._on_frame(current)
                    self.frames_sent += 1
                except Exception:  # noqa: BLE001 - a broken consumer must not kill the stream
                    log.exception("on_frame callback failed")
                current, self._pending_frame = self._pending_frame, None
        finally:
            self._frame_busy = False

    # ------------------------------------------------------------------ lifecycle
    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.stop_screencast()
        for task in list(self._frame_tasks):
            task.cancel()
        try:
            await self.context.close()
        except PlaywrightError:
            pass
        self.owner._sessions.discard(self)


class CrmBrowser:
    """Owns one Playwright + Chromium process shared by all sessions."""

    def __init__(self, base_url: Optional[str] = None, headless: Optional[bool] = None):
        self.base_url = (base_url or settings.espo_url).rstrip("/")
        self.headless = settings.headless if headless is None else headless
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        self._sessions: set[CrmSession] = set()

    async def start(self) -> Browser:
        """Launch Chromium lazily (and relaunch it if it crashed)."""
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self.headless, args=chromium_args())
            return self._browser

    async def new_session(self, on_frame: Optional[FrameCallback] = None,
                          login: bool = True) -> CrmSession:
        """Create an isolated, logged-in session. Starts the screencast if `on_frame` is given."""
        browser = await self.start()
        context = await browser.new_context(viewport=VIEWPORT, locale="en-US",
                                            timezone_id="UTC", device_scale_factor=1)
        try:
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAV_TIMEOUT_MS)

            async def _block(route):
                await route.abort()

            await context.route(lambda url: any(h in url for h in BLOCKED_HOSTS), _block)
            page = await context.new_page()
        except BaseException:
            # Failed or cancelled before the session owns the context: close it here.
            with contextlib.suppress(Exception):
                await context.close()
            raise

        async def _on_dialog(dialog) -> None:
            # Native dialogs are only "leave page?" prompts; we always navigate on purpose.
            try:
                await dialog.accept()
            except PlaywrightError:
                pass

        page.on("dialog", _on_dialog)
        session = CrmSession(self, context, page)
        self._sessions.add(session)
        try:
            if on_frame is not None:
                await session.start_screencast(on_frame)
            if login:
                started = time.monotonic()
                await session.login()
                log.info("CRM login took %.1fs", time.monotonic() - started)
        except BaseException:
            await session.close()
            raise
        return session

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    async def close(self) -> None:
        for session in list(self._sessions):
            await session.close()
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def __aenter__(self) -> "CrmBrowser":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
