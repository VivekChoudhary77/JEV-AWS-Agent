"""observe() against static fixtures in headless Chromium, served over HTTP so iframes are same-origin."""

import functools
import http.server
import threading
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


def test_settle_needs_two_identical_observations(scripted):
    seen = scripted(table(6), table(7), table(7))
    snapshot, _, timed_out = agent.settle(ScriptedPage())
    assert len(seen) == 3
    assert len(snapshot.elements) == 7
    assert not timed_out


def test_settle_never_accepts_an_empty_or_near_empty_table(scripted):
    seen = scripted(table(0), table(0), table(4), table(4), table(9), table(9))
    snapshot, _, _ = agent.settle(ScriptedPage())
    assert len(seen) == 6
    assert len(snapshot.elements) == 9


def test_settle_restarts_when_only_the_url_changes(scripted):
    overview = ScriptedPage.url + "#/budgets/overview"
    seen = scripted(table(6), table(6, overview), table(6, overview))
    snapshot, _, _ = agent.settle(ScriptedPage())
    assert len(seen) == 3
    assert snapshot.url == overview


def test_settle_restarts_when_labels_change_but_the_count_does_not(scripted):
    changed = table(6)
    changed.elements[0].name = "Loaded"
    seen = scripted(table(6), changed, changed)
    agent.settle(ScriptedPage())
    assert len(seen) == 3


def test_settle_treats_a_navigation_mid_read_as_unsettled(scripted):
    seen = scripted(table(6), agent.PlaywrightError("context destroyed"), table(6), table(6))
    agent.settle(ScriptedPage())
    assert len(seen) == 4


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


def test_act_reports_an_element_that_disappeared(page, base_url):
    page.goto(f"{base_url}/controls.html")
    snapshot = agent.observe(page)
    page.evaluate("document.getElementById('next').remove()")
    with pytest.raises(agent.ActionError, match="gone"):
        agent.act(page, decision(snapshot, "CLICK", "Next"))
