"""Decision logic and the loop's stop conditions, with Jev mocked. Only the `live` test uses the network."""

import json
import os

import httpx2
import pytest
from dotenv import load_dotenv
from typesafe_sdk import TypeSafeClient

import agent

INPUTS = {"budget_name": "jevagent-x-budget", "budget_amount": "1"}


def element(idx, role="button", name="Next", tag="button", editable=False, href=""):
    return agent.Element(idx, role, name, tag, "", "", editable, href, {})


def snapshot(*elements, url="https://us-east-1.console.aws.amazon.com/lambda/home"):
    return agent.Snapshot(url, "Title", "page text", list(elements))


def answer(choice, confidence=0.95, **others):
    return agent.Answer(choice, {choice: confidence, **others}, confidence)


class FakeJev:
    """Scripted answers: `op`, `target` and `input` may be plain labels or Answer objects."""

    def __init__(self, op="CLICK", target="1", input="budget_name", scores=None):
        self.script = {"op": op, "target": target, "input": input}
        self.scores = scores or {}
        self.model = "fake"
        self.requests = []
        self.score_batches = []

    def take_cost(self):
        return 0.00002

    def choose_many(self, questions, state):
        self.requests.append((questions, state))
        answers = {}
        for name in questions:
            key = name if name in ("op", "input") else "target"
            value = self.script[key]
            answers[name] = value if isinstance(value, agent.Answer) else answer(value)
        return answers

    def score(self, questions, levels, state):
        self.score_batches.append(questions)
        assert "elements" not in state
        return {name: self.scores.get(name, 0.0) for name in questions}


def text_page(text, *notices, url=None):
    page = snapshot(element(1, name="Run"))
    page.text, page.notices = text, list(notices)
    page.url = url or page.url
    return page


def test_notices_list_alerts_even_on_the_first_observation():
    assert agent.page_notices(text_page("anything", "Saved"), None, {}) == "Saved"


def test_notices_show_only_new_lines():
    before, after = text_page("Heading\nBody"), text_page("Heading\nBody\nfriend\n\nBody")
    assert agent.page_notices(after, before, {}) == "friend"


def test_notices_skip_text_the_table_or_inputs_already_show():
    before = text_page("Heading")
    after = text_page("Heading\nRun\nReply with one word: hello\nfriend")
    assert agent.page_notices(after, before, {"prompt": "Reply with one word: hello"}) == "friend"


def test_notices_dont_repeat_an_alert_as_new_text():
    assert agent.page_notices(text_page("Old\nSaved", "Saved"), text_page("Old"), {}) == "Saved"


def test_a_new_url_is_a_new_page_not_new_text():
    after = text_page("Whole new page", url="https://us-east-1.console.aws.amazon.com/lambda/other")
    assert agent.page_notices(after, text_page("Old"), {}) == ""


def test_a_big_jump_in_text_is_a_new_page_but_alerts_survive():
    after = text_page("x" * (agent.MAX_NEW_TEXT_CHARS + 1), "Saved")
    assert agent.page_notices(after, text_page("Old"), {}) == "Saved"


def test_notices_are_capped_redacted_and_cleaned():
    notice = "Account 111122223333 owner someone@example.com\x1b[2K " + "y" * 500
    result = agent.page_notices(text_page("", notice), None, {})
    assert len(result) == agent.MAX_NOTICE_CHARS
    assert "111122223333" not in result and "someone@" not in result and "\x1b" not in result


def test_notices_are_in_the_state_only_when_present():
    jev = FakeJev(op="WAIT")
    agent.decide(jev, "goal", snapshot(element(1)), [], {}, "Reply: friend")
    agent.decide(jev, "goal", snapshot(element(1)), [], {})
    assert jev.requests[0][1]["page_notices_untrusted"] == "Reply: friend"
    assert "page_notices_untrusted" not in jev.requests[1][1]


def test_click_needs_one_request_and_uses_the_target_head():
    jev = FakeJev()
    decision = agent.decide(jev, "goal", snapshot(element(1)), [], {})
    assert (decision.op, decision.target.idx) == ("CLICK", 1)
    assert len(jev.requests) == 1


def test_state_is_goal_url_title_last_five_actions_and_table():
    jev = FakeJev(op="WAIT")
    history = [{"op": "WAIT", "n": n} for n in range(8)]
    agent.decide(jev, "goal", snapshot(element(1)), history, {})
    state = jev.requests[0][1]
    assert set(state) == {"goal", "url", "title", "last_actions", "elements"}
    assert [entry["n"] for entry in state["last_actions"]] == [3, 4, 5, 6, 7]
    assert "page text" not in json.dumps(state)


