"""Presentation dashboard checks over live HTTP; no Office or desktop automation."""

import base64
import re
import time
from urllib.parse import urlsplit

import pytest
import test_browser
from playwright.sync_api import expect

from conty.config import load_config

# Register the existing fixtures here without collecting test_browser's tests.
browser = test_browser.browser
live_server = test_browser.live_server
paired_page = test_browser.paired_page

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "ScLttAAAAABJRU5ErkJggg=="
)
DECK_A = "window-a-12345678"
DECK_B = "window-b-87654321"


def deck(identity, revision):
    return {
        "id": identity,
        "title": "Quarterly.pptx",
        "revision": revision,
        "connected": True,
        "view": "read",
        "current_slide_id": "slide-101",
        "can_control": True,
        "needs_refresh": False,
        "observed_at": time.monotonic(),
        "preview_count": 2,
        "slides": [
            {
                "id": slide_id,
                "title": title,
                "number": number,
                "hidden": number == 2,
                "page": number,
                "preview_url": (
                    f"/api/presentations/{identity}/slides/{slide_id}/preview?revision={revision}"
                ),
            }
            for number, (slide_id, title) in enumerate(
                [("slide-101", "Opening"), ("slide-202", "Results")], 1
            )
        ],
    }


class PresentationAPI:
    """Only native presentation data is fake; dashboard assets and pairing are live."""

    def __init__(self, page):
        self.page = page
        self.decks = [deck(DECK_A, "revision-a-1"), deck(DECK_B, "revision-b-1")]
        self.posts = []
        self.commands = {}
        self.lookups = []
        self.list_status = 200
        self.list_code = "permission_denied"
        self.list_message = "PowerPoint Automation access is required. Authorize it locally."
        self.list_abort = False
        self.discovering = None
        self.fresh_observations = True
        self.list_reads = 0
        self.hold_list = False
        self.held_lists = []
        self.post_error = None
        self.lookup_status = 200
        self.drop_next = False
        self.hold_next = False
        self.held = None
        self.image_reads = []
        self.image_failures = 0
        self.image_always_fail = False
        self.hold_images = False
        self.held_images = []
        page.route(re.compile(r".*/api/presentations(?:/.*)?$"), self.handle)

    @staticmethod
    def error(route, status, code, message):
        route.fulfill(status=status, json={"error": {"code": code, "message": message}})

    def handle(self, route):
        request = route.request
        path = urlsplit(request.url).path
        parts = path.strip("/").split("/")
        if path == "/api/presentations":
            assert request.method == "GET"
            self.list_reads += 1
            if self.hold_list:
                self.held_lists.append(route)
            else:
                self.respond_list(route)
        elif len(parts) == 6 and parts[3] == "slides" and parts[5] == "preview":
            self.image_reads.append(request.url)
            if self.hold_images:
                self.held_images.append(route)
            elif self.image_always_fail or self.image_failures:
                self.image_failures = max(0, self.image_failures - 1)
                self.error(route, 429, "request_rate_limited", "Try this image again")
            else:
                route.fulfill(content_type="image/png", body=PNG)
        elif len(parts) == 4 and parts[3] == "commands" and request.method == "POST":
            payload = request.post_data_json
            self.posts.append({"deck": parts[2], "payload": payload, "headers": request.headers})
            identity = f"command-{len(self.posts)}"
            record = {
                "id": identity,
                "presentation_id": parts[2],
                "revision": payload["revision"],
                "operation": payload["operation"],
                "slide_id": payload.get("slide_id"),
                "state": "queued",
                "message": "Waiting for PowerPoint",
            }
            self.commands[identity] = record
            if self.post_error:
                status, code, message = self.post_error
                self.error(route, status, code, message)
            elif self.drop_next:
                self.drop_next = False
                route.abort("failed")  # Accepted by the mock, but the response is lost.
            elif self.hold_next:
                self.hold_next = False
                self.held = (route, record)
            else:
                route.fulfill(status=202, json=record)
        elif len(parts) == 5 and parts[3] == "commands":
            self.lookups.append((parts[2], parts[4]))
            if self.lookup_status == 401:
                self.error(route, 401, "session_expired", "Pair this browser again")
            elif self.lookup_status == 404:
                self.error(route, 404, "command_not_found", "Command is not available")
            else:
                assert self.commands[parts[4]]["presentation_id"] == parts[2]
                route.fulfill(json=self.commands[parts[4]])
        else:
            raise AssertionError(f"Unexpected presentation request: {request.method} {path}")

    def take_held_list(self):
        deadline = time.monotonic() + 5
        while not self.held_lists:
            assert time.monotonic() < deadline, "Discovery request was not held"
            self.page.wait_for_timeout(10)
        return self.held_lists.pop(0)

    def respond_list(self, route):
        if self.list_abort:
            route.abort("failed")
        elif self.list_status == 401:
            self.error(route, 401, "session_expired", "Pair this browser again")
        elif self.list_status != 200:
            self.error(route, self.list_status, self.list_code, self.list_message)
        else:
            if self.fresh_observations and self.discovering is not True:
                for item in self.decks:
                    item["observed_at"] = time.monotonic()
            result = {"presentations": self.decks}
            if self.discovering is not None:
                result["discovering"] = self.discovering
            route.fulfill(json=result)

    def scan(self):
        with self.page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/presentations"
        ):
            self.page.get_by_role("button", name="Scan presentations", exact=True).click()

    def poll(self, *, trigger=True):
        # Exercise the public connection event, not the presentation closure's internals.
        # If a poll is already running, the normal two-second timer supplies the next one.
        with self.page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/presentations",
            timeout=5000,
        ):
            if trigger:
                self.page.evaluate("() => document.dispatchEvent(new Event('conty:connection'))")

    def poll_command(self):
        with self.page.expect_response(lambda response: "/commands/" in response.url):
            self.page.evaluate("() => document.dispatchEvent(new Event('conty:connection'))")

    def assert_post(self, index, identity, revision, operation, slide_id=None):
        post = self.posts[index]
        payload = post["payload"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["request_id"])
        expected = {
            "request_id": payload["request_id"],
            "revision": revision,
            "operation": operation,
        }
        if slide_id is not None:
            expected["slide_id"] = slide_id
        assert post["deck"] == identity
        assert payload == expected
        assert post["headers"]["x-csrf-token"] == self.page.evaluate("() => state.csrf")
        assert post["headers"]["origin"] == urlsplit(self.page.url)._replace(path="").geturl()


