# jev-console-agent

A browser agent that completes predefined tasks in the real AWS Console. [Jev](https://docs.typesafe.ai), a fast decision model, chooses each action, Playwright carries it out, and boto3 checks whether the task actually succeeded.

## How it works

| Role | Component | Responsibility |
|---|---|---|
| Decide | Jev, through OpenRouter | Pick the operation, target element and input value |
| Act | Playwright over CDP | Run the action in a Chrome window a human logged into |
| Verify | boto3 | Check AWS state independently, then clean up |

Each step observes the page, asks Jev for a typed decision, and acts on it. Clicks that commit something need human approval in the terminal, and low-confidence decisions go to the human too. Jev never writes text: every typed value comes from the task's inputs.

## Tasks

| Task | What the agent does | Verified by |
|---|---|---|
| `00_read_budget` | Opens an existing budget, read-only | Budget amount matches the page |
| `01_create_budget` | Creates a $1 monthly budget with no alerts | Amount, period, type and alert count |
| `03_lambda_function_url` | Creates a Python function with a function URL | Auth type, role and runtime |
| `04_ec2_launch_t3micro` | Launches a t3.micro with no key pair | Instance name and type |
| `05_bedrock_playground_prompt` | Sends one prompt to Amazon Nova Micro | CloudWatch invocation metric |
| `99_rds_create_db` | Creates a small RDS database (disabled, never run) | Instance exists |

Anything the agent creates is deleted after the run.

## Results

Verified runs from development:

| Task | Runs | Steps | Wall time | Jev cost per run |
|---|---|---|---|---|
| `00_read_budget` | 8 | 2-3 | 12-31 s | $0.0002-0.0003 |
| `01_create_budget` | 2 | 9 | 28-31 s | $0.0016 |
| `03_lambda_function_url` | 2 | 12 | 45-46 s | $0.0014 |
| `04_ec2_launch_t3micro` | 3 | 8-9 | 85-112 s | $0.0015-0.0018 |
| `05_bedrock_playground_prompt` | 4 | 8-9 | 60-90 s | $0.0014-0.0015 |

Wall time includes human approval, verification and teardown. Jev took a median of 192 ms per step (95th percentile 482 ms).

## Safety

- The console user has scoped IAM permissions and only t3.micro instances are allowed.
- The agent stays on `*.console.aws.amazon.com` in `us-east-1` and stops on IAM, billing and similar pages.
- Commit clicks such as create, launch, delete or save need `y` in the terminal. There is no auto-approve.
- Each task has step, time and failure limits.
- Teardown runs even after a failure or Ctrl+C, and only touches `jevagent-<run_id>-*` resources.
- The agent never logs in or handles credentials.

## Setup

You need a browser, [uv](https://docs.astral.sh/uv/) package installer, an [OpenRouter](https://openrouter.ai) API key and an AWS account.

```bash
uv sync
uv run playwright install chromium
cp .env.example .env    # then add your OpenRouter key as TYPESAFE_API_KEY
```

In AWS, using `us-east-1` and an admin user:

1. Create the role `jev-shared-lambda-exec`, trusted by `lambda.amazonaws.com`, with `AWSLambdaBasicExecutionRole`.
2. Create an agent console user with a password and no access keys, scoped to the tasks above. It may pass only that role, and only to Lambda.
3. Create a local AWS profile named `jev-verifier` with read and delete access for the same services.
4. Activate IAM access to Billing, which needs the root user. Tasks 00 and 01 fail without it.
5. Create one budget for task 00 to read, with a name that doesn't start with `jevagent-`.

## Usage

Start Chrome and log in as the agent user:

```bash
./launch_chrome.sh
```

Then, in a second terminal:

```bash
uv run python agent.py 00_read_budget             # run a task
uv run python agent.py 01_create_budget --dry-run # stop at the approval gate
uv run python verify.py teardown <run_id>         # clean up by hand after a crash
uv run pytest                                     # tests, no AWS needed
```

Each run writes its logs and screenshots to `runs/<run_id>/`.
