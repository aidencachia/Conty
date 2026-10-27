"""Real browser/network checks with injected adapters; no desktop automation."""

import asyncio
import copy
import json
import socket
import statistics
import threading
import time
from dataclasses import replace

import pytest
import uvicorn
from conftest import FakeExecutor
from playwright.sync_api import Error, expect, sync_playwright

from conty.config import freeze, load_config, thaw
from conty.server import create_app


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as exc:
            pytest.skip(f"Install the test browser: python -m playwright install chromium ({exc})")
        yield browser
        browser.close()


@pytest.fixture
def live_server(snapshot, fake_executor):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server_config = thaw(snapshot.server)
    server_config["port"] = port
    public = snapshot.public()
    public["dashboards"][0]["sections"][0]["widgets"][1]["action"] = "other"
    second_page = copy.deepcopy(public["dashboards"][0])
    second_page.update(id="another", title="Another page")
    public["dashboards"].append(second_page)
    actions = thaw(snapshot.actions)
    actions["hello"]["env_from"] = {}
    actions["other"] = {**actions["hello"], "argv": ["/bin/echo", "SECOND"]}
    actions["volume_level"] = {
        "type": "volume",
        "target": "system",
        "operation": "set",
        "timeout_seconds": 5,
    }
    snapshot = replace(
        snapshot, server=freeze(server_config), _public=freeze(public), actions=freeze(actions)
    )
    original_execute = fake_executor.execute

    async def execute(action):
        await asyncio.sleep(0.06)
        return await original_execute(action)

    fake_executor.execute = execute
    app = create_app(snapshot, executor=fake_executor, owner_output=lambda _: None)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", app, fake_executor
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def paired_page(browser, live_server, *, width=390):
    url, app, executor = live_server
    context = browser.new_context(viewport={"width": width, "height": 844})
    page = context.new_page()
    page.goto(url)
    expect(page.locator("#connection")).to_have_text("Pairing required")
    page.get_by_label("Enter the six-digit code shown on your computer.").fill(
        app.state.sessions.code
    )
    page.get_by_role("button", name="Connect").click()
    expect(page.locator("#dashboard")).to_be_visible()
    return context, page


def wait_slider_idle(page):
    # A function evaluated directly works under the application's strict CSP;
    # Playwright's string-predicate polling requires unsafe-eval in Chromium.
    page.evaluate("""async () => {
      const deadline = performance.now() + 5000;
      while (state.volumeActive || state.queue.some(item => item.value !== undefined)) {
        if (performance.now() > deadline) throw new Error('Slider did not settle');
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    }""")


def test_pair_layout_navigation_and_rapid_tap_order(browser, live_server):
    context, page = paired_page(browser, live_server)
    try:
        controls = page.locator(".control")
        expect(controls).to_have_count(2)
        assert controls.evaluate_all("nodes => nodes.map(n => n.dataset.widget)") == [
            "first",
            "second",
        ]
        assert controls.nth(0).evaluate("node => getComputedStyle(node).gridColumnStart") == "2"
        assert controls.nth(1).evaluate("node => getComputedStyle(node).gridColumnEnd") == "span 2"
        first, second = controls.nth(0).bounding_box(), controls.nth(1).bounding_box()
        assert first["y"] < second["y"] and second["width"] > first["width"]
        assert first["height"] >= 48
        page.get_by_role("button", name="Another page").click()
        expect(page.locator("#dashboard-title")).to_have_text("Another page")
        page.get_by_role("button", name="Tools", exact=True).click()
        page.evaluate("""() => {
          for (const id of ['first', 'second', 'first', 'second']) {
            document.querySelector(`[data-widget=${id}]`).click();
          }
        }""")
        expect(page.locator("#activity-list li")).to_have_count(4, timeout=6000)
        calls = live_server[2].calls
        assert [action["argv"][-1] for action in calls] == [
            "PRIVATE_ARGUMENT",
            "SECOND",
            "PRIVATE_ARGUMENT",
            "SECOND",
        ]
        assert "PRIVATE_ARGUMENT" not in page.locator("body").inner_text()
    finally:
        context.close()


