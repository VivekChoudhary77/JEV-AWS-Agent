"""Teardown against moto: it deletes the target run's resources, and only those."""

import io
import zipfile
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

import verify

RUN = "2610301514a3f9"
OTHER_RUN = "2610301514beef"
BASIC_EXECUTION = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
TRUST = ('{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"sts:AssumeRole",'
         '"Principal":{"Service":"lambda.amazonaws.com"}}]}')


class RecordingSession:
    """Passes through to moto and remembers which services teardown opened."""

    def __init__(self):
        self.real = boto3.Session(region_name=verify.REGION)
        self.services = set()

    def client(self, service, **kwargs):
        self.services.add(service)
        return self.real.client(service, **kwargs)


@pytest.fixture
def aws(monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws(config={"iam": {"load_aws_managed_policies": True}}):
        recording = RecordingSession()
        monkeypatch.setattr(verify, "session", lambda: recording)
        yield recording.real, recording


@pytest.fixture
def role_arn(aws):
    iam = aws[0].client("iam")
    arn = iam.create_role(RoleName=verify.SHARED_ROLE, AssumeRolePolicyDocument=TRUST)["Role"]["Arn"]
    iam.attach_role_policy(RoleName=verify.SHARED_ROLE, PolicyArn=BASIC_EXECUTION)
    return arn


def owned(run_id, suffix):
    return f"jevagent-{run_id}-{suffix}"


def old_run_id(hours=5):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%y%m%d%H%M") + "0001"


def make_budget(real, name):
    account = real.client("sts").get_caller_identity()["Account"]
    real.client("budgets").create_budget(AccountId=account, Budget={
        "BudgetName": name, "BudgetType": "COST", "TimeUnit": "MONTHLY",
        "BudgetLimit": {"Amount": "1", "Unit": "USD"}})


def budget_names(real):
    return {name for _, name in verify.find_budgets()}


def make_function(real, name, role):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("lambda_function.py", "def lambda_handler(event, context):\n    return 1\n")
    client = real.client("lambda")
    client.create_function(FunctionName=name, Runtime="python3.13", Role=role, Handler="lambda_function.lambda_handler",
                           Code={"ZipFile": archive.getvalue()})
    client.create_function_url_config(FunctionName=name, AuthType="AWS_IAM")


def function_names():
    return {name for _, name in verify.find_functions()}


def make_instance(real, name):
    client = real.client("ec2")
    image = client.describe_images()["Images"][0]["ImageId"]
    return client.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="t3.micro", TagSpecifications=[
        {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}])["Instances"][0]["InstanceId"]


def state_of(real, instance_id):
    reservations = real.client("ec2").describe_instances(InstanceIds=[instance_id])["Reservations"]
    return reservations[0]["Instances"][0]["State"]["Name"]


def make_database(real, identifier):
    real.client("rds").create_db_instance(
        DBInstanceIdentifier=identifier, DBInstanceClass="db.t3.micro", Engine="postgres",
        AllocatedStorage=20, MasterUsername="testuser", MasterUserPassword="fixture-only-value")


def database_names():
    return {name for _, name in verify.find_databases()}


def assert_role_intact(real):
    iam = real.client("iam")
    assert iam.get_role(RoleName=verify.SHARED_ROLE)["Role"]["RoleName"] == verify.SHARED_ROLE
    attached = iam.list_attached_role_policies(RoleName=verify.SHARED_ROLE)["AttachedPolicies"]
    assert [policy["PolicyArn"] for policy in attached] == [BASIC_EXECUTION]


def test_budget_teardown_deletes_only_this_run(aws):
    real, _ = aws
    make_budget(real, owned(RUN, "budget"))
    make_budget(real, owned(OTHER_RUN, "budget"))
    make_budget(real, "my-5-dollar-budget")
    assert verify.teardown_budget(RUN) == [f"deleted budget {owned(RUN, 'budget')}"]
    assert budget_names(real) == {owned(OTHER_RUN, "budget"), "my-5-dollar-budget"}


