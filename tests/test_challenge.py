import asyncio

import pytest

from trackers.base import (
    BaseTracker,
    TrackerError,
    challenge_code,
    is_query_screenshot_page,
    looks_like_no_result,
)


def test_cloudflare_visible_text():
    assert (
        challenge_code("checking your browser\nWhy did this happen?") == "CLOUDFLARE"
    )
    assert challenge_code("Security Check\nManaged Challenge") == "CLOUDFLARE"


def test_captcha_visible_text():
    assert challenge_code("Please complete the hCaptcha") == "CAPTCHA"
    assert challenge_code('<iframe src="https://geo.captcha-delivery.com/captcha/" title="DataDome CAPTCHA">') == "CAPTCHA"


def test_datadome_sdk_script_is_not_a_challenge():
    assert challenge_code("https://js.datadome.co/tags.js") is None


def test_akamai_access_denied_is_cloudflare():
    assert (
        challenge_code("Access Denied\nerrors.edgesuite.net\nReference #18.123")
        == "CLOUDFLARE"
    )


def test_normal_tracking_page_is_not_a_challenge():
    assert challenge_code("Cargo Tracking\nSearch\nContainer No.") is None
    assert challenge_code("Cookie Policy and Cloudflare CDN mention") is None


def test_one_i18n_bundle_is_not_treated_as_no_result():
    visible = (
        "Total 1 result\n"
        "Loaded on Vessel at Port of Loading\n"
        "Vessel Departure from Port of Loading"
    )
    assert not looks_like_no_result(visible)
    assert looks_like_no_result("No Results Found\nPlease modify your search.")
    assert looks_like_no_result("Can't identify your input")


def test_query_screenshot_keeps_tracking_results_only():
    assert is_query_screenshot_page(
        "Latest Event\nLoaded SALALAH",
        '<div class="hal-event-tracking"></div>',
    )
    assert is_query_screenshot_page(
        "Container Status\nOn Board VUNG TAU",
        "<table aria-label='Container Status Information'></table>",
    )
    assert is_query_screenshot_page(
        "Total 1 result\nLoaded on Vessel at Port of Loading",
        '<table class="EventTable_table-container"></table>',
    )
    assert is_query_screenshot_page(
        "Export Loaded on Vessel\nEmpty to Shipper",
        '<div class="msc-flow-tracking__step"></div>',
    )
    assert is_query_screenshot_page(
        "LOADED ON BOARD\nVESSEL DEPARTURE",
        '<div id="gridTrackingDetails"></div>',
    )
    assert is_query_screenshot_page(
        "Gate In Full\nVessel Departed",
        "<table><tr><td>Loaded</td></tr></table>",
    )
    assert not is_query_screenshot_page(
        "checking your browser\nVerify you are human",
        "<html>Security Check</html>",
    )
    assert not is_query_screenshot_page(
        "This website uses cookies. Agree",
        '<div id="onetrust-banner-sdk">Cookie Policy</div>',
    )
    assert not is_query_screenshot_page(
        "Log in to your account\nSign in",
        "<form>login</form>",
    )


class _Invisible:
    def first(self):
        return self

    async def wait_for(self, **kwargs):
        raise TimeoutError("not visible")

    async def is_visible(self, timeout=0):
        return False

    async def click(self, **kwargs):
        return None


class FakePage:
    def __init__(self, *, text: str, succeed_on_call: int | None = None) -> None:
        self.text = text
        self.succeed_on_call = succeed_on_call
        self.calls = 0
        self.gotos: list[str] = []
        self.url = ""

    async def evaluate(self, script):
        return self.text

    async def content(self):
        return self.text

    def locator(self, selector):
        return _Invisible()

    def get_by_role(self, role):
        return _Invisible()

    def on(self, event, handler):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def goto(self, url, wait_until=None):
        self.gotos.append(url)
        self.url = url

    async def wait_for_function(self, script, timeout=0):
        self.calls += 1
        if self.succeed_on_call is not None and self.calls >= self.succeed_on_call:
            self.text = "Latest Event\nShipment details\nContainer No."
            return True
        raise TimeoutError("still challenged")

    async def screenshot(self, path=None, full_page=False, clip=None):
        raise AssertionError("screenshot should not run on a non-query page")


class DummyTracker(BaseTracker):
    carrier_code = "HLCU"
    tracking_url = "https://example.com/track"

    async def open_page(self) -> None:
        return None

    async def search(self, container: str) -> None:
        return None

    async def parse_events(self):
        return []


def test_auto_wait_clears_without_human(monkeypatch):
    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 200)
    monkeypatch.setattr("trackers.base.CHALLENGE_RETRY_DELAYS", ())
    page = FakePage(text="checking your browser", succeed_on_call=1)
    tracker = DummyTracker(page, wait_for_challenge=False)
    asyncio.run(tracker.pass_or_wait_for_challenge())
    assert challenge_code(page.text) is None
    assert page.gotos == []