def test_lost_submission_response_is_not_retried_and_unsent_taps_are_discarded(
    browser, live_server
):
    context, page = paired_page(browser, live_server)
    requests = []

    def lose_response(route):
        requests.append(route.request.post_data_json)
        route.fetch()  # The real server accepts/executes the request.
        route.abort()  # The browser never receives its invocation ID.

    page.route("**/api/invocations", lose_response)
    try:
        page.evaluate("""() => {
          document.querySelector('[data-widget=first]').click();
          document.querySelector('[data-widget=second]').click();
          document.querySelector('[data-widget=first]').click();
        }""")
        expect(page.locator("#activity-list")).to_contain_text("Outcome unknown", timeout=5000)
        expect(page.locator("#connection")).to_have_text("Connected", timeout=6000)
        assert len(requests) == 1
        assert len(live_server[2].calls) == 1
        assert len(live_server[1].state.coordinator.records) == 1
    finally:
        context.close()


def test_narrow_viewport_scroll_preserves_grid_and_focus_order(browser, live_server, tmp_path):
    context, page = paired_page(browser, live_server, width=280)
    try:
        scroll = page.locator(".grid-scroll")
        page.screenshot(path=str(tmp_path / "narrow.png"), full_page=True)
        assert scroll.evaluate("node => node.scrollWidth > node.clientWidth"), page.evaluate("""() => ({
          viewport: innerWidth, width: document.documentElement.scrollWidth,
          elements: ['.shell', '.grid-scroll', '.control-grid'].map(selector => {
            const node = document.querySelector(selector); const style = getComputedStyle(node);
            return {selector, width: node.clientWidth, scroll: node.scrollWidth, columns: style.gridTemplateColumns, overflow: style.overflowX};
          })
        })""")
        assert (
            page.locator(".control-grid").evaluate(
                "node => getComputedStyle(node).gridTemplateColumns.split(' ').length"
            )
            == 3
        )
        first = page.locator('[data-widget="first"]')
        first.focus()
        page.keyboard.press("Tab")
        expect(page.locator('[data-widget="second"]')).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