def test_page_names_sent_to_jev_are_truncated_and_redacted():
    jev = FakeJev(op="WAIT")
    agent.decide(jev, "goal", snapshot(element(1, name="111122223333 " + "x" * 200)), [], {})
    table = jev.requests[0][1]["elements"]
    assert "111122223333" not in table
    assert '"<account> ' + "x" * 70 + '"' in table


def test_type_chooses_input_in_a_second_request_conditioned_on_the_field():
    jev = FakeJev(op="TYPE", target="2", input="budget_name")
    field = element(2, "textbox", "Budget name", "input", editable=True)
    decision = agent.decide(jev, "goal", snapshot(element(1), field), [], INPUTS)
    assert decision.input_key == "budget_name"
    assert len(jev.requests) == 2
    assert jev.requests[1][1]["field"] == field.row


def test_typed_values_can_only_come_from_inputs():
    jev = FakeJev(op="TYPE", target="1")
    field = element(1, "textbox", "Name", "input", editable=True)
    agent.decide(jev, "goal", snapshot(field), [], INPUTS)
    options = jev.requests[1][0]["input"][1]
    assert options.keys() == {*INPUTS, agent.NO_INPUT}


def test_type_and_select_are_not_offered_without_inputs():
    jev = FakeJev(op="WAIT")
    field = element(1, "textbox", "Name", "input", editable=True)
    agent.decide(jev, "goal", snapshot(field, element(2)), [], {})
    assert jev.requests[0][0]["op"][1].keys() >= {"CLICK", "DONE", "BLOCKED"}
    assert not jev.requests[0][0]["op"][1].keys() & {"TYPE", "SELECT"}


def test_255_candidates_skip_scoring():
    jev = FakeJev(op="WAIT")
    agent.decide(jev, "goal", snapshot(*[element(i) for i in range(1, 256)]), [], {})
    assert not jev.score_batches
    assert len(jev.requests[0][0]["CLICK"][1]) == 255


def test_above_255_scores_in_batches_and_keeps_top_50():
    best = {str(i): 3.0 for i in range(301, 351)}
    jev = FakeJev(op="CLICK", target="310", scores=best)
    elements = [element(i, name=f"Row {i}") for i in range(1, 351)]
    decision = agent.decide(jev, "goal", snapshot(*elements), [], {})
    assert len(jev.score_batches) == 4
    assert sum(len(batch) for batch in jev.score_batches) == 350
    options = jev.requests[0][0]["CLICK"][1]
    assert set(options) == set(best)
    assert jev.requests[0][1]["elements"].count("\n") == 49
    assert decision.target.idx == 310


def test_jev_rejects_an_answer_outside_the_options():
    def respond(request):
        return httpx2.Response(200, json={
            "model": "jev-1.14.0", "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {"q": {"type": "choice", "choice": "ghost", "confidence": 1.0,
                              "probabilities": {"a": 1.0}}}})

    client = TypeSafeClient(api_key="test", transport=httpx2.MockTransport(respond))
    with pytest.raises(agent.JevError, match="not an offered"):
        agent.Jev(client=client).choose_many({"q": ("?", {"a": None})}, {"s": 1})


def test_jev_parses_every_primitive_from_wire_json():
    bodies = {
        "choice": {"type": "choice", "choice": "a", "confidence": 0.8, "probabilities": {"a": 0.9, "b": 0.1}},
        "noul": {"type": "noul", "noul": 0.97},
        "score": {"type": "score", "score": 2.5, "confidence": 0.6, "legend": {"0": "low", "1": "high"},
                  "probabilities": {"0": 0.5, "1": 0.5}},
    }

    def respond(request):
        asked = json.loads(request.content)["questions"]
        answers = {name: bodies[question["type"]] for name, question in asked.items()}
        return httpx2.Response(200, json={"model": "jev-1.14.0", "answers": answers,
                                          "usage": {"input_tokens": 1, "output_tokens": 1}})

    jev = agent.Jev(client=TypeSafeClient(api_key="test", transport=httpx2.MockTransport(respond),
                                          compression=False))
    assert jev.choose("?", {"a": None, "b": None}, {"s": 1}) == ("a", {"a": 0.9, "b": 0.1}, 0.8)
    assert jev.noul("?", {"s": 1}) == 0.97
    assert jev.score({"x": "?"}, ["low", "high"], {"s": 1}) == {"x": 2.5}
    assert jev.model == "jev-1.14.0"


