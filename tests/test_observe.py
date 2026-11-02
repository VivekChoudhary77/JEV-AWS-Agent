"""observe() against static fixtures in headless Chromium, served over HTTP so iframes are same-origin."""

import functools
import http.server
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import agent

FIXTURES = Path(__file__).parent / "fixtures"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    handler = functools.partial(QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        chromium = pw.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    yield page
    page.close()


def names(snapshot):
    return [element.name for element in snapshot.elements]


def test_finds_visible_controls_with_names_and_values(page, base_url):
    page.goto(f"{base_url}/controls.html")
    snapshot = agent.observe(page)
    by_name = {element.name: element for element in snapshot.elements}
    assert by_name["Next"].role == "button"
    assert by_name["Budgets"].role == "link"
    assert by_name["Budget name"].value == "draft"
    assert by_name["Budget name"].editable
    assert by_name["Period"].value == "Monthly"
    assert by_name["Create budget"].role == "button"
    assert by_name["Enable"].value == "checked"
    assert '[1] button "Next"' in snapshot.table
    assert '"Budget name" (draft)' in snapshot.table
    assert snapshot.ms > 0


def test_snapshot_reads_the_visible_page_heading(page, base_url):
    page.goto(f"{base_url}/controls.html")
    assert agent.observe(page).heading == "Budgets"


def by_name(snapshot):
    return {element.name: element for element in snapshot.elements}


def test_wrapper_and_inner_textbox_become_one_named_textbox(page, base_url):
    page.goto(f"{base_url}/nested.html")
    snapshot = agent.observe(page)
    prompts = [e for e in snapshot.elements if e.role == "textbox" and e.name == "Write a prompt..."]
    assert len(prompts) == 1
    assert prompts[0].editable
    assert page.locator(f'[data-jev-idx="{prompts[0].idx}"]').get_attribute("id") == "prompt"
    assert "" not in [e.name for e in snapshot.elements if e.role == "textbox"]


def test_act_types_into_the_deduped_inner_textbox(page, base_url):
    page.goto(f"{base_url}/nested.html")
    snapshot = agent.observe(page)
    target = by_name(snapshot)["Write a prompt..."]
    turn = agent.Turn(None, {}, {}, {}, {"prompt": "Reply with one word: hello"}, snapshot)
    agent.act(page, agent.Decision("TYPE", turn, target=target, input_key="prompt"))
    assert page.locator("#prompt").inner_text() == "Reply with one word: hello"


def test_nested_buttons_keep_the_inner_one_with_the_outer_name(page, base_url):
    page.goto(f"{base_url}/nested.html")
    snapshot = agent.observe(page)
    outer = by_name(snapshot)["Open settings"]
    assert page.locator(f'[data-jev-idx="{outer.idx}"]').get_attribute("id") == "inner-button"
    assert not outer.borrowed_name


def test_inner_textbox_that_is_not_editable_does_not_replace_the_real_one(page, base_url):
    page.goto(f"{base_url}/nested.html")
    assert by_name(agent.observe(page))["Editable outer"].editable


def test_tree_and_menu_parents_are_kept(page, base_url):
    page.goto(f"{base_url}/nested.html")
    found = by_name(agent.observe(page))
    assert found["Parent item"].role == "treeitem"
    assert found["Child item"].role == "treeitem"
    assert found["Parent menu"].role == "menuitem"
    assert found["Child menu"].role == "menuitem"


@pytest.mark.parametrize("name", [
    "Aria placeholder", "Plain placeholder", "Ancestor region", "Wrapping label", "Only a title",
])
def test_name_fallbacks(page, base_url, name):
    page.goto(f"{base_url}/nested.html")
    assert name in by_name(agent.observe(page))


def test_placeholder_beats_ancestor_label(page, base_url):
    page.goto(f"{base_url}/nested.html")
    assert not by_name(agent.observe(page))["Plain placeholder"].borrowed_name


def test_a_name_from_an_ancestor_label_is_flagged_for_the_gate(page, base_url):
    page.goto(f"{base_url}/nested.html")
    assert by_name(agent.observe(page))["Ancestor region"].borrowed_name


def test_inner_button_in_an_unnamed_wrapper_stays_flagged(page, base_url):
    page.goto(f"{base_url}/nested.html")
    assert by_name(agent.observe(page))["Inner region"].borrowed_name


def test_success_banner_and_live_regions_are_read_as_notices(page, base_url):
    page.goto(f"{base_url}/banner.html")
    notices = agent.observe(page).notices
    assert notices == ["Budget jevagent-x-budget created", "Saving complete", "Model is ready", "3 errors found"]


def test_new_text_after_an_action_reaches_page_notices(page, base_url):
    page.goto(f"{base_url}/reply.html")
    before = agent.observe(page)
    page.get_by_role("button", name="Run").click()
    after = agent.observe(page)
    assert agent.page_notices(after, before, {"prompt": "Reply with one word: hello"}) == "friend"


def test_opacity_zero_radio_stays_visible(page, base_url):
    page.goto(f"{base_url}/controls.html")
    radio = next(e for e in agent.observe(page).elements if e.name == "Use an existing role")
    assert (radio.role, radio.value) == ("radio", "unchecked")


def test_hidden_disabled_and_secret_inputs_are_skipped(page, base_url):
    page.goto(f"{base_url}/controls.html")
    found = " ".join(names(agent.observe(page)))
    for excluded in ["Disabled", "Aria disabled", "Display none", "Visibility hidden",
                     "Transparent", "Aria hidden", "Password", "password", "Upload", "secret"]:
        assert excluded not in found


def test_indices_are_stable_and_contiguous(page, base_url):
    page.goto(f"{base_url}/controls.html")
    first, second = agent.observe(page), agent.observe(page)
    assert first.table == second.table
    assert [e.idx for e in first.elements] == list(range(1, len(first.elements) + 1))
    assert page.locator("[data-jev-idx]").count() == len(first.elements)


def test_stale_stamps_are_cleared_when_elements_disappear(page, base_url):
    page.goto(f"{base_url}/controls.html")
    before = len(agent.observe(page).elements)
    page.evaluate("document.getElementById('next').style.display = 'none'")
    assert len(agent.observe(page).elements) == before - 1
    assert page.locator("#next").get_attribute("data-jev-idx") is None


def test_open_shadow_root_and_same_origin_iframe_are_walked(page, base_url):
    page.goto(f"{base_url}/shadow.html")
    page.frame_locator("iframe").get_by_role("button").wait_for()
    assert names(agent.observe(page)) == ["Before", "Inside shadow", "Inside frame"]


def test_handles_more_than_300_elements(page, base_url):
    page.goto(f"{base_url}/many.html")
    snapshot = agent.observe(page)
    assert len(snapshot.elements) == 320
    assert snapshot.elements[-1].name == "Row 320"


def test_settle_waits_for_a_page_that_renders_late(page, base_url, monkeypatch):
    monkeypatch.setattr(agent, "blocked_reason", lambda url: None)  # the fixture server isn't the console
    page.goto(f"{base_url}/delayed.html")
    assert agent.observe(page).elements == []
    snapshot, ms, timed_out = agent.settle(page)
    assert len(snapshot.elements) == 8
    assert not timed_out
    assert ms >= 1000
    assert snapshot.url.endswith("#/budgets/overview")


def test_a_stable_empty_table_is_not_settled(page, monkeypatch):
    monkeypatch.setattr(agent, "blocked_reason", lambda url: None)  # about:blank isn't the console
    monkeypatch.setattr(agent, "SETTLE_CAP_S", 0.7)
    page.set_content("<p>Nothing to click here</p>")
    snapshot, ms, timed_out = agent.settle(page)
    assert snapshot.elements == []
    assert timed_out
    assert ms >= 700


def test_settle_outlasts_a_short_pause_between_render_stages(page, base_url, monkeypatch):
    monkeypatch.setattr(agent, "blocked_reason", lambda url: None)
    page.goto(f"{base_url}/stages.html?pause=450")
    assert "Function name" not in names(agent.observe(page))
    snapshot, _, timed_out = agent.settle(page)
    assert "Function name" in names(snapshot)
    assert not timed_out


def test_settle_alone_cant_outlast_a_long_pause_so_ready_when_waits(page, base_url, monkeypatch):
    monkeypatch.setattr(agent, "blocked_reason", lambda url: None)
    page.goto(f"{base_url}/stages.html?pause=1500")
    assert "Function name" not in names(agent.settle(page)[0])
    ms, timed_out = agent.wait_ready(page, "function NAME")
    assert not timed_out
    assert "Function name" in names(agent.settle(page)[0])
    assert ms > 0


def test_wait_ready_gives_up_at_the_cap(page, base_url, monkeypatch):
    monkeypatch.setattr(agent, "blocked_reason", lambda url: None)
    monkeypatch.setattr(agent, "READY_CAP_S", 0.4)
    page.goto(f"{base_url}/stages.html")
    ms, timed_out = agent.wait_ready(page, "Function name")
    assert timed_out
    assert ms >= 400


def test_settle_waits_for_aria_busy_to_clear(page, base_url, monkeypatch):
    monkeypatch.setattr(agent, "blocked_reason", lambda url: None)
    page.goto(f"{base_url}/loading.html?kind=busy&clear=1000")
    assert agent.observe(page).loading
    snapshot, ms, timed_out = agent.settle(page)
    assert not snapshot.loading
    assert not timed_out
    assert ms >= 900


@pytest.mark.parametrize("kind, loading", [
    ("busy", True), ("progressbar", True), ("progress", True), ("spinner", True), ("status", True),
    ("idle", False), ("hidden", False), ("finished", False), ("in_progress", False),
])
def test_observe_reports_loading_indicators(page, base_url, kind, loading):
    page.goto(f"{base_url}/loading.html?kind={kind}")
    assert agent.observe(page).loading is loading


def test_a_loading_indicator_that_never_clears_times_out(page, base_url, monkeypatch):
    monkeypatch.setattr(agent, "blocked_reason", lambda url: None)
    monkeypatch.setattr(agent, "SETTLE_CAP_S", 0.7)
    page.goto(f"{base_url}/loading.html?kind=spinner")
    snapshot, _, timed_out = agent.settle(page)
    assert snapshot.loading
    assert timed_out


@pytest.mark.parametrize("kind, heading", [
    ("dialog", "Close side panel"),
    ("alert", "Are you sure?"),
    ("modal", "Confirm"),
    ("split", "Configure custom execution role"),
    ("toolbar", "Details"),
    ("labelled", "Panel label"),
    ("untitled", "Untitled panel"),
])
def test_open_panel_is_named_and_listed_first(page, base_url, kind, heading):
    page.goto(f"{base_url}/panels.html?kind={kind}")
    snapshot = agent.observe(page)
    assert snapshot.open_panel == heading
    assert names(snapshot) == ["In panel", "Page button", "Other page button"]
    assert snapshot.elements[0].idx == 3


@pytest.mark.parametrize("kind", ["none", "hidden", "closed", "nav"])
def test_no_open_panel_leaves_the_table_alone(page, base_url, kind):
    page.goto(f"{base_url}/panels.html?kind={kind}")
    snapshot = agent.observe(page)
    assert snapshot.open_panel == ""
    assert names(snapshot)[:2] == ["Page button", "Other page button"]


def test_a_modal_takes_priority_over_the_panel_beneath_it(page, base_url):
    page.goto(f"{base_url}/panels.html?kind=stacked")
    snapshot = agent.observe(page)
    assert snapshot.open_panel == "Close side panel"
    assert names(snapshot)[0] == "In dialog"


def test_a_panel_element_keeps_its_stamped_index_for_act(page, base_url):
    page.goto(f"{base_url}/panels.html?kind=dialog")
    page.evaluate("() => { document.querySelector('[role=dialog] button').onclick = e => { e.target.dataset.clicked = 'yes'; }; }")
    agent.act(page, decision(agent.observe(page), "CLICK", "In panel"))
    assert page.evaluate("document.querySelector('[role=dialog] button').dataset.clicked") == "yes"


def test_overlay_elements_are_excluded_from_observation(page, base_url):
    page.goto(f"{base_url}/overlay.html")
    snapshot = agent.observe(page)
    assert names(snapshot) == ["Real button"]
    assert snapshot.notices == ["Real alert"]
    assert not snapshot.loading
    assert snapshot.open_panel == ""
    assert "Overlay" not in snapshot.text
    assert "Real text" in snapshot.text
    assert page.evaluate("getComputedStyle(document.querySelector('[data-jev-ignore]')).display") == "block"
    assert page.locator("[data-jev-idx]").count() == 1


def test_demo_overlays_change_nothing_jev_or_the_verifier_sees(page, base_url):
    page.goto(f"{base_url}/controls.html")
    before = agent.observe(page)
    agent.show_overlay(page, agent.DEMO_HUD, agent.DEMO_HUD_CSS, "Step 7 · Jev 0.94 · 132 ms")
    agent.show_overlay(page, agent.DEMO_BANNER, agent.DEMO_BANNER_CSS, "Waiting for human approval")
    agent.show_overlay(page, agent.DEMO_LABEL, agent.DEMO_LABEL_CSS, "CLICK  Next  0.98")
    assert page.locator("[data-jev-ignore]").count() == 3
    after = agent.observe(page)
    assert after.table == before.table
    assert after.text == before.text
    assert after.notices == before.notices
    assert agent.page_notices(after, before, {}) == ""


def test_demo_target_outlines_then_cleans_up_before_the_action(page, base_url):
    page.goto(f"{base_url}/controls.html")
    target = decision(agent.observe(page), "CLICK", "Next")
    target.heads = {"op": agent.Answer("CLICK", {"CLICK": 0.98}, 0.98)}
    shown = []
    page.expose_function("noteLabel", lambda text: shown.append(text))
    page.evaluate("""() => new MutationObserver(() => {
        const label = document.getElementById('jev-demo-label');
        if (label?.textContent) noteLabel(label.textContent);
    }).observe(document.body, {childList: true, subtree: true, characterData: true})""")
    started = time.perf_counter()
    agent.demo_target(page, target)
    assert time.perf_counter() - started >= agent.DEMO_TARGET_MS / 1000
    assert "CLICK  Next  0.98" in shown
    assert page.locator("[data-jev-ignore]").count() == 0


def test_an_overlay_on_top_of_the_target_doesnt_block_the_click(page, base_url):
    page.goto(f"{base_url}/controls.html")
    target = decision(agent.observe(page), "CLICK", "Next")
    page.evaluate("() => { document.getElementById('next').onclick = e => { e.target.dataset.clicked = 'yes'; }; }")
    box = page.locator("#next").bounding_box()
    agent.show_overlay(page, agent.DEMO_OUTLINE, f"left:{box['x']}px;top:{box['y']}px;width:{box['width']}px;"
                                                 f"height:{box['height']}px;background:#000;")
    agent.act(page, target)
    assert page.evaluate("document.getElementById('next').dataset.clicked") == "yes"


def test_demo_target_survives_an_element_that_vanished(page, base_url):
    page.goto(f"{base_url}/controls.html")
    target = decision(agent.observe(page), "CLICK", "Next")
    page.evaluate("document.getElementById('next').remove()")
    agent.demo_target(page, target)


def blurred(page, selector):
    return page.evaluate("s => getComputedStyle(document.querySelector(s)).filter", selector) == "blur(8px)"


@pytest.fixture
def private_page(page, base_url):
    page.add_init_script(agent.DEMO_PRIVACY_JS)
    page.goto(f"{base_url}/privacy.html")
    page.wait_for_function("getComputedStyle(document.getElementById('arn')).filter !== 'none'", timeout=5000)
    return page


@pytest.mark.parametrize("selector", ["#nav-usernameMenu", "#testid", "#account", "#account-plain", "#arn", "#field"])
def test_demo_blurs_account_details_and_arns(private_page, selector):
    assert blurred(private_page, selector)


@pytest.mark.parametrize("selector", ["#plain", "#short-number", "#hex-id"])
def test_demo_leaves_ordinary_text_alone(private_page, selector):
    assert not blurred(private_page, selector)


def test_demo_blurs_inside_shadow_dom(private_page):
    assert private_page.evaluate(
        "getComputedStyle(document.getElementById('host').shadowRoot.querySelector('span')).filter") == "blur(8px)"


def test_demo_blurs_an_arn_that_appears_later(private_page):
    private_page.evaluate("document.getElementById('plain').insertAdjacentHTML('afterend', '<p id=late>arn:aws:s3:::x</p>')")
    private_page.wait_for_function("getComputedStyle(document.getElementById('late')).filter === 'blur(8px)'")


def test_demo_blurs_an_arn_typed_into_a_field(private_page):
    private_page.fill("#empty", "arn:aws:iam::111122223333:role/x")
    private_page.wait_for_function("getComputedStyle(document.getElementById('empty')).filter === 'blur(8px)'")


def test_demo_blur_survives_navigation(private_page, base_url):
    private_page.goto(f"{base_url}/privacy.html?again=1")
    private_page.wait_for_function("getComputedStyle(document.getElementById('arn')).filter === 'blur(8px)'")


def test_capture_writes_a_real_png(page, base_url, tmp_path):
    page.goto(f"{base_url}/controls.html")
    path = tmp_path / "step_01.png"
    assert agent.capture(page, path) == str(path)
    assert path.read_bytes().startswith(b"\x89PNG")


def test_full_page_capture_is_taller_than_the_viewport(page, base_url, tmp_path):
    def png_height(path):
        return int.from_bytes(path.read_bytes()[20:24], "big")

    page.set_viewport_size({"width": 400, "height": 200})
    page.goto(f"{base_url}/many.html")
    viewport, full = tmp_path / "viewport.png", tmp_path / "full.png"
    agent.capture(page, viewport)
    agent.capture(page, full, full_page=True)
    assert png_height(viewport) == 200
    assert png_height(full) > 200


class ScriptedPage:
    url = "https://us-east-1.console.aws.amazon.com/costmanagement/home"

    def wait_for_timeout(self, ms):
        pass


def table(count, url=ScriptedPage.url):
    elements = [agent.Element(i, "button", f"Row {i}", "button", "", "", False, "", {}) for i in range(1, count + 1)]
    return agent.Snapshot(url, "Title", "", elements)


@pytest.fixture
def scripted(monkeypatch):
    """Makes observe() play back a list; returns the list of observations actually made."""
    seen = []

    def script(*steps):
        queue = iter(steps)

        def observe(page):
            step = next(queue)
            seen.append(step)
            if isinstance(step, Exception):
                raise step
            return step

        monkeypatch.setattr(agent, "observe", observe)
        return seen

    return script


def test_settle_needs_three_identical_observations(scripted):
    seen = scripted(table(6), table(7), table(7), table(7))
    snapshot, _, timed_out = agent.settle(ScriptedPage())
    assert len(seen) == 4
    assert len(snapshot.elements) == 7
    assert not timed_out


def test_settle_never_accepts_an_empty_or_near_empty_table(scripted):
    seen = scripted(table(0), table(0), table(0), table(4), table(4), table(4), table(9), table(9), table(9))
    snapshot, _, _ = agent.settle(ScriptedPage())
    assert len(seen) == 9
    assert len(snapshot.elements) == 9


def test_settle_restarts_when_only_the_url_changes(scripted):
    overview = ScriptedPage.url + "#/budgets/overview"
    seen = scripted(table(6), table(6), table(6, overview), table(6, overview), table(6, overview))
    snapshot, _, _ = agent.settle(ScriptedPage())
    assert len(seen) == 5
    assert snapshot.url == overview


def test_settle_restarts_when_labels_change_but_the_count_does_not(scripted):
    changed = table(6)
    changed.elements[0].name = "Loaded"
    seen = scripted(table(6), table(6), changed, changed, changed)
    agent.settle(ScriptedPage())
    assert len(seen) == 5


def test_settle_treats_a_navigation_mid_read_as_unsettled(scripted):
    seen = scripted(table(6), table(6), agent.PlaywrightError("context destroyed"), table(6), table(6), table(6))
    agent.settle(ScriptedPage())
    assert len(seen) == 6


def loading_table():
    return agent.Snapshot(ScriptedPage.url, "Title", "", table(6).elements, loading=True)


def test_settle_restarts_the_count_once_loading_clears(scripted):
    seen = scripted(*[loading_table() for _ in range(4)], table(6), table(6), table(6))
    snapshot, _, timed_out = agent.settle(ScriptedPage())
    assert len(seen) == 7
    assert not snapshot.loading
    assert not timed_out


def test_settle_observes_anyway_at_the_cap_and_reports_the_timeout(scripted, monkeypatch):
    monkeypatch.setattr(agent, "SETTLE_CAP_S", 0)
    scripted(table(0))
    snapshot, _, timed_out = agent.settle(ScriptedPage())
    assert snapshot.elements == []
    assert timed_out


def test_settle_stops_reading_as_soon_as_the_url_is_off_limits(scripted):
    page = ScriptedPage()

    class Redirected:
        url = ScriptedPage.url

        def wait_for_timeout(self, ms):
            page.url = "https://signin.aws.amazon.com/oauth"

    redirected = Redirected()
    type(redirected).url = property(lambda self: page.url)
    seen = scripted(table(6), table(6))
    _, _, timed_out = agent.settle(redirected)
    assert len(seen) == 1
    assert not timed_out


def decision(snapshot, op, name, input_key=None):
    target = next(e for e in snapshot.elements if e.name == name)
    turn = agent.Turn(None, {}, {}, {}, {"budget_name": "jevagent-x-budget", "period": "Daily"}, snapshot)
    return agent.Decision(op, turn, target=target, input_key=input_key)


def test_act_resolves_the_stamped_index_to_the_right_element(page, base_url):
    page.goto(f"{base_url}/controls.html")
    page.evaluate("() => { document.querySelector('[role=button]').onclick = e => { e.target.dataset.clicked = 'yes'; }; }")
    agent.act(page, decision(agent.observe(page), "CLICK", "Create budget"))
    assert page.locator("[role=button]").first.get_attribute("data-clicked") == "yes"


def test_act_clicks_an_opacity_zero_radio(page, base_url):
    page.goto(f"{base_url}/controls.html")
    agent.act(page, decision(agent.observe(page), "CLICK", "Use an existing role"))
    assert page.locator("input[type=radio]").is_checked()


def test_act_types_and_selects_values_from_inputs(page, base_url):
    page.goto(f"{base_url}/controls.html")
    snapshot = agent.observe(page)
    agent.act(page, decision(snapshot, "TYPE", "Budget name", "budget_name"))
    agent.act(page, decision(snapshot, "SELECT", "Period", "period"))
    assert page.locator("#bucket").input_value() == "jevagent-x-budget"
    assert page.locator("select").input_value() == "Daily"


def test_act_reaches_elements_inside_iframes(page, base_url):
    page.goto(f"{base_url}/shadow.html")
    page.frame_locator("iframe").get_by_role("button").wait_for()
    agent.act(page, decision(agent.observe(page), "CLICK", "Inside frame"))


def test_a_filled_combobox_reports_its_value_not_its_expanded_state(page, base_url):
    page.goto(f"{base_url}/required.html")
    by_name = {element.name: element for element in agent.observe(page).elements}
    assert by_name["Runtime required"].value == "Python 3.14"
    assert by_name["Role required"].value == "collapsed"


def test_missing_required_lists_only_empty_fields_named_required(page, base_url):
    page.goto(f"{base_url}/required.html")
    missing = agent.missing_required(agent.observe(page).elements)
    assert [row.split(" ", 2)[2] for row in missing] == [
        '"Budget name (required)"', '"Role required" (collapsed)', '"Accept terms required" (unchecked)']


def test_act_refuses_an_element_that_changed_after_the_gate(page, base_url):
    page.goto(f"{base_url}/controls.html")
    snapshot = agent.observe(page)
    page.evaluate("document.getElementById('next').textContent = 'Delete everything'")
    with pytest.raises(agent.ActionError, match="changed"):
        agent.act(page, decision(snapshot, "CLICK", "Next"))


def test_act_refuses_a_toggle_that_stopped_being_one_after_the_gate(page, base_url):
    page.goto(f"{base_url}/toggles.html")
    snapshot = agent.observe(page)
    page.evaluate("document.querySelector('[aria-expanded]').removeAttribute('aria-expanded')")
    with pytest.raises(agent.ActionError, match="changed"):
        agent.act(page, decision(snapshot, "CLICK", "Runtime Node.js 24.x"))


@pytest.mark.parametrize("name, gated", [
    ("Runtime Node.js 24.x", False),
    ("Create options", False),
    ("Delete options", False),
    ("Create security group", False),
    ("Run", True),  # aria-haspopup="false" isn't a popup
    ("Create Event Function", True),
    ("Terminate instance", True),
    ("Create log group", True),
])
def test_gate_on_real_controls(page, base_url, name, gated):
    page.goto(f"{base_url}/toggles.html")
    assert agent.needs_gate(decision(agent.observe(page), "CLICK", name)) is gated


def test_act_reports_an_element_that_disappeared(page, base_url):
    page.goto(f"{base_url}/controls.html")
    snapshot = agent.observe(page)
    page.evaluate("document.getElementById('next').remove()")
    with pytest.raises(agent.ActionError, match="gone"):
        agent.act(page, decision(snapshot, "CLICK", "Next"))
