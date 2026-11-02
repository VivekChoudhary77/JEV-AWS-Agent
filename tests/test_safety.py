"""Approval regex, URL rules, redaction and task schema."""

import time

import pytest
from pydantic import ValidationError

import agent

BUDGETS = "https://us-east-1.console.aws.amazon.com/costmanagement/home?region=us-east-1#/budgets"
LAMBDA = "https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/create/function"
HOST = "https://us-east-1.console.aws.amazon.com"


@pytest.mark.parametrize("name", [
    "Create budget", "Launch instance", "Submit", "Delete", "Terminate instance", "Confirm",
    "Purchase", "Subscribe", "Upgrade plan", "Run", "Save changes", "CREATE FUNCTION",
    "Create Event Function", "Re-run",
])
def test_commit_words_require_approval(name):
    assert agent.requires_approval(name)


@pytest.mark.parametrize("name", [
    "Cancel", "Next", "Previous", "Budgets", "Use an existing role", "Runtime Node.js 24.x", "Running instances",
    "Saved views", "Recreate",
])
def test_non_commit_words_pass(name):
    assert not agent.requires_approval(name)


def test_gate_sees_past_the_80_char_truncation():
    name = "x" * 100 + " Delete"
    assert "Delete" not in agent.short(name)
    assert agent.requires_approval(name)


@pytest.mark.parametrize("name", ["", "   "])
def test_unnamed_controls_fail_closed(name):
    assert agent.requires_approval(name)


@pytest.mark.parametrize("url", [
    BUDGETS,
    LAMBDA,
    "https://s3.console.aws.amazon.com/foo",
    "https://1234567-abcdef.us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#Instances:",
    f"{HOST}/ec2/home?region=us-east-1#LaunchInstances:",
    f"{HOST}/bedrock/home?region=us-east-1#/chat-playground",
])
def test_allowed_urls(url):
    assert agent.blocked_reason(url) is None


@pytest.mark.parametrize("url", [
    "https://signin.aws.amazon.com/oauth",
    "https://console.aws.amazon.com/iam/home",
    "https://aws.amazon.com/marketplace",
    "http://us-east-1.console.aws.amazon.com/lambda/home",
    "https://evil.example/#.console.aws.amazon.com",
    "https://us-east-1.console.aws.amazon.com@evil.example/",
    "https://us-east-1.console.aws.amazon.com.evil.example/",
    "https://evil.example%2F.console.aws.amazon.com/",
    "about:blank",
    "https://[bad/",
])
def test_off_allowlist_urls_are_blocked(url):
    assert agent.blocked_reason(url)


@pytest.mark.parametrize("url", [
    "https://eu-west-1.console.aws.amazon.com/ec2/home",
    f"{HOST}/ec2/home?region=eu-west-1",
    f"{HOST}/ec2/home#Instances:region=ap-south-1",
    f"{HOST}/ec2/home#/x?region=eu-west-1",
])
def test_other_regions_are_blocked(url):
    assert "region" in agent.blocked_reason(url)


@pytest.mark.parametrize("path", [
    "/iam/home", "/iamv2/home#/users", "/iam", "/organizations/v2/home",
    "/billing/home#/paymentpreferences", "/billing/home#/account", "/billing/home",
    "/account/home", "/support/plans/home", "/support/home#/plans", "/marketplace/home",
    "/bedrock/home?region=us-east-1#/provisioned-throughput",
    "/bedrock/home#/custom-models", "/bedrock/home#/model-customization",
    "/bedrock/home#/knowledge-bases", "/settings/home",
    "/%69am/home", "/lambda/home#/x?next=%2Fiam%2Fhome", "/IAM/home",
])
def test_blocklisted_pages(path):
    assert "blocklisted" in agent.blocked_reason(HOST + path)


def test_blocklisted_service_hosts():
    assert agent.blocked_reason("https://iam.console.aws.amazon.com/")


def test_redacts_account_ids_and_emails():
    text = agent.redact("111122223333 1111-2222-3333 someone@example.com jevagent-26103015140a3f-fn")
    assert text == "<account> <account> <email> jevagent-26103015140a3f-fn"


def test_redaction_stays_fast_on_long_text_without_an_email():
    started = time.perf_counter()
    agent.redact("abcdefghij." * 40000)
    assert time.perf_counter() - started < 0.5


def test_clean_strips_terminal_escapes():
    assert agent.clean("Cancel\x1b[2K\x1b[1A\u202eCreate\n") == "Cancel [2K [1A Create"


def task(**overrides):
    fields = {"id": "t", "goal": "Make {thing_name}", "done_when": "{thing_name} is shown.",
              "start_url": LAMBDA, "verifier": "read_budget",
              "inputs": {"thing_name": "jevagent-{run_id}-x"}}
    return agent.Task(**{**fields, **overrides})


