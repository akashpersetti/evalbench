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
