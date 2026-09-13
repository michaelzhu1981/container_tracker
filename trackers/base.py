"""Shared Playwright tracker contract."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from artifacts import checked_at, html_path, relative_to_root, screenshot_path
from config import AUTO_CHALLENGE_WAIT_MS, CHALLENGE_RETRY_DELAYS, CHALLENGE_WAIT_MS
from models import CanonicalEvent, TimelineOrder, TrackResult
from status_engine import evaluate

LOGGER = logging.getLogger("container_tracker")

COOKIE_SELECTORS = (
    "#onetrust-pc-sdk #accept-recommended-btn-handler",
    "#accept-recommended-btn-handler",
    "#onetrust-pc-sdk button:has-text('Select All')",
    "#onetrust-pc-sdk button:has-text('Confirm My Choices')",
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept Cookies')",
    "button:has-text('Select All')",
    "button:has-text('Confirm My Choices')",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Agree')",
    "button:has-text('I agree')",
    "button:has-text('Allow all')",
)

_HIDE_OVERLAYS_JS = """() => {
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-pc-sdk",
        "#onetrust-consent-sdk",
        ".onetrust-pc-dark-filter",
        "[id^='onetrust']",
        "[class*='cookie-banner' i]",
        "[class*='CookieBanner']",
        "[id*='cookie-banner' i]",
        ".q-dialog",
        "[role='dialog']",
        "[class*='login-modal' i]",
        "[class*='signin' i][class*='modal' i]",
    ];
    for (const selector of selectors) {
        try {
            document.querySelectorAll(selector).forEach((el) => {
                el.style.setProperty("display", "none", "important");
                el.style.setProperty("visibility", "hidden", "important");
            });
        } catch (err) {}
    }
}"""

_NO_SCREENSHOT_CODES = frozenset({"CLOUDFLARE", "CAPTCHA"})

_QUERY_PAGE_MARKERS = (
    "hal-event",
    "latest event",
    "container status",
    "shipment details",
    "tracking details",
    "on board",
    "gated in",
    "vessel departed",
    "cargo tracking",
)


def is_query_screenshot_page(text: str, html: str = "") -> bool:
    """True when the page shows container tracking results, not login/cookie/CF."""
    if challenge_code(text) or challenge_code(html):
        return False
    blob = f"{text}\n{html}".lower()
    cookie_only = (
        ("cookie" in blob or "onetrust" in blob)
        and not any(marker in blob for marker in _QUERY_PAGE_MARKERS)
    )
    login_only = (
        ("log in" in blob or "sign in" in blob or "login" in blob)
        and not any(marker in blob for marker in _QUERY_PAGE_MARKERS)
    )
    if cookie_only or login_only:
        return False
    return any(marker in blob for marker in _QUERY_PAGE_MARKERS)


_CHALLENGE_GONE_JS = """() => {
    const text = (document.body && document.body.innerText || "").toLowerCase();
    const html = (document.documentElement && document.documentElement.innerHTML || "").toLowerCase();
    const blocked = (
        text.includes("checking your browser") ||
        text.includes("managed challenge") ||
        text.includes("verify you are human") ||
        text.includes("security check") ||
        html.includes("cf-challenge")
    );
    return !blocked;
}"""


def challenge_code(text: str) -> str | None:
    """Return CLOUDFLARE, CAPTCHA, or None from visible page text."""
    blob = text.lower()
    if (
        "checking your browser" in blob
        or "managed challenge" in blob
        or "verify you are human" in blob
        or "security check" in blob
        or ("attention required" in blob and "cloudflare" in blob)
    ):
        return "CLOUDFLARE"
    if "recaptcha" in blob or "hcaptcha" in blob:
        return "CAPTCHA"
    return None


class TrackerError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class BaseTracker(ABC):
    carrier_code: str = ""
    timeline_order: TimelineOrder = "oldest_first"
    tracking_url: str = ""
    screenshot_selectors: tuple[str, ...] = ()

    def __init__(
        self,
        page: Any,
        *,
        wait_for_challenge: bool = True,
        browser: Any | None = None,
    ) -> None:
        self.page = page
        self.wait_for_challenge = wait_for_challenge
        self.browser = browser
        self._screenshot: Path | None = None
        self._html: Path | None = None

    async def dismiss_cookies(self, wait_ms: int = 3000) -> None:
        try:
            await self.page.locator(
                "#onetrust-pc-sdk, #onetrust-banner-sdk, #onetrust-accept-btn-handler"
            ).first.wait_for(state="visible", timeout=wait_ms)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(2):
            clicked = False
            for selector in COOKIE_SELECTORS:
                locator = self.page.locator(selector)
                try:
                    if await locator.first.is_visible(timeout=400):
                        await locator.first.click(timeout=3000, force=True)
                        clicked = True
                        await self.page.wait_for_timeout(500)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                return
            try:
                await self.page.locator("#onetrust-pc-sdk, #onetrust-banner-sdk").first.wait_for(
                    state="hidden", timeout=2500
                )
                return
            except Exception:  # noqa: BLE001
                continue

    async def _visible_text(self) -> str:
        try:
            return await self.page.evaluate(
                "() => (document.body && document.body.innerText) || ''"
            )
        except Exception:  # noqa: BLE001
            try:
                return await self.page.content()
            except Exception:  # noqa: BLE001
                return ""

    def detect_blocks(self, html: str) -> None:
        code = challenge_code(html)
        if code == "CLOUDFLARE":
            raise TrackerError("Cloudflare challenge detected.", "CLOUDFLARE")
        if code == "CAPTCHA":
            raise TrackerError("CAPTCHA detected.", "CAPTCHA")

    async def _wait_challenge_gone(self, timeout_ms: int) -> bool:
        try:
            await self.page.wait_for_function(_CHALLENGE_GONE_JS, timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _after_challenge_cleared(self) -> None:
        await self.page.wait_for_timeout(1000)
        await self.dismiss_cookies(wait_ms=5000)
        still = challenge_code(await self._visible_text())
        if still:
            raise TrackerError(
                f"Timed out waiting for the {still} check to clear.",
                still,
            )

    async def _reload_tracking_page(self) -> None:
        if not self.tracking_url:
            return
        try:
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            LOGGER.info("Reload after challenge failed for %s", self.carrier_code)

    async def pass_or_wait_for_challenge(self) -> None:
        code = challenge_code(await self._visible_text())
        if not code:
            return

        auto_s = AUTO_CHALLENGE_WAIT_MS // 1000
        LOGGER.info("Waiting up to %ss for %s to clear automatically", auto_s, code)
        if await self._wait_challenge_gone(AUTO_CHALLENGE_WAIT_MS):
            await self._after_challenge_cleared()
            return

        if self.wait_for_challenge:
            if self.browser is not None and hasattr(
                self.browser, "hand_off_to_system_chrome"
            ):
                LOGGER.info("Handing %s off to system Chrome for a human check", code)
                handed = await self.browser.hand_off_to_system_chrome(self.tracking_url)
                if handed:
                    self.page = self.browser.page
                    if not challenge_code(await self._visible_text()):
                        await self._after_challenge_cleared()
                        return
            else:
                seconds = CHALLENGE_WAIT_MS // 1000
                print(
                    f"Security check ({code}) is blocking the page. "
                    f"Complete it in the browser window; tracking resumes automatically "
                    f"(timeout {seconds}s)."
                )
                LOGGER.info("Waiting up to %ss for human to complete %s", seconds, code)
                if await self._wait_challenge_gone(CHALLENGE_WAIT_MS):
                    await self._after_challenge_cleared()
                    return
            still = challenge_code(await self._visible_text()) or code
            raise TrackerError(
                f"Timed out waiting for the {still} check to clear.",
                still,
            )

        for delay in CHALLENGE_RETRY_DELAYS:
            LOGGER.info("Retrying after %s: sleeping %.0fs then reloading", code, delay)
            await self.page.wait_for_timeout(int(delay * 1000))
            await self._reload_tracking_page()
            if await self._wait_challenge_gone(AUTO_CHALLENGE_WAIT_MS):
                await self._after_challenge_cleared()
                return

        still = challenge_code(await self._visible_text()) or code
        raise TrackerError(
            f"Timed out waiting for the {still} check to clear.",
            still,
        )

    async def prepare_for_screenshot(self) -> None:
        await self.dismiss_cookies(wait_ms=1500)
        dismiss_onboarding = getattr(self, "dismiss_onboarding", None)
        if callable(dismiss_onboarding):
            try:
                await dismiss_onboarding()
            except Exception:  # noqa: BLE001
                pass
        try:
            await self.page.evaluate(_HIDE_OVERLAYS_JS)
        except Exception:  # noqa: BLE001
            LOGGER.info("Could not hide overlays before screenshot for %s", self.carrier_code)

    async def _screenshot_query_content(self, path: Path) -> bool:
        for selector in self.screenshot_selectors:
            locator = self.page.locator(selector)
            try:
                if await locator.first.is_visible(timeout=800):
                    await locator.first.screenshot(path=str(path))
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def save_artifacts(self, container: str, *, allow_screenshot: bool = True) -> None:
        self._screenshot = None
        self._html = html_path(container)
        try:
            html = await self.page.content()
            self._html.write_text(html, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            if "has been closed" in str(exc) or "TargetClosed" in type(exc).__name__:
                LOGGER.info("Skipped artifacts for %s; browser already closed.", container)
                self._html = None
                return
            LOGGER.exception("HTML save failed for %s", container)
            self._html = None
            html = ""
        if not allow_screenshot:
            return
        text = await self._visible_text()
        if not is_query_screenshot_page(text, html):
            LOGGER.info("Skipping screenshot for %s; page is not a tracking result.", container)
            return
        await self.prepare_for_screenshot()
        shot = screenshot_path(container)
        try:
            if not await self._screenshot_query_content(shot):
                await self.page.screenshot(path=str(shot), full_page=False)
            self._screenshot = shot
        except Exception:  # noqa: BLE001
            LOGGER.exception("Screenshot failed for %s", container)
            self._screenshot = None
            shot.unlink(missing_ok=True)

    @abstractmethod
    async def open_page(self) -> None: ...

    @abstractmethod
    async def search(self, container: str) -> None: ...

    @abstractmethod
    async def parse_events(self) -> list[CanonicalEvent]: ...

    async def track(self, container: str) -> TrackResult:
        stamp = checked_at()
        try:
            await self.open_page()
            if not challenge_code(await self._visible_text()):
                await self.dismiss_cookies()
            await self.pass_or_wait_for_challenge()
            await self.search(container)
            await self.dismiss_cookies(wait_ms=8_000)
            await self.page.wait_for_timeout(1500)
            await self.pass_or_wait_for_challenge()
            html = await self.page.content()
            blob = html.lower()
            if any(
                token in blob
                for token in (
                    "no result",
                    "not found",
                    "no tracing",
                    "no tracking information",
                    "can't identify your input",
                    "cannot identify your input",
                    "can’t identify your input",
                )
            ):
                raise TrackerError("No tracking result for this container.", "NO_RESULT")
            events = await self.parse_events()
            await self.save_artifacts(container)
            if not events:
                return evaluate(
                    [],
                    container=container,
                    carrier=self.carrier_code,
                    timeline_order=self.timeline_order,
                    checked_at=stamp,
                    screenshot_path=relative_to_root(self._screenshot),
                    html_path=relative_to_root(self._html),
                    forced_status="CHECK_FAILED",
                    error_code="PARSE",
                    error="Could not parse tracking events from the page.",
                )
            return evaluate(
                events,
                container=container,
                carrier=self.carrier_code,
                timeline_order=self.timeline_order,
                checked_at=stamp,
                screenshot_path=relative_to_root(self._screenshot),
                html_path=relative_to_root(self._html),
            )
        except TrackerError as exc:
            await self.save_artifacts(
                container,
                allow_screenshot=exc.code not in _NO_SCREENSHOT_CODES,
            )
            status = "MANUAL_CHECK_REQUIRED" if exc.code in {"CAPTCHA"} else "CHECK_FAILED"
            if exc.code == "CLOUDFLARE":
                status = (
                    "MANUAL_CHECK_REQUIRED" if self.wait_for_challenge else "CHECK_FAILED"
                )
            return evaluate(
                [],
                container=container,
                carrier=self.carrier_code,
                timeline_order=self.timeline_order,
                checked_at=stamp,
                screenshot_path=relative_to_root(self._screenshot),
                html_path=relative_to_root(self._html),
                forced_status=status,
                error_code=exc.code,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            from playwright.async_api import TimeoutError as PlaywrightTimeout

            if isinstance(exc, PlaywrightTimeout):
                await self.save_artifacts(container)
                return evaluate(
                    [],
                    container=container,
                    carrier=self.carrier_code,
                    timeline_order=self.timeline_order,
                    checked_at=stamp,
                    screenshot_path=relative_to_root(self._screenshot),
                    html_path=relative_to_root(self._html),
                    forced_status="CHECK_FAILED",
                    error_code="TIMEOUT",
                    error=f"Timed out loading the tracking page: {exc}",
                )
            LOGGER.exception("Tracker failed for %s %s", self.carrier_code, container)
            closed = "has been closed" in str(exc) or "TargetClosed" in type(exc).__name__
            if not closed:
                await self.save_artifacts(container)
            return evaluate(
                [],
                container=container,
                carrier=self.carrier_code,
                timeline_order=self.timeline_order,
                checked_at=stamp,
                screenshot_path=relative_to_root(self._screenshot),
                html_path=relative_to_root(self._html),
                forced_status="CHECK_FAILED",
                error_code="NAVIGATION" if not closed else "CLOUDFLARE",
                error=str(exc),
            )
