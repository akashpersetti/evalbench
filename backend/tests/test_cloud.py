"""Unit tests for the AWS-backed cloud/ helpers, using moto to mock AWS."""

from pathlib import Path

import boto3
from moto import mock_aws

from evalbench.cloud import db_sync

BUCKET = "evalbench-test-db"
KEY = "evalbench.db"


@mock_aws
def test_download_db_writes_local_file_when_object_exists(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    client.put_object(Bucket=BUCKET, Key=KEY, Body=b"sqlite-bytes")

    local_path = tmp_path / "evalbench.db"
    db_sync.download_db(BUCKET, KEY, local_path)

    assert local_path.read_bytes() == b"sqlite-bytes"


@mock_aws
def test_download_db_leaves_local_path_absent_when_object_missing(tmp_path):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

    local_path = tmp_path / "evalbench.db"
    db_sync.download_db(BUCKET, KEY, local_path)

    assert not local_path.exists()


@mock_aws
def test_upload_db_writes_object_from_local_file(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    local_path = tmp_path / "evalbench.db"
    local_path.write_bytes(b"updated-bytes")

    db_sync.upload_db(BUCKET, KEY, local_path)

    body = client.get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
    assert body == b"updated-bytes"


from evalbench.cloud import run_status

RUN_STATUS_TABLE = "evalbench-test-run-status"


def _create_run_status_table():
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName=RUN_STATUS_TABLE,
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@mock_aws
def test_run_status_lifecycle_tracks_progress_and_completion():
    _create_run_status_table()

    run_status.create_status(RUN_STATUS_TABLE, "run-1", total=4)
    assert run_status.get_status(RUN_STATUS_TABLE, "run-1") == {
        "run_id": "run-1",
        "status": "pending",
        "completed": 0,
        "total": 4,
    }

    run_status.set_running(RUN_STATUS_TABLE, "run-1")
    assert run_status.get_status(RUN_STATUS_TABLE, "run-1")["status"] == "running"

    run_status.increment_completed(RUN_STATUS_TABLE, "run-1")
    run_status.increment_completed(RUN_STATUS_TABLE, "run-1")
    assert run_status.get_status(RUN_STATUS_TABLE, "run-1")["completed"] == 2

    run_status.set_done(RUN_STATUS_TABLE, "run-1")
    assert run_status.get_status(RUN_STATUS_TABLE, "run-1")["status"] == "done"


@mock_aws
def test_run_status_records_error_message():
    _create_run_status_table()
    run_status.create_status(RUN_STATUS_TABLE, "run-2", total=1)

    run_status.set_error(RUN_STATUS_TABLE, "run-2", "synthetic failure")

    item = run_status.get_status(RUN_STATUS_TABLE, "run-2")
    assert item["status"] == "error"
    assert item["error"] == "synthetic failure"


@mock_aws
def test_get_status_returns_none_for_missing_run():
    _create_run_status_table()
    assert run_status.get_status(RUN_STATUS_TABLE, "no-such-run") is None


from evalbench.cloud import ssm


@mock_aws
def test_get_parameter_reads_and_caches_secure_string():
    client = boto3.client("ssm", region_name="us-east-1")
    client.put_parameter(
        Name="/evalbench/test/admin-token",
        Value="secret-value",
        Type="SecureString",
    )

    assert ssm.get_parameter("/evalbench/test/admin-token") == "secret-value"

    # Overwrite the parameter; the cached read should still return the old value.
    client.put_parameter(
        Name="/evalbench/test/admin-token",
        Value="rotated-value",
        Type="SecureString",
        Overwrite=True,
    )
    assert ssm.get_parameter("/evalbench/test/admin-token") == "secret-value"

    ssm.get_parameter.cache_clear()
    assert ssm.get_parameter("/evalbench/test/admin-token") == "rotated-value"


import time

from evalbench.cloud import auth

MAGIC_TOKEN_TABLE = "evalbench-test-magic-tokens"
OWNER_EMAIL = "ahadagal@alumni.iu.edu"


def _create_magic_token_table():
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName=MAGIC_TOKEN_TABLE,
        KeySchema=[{"AttributeName": "token", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "token", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _verify_sender(sender_email: str):
    boto3.client("ses", region_name="us-east-1").verify_email_identity(
        EmailAddress=sender_email
    )


@mock_aws
def test_request_magic_link_stores_token_and_sends_email_for_owner():
    _create_magic_token_table()
    _verify_sender(OWNER_EMAIL)
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.Table(MAGIC_TOKEN_TABLE)

    auth.request_magic_link(
        email=OWNER_EMAIL,
        owner_email=OWNER_EMAIL,
        table_name=MAGIC_TOKEN_TABLE,
        base_url="https://example.cloudfront.net/run",
        sender_email=OWNER_EMAIL,
        ttl_seconds=900,
    )

    items = table.scan()["Items"]
    assert len(items) == 1
    assert len(items[0]["token"]) == 64
    assert items[0]["expires_at"] > int(time.time())


@mock_aws
def test_request_magic_link_no_ops_for_non_owner_email():
    _create_magic_token_table()
    _verify_sender(OWNER_EMAIL)
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(
        MAGIC_TOKEN_TABLE
    )

    auth.request_magic_link(
        email="someone-else@example.com",
        owner_email=OWNER_EMAIL,
        table_name=MAGIC_TOKEN_TABLE,
        base_url="https://example.cloudfront.net/run",
        sender_email=OWNER_EMAIL,
        ttl_seconds=900,
    )

    assert table.scan()["Items"] == []


@mock_aws
def test_verify_magic_link_accepts_and_consumes_valid_token():
    _create_magic_token_table()
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(
        MAGIC_TOKEN_TABLE
    )
    table.put_item(Item={"token": "good-token", "expires_at": int(time.time()) + 900})

    assert auth.verify_magic_link(token="good-token", table_name=MAGIC_TOKEN_TABLE)
    assert not auth.verify_magic_link(token="good-token", table_name=MAGIC_TOKEN_TABLE)


@mock_aws
def test_verify_magic_link_rejects_expired_token():
    _create_magic_token_table()
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(
        MAGIC_TOKEN_TABLE
    )
    table.put_item(Item={"token": "stale-token", "expires_at": int(time.time()) - 1})

    assert not auth.verify_magic_link(token="stale-token", table_name=MAGIC_TOKEN_TABLE)


@mock_aws
def test_verify_magic_link_rejects_unknown_token():
    _create_magic_token_table()
    assert not auth.verify_magic_link(token="never-issued", table_name=MAGIC_TOKEN_TABLE)


# Additional Cloud Module Tests

def test_invoke_runner_async_constructs_valid_payload(monkeypatch):
    """Verify runner lambda invocation constructs valid payload."""
    from evalbench.cloud import lambda_invoke
    from evalbench.models import RunConfig
    import json

    # Mock boto3 lambda client
    invoked_payloads = []

    class MockLambdaClient:
        def invoke(self, FunctionName, InvocationType, Payload):
            invoked_payloads.append({
                "function": FunctionName,
                "type": InvocationType,
                "payload": json.loads(Payload.decode())
            })

    def mock_boto3_client(service_name):
        if service_name == "lambda":
            return MockLambdaClient()
        raise ValueError(f"Unexpected service: {service_name}")

    import boto3
    monkeypatch.setattr(boto3, "client", mock_boto3_client)

    config = RunConfig(
        suite="rag",
        domain="overall",
        models=["openai/gpt-4o"],
        judge_model="anthropic/claude-sonnet-4-5"
    )

    lambda_invoke.invoke_runner_async("test-runner-function", "run-123", config)

    assert len(invoked_payloads) == 1
    assert invoked_payloads[0]["function"] == "test-runner-function"
    assert invoked_payloads[0]["type"] == "Event"
    assert invoked_payloads[0]["payload"]["run_id"] == "run-123"
    assert invoked_payloads[0]["payload"]["config"]["suite"] == "rag"


@mock_aws
def test_run_status_tracks_progression_from_pending_to_done():
    """Verify run status transitions through states correctly."""
    _create_run_status_table()

    run_status.create_status(RUN_STATUS_TABLE, "run-1", total=10)
    initial = run_status.get_status(RUN_STATUS_TABLE, "run-1")
    assert initial["status"] == "pending"
    assert initial["completed"] == 0

    run_status.set_running(RUN_STATUS_TABLE, "run-1")
    running = run_status.get_status(RUN_STATUS_TABLE, "run-1")
    assert running["status"] == "running"

    for _ in range(5):
        run_status.increment_completed(RUN_STATUS_TABLE, "run-1")

    partial = run_status.get_status(RUN_STATUS_TABLE, "run-1")
    assert partial["completed"] == 5
    assert partial["status"] == "running"

    run_status.set_done(RUN_STATUS_TABLE, "run-1")
    final = run_status.get_status(RUN_STATUS_TABLE, "run-1")
    assert final["status"] == "done"


@mock_aws
def test_magic_link_flow_end_to_end():
    """Verify complete magic link flow: request → store → verify."""
    _create_magic_token_table()
    _verify_sender(OWNER_EMAIL)

    # Step 1: Request magic link for owner
    auth.request_magic_link(
        email=OWNER_EMAIL,
        owner_email=OWNER_EMAIL,
        table_name=MAGIC_TOKEN_TABLE,
        base_url="https://example.com/run",
        sender_email=OWNER_EMAIL,
        ttl_seconds=900,
    )

    # Step 2: Verify token was stored
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.Table(MAGIC_TOKEN_TABLE)
    items = table.scan()["Items"]
    assert len(items) == 1
    token = items[0]["token"]

    # Step 3: Verify the token
    assert auth.verify_magic_link(token=token, table_name=MAGIC_TOKEN_TABLE)

    # Step 4: Verify token is consumed
    assert not auth.verify_magic_link(token=token, table_name=MAGIC_TOKEN_TABLE)


@mock_aws
def test_ssm_parameter_caching():
    """Verify SSM parameter caching behavior."""
    from evalbench.cloud import ssm

    client = boto3.client("ssm", region_name="us-east-1")
    client.put_parameter(
        Name="/evalbench/test/cached-value",
        Value="original",
        Type="SecureString",
    )

    # First read
    value1 = ssm.get_parameter("/evalbench/test/cached-value")
    assert value1 == "original"

    # Update the parameter
    client.put_parameter(
        Name="/evalbench/test/cached-value",
        Value="updated",
        Type="SecureString",
        Overwrite=True,
    )

    # Second read should return cached value
    value2 = ssm.get_parameter("/evalbench/test/cached-value")
    assert value2 == "original"

    # Clear cache and re-read
    ssm.get_parameter.cache_clear()
    value3 = ssm.get_parameter("/evalbench/test/cached-value")
    assert value3 == "updated"


@mock_aws
def test_database_sync_download_creates_file():
    """Verify database download creates local file from S3."""
    from evalbench.cloud import db_sync
    import tempfile

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="test-db-bucket")
    client.put_object(Bucket="test-db-bucket", Key="evalbench.db", Body=b"db-content")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "evalbench.db"
        db_sync.download_db("test-db-bucket", "evalbench.db", db_path)
        assert db_path.exists()
        assert db_path.read_bytes() == b"db-content"


@mock_aws
def test_database_sync_upload_pushes_file():
    """Verify database upload sends local file to S3."""
    from evalbench.cloud import db_sync
    import tempfile

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="test-db-bucket")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "evalbench.db"
        db_path.write_bytes(b"updated-db-content")
        db_sync.upload_db("test-db-bucket", "evalbench.db", db_path)

        # Verify upload
        response = client.get_object(Bucket="test-db-bucket", Key="evalbench.db")
        assert response["Body"].read() == b"updated-db-content"


import json

from evalbench.cloud import lambda_invoke
from evalbench.models import RunConfig


@mock_aws
def test_invoke_runner_async_invokes_with_event_type_and_json_payload():
    # Create IAM role first
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_role(
        RoleName="test-role",
        AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "lambda.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole"
                }
            ]
        })
    )

    client = boto3.client("lambda", region_name="us-east-1")
    # moto's Lambda mock requires a real function to exist before Invoke succeeds;
    # a minimal inline zip is enough since the handler never actually runs for
    # an async ("Event") invocation under moto.
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("handler.py", "def handler(event, context): return event")
    client.create_function(
        FunctionName="evalbench-dev-runner",
        Runtime="python3.12",
        Role="arn:aws:iam::123456789012:role/test-role",
        Handler="handler.handler",
        Code={"ZipFile": buffer.getvalue()},
    )

    config = RunConfig(suite="structured", domain="software", models=["openai/gpt-4o"])
    lambda_invoke.invoke_runner_async("evalbench-dev-runner", "run-123", config)

    invocations = client.list_functions()["Functions"]
    assert invocations[0]["FunctionName"] == "evalbench-dev-runner"
