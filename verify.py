"""One verify() + teardown() pair per task. boto3 is the source of truth, not the agent's "done"."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

REGION = "us-east-1"
SHARED_ROLE = "jev-shared-lambda-exec"
RUN_ID = r"\d{10}[0-9a-f]{4}"
OWNED_NAME = re.compile(rf"jevagent-({RUN_ID})-[a-z0-9-]+")
NOVA_MICRO_IDS = ["amazon.nova-micro-v1:0", "us.amazon.nova-micro-v1:0"]
METRIC_POLL_S = 300
METRIC_INTERVAL_S = 20


@dataclass
class VerifyResult:
    passed: bool
    detail: str


@dataclass
class Context:
    run_id: str
    inputs: dict[str, str]
    started_at: datetime
    final_url: str
    final_text: str


@lru_cache(maxsize=1)
def session() -> boto3.Session:
    profile = os.environ.get("AWS_VERIFIER_PROFILE")
    if not profile:
        raise RuntimeError("AWS_VERIFIER_PROFILE is not set")
    aws = boto3.Session(profile_name=profile, region_name=REGION)
    if aws.client("sts").get_caller_identity()["Arn"].endswith(":root"):
        raise RuntimeError("refusing to run as the root user")
    return aws


def account_id() -> str:
    return session().client("sts").get_caller_identity()["Account"]


def new_run_id() -> str:
    """Starts with the UTC time, so a resource's age can be read from its name."""
    return datetime.now(timezone.utc).strftime("%y%m%d%H%M") + secrets.token_hex(2)