def test_reference_dashboards_render_locally(browser, live_server, project_root, tmp_path):
    reference = load_config(project_root / "examples/conty.yaml", platform="darwin").public()
    reference["revision"] = live_server[1].state.coordinator.snapshot.revision
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.route(
        "**/api/dashboard",
        lambda route: route.fulfill(content_type="application/json", body=json.dumps(reference)),
    )
    try:
        page.goto(live_server[0])
        expect(page.locator("#connection")).to_have_text("Pairing required")
        page.screenshot(path=str(tmp_path / "pairing.png"), full_page=True)
        page.locator("#code").fill(live_server[1].state.sessions.code)
        page.locator("#pair-submit").click()
        expect(page.locator("#dashboard-title")).to_have_text("Presentation")
        expect(page.locator(".control")).to_have_count(4)
        page.screenshot(path=str(tmp_path / "presentation-mobile.png"), full_page=True)
        page.get_by_role("button", name="Media", exact=True).click()
        expect(page.locator(".control")).to_have_count(5)
        expect(page.get_by_role("slider", name="Volume")).to_be_enabled()
        expect(page.locator(".slider-control output")).to_have_text("37%")
        assert (
            page.locator(".control-grid").evaluate(
                "n => getComputedStyle(n).gridTemplateColumns.split(' ').length"
            )
            == 2
        )
        page.screenshot(path=str(tmp_path / "media-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1280, "height": 900})
        page.screenshot(path=str(tmp_path / "media-desktop.png"), full_page=True)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("style", ["standard", "ios"])
def test_volume_slider_reads_host_state_and_coalesces_live_values(
    browser, live_server, project_root, style
):
    reference = load_config(project_root / "examples/conty.yaml", platform="darwin").public()
    for widget in reference["dashboards"][1]["sections"][0]["widgets"]:
        if widget["type"] == "slider":
            widget["style"] = style
    reference["revision"] = live_server[1].state.coordinator.snapshot.revision
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.route(
        "**/api/dashboard",
        lambda route: route.fulfill(content_type="application/json", body=json.dumps(reference)),
    )
    try:
        page.goto(live_server[0])
        expect(page.locator("#connection")).to_have_text("Pairing required")
        page.locator("#code").fill(live_server[1].state.sessions.code)
        page.locator("#pair-submit").click()
        expect(page.locator("#dashboard")).to_be_visible()
        page.get_by_role("button", name="Media", exact=True).click()
        slider = page.get_by_role("slider", name="Volume")
        expect(slider).to_be_enabled()
        expect(slider).to_have_value("37")
        assert slider.get_attribute("aria-orientation") == "vertical"
        assert (
            page.locator(".slider-control").evaluate("n => getComputedStyle(n).gridRowEnd")
            == "span 2"
        )
        slider.evaluate("""input => {
          for (const value of [42, 55, 68]) {
            input.value = String(value); input.dispatchEvent(new Event('input', {bubbles:true}));
          }
        }""")
        expect(page.locator(".slider-control output")).to_have_text("68%")
        slider.dispatch_event("change")
        wait_slider_idle(page)
        expect(page.locator("#activity-list")).to_contain_text(
            "Output change confirmed", timeout=2000
        )
        assert [call["value"] for call in live_server[2].calls] == [42, 68]
        assert live_server[2].volume == 68
        expect(slider).to_have_value("68")
        assert not page.locator(".activity").evaluate("node => node.open")
        assert not page.locator("footer").count()
    finally:
        context.close()


def paired_slider_page(browser, live_server, *, vertical=True, touch=False):
    reference = live_server[1].state.coordinator.snapshot.public()
    reference["dashboards"] = [
        {
            "id": "sliders",
            "title": "Sliders",
            "sections": [
                {
                    "id": "volume",
                    "title": "Volume",
                    "layout": {"rows": 2, "columns": 2},
                    "widgets": [
                        {
                            "id": style,
                            "type": "slider",
                            "style": style,
                            "label": f"Volume {style}",
                            "symbol": "♫",
                            "action": "volume_level",
                            "position": {
                                "row": 1 if vertical else index + 1,
                                "column": index + 1 if vertical else 1,
                                "row_span": 2 if vertical else 1,
                                "column_span": 1 if vertical else 2,
                            },
                        }
                        for index, style in enumerate(["standard", "ios"])
                    ],
                }
            ],
        }
    ]
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=touch, is_mobile=touch
    )
    page = context.new_page()
    page.route(
        "**/api/dashboard",
        lambda route: route.fulfill(content_type="application/json", body=json.dumps(reference)),
    )
    page.goto(live_server[0])
    page.locator("#code").fill(live_server[1].state.sessions.code)
    page.locator("#pair-submit").click()
    expect(page.get_by_role("slider", name="Volume ios")).to_be_enabled()
    return context, page


@pytest.mark.parametrize("vertical", [True, False])
def test_ios_slider_drag_keyboard_endpoints_and_shared_readback(
    browser, live_server, tmp_path, vertical
):
    context, page = paired_slider_page(browser, live_server, vertical=vertical)
    try:
        slider = page.get_by_role("slider", name="Volume ios")
        standard = page.get_by_role("slider", name="Volume standard")
        fill = page.locator(".slider-fill")
        bounds = slider.bounding_box()
        fill_bounds = fill.bounding_box()
        axis = "height" if vertical else "width"
        assert abs(fill_bounds[axis] / bounds[axis] - 0.37) < 0.01
        page.screenshot(
            path=str(tmp_path / f"slider-{'vertical' if vertical else 'horizontal'}.png")
        )

        def point(value):
            return (
                bounds["x"] + bounds["width"] * (0.5 if vertical else value / 100),
                bounds["y"] + bounds["height"] * (1 - value / 100 if vertical else 0.5),
            )

        page.mouse.move(*point(37))
        page.mouse.down()
        page.mouse.move(*point(75), steps=8)
        expect(slider).to_have_value("75")
        expect(page.locator(".slider-ios output")).to_have_text("75%")
        # The actual host reaches the selected value before releasing the mouse.
        wait_slider_idle(page)
        assert live_server[2].volume == 75
        page.mouse.up()
        expect(page.locator("#activity-list")).to_contain_text("Output change confirmed")
        expect(standard).to_have_value("75")
        drag_values = [call["value"] for call in live_server[2].calls]
        assert drag_values[-1] == 75 and len(drag_values) < 8

        slider.focus()
        slider.press("Home")
        expect(standard).to_have_value("0")
        slider.press("End")
        expect(standard).to_have_value("100")
        assert [call["value"] for call in live_server[2].calls] == [*drag_values, 0, 100]
        assert (
            page.locator(".slider-ios").evaluate("n => n.style.getPropertyValue('--slider-value')")
            == "100%"
        )
    finally:
        context.close()


def test_ios_slider_touch_and_cancelled_drag(browser, live_server):
    context, page = paired_slider_page(browser, live_server, touch=True)
    try:
        slider = page.get_by_role("slider", name="Volume ios")
        bounds = slider.bounding_box()
        page.touchscreen.tap(
            bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] * 0.2
        )
        expect(page.get_by_role("slider", name="Volume standard")).to_have_value("80")
        assert [call["value"] for call in live_server[2].calls] == [80]
        page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] * 0.6)
        page.mouse.down()
        expect(slider).to_have_value("40")
        wait_slider_idle(page)
        assert live_server[2].volume == 40
        slider.dispatch_event("pointercancel", {"pointerId": 1})
        page.mouse.up()
        expect(slider).to_have_value("40")
        assert [call["value"] for call in live_server[2].calls] == [80, 40]
    finally:
        context.close()