@pytest.mark.parametrize("usage, spent", [({"cost": 0.0001}, 0.0002), ({}, None)])
def test_jev_totals_usage_cost_until_taken(usage, spent):
    def respond(request):
        return httpx2.Response(200, json={
            "model": "jev-1.14.0", "answers": {"answer": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 1, "output_tokens": 1, **usage}})

    jev = agent.Jev(client=TypeSafeClient(api_key="test", transport=httpx2.MockTransport(respond),
                                          compression=False))
    jev.noul("?", "state")
    jev.noul("?", "state")
    assert jev.take_cost() == spent
    assert jev.take_cost() is None


# Loop behavior


class FakePage:
    url = "https://us-east-1.console.aws.amazon.com/lambda/home"

    def __init__(self):
        self.shots = []
        self.full_page = {}

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path, timeout, full_page):
        name = path.rsplit("/", 1)[-1]
        self.shots.append(name)
        self.full_page[name] = full_page


class Harness:
    def __init__(self, monkeypatch, tmp_path, snapshots):
        self.acted = []
        self.page = FakePage()
        self.snapshots = iter(snapshots)
        self.log = agent.RunLog("run", tmp_path)
        monkeypatch.setattr(agent, "settle", lambda page: (next(self.snapshots), 12.0, False))
        monkeypatch.setattr(agent, "act", lambda page, decision: self.acted.append(decision))

    def run(self, jev, replies=(), dry_run=False, inputs=INPUTS, max_steps=5):
        answers = iter(replies)
        limits = agent.Limits(max_steps, 0.7, dry_run)
        return agent.run_loop(self.page, jev, "goal", inputs, self.log, limits, lambda text: next(answers))

    def steps(self):
        return [json.loads(line) for line in (self.log.dir / "steps.jsonl").read_text().splitlines()]


def test_low_confidence_asks_the_human_and_abort_stops(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    outcome = harness.run(FakeJev(op=answer("CLICK", 0.4, WAIT=0.3)), replies=["a"])
    assert outcome.status == "aborted"
    assert not harness.acted


def test_human_can_pick_a_different_target_at_low_confidence(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1), element(2)), snapshot(element(1))])
    jev = FakeJev(target=agent.Answer("1", {"1": 0.5, "2": 0.4}, 0.2))
    outcome = harness.run(jev, replies=["2"], max_steps=1)
    assert harness.acted[0].target.idx == 2
    assert outcome.status == "step_limit"


def test_human_pick_is_logged_as_human_with_jevs_own_confidence(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    jev = FakeJev(op=agent.Answer("WAIT", {"WAIT": 0.41, "SCROLL_DOWN": 0.38}, 0.12))
    harness.run(jev, replies=["2"], max_steps=1)
    record = harness.steps()[0]
    assert record["source"] == "human"
    assert record["op"]["choice"] == "SCROLL_DOWN"
    assert record["confidence"] == record["op"]["confidence"] == 0.12


def test_jev_steps_are_logged_as_jev(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    harness.run(FakeJev(), max_steps=1)
    assert harness.steps()[0]["source"] == "jev"


def op_doubt(**options):
    """An op head that lost confidence, with its targets scripted through FakeJev.script."""
    return agent.Answer(next(iter(options)), options, 0.12)


def two_rows():
    return [element(1, name="Budgets"), element(2, name="Reports"), element(3, name="Help")]


def click_targets():
    return agent.Answer("2", {"2": 0.7, "1": 0.2, "3": 0.1}, 0.9)


def test_op_prompt_shows_targets_fragment_and_heading(monkeypatch, tmp_path, capsys):
    url = "https://us-east-1.console.aws.amazon.com/costmanagement/home#/budgets/overview"
    page = agent.Snapshot(url, "Title", "", two_rows(), heading="Budgets overview")
    harness = Harness(monkeypatch, tmp_path, [page])
    harness.run(FakeJev(op=op_doubt(CLICK=0.5, WAIT=0.4), target=click_targets()), replies=["a"])
    out = capsys.readouterr().out
    assert "#/budgets/overview" in out
    assert "Page heading: Budgets overview" in out
    assert '1. CLICK [2] button "Reports" (op 0.50, target 0.70)' in out
    assert '2. CLICK [1] button "Budgets" (op 0.50, target 0.20)' in out
    assert '3. CLICK [3] button "Help" (op 0.50, target 0.10)' in out
    assert "4. WAIT (0.40)" in out


def test_op_prompt_falls_back_to_the_title_without_a_heading(monkeypatch, tmp_path, capsys):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    harness.run(FakeJev(op=op_doubt(WAIT=0.5, DONE=0.4)), replies=["a"])
    assert "Page heading: Title" in capsys.readouterr().out


def test_option_one_runs_the_top_op_on_the_top_target(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(*two_rows())])
    harness.run(FakeJev(op=op_doubt(CLICK=0.5, WAIT=0.4), target=click_targets()), replies=["1"], max_steps=1)
    assert harness.acted[0].op == "CLICK"
    assert harness.acted[0].target.idx == 2


def test_a_numbered_option_picks_a_different_target_and_logs_it(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(*two_rows())])
    harness.run(FakeJev(op=op_doubt(CLICK=0.5, WAIT=0.4), target=click_targets()), replies=["3"], max_steps=1)
    assert harness.acted[0].target.idx == 3
    record = harness.steps()[0]
    assert record["human_target"] == '[3] button "Help"'
    assert record["target"]["choice"] == "3"
    assert record["target"]["confidence"] == 0.9
    assert record["source"] == "human"


