"""Shared Playwright tracker contract."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from artifacts import checked_at, html_path, relative_to_root, screenshot_path
from models import CanonicalEvent, TimelineOrder, TrackResult
from status_engine import evaluate

LOGGER = logging.getLogger("container_tracker")

COOKIE_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Agree')",
    "button:has-text('I agree')",
    "button:has-text('Allow all')",
)


class TrackerError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class BaseTracker(ABC):
    carrier_code: str = ""
    timeline_order: TimelineOrder = "oldest_first"
    tracking_url: str = ""

    def __init__(self, page: Any) -> None:
        self.page = page
        self._screenshot: Path | None = None
        self._html: Path | None = None

    async def dismiss_cookies(self, wait_ms: int = 3000) -> None:
        banner = self.page.locator(", ".join(COOKIE_SELECTORS))
        try:
            await banner.first.wait_for(state="visible", timeout=wait_ms)
            await banner.first.click(timeout=3000, force=True)
            try:
                await banner.first.wait_for(state="hidden", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            await self.page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            return

    def detect_blocks(self, html: str) -> None:
        blob = html.lower()
        if any(
            token in blob
            for token in (
                "managed challenge",
                "checking your browser",
                "cf-challenge",
                "attention required",
                "cloudflare",
            )
        ) and ("challenge" in blob or "checking your browser" in blob or "managed challenge" in blob):
            raise TrackerError("Cloudflare challenge detected.", "CLOUDFLARE")
        if any(token in blob for token in ("captcha", "recaptcha", "hcaptcha")):
            raise TrackerError("CAPTCHA detected.", "CAPTCHA")

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
            self.detect_blocks(await self.page.content())
            await self.search(container)
            await self.page.wait_for_timeout(1500)
            html = await self.page.content()
            self.detect_blocks(html)
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