def test_schema_accepts_and_renders_a_valid_task():
    goal, inputs, done_when, ready_when_commit = task().render("26103015140a3f")
    assert goal == "Make jevagent-26103015140a3f-x"
    assert ready_when_commit == ""
    assert done_when == "jevagent-26103015140a3f-x is shown."
    assert inputs == {"thing_name": "jevagent-26103015140a3f-x"}


def test_schema_rejects_missing_inputs_field():
    fields = {"id": "t", "goal": "g", "done_when": "d", "start_url": LAMBDA, "verifier": "read_budget"}
    with pytest.raises(ValidationError):
        agent.Task(**fields)


def test_schema_requires_done_when():
    fields = {"id": "t", "goal": "g", "inputs": {}, "start_url": LAMBDA, "verifier": "read_budget"}
    with pytest.raises(ValidationError, match="done_when"):
        agent.Task(**fields)


def test_schema_rejects_done_when_placeholder_without_input():
    with pytest.raises(ValidationError, match="missing inputs"):
        task(done_when="{other} is shown.")


def test_done_when_resolves_in_the_real_tasks():
    run_id = "26103015140a3f"
    budget = agent.load_task("01_create_budget").render(run_id)[2]
    assert budget == f"A banner says the budget jevagent-{run_id}-budget was created successfully."
    database = agent.load_task("99_rds_create_db").render(run_id)[2]
    assert database == f"A banner says the database jevagent-{run_id}-db is being created."
    ec2 = agent.load_task("04_ec2_launch_t3micro").render(run_id)[2]
    assert ec2 == 'A banner says "Successfully initiated launch of instance".'


def test_ready_when_commit_resolves_in_the_real_tasks():
    rendered = {i: agent.load_task(i).render("26103015140a3f")[3] for i in (
        "00_read_budget", "01_create_budget", "03_lambda_function_url", "04_ec2_launch_t3micro",
        "05_bedrock_playground_prompt", "99_rds_create_db")}
    assert {i: claim for i, claim in rendered.items() if claim} == {
        "03_lambda_function_url": "Runtime is Python 3.14, execution role is jev-shared-lambda-exec, "
                                  "and Function URL auth type is AWS_IAM.",
        "04_ec2_launch_t3micro": 'Name, key pair "Proceed without a key pair", and the default security group are set.',
        "05_bedrock_playground_prompt": 'The model "Nova Micro" is selected and the prompt box contains '
                                        '"Reply with one word: hello".'}


def test_schema_rejects_ready_when_commit_placeholder_without_input():
    with pytest.raises(ValidationError, match="missing inputs"):
        task(ready_when_commit="{other} is set.")


def test_schema_rejects_goal_placeholder_without_input():
    with pytest.raises(ValidationError, match="missing inputs"):
        task(goal="Make {other}")


def test_schema_rejects_created_name_without_prefix():
    with pytest.raises(ValidationError, match="must start with"):
        task(inputs={"thing_name": "plain"})


def test_schema_exempts_existing_resources_from_prefix():
    assert task(inputs={"thing_name": "jevagent-{run_id}-x", "existing_role_name": "shared"})


def test_schema_rejects_blocked_start_url():
    with pytest.raises(ValidationError, match="start_url"):
        task(start_url=f"{HOST}/iam/home")


def test_ready_when_is_optional_and_only_task_03_sets_it():
    assert task().ready_when == ""
    ids = ["00_read_budget", "01_create_budget", "03_lambda_function_url",
           "04_ec2_launch_t3micro", "05_bedrock_playground_prompt", "99_rds_create_db"]
    assert {i: agent.load_task(i).ready_when for i in ids if agent.load_task(i).ready_when} == {
        "03_lambda_function_url": "Function name"}


def test_only_task_03_has_ungated_labels():
    assert task().ungated_labels == []
    ids = ["00_read_budget", "01_create_budget", "03_lambda_function_url",
           "04_ec2_launch_t3micro", "05_bedrock_playground_prompt", "99_rds_create_db"]
    assert {i: agent.load_task(i).ungated_labels for i in ids if agent.load_task(i).ungated_labels} == {
        "03_lambda_function_url": ["Save"]}


def test_every_task_in_the_file_loads():
    ids = ["00_read_budget", "01_create_budget", "03_lambda_function_url",
           "04_ec2_launch_t3micro", "05_bedrock_playground_prompt", "99_rds_create_db"]
    tasks = [agent.load_task(task_id) for task_id in ids]
    assert [t.enabled for t in tasks] == [True] * 5 + [False]
    assert tasks[2].inputs["existing_role_name"] == "jev-shared-lambda-exec"


@pytest.mark.parametrize("value", ["", "   "])
def test_startup_exits_when_the_api_key_is_empty(monkeypatch, value):
    monkeypatch.setattr(agent, "load_dotenv", lambda path: None)
    monkeypatch.setattr("sys.argv", ["agent.py", "00_read_budget"])
    monkeypatch.setenv("TYPESAFE_API_KEY", value)
    with pytest.raises(SystemExit, match="TYPESAFE_API_KEY is empty"):
        agent.main()