def role(root, name):
    return root.locator(f'[data-role="presentation-{name}"]')


def card(root, slide_id="slide-202"):
    return root.locator(f'[data-slide-id="{slide_id}"]')


def select(page, identity=DECK_A, root=None):
    root = root if root is not None else page.locator(".presentation-panel")
    role(root, "select").select_option(identity)
    expect(root.locator(".slide-card").first).to_be_visible()
    return root


def activate(root, slide_id="slide-202"):
    card(root, slide_id).dblclick()


@pytest.fixture
def widget_factory(browser, live_server):
    """Use the real renderer and pairing with widgets in the dashboard definition."""
    contexts = []

    def create(*, count=1, configure=None, width=900, wait_for_list=True, reference=None):
        context, page = paired_page(browser, live_server, width=width)
        contexts.append(context)
        api = PresentationAPI(page)
        if configure:
            configure(api)

        def dashboard(route):
            definition = route.fetch().json()
            if reference is not None:
                definition = {**reference, "revision": definition["revision"]}
            elif count:
                definition["dashboards"][0]["sections"].insert(
                    0,
                    {
                        "id": "presentations",
                        "title": "Presentations",
                        "layout": {"rows": 4 * count, "columns": 8},
                        "widgets": [
                            {
                                "id": f"powerpoint-{index}",
                                "type": "powerpoint",
                                "label": f"PowerPoint {index + 1}",
                                "position": {
                                    "row": 1 + 4 * index,
                                    "column": 1,
                                    "row_span": 4,
                                    "column_span": 8,
                                },
                            }
                            for index in range(count)
                        ],
                    },
                )
            route.fulfill(json=definition)

        page.route("**/api/dashboard", dashboard)
        page.reload()
        expect(page.locator("#dashboard")).to_be_visible()
        expect(page.locator(".presentation-panel")).to_have_count(count)
        if count and wait_for_list:
            expect(
                role(page.locator(".presentation-panel").first, "select").locator("option")
            ).to_have_count(len(api.decks) + 1)
        return page, api

    yield create
    for context in contexts:
        context.close()
    assert live_server[2].calls == [], "Presentation controls must not use native actions"


@pytest.fixture
def presentation_page(widget_factory):
    return widget_factory()


def test_multiple_decks_require_selection_and_double_click_sends_exact_target(presentation_page):
    page, api = presentation_page
    panel = page.locator(".presentation-panel")
    expect(role(panel, "select")).to_have_value("")
    expect(role(panel, "select").locator("option")).to_have_text(
        ["Choose a running presentation", "Quarterly.pptx · window-a", "Quarterly.pptx · window-b"]
    )
    expect(panel.locator(".slide-card")).to_have_count(0)
    select(page, DECK_B)
    selected = card(panel)
    selected.click()
    expect(selected).to_have_attribute("aria-pressed", "true")
    expect(role(panel, "selection-status")).to_contain_text("Double-click or double-tap")
    expect(selected.locator("img")).to_have_js_property("naturalWidth", 1)
    expect(selected.locator(".slide-badge")).to_have_attribute("title", "Hidden slide")
    expect(selected).to_have_attribute("data-page", "2")
    api.poll()
    assert api.posts == [], "Browsing thumbnails must not navigate PowerPoint"
    activate(panel)
    expect(role(panel, "message")).to_contain_text("Queued")
    api.assert_post(0, DECK_B, "revision-b-1", "show_slide", "slide-202")
    api.commands["command-1"].update(state="succeeded", message="Navigation completed")
    api.poll()
    expect(role(panel, "message")).to_have_text("Navigation completed")
    expect(page.locator("#activity-list")).to_contain_text("Navigation completed")
    assert set(api.lookups) == {(DECK_B, "command-1")}
    assert len(api.posts) == 1