def test_both_slider_styles_disable_and_recover_with_unknown_volume(browser, live_server):
    context, page = paired_slider_page(browser, live_server)
    try:
        page.route(
            "**/api/volume",
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"error": {"code": "volume_unavailable"}}),
            ),
        )
        page.evaluate("refreshVolume()")
        expect(page.get_by_role("slider", name="Volume ios")).to_be_disabled()
        expect(page.get_by_role("slider", name="Volume standard")).to_be_disabled()
        expect(page.locator(".slider-ios output")).to_have_text("Unavailable")
        expect(page.locator(".slider-fill")).to_be_hidden()
        page.locator(".slider-ios").click()
        assert not live_server[2].calls

        live_server[2].volume = 62
        page.unroute("**/api/volume")
        page.evaluate("refreshVolume()")
        expect(page.get_by_role("slider", name="Volume ios")).to_be_enabled()
        expect(page.get_by_role("slider", name="Volume standard")).to_have_value("62")
        expect(page.locator(".slider-fill")).to_be_visible()
        assert (
            page.locator(".slider-ios").evaluate("n => n.style.getPropertyValue('--slider-value')")
            == "62%"
        )
    finally:
        context.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_live_slider_slow_host_keeps_only_one_latest_pending_value(browser, live_server, cancel):
    original = live_server[2].execute

    async def slow_execute(action):
        await asyncio.sleep(0.3)
        return await original(action)

    live_server[2].execute = slow_execute
    context, page = paired_slider_page(browser, live_server)
    try:
        slider = page.get_by_role("slider", name="Volume ios")
        slider.evaluate("""input => {
          for (const value of [45, ...Array.from({length:45}, (_, i) => 46 + i)]) {
            input.value = String(value);
            input.dispatchEvent(new Event('input', {bubbles:true}));
          }
        }""")
        assert page.evaluate(
            "state.queue.filter(i => i.value !== undefined).map(i => i.value)"
        ) == [90]
        assert page.evaluate("state.volumeActive.value") == 45
        assert len(live_server[1].state.coordinator.records) <= 1
        slider.dispatch_event("pointercancel" if cancel else "change")
        # Blurring a released slider must not discard its final pending value.
        if not cancel:
            slider.dispatch_event("blur")
        wait_slider_idle(page)
        expected = [45] if cancel else [45, 90]
        assert [call["value"] for call in live_server[2].calls] == expected
        assert len(live_server[1].state.coordinator.records) == len(expected)
        expect(page.get_by_role("slider", name="Volume standard")).to_have_value(str(expected[-1]))
    finally:
        context.close()


@pytest.mark.parametrize("style", ["standard", "ios"])
def test_live_slider_feedback_latency_before_release(browser, live_server, style):
    context, page = paired_slider_page(browser, live_server)
    try:
        timings = page.get_by_role("slider", name=f"Volume {style}").evaluate("""async input => {
          const samples = [];
          for (const value of [45, 50, 55, 60, 65]) {
            const start = performance.now();
            input.value = String(value);
            input.dispatchEvent(new Event('input', {bubbles:true}));
            const preview = performance.now() - start;
            while (state.volumeActive || state.queue.some(item => item.value !== undefined)) {
              if (performance.now() - start > 2000) throw new Error('Live update timed out');
              await new Promise(resolve => setTimeout(resolve, 1));
            }
            samples.push({preview, applied: performance.now() - start});
          }
          return samples;
        }""")
        median = statistics.median(sample["applied"] for sample in timings)
        print(f"Live slider {style}: median input-to-host confirmation={median:.2f} ms")
        assert median < 200  # Includes a 60 ms fake action, without releasing.
        assert max(sample["preview"] for sample in timings) < 50
        assert live_server[2].volume == 65
        assert [call["value"] for call in live_server[2].calls] == [45, 50, 55, 60, 65]
        assert (
            page.get_by_role("slider", name=f"Volume {style}").get_attribute("data-editing")
            == "true"
        )
    finally:
        context.close()


