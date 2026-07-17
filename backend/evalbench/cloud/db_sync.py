"""S3 round-trip helpers for per-run SQLite shard files."""

import shutil
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

_MERGE_CONCURRENCY = 16


def download_db(bucket: str, key: str, local_path: Path) -> None:
    """Fetch one S3 object to local_path, or no-op if it doesn't exist yet."""
    try:
        boto3.client("s3").download_file(bucket, key, str(local_path))
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise


def upload_db(bucket: str, key: str, local_path: Path) -> None:
    """Push the local SQLite file to an S3 object."""
    boto3.client("s3").upload_file(str(local_path), bucket, key)


def run_db_key(prefix: str, run_id: str) -> str:
    """Return the S3 key for one run's shard file."""
    return f"{prefix}{run_id}.db"


def merge_all_runs(bucket: str, prefix: str, local_path: Path) -> None:
    """Download every run shard under prefix and merge into one SQLite file at local_path.

    No-ops (leaves local_path absent) when no shards exist yet, matching
    download_db's contract for a missing object.
    """
    client = boto3.client("s3")
    paginator = client.get_paginator("list_objects_v2")
    keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]
    if not keys:
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        shard_paths = [Path(tmpdir) / f"shard_{i}.db" for i in range(len(keys))]

        def _fetch(pair: tuple[str, Path]) -> None:
            key, path = pair
            client.download_file(bucket, key, str(path))

        with ThreadPoolExecutor(max_workers=_MERGE_CONCURRENCY) as pool:
            list(pool.map(_fetch, zip(keys, shard_paths)))

        shutil.copy(shard_paths[0], local_path)
        connection = sqlite3.connect(local_path)
        try:
            for shard_path in shard_paths[1:]:
                connection.execute("ATTACH DATABASE ? AS shard", (str(shard_path),))
                connection.execute(
                    "INSERT INTO metric_records SELECT * FROM shard.metric_records"
                )
                connection.commit()
                connection.execute("DETACH DATABASE shard")
            connection.commit()
        finally:
            connection.close()