def test_missing_deck_never_retargets_same_filename(presentation_page):
    page, api = presentation_page
    panel = select(page)
    card(panel).click()
    api.decks.pop(0)
    api.poll()
    expect(role(panel, "select")).to_have_value(DECK_A)
    expect(role(panel, "select").locator("option:checked")).to_have_text(
        "Previously selected presentation · Unavailable"
    )
    expect(role(panel, "info")).to_have_text("Presentation unavailable")
    expect(panel.locator(".slide-card")).to_have_count(0)
    expect(role(panel, "refresh")).to_be_disabled()
    api.poll()
    expect(role(panel, "select")).to_have_value(DECK_A)
    assert api.posts == []
    select(page, DECK_B)
    activate(panel)
    expect(role(panel, "message")).to_contain_text("Queued")
    api.assert_post(0, DECK_B, "revision-b-1", "show_slide", "slide-202")


def test_revision_change_clears_selection_and_activation_uses_new_revision(presentation_page):
    page, api = presentation_page
    panel = select(page)
    card(panel).click()
    api.decks[0] = deck(DECK_A, "revision-a-2")
    api.poll()
    expect(panel.locator('.slide-card[aria-pressed="true"]')).to_have_count(0)
    expect(card(panel).locator("img")).to_have_attribute(
        "src", f"/api/presentations/{DECK_A}/slides/slide-202/preview?revision=revision-a-2"
    )
    assert api.posts == []
    activate(panel)
    expect(role(panel, "message")).to_contain_text("Queued")
    api.assert_post(0, DECK_A, "revision-a-2", "show_slide", "slide-202")


@pytest.mark.parametrize(
    "outcome", ["lost_response", "outcome_unknown", "command_missing", "expired"]
)
def test_unknown_commands_are_not_retried_and_block_only_affected_deck(presentation_page, outcome):
    page, api = presentation_page
    panel = select(page)
    api.drop_next = outcome == "lost_response"
    activate(panel)
    if outcome != "lost_response":
        expect(role(panel, "message")).to_contain_text("Queued")
        if outcome == "outcome_unknown":
            api.commands["command-1"].update(
                state="outcome_unknown", message="Navigation outcome unknown"
            )
        elif outcome == "command_missing":
            api.lookup_status = 404
        else:
            page.evaluate("""() => {
                const now = performance.now.bind(performance);
                performance.now = () => now() + 121000;
            }""")
        api.poll()
    expect(role(panel, "message")).to_contain_text("Restart Conty locally")
    lookups = list(api.lookups)
    activate(panel)
    api.poll()
    page.wait_for_timeout(2200)
    assert len(api.posts) == 1
    assert api.lookups == lookups
    api.assert_post(0, DECK_A, "revision-a-1", "show_slide", "slide-202")
    select(page, DECK_B)
    api.lookup_status = 200
    activate(panel)
    expect(role(panel, "message")).to_contain_text("Queued")
    assert len(api.posts) == 2
    api.assert_post(1, DECK_B, "revision-b-1", "show_slide", "slide-202")


@pytest.mark.parametrize(
    "status,code,message",
    [
        (403, "permission_denied", "Slide jumps require Accessibility access."),
        (409, "stale_revision", "Presentation revision has changed"),
        (500, "internal_error", "The host could not complete this request."),
    ],
)
def test_submission_errors_are_visible_and_never_retried(presentation_page, status, code, message):
    page, api = presentation_page
    panel = select(page)
    api.post_error = status, code, message
    activate(panel)
    expect(role(panel, "message")).to_contain_text(
        "Submission outcome unknown" if status == 500 else message
    )
    api.poll()
    page.wait_for_timeout(2200)
    assert len(api.posts) == 1


def test_live_marker_requires_fresh_read_observation_not_selection_or_command(presentation_page):
    page, api = presentation_page
    panel = select(page)
    live = card(panel, "slide-101")
    selected = card(panel)
    expect(live).to_have_attribute("aria-current", "true")
    activate(panel)
    expect(role(panel, "message")).to_contain_text("Queued")
    expect(selected).not_to_have_attribute("aria-current", "true")
    api.commands["command-1"].update(state="succeeded", message="Completed")
    api.poll()
    expect(live).to_have_attribute("aria-current", "true")
    api.fresh_observations = False
    api.discovering = True
    page.evaluate("""() => {
        const now = performance.now.bind(performance);
        performance.now = () => now() + 7000;
    }""")
    api.poll()
    expect(panel.locator("[aria-current]")).to_have_count(0)
    expect(selected).to_have_attribute("aria-pressed", "true")
    activate(panel)
    assert len(api.posts) == 1
    # Cached scans do not renew freshness; a changed host observation does, even
    # if the following native discovery has already started.
    api.decks[0]["observed_at"] += 1
    api.poll()
    expect(live).to_have_attribute("aria-current", "true")
    api.decks[0].update(view="edit", current_slide_id="slide-202", can_control=False)
    api.poll()
    expect(panel.locator("[aria-current]")).to_have_count(0)
    activate(panel)
    assert len(api.posts) == 1