def test_human_target_is_null_when_jev_picked_the_target(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    harness.run(FakeJev(), max_steps=1)
    assert harness.steps()[0]["human_target"] is None


def test_picking_a_non_targeted_op_needs_no_target(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(*two_rows())])
    harness.run(FakeJev(op=op_doubt(CLICK=0.5, WAIT=0.4), target=click_targets()), replies=["4"], max_steps=1)
    assert harness.acted[0].op == "WAIT"
    assert harness.steps()[0]["human_target"] is None


def test_op_pick_settles_a_doubtful_target_with_one_prompt(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    jev = FakeJev(op=answer("CLICK", 0.3), target=agent.Answer("1", {"1": 0.6}, 0.2))
    harness.run(jev, replies=["1"], max_steps=1)
    assert len(harness.acted) == 1


def test_type_pick_asks_jev_for_the_input_on_the_chosen_field(monkeypatch, tmp_path):
    fields = [element(i, "textbox", name, "input", editable=True) for i, name in ((1, "Name"), (2, "Amount"))]
    harness = Harness(monkeypatch, tmp_path, [snapshot(*fields)])
    jev = FakeJev(op=op_doubt(TYPE=0.5, WAIT=0.4), target=agent.Answer("1", {"1": 0.8, "2": 0.2}, 0.9))
    harness.run(jev, replies=["2"], max_steps=1)
    assert harness.acted[0].target.idx == 2
    assert jev.requests[-1][1]["field"] == fields[1].row


def test_a_commit_click_picked_at_the_op_prompt_still_reaches_the_gate(monkeypatch, tmp_path):
    rows = [element(1, name="Create budget"), element(2, name="Help")]
    harness = Harness(monkeypatch, tmp_path, [snapshot(*rows)])
    jev = FakeJev(op=op_doubt(CLICK=0.5, WAIT=0.4), target=agent.Answer("1", {"1": 0.8, "2": 0.2}, 0.9))
    outcome = harness.run(jev, replies=["1", "n"])
    assert outcome.status == "denied"
    assert not harness.acted


def test_every_step_saves_a_screenshot_and_logs_its_path(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1)), snapshot(element(1))])
    harness.run(FakeJev(), max_steps=2)
    assert harness.page.shots == ["step_01.png", "step_02.png"]
    first, second = harness.steps()
    assert first["screenshot"] == str(harness.log.dir / "step_01.png")
    assert second["screenshot"] == str(harness.log.dir / "step_02.png")
    assert first["gate_screenshot"] is None


def test_the_final_done_step_gets_a_screenshot(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    harness.run(FakeJev(op="DONE"))
    assert harness.page.shots == ["step_01.png"]


def test_gate_screenshot_is_taken_before_the_human_is_asked(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1, name="Create budget"))])
    seen = []

    def ask(text):
        seen.extend(harness.page.shots)
        return "n"

    agent.run_loop(harness.page, FakeJev(), "goal", {}, harness.log, agent.Limits(5, 0.7), ask)
    assert seen == ["step_01.png", "step_01_gate.png"]
    assert harness.steps()[0]["gate_screenshot"] == str(harness.log.dir / "step_01_gate.png")


