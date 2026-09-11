"""Shared Playwright tracker contract."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from artifacts import checked_at, html_path, relative_to_root, screenshot_path
from config import CHALLENGE_WAIT_MS
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

    def __init__(self, page: Any, *, wait_for_challenge: bool = False) -> None:
        self.page = page
        self.wait_for_challenge = wait_for_challenge
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
            return await self.page.content()

    def detect_blocks(self, html: str) -> None:
        code = challenge_code(html)
        if code == "CLOUDFLARE":
            raise TrackerError("Cloudflare challenge detected.", "CLOUDFLARE")
        if code == "CAPTCHA":
            raise TrackerError("CAPTCHA detected.", "CAPTCHA")

    async def pass_or_wait_for_challenge(self) -> None:
        code = challenge_code(await self._visible_text())
        if not code:
            return
        if not self.wait_for_challenge:
            self.detect_blocks(await self._visible_text())
            return
        seconds = CHALLENGE_WAIT_MS // 1000
        print(
            f"Security check ({code}) is blocking the page. "
            f"Complete it in the browser window; tracking resumes automatically "
            f"(timeout {seconds}s)."
        )
        LOGGER.info("Waiting up to %ss for human to complete %s", seconds, code)
        try:
            await self.page.wait_for_function(
                _CHALLENGE_GONE_JS, timeout=CHALLENGE_WAIT_MS
            )
        except Exception as exc:  # noqa: BLE001
            raise TrackerError(
                f"Timed out waiting for you to complete the {code} check.",
                code,
            ) from exc
        await self.page.wait_for_timeout(1000)
        await self.dismiss_cookies(wait_ms=5000)
        still = challenge_code(await self._visible_text())
        if still:
            raise TrackerError(
                f"Timed out waiting for you to complete the {still} check.",
                still,
            )

    async def save_artifacts(self, container: str) -> None:
        self._screenshot = screenshot_path(container)
        self._html = html_path(container)
        try:
            await self.page.screenshot(path=str(self._screenshot), full_page=True)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Screenshot failed for %s", container)
            self._screenshot = None
        try:
            self._html.write_text(await self.page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            LOGGER.exception("HTML save failed for %s", container)
            self._html = None

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
            await self.save_artifacts(container)
            status = "MANUAL_CHECK_REQUIRED" if exc.code in {"CAPTCHA"} else "CHECK_FAILED"
            if exc.code == "CLOUDFLARE":
                status = "CHECK_FAILED"
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
                error_code="NAVIGATION",
                error=str(exc),
            )
