"""S3 round-trip helpers for the shared SQLite metric-records file."""

from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def download_db(bucket: str, key: str, local_path: Path) -> None:
    """Fetch the shared SQLite file to local_path, or no-op if none exists yet."""
    try:
        boto3.client("s3").download_file(bucket, key, str(local_path))
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise


def upload_db(bucket: str, key: str, local_path: Path) -> None:
    """Push the local SQLite file back to S3."""
    boto3.client("s3").upload_file(str(local_path), bucket, key)