def test_live_slider_lost_response_discards_pending_values_without_replay(browser, live_server):
    context, page = paired_slider_page(browser, live_server)
    requests = []

    def lose_response(route):
        if route.request.method != "POST":
            return route.continue_()
        requests.append(route.request.post_data_json)
        route.fetch()
        route.abort()

    page.route("**/api/invocations*", lose_response)
    try:
        page.get_by_role("slider", name="Volume ios").evaluate("""input => {
          for (const value of [45, 60, 75, 90]) {
            input.value = String(value);
            input.dispatchEvent(new Event('input', {bubbles:true}));
          }
          input.dispatchEvent(new Event('change', {bubbles:true}));
        }""")
        expect(page.locator("#activity-list")).to_contain_text("Outcome unknown")
        expect(page.locator("#connection")).to_have_text("Connected", timeout=6000)
        assert len(requests) == 1 and requests[0]["value"] == 45
        assert [call["value"] for call in live_server[2].calls] == [45]
        assert page.evaluate("state.queue.length") == 0
    finally:
        context.close()


def test_live_slider_fast_host_is_throttled_without_delaying_preview(browser, live_server):
    async def fast_execute(action):
        return await FakeExecutor.execute(live_server[2], action)

    live_server[2].execute = fast_execute
    context, page = paired_slider_page(browser, live_server)
    try:
        sends = page.get_by_role("slider", name="Volume standard").evaluate("""async input => {
          const original = window.fetch;
          const sends = [];
          window.fetch = (...args) => {
            if (String(args[0]).startsWith('/api/invocations?wait='))
              sends.push(performance.now());
            return original(...args);
          };
          try {
            for (let value = 20; value < 60; value++) {
              input.value = String(value);
              input.dispatchEvent(new Event('input', {bubbles:true}));
              if (input.parentElement.querySelector('output').textContent !== `${value}%`)
                throw new Error('Preview did not update synchronously');
              await new Promise(resolve => setTimeout(resolve, 15));
            }
            input.dispatchEvent(new Event('change', {bubbles:true}));
            return sends;
          } finally { window.fetch = original; }
        }""")
        wait_slider_idle(page)
        assert 4 <= len(sends) <= 16
        assert all(later - earlier >= 47 for earlier, later in zip(sends, sends[1:]))
        assert live_server[2].volume == 59
    finally:
        context.close()


def test_cancelling_a_throttled_slider_does_not_strand_queued_buttons(browser, live_server):
    context, page = paired_slider_page(browser, live_server)
    try:
        page.get_by_role("slider", name="Volume ios").evaluate("""input => {
          state.volumeLastSent = performance.now();
          input.value = '60';
          input.dispatchEvent(new Event('input', {bubbles:true}));
          enqueue({label:'Button', action:'hello'}, 'button');
          input.dispatchEvent(new Event('pointercancel', {bubbles:true}));
        }""")
        expect(page.locator("#activity-list")).to_contain_text(
            "Configured process exited successfully"
        )
        assert len(live_server[2].calls) == 1
        assert live_server[2].calls[0]["type"] == "process"
        assert page.evaluate("state.queue.length") == 0
    finally:
        context.close()


def test_tap_feedback_latency(browser, live_server):
    context, page = paired_page(browser, live_server)
    try:
        measurements = page.evaluate("""async () => {
          const samples = [];
          for (let i = 0; i < 5; i++) {
            await new Promise(resolve => {
              const start = performance.now();
              const observer = new MutationObserver(() => {
                observer.disconnect(); samples.push(performance.now() - start); resolve();
              });
              observer.observe(document.querySelector('#activity-list'), {childList:true});
              document.querySelector('[data-widget=first]').click();
            });
          }
          return samples;
        }""")
        median = statistics.median(measurements)
        print(f"Browser tap-to-feedback: median={median:.2f} ms, samples={measurements}")
        # The fake action itself takes 60 ms. Leave a generous scheduling/network
        # budget, but catch the old 500 ms feedback timer.
        assert median < 300
    finally:
        context.close()


def test_network_recovery_checks_immediately_without_replaying_actions(browser, live_server):
    context, page = paired_page(browser, live_server)
    try:
        context.set_offline(True)
        expect(page.locator("#connection")).to_have_text("Reconnecting")
        context.set_offline(False)
        expect(page.locator("#connection")).to_have_text("Connected", timeout=1000)
        assert not live_server[2].calls
        assert not live_server[1].state.coordinator.records
    finally:
        context.close()