def is_stale(run_id: str, hours: float) -> bool:
    created = datetime.strptime(run_id[:10], "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(hours=hours)


# Finders return (id, name) pairs. Deleters take the id. Neither ever calls IAM.


def find_budgets() -> Iterable[tuple[str, str]]:
    client = session().client("budgets")
    try:
        for page in client.get_paginator("describe_budgets").paginate(AccountId=account_id()):
            yield from ((b["BudgetName"], b["BudgetName"]) for b in page.get("Budgets", []))
    except client.exceptions.NotFoundException:
        return


def delete_budget(name: str) -> None:
    session().client("budgets").delete_budget(AccountId=account_id(), BudgetName=name)


def find_functions() -> Iterable[tuple[str, str]]:
    for page in session().client("lambda").get_paginator("list_functions").paginate():
        yield from ((f["FunctionName"], f["FunctionName"]) for f in page["Functions"])


def delete_function(name: str) -> None:
    client = session().client("lambda")
    try:
        client.delete_function_url_config(FunctionName=name)
    except client.exceptions.ResourceNotFoundException:
        pass
    client.delete_function(FunctionName=name)


def find_instances() -> Iterable[tuple[str, str]]:
    filters = [{"Name": "tag:Name", "Values": ["jevagent-*"]},
               {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    for page in session().client("ec2").get_paginator("describe_instances").paginate(Filters=filters):
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                yield instance["InstanceId"], _name_tag(instance)


def _name_tag(instance: dict) -> str:
    return next((tag["Value"] for tag in instance.get("Tags", []) if tag["Key"] == "Name"), "")


def terminate_instance(instance_id: str) -> None:
    client = session().client("ec2")
    client.terminate_instances(InstanceIds=[instance_id])
    client.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])


def find_databases() -> Iterable[tuple[str, str]]:
    for page in session().client("rds").get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            if db["DBInstanceStatus"] != "deleting":
                yield db["DBInstanceIdentifier"], db["DBInstanceIdentifier"]


def delete_database(identifier: str) -> None:
    session().client("rds").delete_db_instance(
        DBInstanceIdentifier=identifier, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)


Kind = tuple[str, Callable[[], Iterable[tuple[str, str]]], Callable[[str], None]]
BUDGETS: Kind = ("budget", find_budgets, delete_budget)
FUNCTIONS: Kind = ("function", find_functions, delete_function)
INSTANCES: Kind = ("instance", find_instances, terminate_instance)
DATABASES: Kind = ("database", find_databases, delete_database)
ALL_KINDS = (BUDGETS, FUNCTIONS, INSTANCES, DATABASES)


def sweep(kind: Kind, selected: Callable[[str], bool]) -> list[str]:
    """Delete owned resources whose run ID is selected. Anything that doesn't parse is left alone."""
    label, find, delete = kind
    report = []
    for resource, name in list(find()):
        owned = OWNED_NAME.fullmatch(name)
        if not owned:
            if name.startswith("jevagent-"):
                report.append(f"skipped {label} {name}: no run ID in the name")
            continue
        if selected(owned[1]):
            try:
                delete(resource)
                report.append(f"deleted {label} {name}")
            except ClientError as error:
                report.append(f"FAILED {label} {name}: {error.response['Error']['Code']}")
    return report


def for_run(run_id: str) -> Callable[[str], bool]:
    return lambda candidate: candidate == run_id


def teardown_budget(run_id: str) -> list[str]:
    return sweep(BUDGETS, for_run(run_id))


def teardown_lambda(run_id: str) -> list[str]:
    return sweep(FUNCTIONS, for_run(run_id))


def teardown_ec2(run_id: str) -> list[str]:
    return sweep(INSTANCES, for_run(run_id))


def teardown_rds(run_id: str) -> list[str]:
    return sweep(DATABASES, for_run(run_id))


def teardown_run(run_id: str) -> list[str]:
    return [line for kind in ALL_KINDS for line in sweep(kind, for_run(run_id))]


def teardown_stale(hours: float) -> list[str]:
    return [line for kind in ALL_KINDS for line in sweep(kind, lambda run_id: is_stale(run_id, hours))]


# Verifiers


def verify_read_budget(run_id: str, ctx: Context) -> VerifyResult:
    client = session().client("budgets")
    budgets = client.describe_budgets(AccountId=account_id()).get("Budgets", [])
    shown = [b for b in budgets if not b["BudgetName"].startswith("jevagent-") and b["BudgetName"] in ctx.final_text]
    if not shown:
        return VerifyResult(False, "the final page shows no existing budget's name")
    amount = float(shown[0]["BudgetLimit"]["Amount"])
    digits = re.escape(f"{amount:,.0f}") + r"(\.0+)?" if amount.is_integer() else re.escape(f"{amount:,.2f}")
    if re.search(rf"\$\s*{digits}(?![\d,]|\.\d)", ctx.final_text):
        return VerifyResult(True, f"page shows the budget amount {amount:,.2f}")
    return VerifyResult(False, f"page doesn't show the budget amount {amount:,.2f}")


def verify_create_budget(run_id: str, ctx: Context) -> VerifyResult:
    name = ctx.inputs["budget_name"]
    client = session().client("budgets")
    try:
        budget = client.describe_budget(AccountId=account_id(), BudgetName=name)["Budget"]
    except client.exceptions.NotFoundException:
        return VerifyResult(False, f"budget {name} doesn't exist")
    notifications = client.describe_notifications_for_budget(AccountId=account_id(), BudgetName=name)
    facts = {"amount": float(budget["BudgetLimit"]["Amount"]), "unit": budget["TimeUnit"],
             "type": budget["BudgetType"], "alerts": len(notifications.get("Notifications", []))}
    expected = {"amount": float(ctx.inputs["budget_amount"]), "unit": "MONTHLY", "type": "COST", "alerts": 0}
    return VerifyResult(facts == expected, f"found {facts}")


def verify_lambda(run_id: str, ctx: Context) -> VerifyResult:
    name = ctx.inputs["function_name"]
    client = session().client("lambda")
    try:
        auth_type = client.get_function_url_config(FunctionName=name)["AuthType"]
        config = client.get_function_configuration(FunctionName=name)
    except client.exceptions.ResourceNotFoundException:
        return VerifyResult(False, f"function {name} or its URL doesn't exist")
    role = config["Role"].rsplit("/", 1)[-1]
    python = config.get("Runtime", "").startswith("python")
    passed = auth_type == "AWS_IAM" and role == SHARED_ROLE and python
    return VerifyResult(passed, f"auth={auth_type} role={role} runtime={config.get('Runtime')}")


def verify_ec2(run_id: str, ctx: Context) -> VerifyResult:
    client = session().client("ec2")
    filters = [{"Name": "tag:Name", "Values": [ctx.inputs["instance_name"]]},
               {"Name": "instance-state-name", "Values": ["pending", "running"]}]
    instances = [i for r in client.describe_instances(Filters=filters)["Reservations"] for i in r["Instances"]]
    if len(instances) != 1:
        return VerifyResult(False, f"expected 1 instance, found {len(instances)}")
    instance = instances[0]
    vpcs = client.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    default_vpc = instance.get("VpcId") in [vpc["VpcId"] for vpc in vpcs]
    passed = instance["InstanceType"] == "t3.micro" and default_vpc and "KeyName" not in instance
    return VerifyResult(passed, f"type={instance['InstanceType']} default_vpc={default_vpc} "
                                f"key_pair={'KeyName' in instance}")


def verify_bedrock(run_id: str, ctx: Context) -> VerifyResult:
    """Checks both IDs because the playground may invoke through the inference profile."""
    client = session().client("cloudwatch")
    start = ctx.started_at.replace(second=0, microsecond=0)
    deadline = time.monotonic() + METRIC_POLL_S
    while True:
        for model_id in NOVA_MICRO_IDS:
            points = client.get_metric_statistics(
                Namespace="AWS/Bedrock", MetricName="Invocations", Statistics=["Sum"], Period=60,
                Dimensions=[{"Name": "ModelId", "Value": model_id}],
                StartTime=start, EndTime=datetime.now(timezone.utc))["Datapoints"]
            total = sum(point["Sum"] for point in points)
            if total > 0:
                return VerifyResult(True, f"{total:.0f} invocation(s) of {model_id}")
        if time.monotonic() >= deadline:
            return VerifyResult(False, f"no Invocations metric after {METRIC_POLL_S}s")
        time.sleep(METRIC_INTERVAL_S)


def verify_rds(run_id: str, ctx: Context) -> VerifyResult:
    client = session().client("rds")
    identifier = ctx.inputs["db_instance_name"]
    try:
        db = client.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
    except client.exceptions.DBInstanceNotFoundFault:
        return VerifyResult(False, f"database {identifier} doesn't exist")
    passed = not db["MultiAZ"] and db["BackupRetentionPeriod"] == 0
    return VerifyResult(passed, f"class={db['DBInstanceClass']} multi_az={db['MultiAZ']} "
                                f"backups={db['BackupRetentionPeriod']}")


@dataclass(frozen=True)
class Check:
    verify: Callable[[str, Context], VerifyResult]
    teardown: Callable[[str], list[str]] | None = None


CHECKS = {
    "read_budget": Check(verify_read_budget),
    "create_budget": Check(verify_create_budget, teardown_budget),
    "lambda_function_url": Check(verify_lambda, teardown_lambda),
    "ec2_instance": Check(verify_ec2, teardown_ec2),
    "bedrock_prompt": Check(verify_bedrock),
    "rds_instance": Check(verify_rds, teardown_rds),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up jevagent resources.")
    commands = parser.add_subparsers(dest="command", required=True)
    teardown = commands.add_parser("teardown")
    teardown.add_argument("run_id", nargs="?")
    teardown.add_argument("--all-stale", action="store_true")
    teardown.add_argument("--older-than-hours", type=float, default=2.0)
    args = parser.parse_args()
    if bool(args.run_id) == args.all_stale:
        parser.error("pass a run ID or --all-stale, not both")
    if args.run_id and not re.fullmatch(RUN_ID, args.run_id):
        parser.error(f"{args.run_id} isn't a run ID")
    load_dotenv(Path(__file__).parent / ".env")
    report = teardown_stale(args.older_than_hours) if args.all_stale else teardown_run(args.run_id)
    print("\n".join(report) or "Nothing to delete.")


if __name__ == "__main__":
    main()