def test_initial_pending_cache_is_not_a_live_observation(widget_factory):
    def configure(api):
        api.discovering = True

    page, api = widget_factory(configure=configure)
    panel = select(page, DECK_B)
    expect(role(panel, "select")).to_have_attribute("aria-busy", "true")
    expect(panel.locator("[aria-current]")).to_have_count(0)
    activate(panel)
    assert api.posts == []
    api.discovering = False
    api.poll()
    expect(card(panel, "slide-101")).to_have_attribute("aria-current", "true")


def test_disconnect_removes_previews_and_blocks_activation(presentation_page):
    page, api = presentation_page
    panel = select(page)
    expect(panel.locator("img")).to_have_count(2)
    page.route("**/api/status", lambda route: route.abort("failed"))
    page.evaluate("() => window.dispatchEvent(new Event('offline'))")
    expect(page.locator("#connection")).to_have_text("Reconnecting")
    expect(role(panel, "select")).to_be_disabled()
    expect(role(panel, "refresh")).to_be_disabled()
    expect(panel.locator("img")).to_have_count(0)
    expect(panel.locator(".slide-card")).to_have_count(0)
    page.evaluate("() => document.dispatchEvent(new Event('conty:connection'))")
    page.wait_for_timeout(300)
    assert api.posts == []


@pytest.mark.parametrize("phase", ["pending", "submitting"])
def test_session_expiry_clears_selection_images_and_late_command(
    presentation_page, live_server, phase
):
    page, api = presentation_page
    panel = select(page)
    api.hold_next = phase == "submitting"
    activate(panel)
    if phase == "pending":
        expect(role(panel, "message")).to_contain_text("Queued")
        api.lookup_status = 401
        api.poll_command()
    else:
        expect(role(panel, "message")).to_contain_text("Submitting")
        page.route(
            "**/api/status",
            lambda route: api.error(route, 401, "session_expired", "Pair this browser again"),
        )
        page.evaluate("() => checkStatus(false)")
    expect(page.locator("#connection")).to_have_text("Pairing required")
    expect(page.locator("#dashboard")).to_be_hidden()
    expect(panel.locator(".slide-card")).to_have_count(0)
    expect(role(panel, "select")).to_have_value("")
    expect(role(panel, "select").locator("option")).to_have_count(1)
    expect(role(panel, "message")).to_be_empty()
    lookups = list(api.lookups)
    api.lookup_status = 200
    if phase == "submitting":
        page.unroute("**/api/status")
    page.get_by_label("Enter the six-digit code shown on your computer.").fill(
        live_server[1].state.sessions.code
    )
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#dashboard")).to_be_visible()
    expect(role(panel, "select").locator("option")).to_have_count(3)
    if phase == "submitting":
        route, record = api.held
        route.fulfill(status=202, json=record)
    api.poll()
    expect(role(panel, "select")).to_have_value("")
    expect(panel.locator("img")).to_have_count(0)
    expect(role(panel, "message")).to_be_empty()
    assert api.lookups == lookups
    assert len(api.posts) == 1


def test_preview_failure_has_bounded_safe_retries_and_manual_image_recovery(presentation_page):
    page, api = presentation_page
    api.image_always_fail = True
    panel = select(page)
    expect(role(panel, "preview-status")).to_contain_text("2 unavailable", timeout=10000)
    assert len(api.image_reads) == 6
    page.wait_for_timeout(2200)
    assert len(api.image_reads) == 6
    assert api.posts == []
    assert api.lookups == []
    for url in api.image_reads:
        assert f"/api/presentations/{DECK_A}/slides/" in url
        assert "revision=revision-a-1" in url
    api.image_always_fail = False
    role(panel, "retry-previews").click()
    expect(role(panel, "preview-status")).to_contain_text("2/2 preview images loaded")
    expect(role(panel, "retry-previews")).to_be_hidden()
    assert len(api.image_reads) == 8
    assert api.posts == []


def test_single_failed_preview_recovers_automatically_without_export(presentation_page):
    page, api = presentation_page
    api.image_failures = 1
    panel = select(page)
    expect(role(panel, "preview-status")).to_contain_text("2/2 preview images loaded")
    assert len(api.image_reads) == 3
    assert api.posts == []