def test_lambda_teardown_deletes_url_and_function_but_never_the_role(aws, role_arn):
    real, recording = aws
    make_function(real, owned(RUN, "fn"), role_arn)
    make_function(real, owned(OTHER_RUN, "fn"), role_arn)
    assert verify.teardown_lambda(RUN) == [f"deleted function {owned(RUN, 'fn')}"]
    assert function_names() == {owned(OTHER_RUN, "fn")}
    real.client("lambda").get_function_url_config(FunctionName=owned(OTHER_RUN, "fn"))
    assert_role_intact(real)
    assert "iam" not in recording.services


def test_lambda_teardown_tolerates_a_function_without_a_url(aws, role_arn):
    real, _ = aws
    make_function(real, owned(RUN, "fn"), role_arn)
    real.client("lambda").delete_function_url_config(FunctionName=owned(RUN, "fn"))
    assert verify.teardown_lambda(RUN) == [f"deleted function {owned(RUN, 'fn')}"]


def test_ec2_teardown_terminates_only_this_run(aws):
    real, _ = aws
    mine = make_instance(real, owned(RUN, "ec2"))
    theirs = make_instance(real, owned(OTHER_RUN, "ec2"))
    untagged = make_instance(real, "web-server")
    assert verify.teardown_ec2(RUN) == [f"deleted instance {owned(RUN, 'ec2')}"]
    assert state_of(real, mine) == "terminated"
    assert state_of(real, theirs) == "running"
    assert state_of(real, untagged) == "running"


def test_rds_teardown_deletes_only_this_run(aws):
    real, _ = aws
    make_database(real, owned(RUN, "db"))
    make_database(real, owned(OTHER_RUN, "db"))
    make_database(real, "production-db")
    assert verify.teardown_rds(RUN) == [f"deleted database {owned(RUN, 'db')}"]
    assert database_names() == {owned(OTHER_RUN, "db"), "production-db"}


def test_full_run_teardown_spans_services_and_leaves_other_runs(aws, role_arn):
    real, recording = aws
    make_budget(real, owned(RUN, "budget"))
    make_function(real, owned(RUN, "fn"), role_arn)
    make_function(real, owned(OTHER_RUN, "fn"), role_arn)
    instance = make_instance(real, owned(RUN, "ec2"))
    make_database(real, owned(RUN, "db"))
    report = verify.teardown_run(RUN)
    assert len(report) == 4 and all(line.startswith("deleted") for line in report)
    assert state_of(real, instance) == "terminated"
    assert function_names() == {owned(OTHER_RUN, "fn")}
    assert_role_intact(real)
    assert "iam" not in recording.services


def test_stale_sweep_respects_age_and_keeps_recent_runs(aws, role_arn):
    real, recording = aws
    stale, fresh = old_run_id(), verify.new_run_id()
    make_function(real, owned(stale, "fn"), role_arn)
    make_function(real, owned(fresh, "fn"), role_arn)
    make_budget(real, owned(stale, "budget"))
    make_budget(real, owned(fresh, "budget"))
    report = verify.teardown_stale(2)
    assert sorted(report) == [f"deleted budget {owned(stale, 'budget')}", f"deleted function {owned(stale, 'fn')}"]
    assert function_names() == {owned(fresh, "fn")}
    assert budget_names(real) == {owned(fresh, "budget")}
    assert_role_intact(real)
    assert "iam" not in recording.services


@pytest.mark.parametrize("teardown", [lambda: verify.teardown_run(RUN), lambda: verify.teardown_stale(0)])
def test_shared_role_name_is_ignored_even_as_a_resource_name(aws, role_arn, teardown):
    real, _ = aws
    make_function(real, verify.SHARED_ROLE, role_arn)
    make_budget(real, verify.SHARED_ROLE)
    assert teardown() == []
    assert function_names() == {verify.SHARED_ROLE}
    assert budget_names(real) == {verify.SHARED_ROLE}
    assert_role_intact(real)


def test_prefixed_names_without_a_run_id_are_reported_and_kept(aws, role_arn):
    real, _ = aws
    make_function(real, "jevagent-lambda-exec", role_arn)
    make_function(real, f"jevagent-{RUN}x-fn", role_arn)
    report = verify.teardown_stale(0)
    assert len(report) == 2 and all(line.startswith("skipped") for line in report)
    assert len(function_names()) == 2