def test_dry_run_still_saves_the_gate_screenshot(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1, name="Create budget"))])
    harness.run(FakeJev(), dry_run=True)
    assert "step_01_gate.png" in harness.page.shots


def test_only_the_gate_screenshot_is_full_page(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1, name="Create budget"))])
    harness.run(FakeJev(), dry_run=True)
    assert harness.page.full_page == {"step_01.png": False, "step_01_gate.png": True}


def test_a_failed_screenshot_is_logged_and_does_not_stop_the_run(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])

    def fail(path, timeout, full_page):
        raise agent.PlaywrightError("target closed")

    monkeypatch.setattr(harness.page, "screenshot", fail)
    harness.run(FakeJev(), max_steps=1)
    assert len(harness.acted) == 1
    assert harness.steps()[0]["screenshot"] is None


def test_blocked_pages_are_never_screenshotted(monkeypatch, tmp_path):
    iam = "https://us-east-1.console.aws.amazon.com/iam/home"
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1), url=iam)])
    harness.run(FakeJev())
    assert not harness.page.shots


def test_blocked_redirect_during_settle_stops_with_no_snapshot(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [])
    monkeypatch.setattr(agent, "settle", lambda page: (None, 5.0, False))
    monkeypatch.setattr(FakePage, "url", "https://signin.aws.amazon.com/oauth")
    jev = FakeJev()
    assert harness.run(jev).status == "blocked"
    assert not jev.requests


def test_low_confidence_on_a_commit_click_still_reaches_the_gate(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1, name="Create"))])
    jev = FakeJev(target=agent.Answer("1", {"1": 0.6}, 0.2))
    outcome = harness.run(jev, replies=["1", "n"])
    assert outcome.status == "denied"
    assert not harness.acted


def test_commit_click_requires_y(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1, name="Create budget")), snapshot(element(1))])
    jev = FakeJev()
    jev.script["op"] = "CLICK"
    harness.run(jev, replies=["y"], max_steps=1)
    assert len(harness.acted) == 1
    assert harness.steps()[0]["gate"] == "approved"


def test_a_control_named_only_by_an_ancestor_still_needs_approval(monkeypatch, tmp_path):
    borrowed = element(1, name="Instances table")
    borrowed.borrowed_name = True
    harness = Harness(monkeypatch, tmp_path, [snapshot(borrowed)])
    assert harness.run(FakeJev(), dry_run=True).status == "dry_run_stop"
    assert not harness.acted


def test_dry_run_stops_at_the_gate_without_acting(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1, name="Create budget"))])
    outcome = harness.run(FakeJev(), dry_run=True)
    assert outcome.status == "dry_run_stop"
    assert not harness.acted
    assert harness.steps()[0]["gate"] == "dry_run_stop"


def test_no_input_fits_is_blocked(monkeypatch, tmp_path):
    field = element(1, "textbox", "Password hint", "input", editable=True)
    harness = Harness(monkeypatch, tmp_path, [snapshot(field)])
    outcome = harness.run(FakeJev(op="TYPE", input=agent.NO_INPUT))
    assert outcome.status == "blocked"
    assert not harness.acted


def test_blocklisted_page_stops_before_anything_reaches_jev(monkeypatch, tmp_path):
    iam = "https://us-east-1.console.aws.amazon.com/iam/home"
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1), url=iam)])
    jev = FakeJev()
    outcome = harness.run(jev)
    assert outcome.status == "blocked"
    assert "blocklisted" in outcome.reason
    assert not jev.requests


def test_link_to_a_blocklisted_page_is_never_clicked(monkeypatch, tmp_path):
    link = element(1, "link", "Users", "a", href="https://us-east-1.console.aws.amazon.com/iam/home")
    harness = Harness(monkeypatch, tmp_path, [snapshot(link)])
    outcome = harness.run(FakeJev())
    assert outcome.status == "blocked"
    assert not harness.acted


def test_three_consecutive_failures_stop_the_loop(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1)) for _ in range(5)])

    def fail(page, decision):
        raise agent.ActionError("element 1 is gone")

    monkeypatch.setattr(agent, "act", fail)
    assert harness.run(FakeJev()).status == "too_many_failures"
    assert len(harness.steps()) == 3


def test_time_limit_ignores_human_pauses(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "MAX_WALL_S", 0)
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    assert harness.run(FakeJev()).status == "time_limit"