def test_sole_connected_deck_is_selected_without_user_action(widget_factory):
    def configure(api):
        api.decks[1]["connected"] = False

    page, api = widget_factory(configure=configure)
    panel = page.locator(".presentation-panel")
    expect(role(panel, "select")).to_have_value(DECK_A)
    expect(card(panel, "slide-101")).to_have_attribute("aria-current", "true")
    assert api.posts == []
    api.decks.pop()
    api.poll()
    expect(role(panel, "select")).to_be_hidden()
    assert api.posts == []


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "succeeded"])
def test_auto_export_edit_only_once_per_revision_even_after_failure(presentation_page, terminal):
    page, api = presentation_page
    api.decks = [api.decks[0]]
    api.decks[0].update(needs_refresh=True)
    for slide in api.decks[0]["slides"]:
        slide.update(preview_url=None, page=None)
    api.poll()
    panel = page.locator(".presentation-panel")
    expect(role(panel, "select")).to_have_value(DECK_A)
    expect(role(panel, "select")).to_be_hidden()
    expect(role(panel, "info")).to_have_text("Stop slideshow to load previews")
    api.poll()
    assert api.posts == [], "Read view must never auto-export"
    api.decks[0].update(view="edit", can_control=False, current_slide_id=None)
    api.poll()
    expect(role(panel, "message")).to_contain_text("queued")
    api.assert_post(0, DECK_A, "revision-a-1", "refresh")
    expect(role(panel, "message")).to_contain_text("90 seconds")
    api.commands["command-1"].update(state=terminal, message=f"Export {terminal}")
    # Keep needs_refresh true to prove even an unsuccessful export is not looped.
    api.poll()
    expect(role(panel, "message")).to_have_text(
        "PDF previews ready" if terminal == "succeeded" else f"Export {terminal}"
    )
    api.poll()
    page.wait_for_timeout(2200)
    assert len(api.posts) == 1
    api.decks[0]["revision"] = "revision-a-2"
    api.poll()
    expect(role(panel, "message")).to_contain_text("queued")
    assert len(api.posts) == 2
    api.assert_post(1, DECK_A, "revision-a-2", "refresh")


def test_auto_export_submission_failure_is_not_automatically_retried(presentation_page):
    page, api = presentation_page
    api.post_error = 403, "permission_denied", "Authorize PowerPoint locally"
    api.decks = [api.decks[0]]
    api.decks[0].update(view="edit", needs_refresh=True, can_control=False)
    api.poll()
    panel = page.locator(".presentation-panel")
    expect(role(panel, "message")).to_have_text("Authorize PowerPoint locally")
    api.poll()
    page.wait_for_timeout(2200)
    assert len(api.posts) == 1


@pytest.mark.parametrize("operation", ["show_slide", "refresh"])
@pytest.mark.parametrize("command_state", ["queued", "running"])
def test_pending_status_polls_without_discovery_or_resubmission(
    presentation_page, operation, command_state
):
    page, api = presentation_page
    if operation == "refresh":
        api.decks[0].update(view="edit", can_control=False)
        api.poll()
    panel = select(page)
    reads = api.list_reads
    if operation == "refresh":
        role(panel, "refresh").click()
    else:
        activate(panel)
    expect(panel).to_have_attribute("aria-busy", "true")
    api.commands["command-1"].update(state=command_state)
    lookups = len(api.lookups)
    page.wait_for_timeout(850)
    assert 2 <= len(api.lookups) - lookups <= 6
    assert api.list_reads == reads
    expect(role(panel, "scan")).to_be_disabled()
    activate(panel)
    assert len(api.posts) == 1
    api.commands["command-1"].update(state="succeeded", message="Completed")
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/api/presentations", timeout=1500
    ):
        pass
    expect(panel).to_have_attribute("aria-busy", "false")
    assert len(api.posts) == 1


def test_inflight_submission_does_not_start_discovery(presentation_page):
    page, api = presentation_page
    panel = select(page)
    api.hold_next = True
    reads = api.list_reads
    activate(panel)
    expect(role(panel, "message")).to_contain_text("Submitting")
    page.wait_for_timeout(2200)
    assert api.list_reads == reads
    assert api.lookups == []
    activate(panel)
    assert len(api.posts) == 1
    route, record = api.held
    route.fulfill(status=202, json=record)
    expect(role(panel, "message")).to_contain_text("Queued")


def test_keyboard_browses_without_submitting_and_enter_activates(presentation_page):
    page, api = presentation_page
    panel = select(page)
    first, second = card(panel, "slide-101"), card(panel)
    first.focus()
    first.press("ArrowRight")
    expect(second).to_be_focused()
    second.press("Home")
    expect(first).to_be_focused()
    first.press("End")
    expect(second).to_be_focused()
    second.press("ArrowLeft")
    expect(first).to_be_focused()
    first.press("Space")
    expect(first).to_have_attribute("aria-pressed", "true")
    assert api.posts == []
    second.focus()
    second.press("Enter")
    expect(role(panel, "message")).to_contain_text("Queued")
    api.assert_post(0, DECK_A, "revision-a-1", "show_slide", "slide-202")


