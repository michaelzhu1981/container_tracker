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


@pytest.mark.parametrize("html", [
    '<div title=".hcaptcha.com">Provider: .hcaptcha.com</div>',
    'Cookie provider: .hcaptcha.com; Google reCAPTCHA privacy policy',
    '<script src="https://www.google.com/recaptcha/api.js"></script>',
    '<script>const text = "verify you are human; captcha-delivery.com";</script>',
    '<div hidden>Please complete the hCaptcha</div>',
    '<div style="display:none"><iframe src="https://hcaptcha.com/challenge"></iframe></div>',
    '<iframe src="https://www.google.com/recaptcha/api2/anchor?size=invisible"></iframe>',
])
def test_provider_disclosures_and_inactive_widgets_are_not_challenges(html):
    assert challenge_code(html) is None


@pytest.mark.parametrize("html", [
    '<iframe src="https://geo.captcha-delivery.com/captcha/" title="DataDome CAPTCHA"></iframe>',
    '<iframe src="https://newassets.hcaptcha.com/captcha/v1/?frame=challenge"></iframe>',
    '<iframe src="https://www.google.com/recaptcha/api2/bframe"></iframe>',
    'Please solve the CAPTCHA',
])
def test_active_captcha_still_detected(html):
    assert challenge_code(html) == "CAPTCHA"


def test_normal_result_with_cookie_provider_can_be_saved():
    assert is_query_screenshot_page(
        "Loaded on Vessel", '<div>Provider: .hcaptcha.com</div>'
    )


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


def test_cma_provisional_moves_disclaimer_is_not_no_result():
    visible = (
        "Tracking details\n"
        "Loaded on board\n"
        "TCLU8224047\n"
        "Gate out empty from depot\n"
        "Provisional moves not found, please feel free to use Contact Support"
    )
    assert not looks_like_no_result(visible)
    assert looks_like_no_result(
        "Your shipment was not found, please modify your search\n"
        "returned to the depot more than 15 days"
    )


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

    async def wait_for_function(self, script, timeout=0, polling=None):
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


@pytest.mark.asyncio
async def test_manual_challenge_slots_are_serialized():
    class Browser:
        def __init__(self):
            self.manual_challenge_lock = asyncio.Lock()
            self.should_abort = lambda: False

    browser = Browser()
    trackers = [
        DummyTracker(FakePage(text=""), browser=browser),
        DummyTracker(FakePage(text=""), browser=browser),
    ]
    active = 0
    max_active = 0

    async def occupy(tracker):
        nonlocal active, max_active
        async with tracker._manual_challenge_slot():
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(occupy(tracker) for tracker in trackers))
    assert max_active == 1


def test_wait_does_not_clear_when_js_gone_but_snapshot_still_captcha(monkeypatch):
    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 400)
    monkeypatch.setattr("trackers.base.CHALLENGE_RETRY_DELAYS", ())
    page = FakePage(text="Please complete the CAPTCHA")

    async def pretend_gone(script, timeout=0, polling=None):
        page.calls += 1
        return True

    page.wait_for_function = pretend_gone  # type: ignore[method-assign]
    tracker = DummyTracker(page, wait_for_challenge=False)
    with pytest.raises(TrackerError) as exc:
        asyncio.run(tracker.pass_or_wait_for_challenge())
    assert exc.value.code == "CAPTCHA"
    assert page.calls >= 1
    assert challenge_code(page.text) == "CAPTCHA"


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

    async def pretend_gone(script, timeout=0, polling=None):
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


@pytest.mark.parametrize("tracker_name", ["cma", "maersk"])
def test_cmdu_maeu_keep_verification_in_same_window(monkeypatch, tracker_name):
    from trackers.cma import CmaTracker
    from trackers.maersk import MaerskTracker

    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 50)
    page = FakePage(text="Please complete the CAPTCHA", succeed_on_call=1)
    browser = _HandoffBrowser(page)
    messages = []
    browser.on_challenge = messages.append
    cls = CmaTracker if tracker_name == "cma" else MaerskTracker
    tracker = cls(page, wait_for_challenge=True, browser=browser)
    assert asyncio.run(tracker.pass_or_wait_for_challenge()) is True
    assert browser.handed is False
    assert tracker.page is page
    assert page.gotos == []
    assert any(item["mode"] == "current_browser" for item in messages)
    assert messages[-1]["code"] is None
    page.text = "Please complete the CAPTCHA"
    with pytest.raises(TrackerError) as exc:
        asyncio.run(tracker.pass_or_wait_for_challenge())
    assert exc.value.code == "CAPTCHA"
    assert page.calls == 1


