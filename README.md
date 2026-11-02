# jev-console-agent

A small browser agent that completes predefined tasks in the real AWS Management Console. A fast decision model chooses each browser action, deterministic code carries it out, and a separate boto3 check decides whether the task actually succeeded.

The project is a hands-on study of how a decision model fits into an agent loop. The tasks mirror AWS's "Explore AWS" activities, but the goal is the pattern, not the credits.

## How it works

The model never writes text. [Jev](https://docs.typesafe.ai), from TypeSafe, returns typed decisions: a choice from a list, a score, or a yes/no probability. Everything else is ordinary code.

| Role | Component | Responsibility |
|---|---|---|
| Decide | Jev, through OpenRouter | Pick the operation, the target element and the input value |
| Act | Playwright over CDP | Run the chosen action in a Chrome window a human logged into |
| Verify | boto3 (`verify.py`) | Check AWS state independently, then clean up |

Each step runs the same loop:

```
observe  ->  decide  ->  gate  ->  act
   ^                                |
   +--------------------------------+
```

1. **Observe.** Wait for the page to settle, then build a numbered table of visible, enabled controls.
2. **Decide.** Ask Jev for an operation (`CLICK`, `TYPE`, `SELECT`, `SCROLL_DOWN`, `SCROLL_UP`, `WAIT`, `DONE` or `BLOCKED`), then a target row. For `TYPE` and `SELECT`, also ask which of the task's inputs to use.
3. **Gate.** Stop for human approval before any click that commits something.
4. **Act.** Run the action with Playwright and observe again.

If Jev's confidence drops below 0.7, the agent pauses and asks the human to pick. When the loop ends, the verifier decides the result. The agent's own `DONE` is never treated as success.

### Why it is built this way

- **Typed decisions only.** Every typed value comes from the task's `inputs`. If no input fits a field, the task stops as `BLOCKED`, so the model has no way to invent a value.
- **Small state.** Jev sees only the goal, URL, title, recent actions, the element table and a short block of page notices. Its accuracy drops with irrelevant state.
- **Page text is data.** Element names are cut to 80 characters and notices are labeled as untrusted, which limits prompt injection from page content.
- **Counting and dates stay in code.** These are known Jev weaknesses, so code handles them.

## Tasks

Tasks are defined in `tasks.yaml`.

| Task | What the agent does | How it is verified | Cleanup |
|---|---|---|---|
| `00_read_budget` | Opens an existing budget, read-only | Budget amount matches the page | None needed |
| `01_create_budget` | Creates a $1 monthly cost budget with no alerts | Amount, period, type and alert count all match | Budget deleted |
| `03_lambda_function_url` | Creates a Python function with a function URL | Auth type is `AWS_IAM`, role and runtime are correct | URL config and function deleted |
| `04_ec2_launch_t3micro` | Launches a t3.micro with no key pair | Instance exists with the right type | Instance terminated |
| `05_bedrock_playground_prompt` | Sends one prompt to Amazon Nova Micro | CloudWatch shows an invocation | None needed |
| `99_rds_create_db` | Creates a small RDS database | Instance exists | Instance deleted, no final snapshot |

Task 99 is disabled and has never been run.

## Results

These figures come from the 38 runs logged during development, on model `jev-1.14-20261014`. The goals and agent changed between runs, so treat them as a development record rather than a benchmark.

| Outcome | Runs |
|---|---|
| Passed verification | 19 |
| Stopped at the approval gate by `--dry-run` | 15 |
| Aborted by the human | 2 |
| Interrupted | 1 |
| Claimed done, but the verifier crashed | 1 |

The verifier crash was a bug in the verifier and has been fixed. No run that the verifier completed was rejected, so no false `DONE` was caught in these logs.

Verified runs by task:

| Task | Verified runs | Steps | Approvals per run | Human picks | Wall time | Jev cost per run |
|---|---|---|---|---|---|---|
| `00_read_budget` | 8 | 2-3 | 0 | 0 | 12-31 s | $0.0002-0.0003 |
| `01_create_budget` | 2 | 9 | 1 | 0 | 28-31 s | $0.0016 |
| `03_lambda_function_url` | 2 | 12 | 1 | 0 | 45-46 s | $0.0014 |
| `04_ec2_launch_t3micro` | 3 | 8-9 | 1 | 0 | 85-112 s | $0.0015-0.0018 |
| `05_bedrock_playground_prompt` | 4 | 8-9 | 1 | 3 | 60-90 s | $0.0014-0.0015 |

Wall time includes human approval, verification and teardown. All 3 human picks came from the first two verified Bedrock runs; the two latest needed none.

Time per step, across the 120 steps in verified runs:

| Stage | Median | 95th percentile |
|---|---|---|
| Observe | 65 ms | 187 ms |
| Jev | 192 ms | 482 ms |
| Act | 271 ms | 1,115 ms |

Jev cost $0.043 across all 38 runs. That covers only the Jev calls, not any AWS charges.

The latest verified runs of tasks 03, 04 and 05 were recorded in `--demo` mode, with overlays and blurring active.

## Safety

The agent is assumed to make mistakes, so each layer limits the damage from the one before it.

- **Scoped IAM.** The console user can do little beyond these tasks. Other EC2 instance types are denied, and IAM changes are denied.
- **URL rules.** The agent acts only on `*.console.aws.amazon.com` in `us-east-1`. IAM, Organizations, billing, Support plans and Marketplace pages stop the run.
- **Approval gate.** Clicks with words such as `create`, `launch`, `delete`, `run` or `save` need `y` in the terminal. Unnamed controls do too. There is no auto-approve option.
- **Readiness check.** Each gate shows whether the form looks complete. It only informs the human; approval never depends on it.
- **Limits.** Each task is capped at 40 steps or fewer and 5 minutes of agent time, and stops after 3 failed actions in a row.
- **Teardown.** Cleanup runs even after a failure or Ctrl+C, and only touches `jevagent-<run_id>-*` resources.
- **Credentials.** The agent never logs in and never handles passwords, MFA or keys. A human logs in, and the agent attaches to that browser.

## Setup

### Requirements

- Linux with Google Chrome (`launch_chrome.sh` calls `google-chrome`)
- Python 3.10 or later and [uv](https://docs.astral.sh/uv/)
- An [OpenRouter](https://openrouter.ai) API key with access to Jev
- An AWS account with budget alerts turned on

### 1. Install

```bash
uv sync
uv run playwright install chromium
cp .env.example .env
```

Add your OpenRouter key to `.env` as `TYPESAFE_API_KEY`.

### 2. Prepare AWS

Create these with an admin user, never root. Use `us-east-1` throughout.

**Shared Lambda role.** Create `jev-shared-lambda-exec` with a trust policy for `lambda.amazonaws.com` and only the AWS-managed `AWSLambdaBasicExecutionRole` policy.

**Agent console user.** Give this user a console password and no access keys. Scope it to the tasks above, and add these limits:

- Deny all IAM actions except `iam:GetRole`, `iam:ListRoles` and `iam:PassRole`.
- Allow `iam:PassRole` only for `jev-shared-lambda-exec`, with `iam:PassedToService` set to `lambda.amazonaws.com`.
- Deny `ec2:RunInstances` for every instance type except `t3.micro`.
- Deny RDS unless task 99 is enabled.

**Verifier profile.** Create a separate local AWS profile named `jev-verifier`. It needs these actions:

| Service | Actions |
|---|---|
| Budgets | `ViewBudget`, `ModifyBudget` |
| Lambda | `ListFunctions`, `GetFunctionConfiguration`, `GetFunctionUrlConfig`, `DeleteFunctionUrlConfig`, `DeleteFunction` |
| EC2 | `DescribeInstances`, `DescribeVpcs`, `TerminateInstances` |
| RDS | `DescribeDBInstances`, `DeleteDBInstance` |
| CloudWatch | `GetMetricStatistics` |

**Billing access.** Tasks 00 and 01 fail for IAM users unless IAM access to Billing is activated. Only the root user can turn this on.

**Existing budget.** Task 00 reads a budget whose name does not start with `jevagent-`, so the account needs one.

### 3. Run

Start the dedicated Chrome profile and log in as the agent user:

```bash
./launch_chrome.sh
```

In a second terminal, start with the read-only task:

```bash
uv run python agent.py 00_read_budget
```

Preview a task that creates something. This stops at the approval gate:

```bash
uv run python agent.py 01_create_budget --dry-run
```

Then run it for real, and answer `y` at the gate if the form looks right:

```bash
uv run python agent.py 01_create_budget
```

Each run writes `runs/<run_id>/steps.jsonl`, `summary.json` and screenshots. The `runs/` folder is gitignored because screenshots can show account details.

If a crash leaves anything behind, clean it up by hand:

```bash
uv run python verify.py teardown <run_id>
uv run python verify.py teardown --all-stale --older-than-hours 2
```

### Recording a demo

`--demo` adds an action overlay and blurs the account menu, ARNs, account IDs and email addresses. Check the first frames before sharing: the blur cannot reach the address bar or tab title.

```bash
uv run python agent.py 03_lambda_function_url --demo
uv run python scripts/make_demo.py --help
```

`scripts/make_demo.py` builds contact sheets, cuts and captions clips, and joins them. It writes only to `~/Videos/jev-demo/`.

## Tests

```bash
uv run pytest                                # no AWS or network needed
uv run pytest tests/test_safety.py           # one file
uv run pytest -m live                        # one real Jev call, needs an API key
```

The suite has 435 tests. They cover the approval gate and URL rules, page observation in headless Chromium, decisions with Jev mocked, teardown scoping with moto, and the video tool.

## Project layout

```
agent.py            observe, decide, gate, act, loop and logging
verify.py           verify and teardown for each task
tasks.yaml          goals, start URLs, inputs and limits
launch_chrome.sh    Chrome with remote debugging on a dedicated profile
scripts/            make_demo.py, for editing recordings
tests/              unit tests and small HTML fixtures
```

## Limitations

- **Console changes.** Task goals describe the current console, so a redesign can break them.
- **Gate coverage.** The approval words do not include `stop`, `remove`, `update` or `apply`. Scoped IAM is the backstop for those.
- **Panel detection.** Some open dropdowns are mistaken for panels, which can reorder the element table.
- **Task-wide exemption.** Task 03 skips the gate for any button named exactly "Save", not only those inside its side panels.
- **File size.** `agent.py` is about 1,200 lines, well past the 300 lines where the project planned to consider splitting it.

This is a learning project, not production software.