def test_step_log_has_every_required_field(monkeypatch, tmp_path, capsys):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1)), snapshot(element(1))])
    harness.run(FakeJev(), max_steps=1)
    record = harness.steps()[0]
    assert record.keys() >= {"step", "url", "element_count", "op", "target", "gate", "latency_ms", "screenshot"}
    assert record["op"].keys() == {"choice", "confidence", "top3"}
    assert record["latency_ms"].keys() == {"observe", "jev", "act"}
    assert record["usage_cost"] == 0.00002
    assert (record["settle_ms"], record["settle_timeout"]) == (12.0, False)
    assert record.keys() >= {"source", "gate_screenshot"}
    assert "step 01 CLICK" in capsys.readouterr().out


def test_loop_logs_notices_and_sends_them_to_jev(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [text_page("Old"), text_page("Old\nfriend")])
    jev = FakeJev(op="SCROLL_DOWN")
    harness.run(jev, max_steps=2)
    assert [step["page_notices"] for step in harness.steps()] == ["", "friend"]
    assert jev.requests[-1][1]["page_notices_untrusted"] == "friend"


def test_text_that_appears_during_a_wait_stays_new(monkeypatch, tmp_path):
    pages = [text_page("Old"), text_page("Old\nfriend"), text_page("Old\nfriend")]
    harness = Harness(monkeypatch, tmp_path, pages)
    harness.run(FakeJev(op="WAIT"), max_steps=3)
    assert [step["page_notices"] for step in harness.steps()] == ["", "friend", "friend"]


def test_done_ends_the_loop(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path, [snapshot(element(1))])
    assert harness.run(FakeJev(op="DONE")).status == "done"
    assert not harness.acted


@pytest.mark.live
def test_live_noul_confirms_the_api_key():
    load_dotenv(agent.ROOT / ".env")
    if not os.environ.get("TYPESAFE_API_KEY"):
        pytest.skip("TYPESAFE_API_KEY isn't set")
    with TypeSafeClient() as client:
        probability = agent.Jev(client=client).noul("Is this text a greeting?", "Hello there!")
    assert 0.0 <= probability <= 1.0


@pytest.mark.parametrize("report, expected", [
    (["deleted budget jevagent-x-budget"], ["Teardown: deleted [budget jevagent-x-budget]"]),
    (["deleted function a", "deleted instance b"], ["Teardown: deleted [function a, instance b]"]),
    ([], ["Teardown: nothing to delete"]),
    (["skipped function jevagent-x: no run ID in the name"], ["Teardown: nothing to delete"]),
    (["FAILED budget b: AccessDenied"], ["Teardown FAILED: budget b: AccessDenied"]),
    (["FAILED: boom; run: python verify.py teardown 1"], ["Teardown FAILED: boom; run: python verify.py teardown 1"]),
    (["deleted budget a", "FAILED function b: AccessDenied"],
     ["Teardown: deleted [budget a]", "Teardown FAILED: function b: AccessDenied"]),
])
def test_teardown_lines(report, expected):
    assert agent.teardown_lines(report) == expected


def fake_run(monkeypatch, tmp_path, check):
    real_log = agent.RunLog
    monkeypatch.setattr(agent, "preflight", lambda cdp_url: [])
    monkeypatch.setattr(agent, "RunLog", lambda run_id: real_log(run_id, tmp_path))
    monkeypatch.setattr(agent, "drive", lambda *args: agent.Outcome("done", snapshot(element(1))))
    monkeypatch.setitem(agent.verify.CHECKS, "create_budget", check)


def test_run_prints_teardown_after_the_verifier(monkeypatch, tmp_path, capsys):
    passed = agent.verify.VerifyResult(True, "ok")
    fake_run(monkeypatch, tmp_path, agent.verify.Check(lambda run_id, ctx: passed, lambda run_id: ["deleted budget b"]))
    assert agent.run_task("01_create_budget") == 0
    out = capsys.readouterr().out
    assert out.index("Verifier passed") < out.index("Teardown: deleted [budget b]") < out.index("Finished:")


def test_run_prints_a_teardown_failure(monkeypatch, tmp_path, capsys):
    def boom(run_id):
        raise RuntimeError("no credentials")

    passed = agent.verify.VerifyResult(True, "ok")
    fake_run(monkeypatch, tmp_path, agent.verify.Check(lambda run_id, ctx: passed, boom))
    agent.run_task("01_create_budget")
    assert "Teardown FAILED: no credentials" in capsys.readouterr().out