def test_a_failed_delete_is_reported_not_raised(aws, monkeypatch):
    real, _ = aws
    make_budget(real, owned(RUN, "budget"))

    def denied(name):
        raise verify.ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteBudget")

    kind = ("budget", verify.find_budgets, denied)
    assert verify.sweep(kind, verify.for_run(RUN)) == [f"FAILED budget {owned(RUN, 'budget')}: AccessDenied"]


def test_verifier_refuses_root(monkeypatch):
    class Root:
        def __init__(self, **kwargs):
            pass

        def client(self, service):
            return self

        def get_caller_identity(self):
            return {"Arn": "arn:aws:iam::000000000000:root"}

    verify.session.cache_clear()
    monkeypatch.setenv("AWS_VERIFIER_PROFILE", "anything")
    monkeypatch.setattr(verify.boto3, "Session", Root)
    with pytest.raises(RuntimeError, match="root"):
        verify.session()
    verify.session.cache_clear()


def test_run_ids_fit_the_owned_name_pattern_and_age_correctly():
    run_id = verify.new_run_id()
    assert verify.OWNED_NAME.fullmatch(owned(run_id, "fn"))[1] == run_id
    assert not verify.is_stale(run_id, 1)
    assert verify.is_stale(old_run_id(), 2)


def test_every_task_has_a_registered_check():
    assert set(verify.CHECKS) == {"read_budget", "create_budget", "lambda_function_url",
                                  "ec2_instance", "bedrock_prompt", "rds_instance"}
    assert verify.CHECKS["read_budget"].teardown is None
    assert verify.CHECKS["bedrock_prompt"].teardown is None


def context(final_text="", **inputs):
    return verify.Context(RUN, inputs, datetime.now(timezone.utc), "", final_text)


def test_lambda_verifier_passes_with_the_shared_role_and_iam_auth(aws, role_arn):
    real, _ = aws
    make_function(real, owned(RUN, "fn"), role_arn)
    assert verify.verify_lambda(RUN, context(function_name=owned(RUN, "fn"))).passed


def test_lambda_verifier_fails_on_any_other_role(aws):
    real, _ = aws
    other = real.client("iam").create_role(RoleName="created-by-console", AssumeRolePolicyDocument=TRUST)["Role"]["Arn"]
    make_function(real, owned(RUN, "fn"), other)
    assert not verify.verify_lambda(RUN, context(function_name=owned(RUN, "fn"))).passed


def test_lambda_verifier_fails_when_auth_is_none(aws, role_arn):
    real, _ = aws
    make_function(real, owned(RUN, "fn"), role_arn)
    real.client("lambda").update_function_url_config(FunctionName=owned(RUN, "fn"), AuthType="NONE")
    assert not verify.verify_lambda(RUN, context(function_name=owned(RUN, "fn"))).passed


@pytest.mark.parametrize("text, passed", [
    ("my-budget Budgeted $5.00 this month", True),
    ("my-budget Budgeted $5 this month", True),
    ("my-budget Budgeted $50.00 this month", False),
    ("my-budget Budgeted $5.50 this month", False),
    ("some other page $5.00", False),
])
def test_read_budget_compares_the_amount_on_the_final_page(aws, text, passed):
    real, _ = aws
    account = real.client("sts").get_caller_identity()["Account"]
    real.client("budgets").create_budget(AccountId=account, Budget={
        "BudgetName": "my-budget", "BudgetType": "COST", "TimeUnit": "MONTHLY",
        "BudgetLimit": {"Amount": "5", "Unit": "USD"}})
    assert verify.verify_read_budget(RUN, context(text)).passed is passed


def test_create_budget_verifier_checks_amount_and_period(aws):
    real, _ = aws
    make_budget(real, owned(RUN, "budget"))
    ctx = context(budget_name=owned(RUN, "budget"), budget_amount="1")
    assert verify.verify_create_budget(RUN, ctx).passed
    ctx = context(budget_name=owned(RUN, "budget"), budget_amount="2")
    assert not verify.verify_create_budget(RUN, ctx).passed


def test_ec2_verifier_checks_the_instance_type(aws):
    real, _ = aws
    make_instance(real, owned(RUN, "ec2"))
    assert verify.verify_ec2(RUN, context(instance_name=owned(RUN, "ec2"))).passed
    assert not verify.verify_ec2(RUN, context(instance_name="missing")).passed
