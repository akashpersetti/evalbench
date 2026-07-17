"""Unit tests for the AWS-backed cloud/ helpers, using moto to mock AWS."""

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