def test_auto_retry_reloads_then_clears(monkeypatch):
    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 50)
    monkeypatch.setattr("trackers.base.CHALLENGE_RETRY_DELAYS", (0.0,))
    page = FakePage(text="managed challenge", succeed_on_call=2)
    tracker = DummyTracker(page, wait_for_challenge=False)
    asyncio.run(tracker.pass_or_wait_for_challenge())
    assert page.gotos == [DummyTracker.tracking_url]
    assert challenge_code(page.text) is None


class _HandoffBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.handed = False

    async def hand_off_to_system_chrome(self, url: str) -> bool:
        self.handed = True
        assert url
        self.page.text = "Latest Event\nSearch\nContainer No."
        return True


def test_auto_wait_false_success_still_hands_off(monkeypatch):
    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 50)
    page = FakePage(text="verify you are human")

    async def pretend_gone(script, timeout=0):
        page.calls += 1
        return True

    page.wait_for_function = pretend_gone  # type: ignore[method-assign]
    browser = _HandoffBrowser(page)
    tracker = DummyTracker(page, wait_for_challenge=True, browser=browser)
    asyncio.run(tracker.pass_or_wait_for_challenge())
    assert browser.handed is True
    assert challenge_code(page.text) is None


def test_open_tracking_reuses_existing_carrier_tab():
    page = FakePage(text="Search\nContainer No.")
    page.url = "https://www.cma-cgm.com/ebusiness/tracking"
    tracker = DummyTracker(page)
    tracker.tracking_url = "https://www.cma-cgm.com/ebusiness/tracking"
    assert asyncio.run(tracker.open_tracking_or_reuse("cma-cgm.com")) is True
    assert page.gotos == []


def test_open_tracking_stops_when_landing_is_challenged():
    page = FakePage(text="verify you are human")
    tracker = DummyTracker(page)
    assert asyncio.run(tracker.open_tracking_or_reuse("maersk.com")) is False
    assert page.gotos == [DummyTracker.tracking_url]


def test_human_handoff_opens_system_chrome_instead_of_clicking_widget(monkeypatch):
    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 50)
    page = FakePage(text="verify you are human")
    browser = _HandoffBrowser(page)
    tracker = DummyTracker(page, wait_for_challenge=True, browser=browser)
    asyncio.run(tracker.pass_or_wait_for_challenge())
    assert browser.handed is True
    assert page.gotos == []
    assert challenge_code(page.text) is None


def test_human_wait_used_after_auto_fails(monkeypatch):
    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 50)
    monkeypatch.setattr("trackers.base.CHALLENGE_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr("trackers.base.CHALLENGE_WAIT_MS", 5_000)
    page = FakePage(text="verify you are human", succeed_on_call=2)
    tracker = DummyTracker(page, wait_for_challenge=True)
    asyncio.run(tracker.pass_or_wait_for_challenge())
    assert page.calls == 2
    assert page.gotos == []
    assert challenge_code(page.text) is None


def test_save_artifacts_skips_screenshot_on_cloudflare_page(tmp_path, monkeypatch):
    monkeypatch.setattr("trackers.base.html_path", lambda container: tmp_path / f"{container}.html")
    monkeypatch.setattr("trackers.base.screenshot_path", lambda container: tmp_path / f"{container}.png")
    page = FakePage(text="checking your browser\nVerify you are human")
    tracker = DummyTracker(page)
    asyncio.run(tracker.save_artifacts("HLXU1234567"))
    assert tracker._screenshot is None
    assert not (tmp_path / "HLXU1234567.png").exists()
    assert (tmp_path / "HLXU1234567.html").exists()


def test_no_human_fallback_raises_after_auto_fails(monkeypatch):
    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 50)
    monkeypatch.setattr("trackers.base.CHALLENGE_RETRY_DELAYS", ())
    page = FakePage(text="checking your browser")
    tracker = DummyTracker(page, wait_for_challenge=False)
    with pytest.raises(TrackerError) as exc:
        asyncio.run(tracker.pass_or_wait_for_challenge())
    assert exc.value.code == "CLOUDFLARE"


def test_cma_search_does_not_use_get_url():
    from trackers.cma import CmaTracker

    page = FakePage(text="Search")
    page.url = "https://www.cma-cgm.com/ebusiness/tracking"
    tracker = CmaTracker(page)
    with pytest.raises(TrackerError) as exc:
        asyncio.run(tracker.search("CMAU1234567"))
    assert exc.value.code == "SELECTOR"
    assert all("tracking/search" not in url for url in page.gotos)


def test_maersk_search_does_not_deeplink_container():
    from trackers.maersk import TRACK_URL, MaerskTracker

    page = FakePage(text="Search")
    page.url = "https://www.maersk.com/tracking/"
    tracker = MaerskTracker(page)
    with pytest.raises(TrackerError) as exc:
        asyncio.run(tracker.search("HASU4566923"))
    assert exc.value.code == "SELECTOR"
    assert all("HASU4566923" not in url for url in page.gotos)
    assert page.gotos == [TRACK_URL]
