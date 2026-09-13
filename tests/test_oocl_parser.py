from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.base import TrackerError, looks_like_no_result
from trackers.oocl import (
    OoclTracker,
    _SEARCH_FIELD_SELECTORS,
    _SUBMIT_BUTTON_SELECTORS,
    is_oocl_entry_url,
    is_oocl_site_error_page,
    parse_oocl_html,
)

FIXTURES = Path(__file__).parent / "fixtures" / "oocl"
OOCL_404_VISIBLE = (
    "Oops!\n"
    "Page Not Found\n"
    "The page you are looking for might have been removed, had its name changed,\n"
    "or is temporarily unavailable. If you typed the page URL, check the spelling."
)


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_oocl_html(html)
    assert events[0].type == "DEPA"
    assert events[0].vessel == "OOCL SHANGHAI"
    assert events[0].voyage == "046E"
    result = evaluate(
        events,
        container="OOLU6895702",
        carrier="OOLU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11 03:40"
    assert result.vessel == "OOCL SHANGHAI"


def test_parse_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_oocl_html(html)
    result = evaluate(
        events,
        container="OOLU6895702",
        carrier="OOLU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"
    assert result.sailed is False


def test_search_field_selectors_skip_site_search_box():
    assert "input[type='text']" not in _SEARCH_FIELD_SELECTORS
    assert "#SEARCH_NUMBER" in _SEARCH_FIELD_SELECTORS


@pytest.mark.asyncio
async def test_first_visible_search_field_ignores_hidden_header_search():
    seen: list[str] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            seen.append(self.selector)
            return self.selector == "#SEARCH_NUMBER"

        async def wait_for(self, **kwargs):
            raise AssertionError("should not wait on hidden site search")

        async def scroll_into_view_if_needed(self):
            return None

    class Page:
        def locator(self, selector):
            return Locator(selector)

    field = await OoclTracker(Page())._first_visible_search_field()
    assert field is not None
    assert field.selector == "#SEARCH_NUMBER"
    assert seen[0] == "#SEARCH_NUMBER"


def test_site_404_is_navigation_not_container_miss():
    html = (FIXTURES / "page_not_found.html").read_text(encoding="utf-8")
    assert parse_oocl_html(html) == []
    assert is_oocl_site_error_page(OOCL_404_VISIBLE, html) is True
    assert looks_like_no_result(OOCL_404_VISIBLE) is False


def test_search_clicks_cargo_tracking_button():
    assert "#container_btn" in _SUBMIT_BUTTON_SELECTORS
    assert "#SEARCH_NUMBER" not in _SUBMIT_BUTTON_SELECTORS


def test_oocl_uses_system_chrome_like_cmdu():
    assert OoclTracker.use_system_chrome is True
    assert OoclTracker.wait_in_current_browser is True
    assert OoclTracker.system_chrome_host == "oocl.com"
    assert OoclTracker.system_chrome_challenge == "CAPTCHA"
    assert is_oocl_entry_url(
        "https://www.oocl.com/eng/ourservices/eservices/cargotracking/Pages/cargotracking.aspx"
    )
    assert not is_oocl_entry_url(
        "https://www.oocl.com/Pages/cargotracking/result?number=TCNU1971808"
    )


@pytest.mark.asyncio
async def test_parse_events_treats_site_404_as_navigation():
    html = (FIXTURES / "page_not_found.html").read_text(encoding="utf-8")

    class Page:
        async def content(self):
            return html

        async def evaluate(self, script):
            return OOCL_404_VISIBLE

    with pytest.raises(TrackerError) as exc:
        await OoclTracker(Page()).parse_events()
    assert exc.value.code == "NAVIGATION"


@pytest.mark.asyncio
async def test_search_does_not_open_removed_express_link():
    gotos: list[str] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            return "#SEARCH_NUMBER" in self.selector or "#container_btn" in self.selector

        async def click(self, **kwargs):
            return None

        async def fill(self, value):
            return None

        async def wait_for(self, **kwargs):
            return None

        async def scroll_into_view_if_needed(self):
            return None

        async def inner_text(self):
            return "Container #"

        async def select_option(self, **kwargs):
            return None

    class Keyboard:
        async def press(self, key):
            return None

    class Page:
        def __init__(self):
            self.keyboard = Keyboard()

        async def goto(self, url, **kwargs):
            gotos.append(url)

        def locator(self, selector):
            return Locator(selector)

        async def evaluate(self, script, arg=None):
            return ""

        def expect_popup(self, timeout=0):
            raise TimeoutError("no popup")

        async def wait_for_function(self, script, timeout=0):
            return None

        async def wait_for_timeout(self, ms):
            return None

    await OoclTracker(Page()).search("TCNU1971808")
    assert not any("ExpressLink" in url for url in gotos)


@pytest.mark.asyncio
async def test_search_without_field_is_selector_not_express_link():
    gotos: list[str] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            return False

        async def wait_for(self, **kwargs):
            raise TimeoutError(self.selector)

        async def scroll_into_view_if_needed(self):
            return None

        async def click(self, **kwargs):
            return None

        async def inner_text(self):
            return ""

        async def select_option(self, **kwargs):
            return None

    class Page:
        async def goto(self, url, **kwargs):
            gotos.append(url)

        def locator(self, selector):
            return Locator(selector)

        async def evaluate(self, script, arg=None):
            return ""

        async def wait_for_timeout(self, ms):
            return None

    with pytest.raises(TrackerError) as exc:
        await OoclTracker(Page()).search("TCNU1971808")
    assert exc.value.code == "SELECTOR"
    assert not any("ExpressLink" in url for url in gotos)


@pytest.mark.asyncio
async def test_search_adopts_new_result_tab():
    class Locator:
        def __init__(self, selector: str, page: "Page"):
            self.selector = selector
            self.page = page
            self.first = self

        async def is_visible(self, timeout=0):
            return "#SEARCH_NUMBER" in self.selector or "#container_btn" in self.selector

        async def click(self, **kwargs):
            if "#container_btn" in self.selector:
                self.page.context.pages.append(self.page.result)

        async def fill(self, value):
            return None

        async def wait_for(self, **kwargs):
            return None

        async def scroll_into_view_if_needed(self):
            return None

        async def inner_text(self):
            return "Container #"

        async def select_option(self, **kwargs):
            return None

    class Keyboard:
        async def press(self, key):
            return None

    class Context:
        def __init__(self):
            self.pages = []

    class Page:
        def __init__(self, url: str, context: Context):
            self.url = url
            self.context = context
            self.keyboard = Keyboard()
            self.result = None
            self._closed = False

        def is_closed(self):
            return self._closed

        async def close(self):
            self._closed = True
            if self in self.context.pages:
                self.context.pages.remove(self)

        async def goto(self, url, **kwargs):
            self.url = url

        def locator(self, selector):
            return Locator(selector, self)

        async def evaluate(self, script, arg=None):
            return ""

        async def wait_for_function(self, script, timeout=0):
            return None

        async def wait_for_timeout(self, ms):
            return None

    context = Context()
    entry = Page(
        "https://www.oocl.com/eng/ourservices/eservices/cargotracking/Pages/cargotracking.aspx",
        context,
    )
    result = Page("https://www.oocl.com/Pages/ct/result?n=TCNU1971808", context)
    entry.result = result
    context.pages = [entry]
    tracker = OoclTracker(entry)
    await tracker.search("TCNU1971808")
    assert tracker.page is result
    assert result in context.pages


@pytest.mark.asyncio
async def test_return_to_entry_closes_result_tab():
    class Locator:
        def __init__(self):
            self.first = self

        async def is_visible(self, timeout=0):
            return False

        async def click(self, **kwargs):
            return None

    class Context:
        def __init__(self):
            self.pages = []

    class Page:
        def __init__(self, url: str, context: Context):
            self.url = url
            self.context = context
            self._closed = False

        def is_closed(self):
            return self._closed

        async def close(self):
            self._closed = True
            if self in self.context.pages:
                self.context.pages.remove(self)

        async def goto(self, url, **kwargs):
            self.url = url

        def locator(self, selector):
            return Locator()

        async def evaluate(self, script, arg=None):
            return ""

    context = Context()
    entry = Page(
        "https://www.oocl.com/eng/ourservices/eservices/cargotracking/Pages/cargotracking.aspx",
        context,
    )
    result = Page("https://www.oocl.com/Pages/ct/result?n=TCNU1971808", context)
    context.pages = [entry, result]
    tracker = OoclTracker(result)
    tracker._entry_page = entry
    tracker._result_urls = [result.url]
    await tracker._return_to_entry()
    assert tracker.page is entry
    assert result._closed is True
    assert result not in context.pages
    assert entry in context.pages