def test_cma_prepare_session_waits_in_current_window(monkeypatch):
    from trackers.cma import CmaTracker

    monkeypatch.setattr("trackers.base.AUTO_CHALLENGE_WAIT_MS", 50)
    monkeypatch.setattr("trackers.base.CURRENT_BROWSER_WAIT_MS", 5_000)
    page = FakePage(text="Please complete the CAPTCHA", succeed_on_call=1)
    page.url = "https://www.cma-cgm.com/ebusiness/tracking"
    browser = _HandoffBrowser(page)
    tracker = CmaTracker(page, wait_for_challenge=True, browser=browser)
    asyncio.run(tracker.prepare_session())
    assert browser.handed is False
    assert tracker.page is page
    assert challenge_code(page.text) is None


def test_cma_prepare_session_skips_wait_when_page_is_clear():
    from trackers.cma import CmaTracker

    page = FakePage(text="Search\nContainer No.")
    page.url = "https://www.cma-cgm.com/ebusiness/tracking"
    browser = _HandoffBrowser(page)
    tracker = CmaTracker(page, wait_for_challenge=True, browser=browser)
    asyncio.run(tracker.prepare_session())
    assert browser.handed is False
    assert page.gotos == []
    assert page.calls == 0


@pytest.mark.asyncio
async def test_challenge_wait_responds_to_stop():
    page = FakePage(text="Please complete the CAPTCHA")
    waiting = asyncio.Event()
    cancelled_wait = asyncio.Event()
    stop = asyncio.Event()

    async def blocked(*args, **kwargs):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled_wait.set()

    page.wait_for_function = blocked
    browser = _HandoffBrowser(page)
    browser.should_abort = stop.is_set
    tracker = DummyTracker(page, browser=browser)
    task = asyncio.create_task(tracker.pass_or_wait_for_challenge())
    await waiting.wait()
    stop.set()
    with pytest.raises(TrackerError) as exc:
        await asyncio.wait_for(task, timeout=1)
    assert exc.value.code == "CANCELLED"
    assert cancelled_wait.is_set()
    assert browser.handed is False


def test_unattended_captcha_does_not_reload_or_open_manual_window():
    page = FakePage(text="Please complete the CAPTCHA")
    browser = _HandoffBrowser(page)
    tracker = DummyTracker(page, wait_for_challenge=False, browser=browser)
    with pytest.raises(TrackerError) as exc:
        asyncio.run(tracker.pass_or_wait_for_challenge())
    assert exc.value.code == "CAPTCHA"
    assert page.calls == 1
    assert page.gotos == []
    assert browser.handed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["page_replaced", "not_submitted", "empty_form", "results_present"])
async def test_search_resumes_after_challenge_without_duplicate_queries(recovery):
    class RecoveringTracker(DummyTracker):
        def __init__(self):
            super().__init__(FakePage(text="Search"))
            self.searches = []
            self.waits = 0
            self.parses = 0

        async def search(self, container):
            self.searches.append(container)
            self._search_submitted = recovery != "not_submitted" or len(self.searches) > 1

        async def pass_or_wait_for_challenge(self):
            self.waits += 1
            if self.waits == 2:
                if recovery == "page_replaced":
                    self.page = FakePage(text="Search")
                return True
            return False

        async def parse_events(self):
            self.parses += 1
            if recovery == "empty_form" and self.parses == 1:
                raise TrackerError("No table after challenge", "PARSE")
            return []

        async def save_artifacts(self, *args, **kwargs):
            pass

    tracker = RecoveringTracker()
    await tracker.track("ECMU7271573")
    expected = 1 if recovery == "results_present" else 2
    assert tracker.searches == ["ECMU7271573"] * expected


def test_maersk_rebinds_response_listener_after_page_replacement():
    from trackers.maersk import MaerskTracker

    page = FakePage(text="Search")
    tracker = MaerskTracker(page)
    asyncio.run(tracker._bind_tracking_response())
    replacement = FakePage(text="Search")
    tracker.page = replacement
    with pytest.raises(TrackerError):
        asyncio.run(tracker.search("HASU4566923"))
    assert replacement._ct_maersk_bound is True


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


def test_save_artifacts_ignores_missing_screenshot_file(tmp_path, monkeypatch):
    monkeypatch.setattr("trackers.base.html_path", lambda container: tmp_path / f"{container}.html")
    monkeypatch.setattr("trackers.base.screenshot_path", lambda container: tmp_path / f"{container}.png")

    class NoShotPage(FakePage):
        async def screenshot(self, path=None, full_page=False, clip=None):
            return None

    page = NoShotPage(text="Tracking details\nLoaded on board\nTCLU8224047")
    tracker = DummyTracker(page)
    asyncio.run(tracker.save_artifacts("TCLU8224047"))
    assert tracker._screenshot is None
    assert not (tmp_path / "TCLU8224047.png").exists()


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