def touch(target, event, x, y):
    target.dispatch_event(
        event,
        {
            "pointerType": "touch",
            "pointerId": 1,
            "isPrimary": True,
            "clientX": x,
            "clientY": y,
            "bubbles": True,
        },
    )


def test_swipe_and_cancel_do_not_submit_but_double_tap_does(presentation_page):
    page, api = presentation_page
    panel = select(page)
    target = card(panel)
    # Deterministic pointer sequences exercise the gesture handler, including
    # movement/cancellation that must reset the pending double-tap gesture.
    for _ in range(2):
        touch(target, "pointerdown", 200, 200)
        touch(target, "pointerup", 120, 200)
    touch(target, "pointerdown", 200, 200)
    touch(target, "pointerup", 200, 200)
    touch(target, "pointercancel", 200, 200)
    touch(target, "pointerdown", 200, 200)
    touch(target, "pointerup", 200, 200)
    assert api.posts == []
    touch(target, "pointerdown", 201, 201)
    touch(target, "pointerup", 202, 202)
    expect(role(panel, "message")).to_contain_text("Queued")
    assert len(api.posts) == 1
    api.assert_post(0, DECK_A, "revision-a-1", "show_slide", "slide-202")


@pytest.mark.parametrize("manual_browse", [False, True])
def test_live_slide_is_centered_including_changes_after_manual_browsing(
    presentation_page, manual_browse
):
    page, api = presentation_page
    item = api.decks[0]
    template = item["slides"][0]
    item["slides"] = [
        {
            **template,
            "id": f"slide-{n}",
            "number": n,
            "title": f"Slide {n}",
            "page": n,
            "preview_url": f"/api/presentations/{DECK_A}/slides/slide-{n}/preview?revision=revision-a-1",
        }
        for n in range(1, 11)
    ]
    item["current_slide_id"] = "slide-5"
    page.emulate_media(reduced_motion="reduce")
    api.poll()
    panel = select(page)
    carousel = role(panel, "slides")

    def centered(slide_id):
        page.wait_for_function(
            """([slideId]) => {
            const slide = document.querySelector(`[data-slide-id="${slideId}"]`);
            const a = slide.getBoundingClientRect();
            const b = slide.parentElement.getBoundingClientRect();
            return Math.abs(a.left + a.width / 2 - b.left - b.width / 2) < 3;
        }""",
            arg=[slide_id],
            timeout=5000,
        )

    if manual_browse:
        carousel.evaluate("node => node.scrollTo({left: node.scrollWidth, behavior: 'instant'})")
        card(panel, "slide-10").click()
        assert api.posts == []
        item["current_slide_id"] = "slide-3"
        api.poll()
        expect(card(panel, "slide-3")).to_have_attribute("aria-current", "true")
        expect(card(panel, "slide-10")).to_have_attribute("aria-pressed", "true")
    centered("slide-3" if manual_browse else "slide-5")
    assert api.posts == []


def test_no_widget_means_no_presentation_discovery(widget_factory):
    page, api = widget_factory(count=0)
    page.evaluate("() => document.dispatchEvent(new Event('conty:connection'))")
    page.wait_for_timeout(2300)
    assert api.list_reads == 0
    assert api.image_reads == []
    assert api.posts == []


def test_two_widgets_keep_selection_and_commands_independent(widget_factory):
    page, api = widget_factory(count=2)
    first, second = (
        page.locator(".presentation-panel").nth(0),
        page.locator(".presentation-panel").nth(1),
    )
    expect(role(second, "select").locator("option")).to_have_count(3)
    select(page, DECK_A, first)
    select(page, DECK_B, second)
    card(first, "slide-101").click()
    card(second).click()
    expect(role(first, "select")).to_have_value(DECK_A)
    expect(role(second, "select")).to_have_value(DECK_B)
    expect(card(first, "slide-101")).to_have_attribute("aria-pressed", "true")
    expect(card(second)).to_have_attribute("aria-pressed", "true")
    activate(first, "slide-101")
    expect(role(first, "message")).to_contain_text("Queued")
    expect(role(second, "message")).to_be_empty()
    activate(second)
    expect(role(second, "message")).to_contain_text("Queued")
    assert len(api.posts) == 2
    api.assert_post(0, DECK_A, "revision-a-1", "show_slide", "slide-101")
    api.assert_post(1, DECK_B, "revision-b-1", "show_slide", "slide-202")
    assert api.posts[0]["payload"]["request_id"] != api.posts[1]["payload"]["request_id"]


def test_discovery_error_retains_target_and_scan_recovers(presentation_page):
    page, api = presentation_page
    panel = select(page)
    card(panel).click()
    api.list_status = 403
    api.poll()
    expect(role(panel, "discovery-status")).to_have_text(api.list_message)
    expect(role(panel, "select")).to_have_value(DECK_A)
    expect(role(panel, "scan")).to_be_enabled()
    activate(panel)
    assert api.posts == []
    api.list_status = 200
    api.scan()
    expect(role(panel, "discovery-status")).to_be_empty()
    expect(card(panel)).to_have_attribute("aria-pressed", "true")


@pytest.mark.parametrize("pause", ["dashboard", "hidden"])
def test_pending_polling_pauses_when_widget_is_not_visible(presentation_page, pause):
    page, api = presentation_page
    panel = select(page)
    activate(panel)
    expect(role(panel, "message")).to_contain_text("Queued")
    if pause == "dashboard":
        page.get_by_role("button", name="Another page", exact=True).click()
    else:
        page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
    page.wait_for_timeout(100)
    reads, lookups = api.list_reads, len(api.lookups)
    page.wait_for_timeout(850)
    assert api.list_reads == reads
    assert len(api.lookups) == lookups
    api.commands["command-1"].update(state="succeeded", message="Completed")
    if pause == "dashboard":
        page.get_by_role("button", name="Another page", exact=True).evaluate(
            "node => node.parentElement.querySelector('button').click()"
        )
    else:
        page.evaluate("""() => {
            delete document.hidden;
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
    expect(role(panel, "message")).to_have_text("Completed")
    assert len(api.posts) == 1


@pytest.mark.parametrize("found", [False, True])
def test_initial_discovery_is_busy_without_overlapping_requests(widget_factory, found):
    def configure(api):
        api.hold_list = True
        if not found:
            api.decks = []

    # The first response is deliberately held, so the factory checks only the
    # initial placeholder; assertions below observe the real loading transition.
    page, api = widget_factory(configure=configure, wait_for_list=False)
    panel = page.locator(".presentation-panel")
    request = api.take_held_list()
    expect(role(panel, "discovery-status")).to_have_text(
        "Looking for open PowerPoint presentations…"
    )
    expect(role(panel, "select")).to_have_attribute("aria-busy", "true")
    expect(role(panel, "select")).to_be_disabled()
    expect(role(panel, "select").locator("option")).to_have_text(["Looking…"])
    page.evaluate("""() => {
        for (let i = 0; i < 3; i++) document.dispatchEvent(new Event('conty:connection'));
    }""")
    page.wait_for_timeout(2200)
    assert api.list_reads == 1
    api.hold_list = False
    api.respond_list(request)
    expect(role(panel, "select")).to_have_attribute("aria-busy", "false")
    expect(role(panel, "select")).to_be_enabled()
    expect(role(panel, "scan")).to_be_enabled()
    expect(role(panel, "select")).to_have_value("")
    if found:
        expect(role(panel, "select").locator("option")).to_have_count(3)
    else:
        expect(role(panel, "discovery-status")).to_contain_text("No presentations found")
        api.decks = [deck(DECK_A, "revision-a-1")]
        api.scan()
        expect(role(panel, "select")).to_have_value(DECK_A)
        expect(role(panel, "select")).to_be_hidden()
    assert api.posts == []


def test_preview_download_concurrency_is_bounded(presentation_page):
    page, api = presentation_page
    api.hold_images = True
    item = api.decks[0]
    template = item["slides"][0]
    item["slides"] = [
        {
            **template,
            "id": f"slide-{n}",
            "number": n,
            "page": n,
            "preview_url": f"/api/presentations/{DECK_A}/slides/slide-{n}/preview?revision=revision-a-1",
        }
        for n in range(1, 11)
    ]
    item["current_slide_id"] = "slide-1"
    page.emulate_media(reduced_motion="reduce")
    api.poll()
    panel = select(page)
    page.wait_for_function("""() => {
        const node = document.querySelector('[data-role="presentation-slides"]');
        return node.style.getPropertyValue('--slide-width');
    }""")
    first = card(panel, "slide-1")
    first.focus()
    # Previews are lazy-loaded relative to the horizontal carousel. Browse far
    # enough to enqueue more than three without activating a PowerPoint command.
    first.press("End")
    expect(card(panel, "slide-10")).to_be_focused()
    page.wait_for_function(
        """() => {
        const last = document.querySelector('[data-slide-id="slide-10"]');
        const a = last.getBoundingClientRect();
        const b = last.parentElement.getBoundingClientRect();
        return Math.abs(a.left + a.width / 2 - b.left - b.width / 2) < 3;
    }""",
        timeout=5000,
    )
    deadline = time.monotonic() + 5
    while len(api.held_images) < 3:
        assert time.monotonic() < deadline, "Expected three concurrent preview GETs after browsing"
        page.wait_for_timeout(20)
    card(panel, "slide-10").press("Home")
    expect(first).to_be_focused()
    page.wait_for_timeout(300)
    assert len(api.image_reads) == 3
    assert api.posts == []

    # Releasing exactly one slot must start exactly one queued image; this also
    # proves the cap wasn't satisfied merely because only three were visible.
    api.held_images.pop(0).fulfill(content_type="image/png", body=PNG)
    deadline = time.monotonic() + 5
    while len(api.image_reads) < 4:
        assert time.monotonic() < deadline, "A queued preview did not use the released slot"
        page.wait_for_timeout(20)
    page.wait_for_timeout(300)
    assert len(api.image_reads) == 4
    assert len(api.held_images) == 3
    api.hold_images = False
    for route in api.held_images:
        route.fulfill(content_type="image/png", body=PNG)
    expect(panel.locator("img").first).to_have_js_property("naturalWidth", 1)
    assert api.posts == []


@pytest.mark.parametrize("restriction", ["edit", "offline", "unfocused", "unmapped"])
def test_slide_activation_respects_deck_safety_guards(presentation_page, restriction):
    page, api = presentation_page
    panel = select(page)
    item = api.decks[0]
    if restriction == "edit":
        item.update(view="edit", can_control=False)
    elif restriction == "offline":
        item.update(connected=False)
    elif restriction == "unfocused":
        item.update(can_control=False)
    else:
        item.update(needs_refresh=True)
        item["slides"][1].update(page=None, preview_url=None)
    api.poll()
    if restriction == "offline":
        expect(panel.locator(".slide-card")).to_have_count(0)
        expect(role(panel, "info")).to_have_text("Presentation disconnected")
    else:
        activate(panel)
        if restriction == "unfocused":
            expect(role(panel, "info")).to_have_text("Focus slideshow in PowerPoint")
        if restriction == "unmapped":
            expect(role(panel, "info")).to_have_text("Stop slideshow to load previews")
    assert api.posts == []


@pytest.mark.parametrize("width", [320, 390])
def test_sample_mobile_two_row_carousel_geometry_and_external_buttons(
    widget_factory, project_root, width
):
    reference = load_config(project_root / "examples/conty.yaml", platform="darwin").public()
    page, api = widget_factory(width=width, reference=reference)
    panel = select(page)
    carousel = role(panel, "slides")
    expect(carousel).to_be_visible()
    # Wait for ResizeObserver's sizing to settle before checking real geometry,
    # rather than asserting CSS declarations that could still yield clipping.
    page.wait_for_function("""() => {
        const slides = document.querySelector('[data-role="presentation-slides"]');
        return slides.clientHeight > 0 && slides.style.getPropertyValue('--slide-width');
    }""")

    def geometry():
        return carousel.evaluate("""node => {
            const bounds = node.getBoundingClientRect();
            const panel = node.closest('.presentation-panel');
            const cards = [...node.querySelectorAll('.slide-card')].map(card => {
                const rect = card.getBoundingClientRect();
                return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            });
            return {
                width: node.clientWidth, height: node.clientHeight,
                scrollWidth: node.scrollWidth, scrollHeight: node.scrollHeight,
                panelHeight: panel.getBoundingClientRect().height,
                panelScrollHeight: panel.scrollHeight, panelClientHeight: panel.clientHeight,
                rowSpan: panel.style.gridRow, columnSpan: panel.style.gridColumn,
                top: bounds.top, bottom: bounds.bottom, cards,
            };
        }""")

    before = geometry()
    assert before["rowSpan"] == "1 / span 2"
    assert before["columnSpan"] == "1 / span 3"
    assert before["panelHeight"] <= 240, before
    assert 0 < before["width"] < width, before
    assert before["scrollWidth"] > before["width"], before
    assert before["scrollHeight"] <= before["height"] + 1, before
    assert before["panelScrollHeight"] <= before["panelClientHeight"] + 1, before
    first, second = before["cards"]
    assert abs(first["y"] - second["y"]) < 1, before
    assert second["x"] >= first["x"] + first["width"], before
    for item in before["cards"]:
        assert item["width"] > 0 and item["height"] > 0, before
        assert item["y"] >= before["top"] - 1, before
        assert item["y"] + item["height"] <= before["bottom"] + 1, before

    card(panel, "slide-101").focus()
    card(panel, "slide-101").press("End")
    expect(card(panel)).to_be_focused()
    page.wait_for_function("""() => {
        const node = document.querySelector('[data-role="presentation-slides"]');
        return node.scrollLeft > 0;
    }""")
    after = geometry()
    assert after["cards"][0]["x"] < before["cards"][0]["x"], after
    assert abs(after["cards"][0]["y"] - after["cards"][1]["y"]) < 1, after
    assert after["scrollHeight"] <= after["height"] + 1, after
    assert api.posts == [], "Horizontal browsing must not submit slide commands"

    # Load the actual sample, not invented panel controls: these remain normal
    # app-wide keyboard tiles outside the deck-bound PowerPoint command lane.
    for label, widget_id in (("Previous", "previous"), ("Next", "next")):
        button = page.get_by_role("button", name=label, exact=True)
        expect(button).to_be_visible()
        expect(button).to_be_enabled()
        expect(button).to_have_attribute("data-widget", widget_id)
        assert button.evaluate("node => node.closest('.presentation-panel') === null")
        expect(panel.get_by_role("button", name=label, exact=True)).to_have_count(0)
    assert api.posts == []
