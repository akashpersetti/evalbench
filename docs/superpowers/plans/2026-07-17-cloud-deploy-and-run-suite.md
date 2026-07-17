# Cloud Deployment + Interactive Run Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy EvalBench's API + dashboard to AWS at near-zero cost, add a magic-link-gated `/run` page for triggering suites interactively from the browser, and migrate the existing local `evalbench.db` into the cloud store.

**Architecture:** Two Lambdas (`api` for fast HTTP responses, `runner` for long suite runs up to 15 min) behind one API Gateway HTTP API. Magic-link auth (DynamoDB + SES) gates run-triggering routes. SQLite round-trips through S3 for persistence — no VPC, no NAT Gateway, no always-on server. Frontend is a Next.js static export on S3 + CloudFront. Full details: `docs/superpowers/specs/2026-07-17-cloud-deploy-and-run-suite-design.md`.

**Tech Stack:** FastAPI/Mangum, boto3, DynamoDB, S3, SES, SSM Parameter Store, Lambda, API Gateway HTTP API, Terraform, GitHub Actions (OIDC), Next.js (existing), moto (test mocking).

## Global Constraints

- Local dev (`make api`, `make web`, `make run-suite`) must keep working unchanged — every cloud code path is additive and no-ops locally.
- Owner email for magic link: `ahadagal@alumni.iu.edu` (matches `akashpersetti/twin`'s pattern).
- No VPC / no NAT Gateway — this is what keeps cost near-zero; every AWS service used here (S3, DynamoDB, SES, SSM, Lambda, API Gateway) is reachable without one.
- No custom domain for this phase — plain CloudFront + API Gateway URLs.
- New route auth: `POST /runs` and `POST /runs/async` require `Authorization: Bearer <admin_token>` only when `REQUIRE_AUTH=true` (set on the Lambda in Terraform, unset locally).
- Existing `evalbench.db` (189 records, 26 runs) is the seed file for the cloud S3 db bucket — same `MetricRecordRow` schema, no transformation.
- Frontend styling for anything new must reuse the existing palette exactly: background `#f7f5ef`, text `#202822`, muted `#777970`/`#62675f`, borders `#dedbd2`/`#cbc8be`/`#e4e1d9`, accent/buttons `#283b32`, uppercase-tracked eyebrow labels, `rounded-md` inputs with `focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]`. Plain Tailwind utilities, no component library.

## Design corrections made during planning (not in the original spec — read before implementing)

1. **`execute_run` generates its own `run_id` internally** (`runner.py:579`, `run_id = str(uuid.uuid4())`), but the async route must pre-generate a `run_id` to create the DynamoDB status row *before* invoking the runner Lambda. Task 5 adds an optional `run_id` override parameter so the caller's ID is the one actually used and returned — without this fix, the run the frontend polls for and the run that gets persisted would carry two different IDs.
2. **The `api` Lambda also serves `GET /results` and `GET /runs/{run_id}`**, which read from the SQLite file — but only the `runner` Lambda was originally going to touch S3. A warm `api` Lambda container would keep serving a stale pre-run snapshot until it happened to cold-start again, breaking the "see your finished run inline" UX. Task 6 makes `get_session_factory` re-download the db from S3 on every request when running in cloud mode (cheap: the file is well under 1MB), so reads are always fresh.
3. **`POST /runs` (synchronous) would silently lose data in cloud mode**: it writes to the per-request local SQLite copy but nothing ever uploads that copy back to S3, so the run vanishes once the container recycles. Task 6 makes `POST /runs` return `400` in cloud mode (detected via `settings.db_bucket` being set) instead of silently losing writes — `/runs/async` is the only way to run a suite once deployed.
4. **Provider API keys don't need a runtime SSM read.** Twin only does a runtime SSM fetch for its `admin_token` (compared per-request); its model config is a plain Lambda env var. Same split here: `admin_token` is fetched at runtime via cached SSM read (Task 3, used by `verify_token`) because it's checked on every protected request. Provider API keys (`openai_api_key`, `anthropic_api_key`, etc.) and `judge_model` are read from SSM *by Terraform* at `apply` time and injected as plain `runner` Lambda environment variables (Task 11) — this needs zero code changes to `Settings` (it already reads these as env vars for local dev) and avoids granting the `runner` Lambda any SSM IAM permissions at all.

---

### Task 1: S3 SQLite round-trip helpers

**Files:**
- Create: `backend/evalbench/cloud/__init__.py`
- Create: `backend/evalbench/cloud/db_sync.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_cloud.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `db_sync.download_db(bucket: str, key: str, local_path: Path) -> None` — pulls the object to `local_path`; if the object doesn't exist yet, leaves `local_path` untouched (fresh schema gets created by `init_db` downstream).
- Produces: `db_sync.upload_db(bucket: str, key: str, local_path: Path) -> None` — pushes `local_path` to the object.

- [ ] **Step 1: Add boto3 and moto to dependencies**

Edit `pyproject.toml`:

```toml
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "pydantic>=2",
    "pydantic-settings",
    "litellm",
    "sqlalchemy[asyncio]>=2",
    "aiosqlite",
    "python-dotenv",
    "boto3",
]

[dependency-groups]
dev = [
    "pytest",
    "pytest-asyncio",
    "httpx",
    "moto[s3,dynamodb,ses,lambda]>=5",
]
```

Run: `uv sync`
Expected: lockfile updates, `boto3` and `moto` install without error.

- [ ] **Step 2: Add the shared AWS test fixture**

Create `backend/tests/conftest.py`:

```python
"""Shared pytest fixtures for the EvalBench test suite."""

import pytest


@pytest.fixture(autouse=True)
def _dummy_aws_environment(monkeypatch):
    """Ensure boto3 never resolves real credentials or region during tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
```

- [ ] **Step 3: Write the failing tests**

Create `backend/evalbench/cloud/__init__.py` (empty file).

Create `backend/tests/test_cloud.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evalbench.cloud.db_sync'`

- [ ] **Step 5: Implement db_sync.py**

Create `backend/evalbench/cloud/db_sync.py`:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock backend/tests/conftest.py backend/tests/test_cloud.py backend/evalbench/cloud/__init__.py backend/evalbench/cloud/db_sync.py
git commit -m "feat: add S3 SQLite round-trip helpers for cloud deployment"
```

---

### Task 2: DynamoDB run_status helpers

**Files:**
- Create: `backend/evalbench/cloud/run_status.py`
- Modify: `backend/tests/test_cloud.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `run_status.create_status(table_name: str, run_id: str, total: int) -> None`, `run_status.set_running(table_name: str, run_id: str) -> None`, `run_status.increment_completed(table_name: str, run_id: str) -> None`, `run_status.set_done(table_name: str, run_id: str) -> None`, `run_status.set_error(table_name: str, run_id: str, message: str) -> None`, `run_status.get_status(table_name: str, run_id: str) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_cloud.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evalbench.cloud.run_status'`

- [ ] **Step 3: Implement run_status.py**

Create `backend/evalbench/cloud/run_status.py`:

```python
"""DynamoDB-backed progress tracking for asynchronous suite runs."""

from typing import Any

import boto3


def _table(table_name: str):
    return boto3.resource("dynamodb").Table(table_name)


def create_status(table_name: str, run_id: str, total: int) -> None:
    _table(table_name).put_item(
        Item={"run_id": run_id, "status": "pending", "completed": 0, "total": total}
    )


def set_running(table_name: str, run_id: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "running"},
    )


def increment_completed(table_name: str, run_id: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET completed = completed + :one",
        ExpressionAttributeValues={":one": 1},
    )


def set_done(table_name: str, run_id: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "done"},
    )


def set_error(table_name: str, run_id: str, message: str) -> None:
    _table(table_name).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :status, #e = :error",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":status": "error", ":error": message},
    )


def get_status(table_name: str, run_id: str) -> dict[str, Any] | None:
    return _table(table_name).get_item(Key={"run_id": run_id}).get("Item")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add backend/evalbench/cloud/run_status.py backend/tests/test_cloud.py
git commit -m "feat: add DynamoDB run-status progress tracking"
```

---

### Task 3: Cached SSM parameter helper

**Files:**
- Create: `backend/evalbench/cloud/ssm.py`
- Modify: `backend/tests/test_cloud.py`

**Interfaces:**
- Produces: `ssm.get_parameter(name: str) -> str` — cached (`lru_cache`) SecureString read.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cloud.py`:

```python
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
```

Update the `_dummy_aws_environment` fixture usage note: since `get_parameter` is `lru_cache`d at module scope, add a second autouse fixture so cache state never leaks between tests. Append to `backend/tests/conftest.py`:

```python
from evalbench.cloud import ssm


@pytest.fixture(autouse=True)
def _clear_ssm_cache():
    ssm.get_parameter.cache_clear()
    yield
    ssm.get_parameter.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evalbench.cloud.ssm'` (this also breaks conftest's import, so every test in the file will error until Step 3 is done)

- [ ] **Step 3: Implement ssm.py**

Create `backend/evalbench/cloud/ssm.py`:

```python
"""Cached SSM Parameter Store reads for secrets checked on every request."""

from functools import lru_cache

import boto3


@lru_cache(maxsize=None)
def get_parameter(name: str) -> str:
    client = boto3.client("ssm")
    return client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/evalbench/cloud/ssm.py backend/tests/test_cloud.py backend/tests/conftest.py
git commit -m "feat: add cached SSM parameter reads"
```

---

### Task 4: Magic-link auth helpers

**Files:**
- Create: `backend/evalbench/cloud/auth.py`
- Modify: `backend/tests/test_cloud.py`

**Interfaces:**
- Produces: `auth.request_magic_link(*, email: str, owner_email: str, table_name: str, base_url: str, sender_email: str, ttl_seconds: int) -> None` — no-ops silently if `email != owner_email`.
- Produces: `auth.verify_magic_link(*, token: str, table_name: str) -> bool` — returns `True` and deletes the token if valid and unexpired, `False` otherwise.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_cloud.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evalbench.cloud.auth'`

- [ ] **Step 3: Implement auth.py**

Create `backend/evalbench/cloud/auth.py`:

```python
"""Magic-link request/verify against DynamoDB + SES, matching the twin blog admin pattern."""

import secrets
import time

import boto3


def request_magic_link(
    *,
    email: str,
    owner_email: str,
    table_name: str,
    base_url: str,
    sender_email: str,
    ttl_seconds: int,
) -> None:
    """Email a one-time sign-in link if email matches the owner; silently no-op otherwise."""
    if email != owner_email:
        return

    token = secrets.token_hex(32)
    expires_at = int(time.time()) + ttl_seconds
    boto3.resource("dynamodb").Table(table_name).put_item(
        Item={"token": token, "expires_at": expires_at}
    )

    link = f"{base_url}?magic={token}"
    boto3.client("ses").send_email(
        Source=sender_email,
        Destination={"ToAddresses": [owner_email]},
        Message={
            "Subject": {"Data": "Your EvalBench sign-in link", "Charset": "UTF-8"},
            "Body": {
                "Text": {
                    "Data": (
                        "Sign in to run a suite:\n\n"
                        f"{link}\n\n"
                        "This link expires in 15 minutes."
                    ),
                    "Charset": "UTF-8",
                },
                "Html": {
                    "Data": (
                        f'<p><a href="{link}">Sign in to run a suite</a></p>'
                        "<p>This link expires in 15 minutes.</p>"
                    ),
                    "Charset": "UTF-8",
                },
            },
        },
    )


def verify_magic_link(*, token: str, table_name: str) -> bool:
    """Return True and consume the token if it exists and is unexpired."""
    table = boto3.resource("dynamodb").Table(table_name)
    item = table.get_item(Key={"token": token}, ConsistentRead=True).get("Item")
    if item is None:
        return False

    is_valid = int(item["expires_at"]) > int(time.time())
    table.delete_item(Key={"token": token})
    return is_valid
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_cloud.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add backend/evalbench/cloud/auth.py backend/tests/test_cloud.py
git commit -m "feat: add magic-link auth helpers (DynamoDB + SES)"
```

---

### Task 5: Runner progress callback and run_id override

**Files:**
- Modify: `backend/evalbench/runner.py:568-641` (`execute_run`)
- Modify: `backend/tests/test_runner.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `execute_run(config, *, session_factory=None, completion_fn=None, embedding_fn=None, run_id: str | None = None, on_progress: Callable[[int, int], None] | None = None) -> SuiteResult`. When `run_id` is given, it's used verbatim instead of generating a new uuid4 — required so the caller (the async API route) and the persisted records agree on the same ID. `on_progress(completed, total)` fires once per finished task/model pair, `total = len(tasks) * len(models)`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_runner.py` (near `test_execute_run_records_persists_and_continues_with_bounded_concurrency`):

```python
async def test_execute_run_uses_provided_run_id_instead_of_generating_one(
    monkeypatch, tmp_path: Path
) -> None:
    suite = FakeSuite()
    completion = BoundedFakeCompletion(cap=2)
    database_path = (tmp_path / "runner-fixed-id.db").resolve()
    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = create_session_factory(engine)
    await init_db(engine)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        litellm_timeout_seconds=4.0,
        max_concurrency=2,
    )
    config = RunConfig(suite="fake", domain="overall", models=["openai/gpt-4o"])

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(registry_module, "SUITES", {})
            registry_module.register_suite(suite)
            scoped.setattr(runner_module, "get_settings", lambda: settings)
            result = await runner_module.execute_run(
                config,
                session_factory=factory,
                completion_fn=completion,
                embedding_fn=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("unexpected embedding call")
                ),
                run_id="caller-supplied-run-id",
            )
        persisted = await get_run_records(factory, "caller-supplied-run-id")
    finally:
        await engine.dispose()

    assert result.run_id == "caller-supplied-run-id"
    assert all(record.run_id == "caller-supplied-run-id" for record in result.records)
    assert len(persisted) == 2


async def test_execute_run_reports_progress_once_per_completed_task(
    monkeypatch, tmp_path: Path
) -> None:
    suite = FakeSuite()
    completion = BoundedFakeCompletion(cap=2)
    database_path = (tmp_path / "runner-progress.db").resolve()
    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = create_session_factory(engine)
    await init_db(engine)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        litellm_timeout_seconds=4.0,
        max_concurrency=4,
    )
    config = RunConfig(
        suite="fake",
        domain="overall",
        models=["openai/gpt-4o", "anthropic/claude-sonnet-4-5"],
    )
    progress_calls: list[tuple[int, int]] = []

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(registry_module, "SUITES", {})
            registry_module.register_suite(suite)
            scoped.setattr(runner_module, "get_settings", lambda: settings)
            await runner_module.execute_run(
                config,
                session_factory=factory,
                completion_fn=completion,
                embedding_fn=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("unexpected embedding call")
                ),
                on_progress=lambda completed, total: progress_calls.append(
                    (completed, total)
                ),
            )
    finally:
        await engine.dispose()

    assert len(progress_calls) == 4
    assert sorted(completed for completed, _ in progress_calls) == [1, 2, 3, 4]
    assert all(total == 4 for _, total in progress_calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_runner.py -k "run_id_instead_of_generating or reports_progress" -v`
Expected: FAIL with `TypeError: execute_run() got an unexpected keyword argument 'run_id'`

- [ ] **Step 3: Implement the changes in runner.py**

In `backend/evalbench/runner.py`, modify the `execute_run` signature and body (replace lines 568–637):

```python
async def execute_run(
    config: RunConfig,
    *,
    session_factory: SessionFactory | None = None,
    completion_fn: Callable[..., Any] | None = None,
    embedding_fn: Callable[..., Any] | None = None,
    run_id: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> SuiteResult:
    """Execute and atomically persist every task/model pair for one run."""
    settings = get_settings()
    suite = get_suite(config.suite)
    tasks = suite.load_tasks(config.domain)
    run_id = run_id or str(uuid.uuid4())
    selected_completion = (
        completion_fn if completion_fn is not None else litellm.completion
    )
    selected_embedding = (
        embedding_fn if embedding_fn is not None else litellm.embedding
    )
    engine = None
    if session_factory is None:
        engine = create_engine()
        await init_db(engine)
        session_factory = create_session_factory(engine)

    models = list(dict.fromkeys(config.models))
    work_items = [
        (index, task, model)
        for index, (task, model) in enumerate(
            (task, model) for task in tasks for model in models
        )
    ]
    semaphore = asyncio.Semaphore(settings.max_concurrency)
    completed = 0

    async def execute_item(
        index: int, source_task: Task, model: str
    ) -> tuple[int, MetricRecord]:
        nonlocal completed
        task = source_task.model_copy(deep=True)
        async with semaphore:
            record = await asyncio.to_thread(
                _execute_one_sync,
                suite=suite,
                task=task,
                model=model,
                run_id=run_id,
                judge_model=config.judge_model,
                completion_fn=selected_completion,
                embedding_fn=selected_embedding,
                timeout_seconds=settings.litellm_timeout_seconds,
            )
        completed += 1
        if on_progress is not None:
            on_progress(completed, len(work_items))
        print(
            f"run_id={run_id} progress={completed}/{len(work_items)} "
            f"error={record.error or 'None'}"
        )
        return index, record

    try:
        indexed_records = await asyncio.gather(
            *(
                execute_item(index, task, model)
                for index, task, model in work_items
            )
        )
        records = [
            record for _, record in sorted(indexed_records, key=lambda item: item[0])
        ]
        await save_records(session_factory, records)
        return SuiteResult(run_id=run_id, records=records)
    finally:
        if engine is not None:
            await engine.dispose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_runner.py -v`
Expected: all pass (existing tests unaffected — both new params are optional and default to prior behavior)

- [ ] **Step 5: Commit**

```bash
git add backend/evalbench/runner.py backend/tests/test_runner.py
git commit -m "feat: support caller-supplied run_id and progress callback in execute_run"
```

---

### Task 6: New API routes, auth gating, and cloud-mode db freshness

**Files:**
- Modify: `backend/evalbench/config.py:13-24` (`Settings`)
- Modify: `backend/evalbench/api/app.py` (full file)
- Modify: `.env.example`
- Modify: `backend/tests/test_runner.py`

**Interfaces:**
- Consumes: `db_sync.download_db`/`upload_db` (Task 1), `run_status.create_status`/`get_status` (Task 2), `ssm.get_parameter` (Task 3), `auth.request_magic_link`/`verify_magic_link` (Task 4), `execute_run(..., run_id=..., on_progress=...)` (Task 5).
- Produces: `POST /api/auth/request`, `GET /api/auth/verify`, `POST /runs/async`, `GET /runs/{run_id}/status`; `verify_token` FastAPI dependency; `get_run_invoker` dependency of type `Callable[[str, RunConfig], None]`.

- [ ] **Step 1: Add cloud Settings fields**

In `backend/evalbench/config.py`, replace the `Settings` class body (lines 13–24):

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    xai_api_key: str | None = None
    database_url: str = "sqlite+aiosqlite:///./evalbench.db"
    judge_model: str = "anthropic/claude-sonnet-4-5"
    litellm_timeout_seconds: float = 60.0
    max_concurrency: int = Field(default=4, ge=1)

    # Cloud deployment only — all default to local-dev-safe no-ops.
    require_auth: bool = False
    admin_token_ssm_param: str = "/evalbench/dev/admin-token"
    owner_email: str = "ahadagal@alumni.iu.edu"
    ses_sender_email: str = "ahadagal@alumni.iu.edu"
    magic_link_base_url: str = "http://localhost:3000/run"
    magic_token_table: str = "evalbench-dev-magic-tokens"
    magic_token_ttl_seconds: int = 900
    run_status_table: str = "evalbench-dev-run-status"
    db_bucket: str = ""
    db_key: str = "evalbench.db"
    runner_function_name: str = ""
```

- [ ] **Step 2: Document the new env vars**

Append to `.env.example`:

```env

# Cloud deployment only (set by Terraform on the Lambda; leave unset locally)
REQUIRE_AUTH=false
ADMIN_TOKEN_SSM_PARAM=/evalbench/dev/admin-token
OWNER_EMAIL=ahadagal@alumni.iu.edu
SES_SENDER_EMAIL=ahadagal@alumni.iu.edu
MAGIC_LINK_BASE_URL=http://localhost:3000/run
MAGIC_TOKEN_TABLE=evalbench-dev-magic-tokens
RUN_STATUS_TABLE=evalbench-dev-run-status
DB_BUCKET=
DB_KEY=evalbench.db
RUNNER_FUNCTION_NAME=
```

- [ ] **Step 3: Write the failing tests**

Append to `backend/tests/test_runner.py`:

```python
from evalbench.cloud import auth as auth_module
from evalbench.cloud import run_status as run_status_module
from evalbench.cloud import ssm as ssm_module


async def test_auth_request_delegates_to_magic_link_helper_with_settings(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_request_magic_link(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(auth_module, "request_magic_link", fake_request_magic_link)

    transport = ASGITransport(app=api_module.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/auth/request", json={"email": "ahadagal@alumni.iu.edu"}
        )

    assert response.status_code == 200
    assert response.json() == {"sent": True}
    assert captured["email"] == "ahadagal@alumni.iu.edu"
    assert captured["owner_email"] == "ahadagal@alumni.iu.edu"
    assert captured["table_name"] == "evalbench-dev-magic-tokens"


async def test_auth_verify_returns_admin_token_for_valid_magic_token(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auth_module, "verify_magic_link", lambda **kwargs: True
    )
    monkeypatch.setattr(
        ssm_module, "get_parameter", lambda name: "resolved-admin-token"
    )

    transport = ASGITransport(app=api_module.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/auth/verify", params={"token": "good"})

    assert response.status_code == 200
    assert response.json() == {"admin_token": "resolved-admin-token"}


async def test_auth_verify_rejects_invalid_magic_token(monkeypatch) -> None:
    monkeypatch.setattr(auth_module, "verify_magic_link", lambda **kwargs: False)

    transport = ASGITransport(app=api_module.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/auth/verify", params={"token": "bad"})

    assert response.status_code == 401


async def test_runs_requires_bearer_token_when_auth_required(
    api_client, monkeypatch
) -> None:
    client, _ = api_client
    settings = Settings(require_auth=True, admin_token_ssm_param="/x/admin-token")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ssm_module, "get_parameter", lambda name: "expected-token")
    registry_module.register_suite(FakeSuite())

    # api_client's own get_run_executor override (reject_real_run) raises if
    # actually called, so the "correct token" case needs its own override that
    # succeeds instead — otherwise a passing auth check would still 500.
    async def fake_execute_run(config: RunConfig, **kwargs: Any) -> SuiteResult:
        return SuiteResult(run_id="run-auth-ok", records=[])

    api_module.app.dependency_overrides[api_module.get_run_executor] = (
        lambda: fake_execute_run
    )

    body = {"suite": "fake", "domain": "overall", "models": ["openai/gpt-4o"]}

    no_header = await client.post("/runs", json=body)
    wrong_token = await client.post(
        "/runs", json=body, headers={"Authorization": "Bearer nope"}
    )
    correct_token = await client.post(
        "/runs", json=body, headers={"Authorization": "Bearer expected-token"}
    )

    assert no_header.status_code == 401
    assert wrong_token.status_code == 401
    assert correct_token.status_code == 200
    assert correct_token.json() == {"run_id": "run-auth-ok"}


async def test_runs_rejects_in_cloud_mode(api_client, monkeypatch) -> None:
    client, _ = api_client
    settings = Settings(db_bucket="evalbench-dev-db")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    registry_module.register_suite(FakeSuite())
    body = {"suite": "fake", "domain": "overall", "models": ["openai/gpt-4o"]}

    response = await client.post("/runs", json=body)

    assert response.status_code == 400


async def test_runs_async_creates_status_and_invokes_runner(
    api_client, monkeypatch
) -> None:
    client, _ = api_client
    registry_module.register_suite(FakeSuite())
    invoked: list[tuple[str, RunConfig]] = []

    api_module.app.dependency_overrides[api_module.get_run_invoker] = (
        lambda: (lambda run_id, config: invoked.append((run_id, config)))
    )

    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="evalbench-dev-run-status",
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        response = await client.post(
            "/runs/async",
            json={
                "suite": "fake",
                "domain": "overall",
                "models": ["openai/gpt-4o", "anthropic/claude-sonnet-4-5"],
            },
        )
        run_id = response.json()["run_id"]
        status = run_status_module.get_status("evalbench-dev-run-status", run_id)

    assert response.status_code == 200
    assert status == {
        "run_id": run_id,
        "status": "pending",
        "completed": 0,
        "total": 4,
    }
    assert len(invoked) == 1
    assert invoked[0][0] == run_id


async def test_run_status_endpoint_returns_stored_item(api_client) -> None:
    client, _ = api_client
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="evalbench-dev-run-status",
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        run_status_module.create_status("evalbench-dev-run-status", "run-9", total=2)
        response = await client.get("/runs/run-9/status")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-9",
        "status": "pending",
        "completed": 0,
        "total": 2,
    }


async def test_run_status_endpoint_404_for_missing_run(api_client) -> None:
    client, _ = api_client
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="evalbench-dev-run-status",
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        response = await client.get("/runs/no-such-run/status")

    assert response.status_code == 404


async def test_get_session_factory_redownloads_db_from_s3_in_cloud_mode(
    tmp_path, monkeypatch
) -> None:
    settings = Settings(db_bucket="evalbench-dev-db", db_key="evalbench.db")
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_module, "_CLOUD_DB_PATH", tmp_path / "cloud.db")

    downloads: list[tuple[str, str]] = []

    def fake_download_db(bucket: str, key: str, local_path) -> None:
        downloads.append((bucket, key))

    monkeypatch.setattr(api_module.db_sync, "download_db", fake_download_db)

    await api_module.get_session_factory()
    await api_module.get_session_factory()

    assert downloads == [
        ("evalbench-dev-db", "evalbench.db"),
        ("evalbench-dev-db", "evalbench.db"),
    ]
```

Add `import boto3` and `from moto import mock_aws` to the top of `backend/tests/test_runner.py`.

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_runner.py -k "auth_ or runs_requires or runs_rejects or runs_async or run_status_endpoint or redownloads" -v`
Expected: FAIL — routes/attributes don't exist yet (404s, `AttributeError`, `TypeError`)

- [ ] **Step 5: Implement the new routes in app.py**

Replace `backend/evalbench/api/app.py` in full:

```python
"""Minimal HTTP API over the EvalBench registry, runner, and store."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any, Literal
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from evalbench.cloud import auth as auth_module
from evalbench.cloud import db_sync
from evalbench.cloud import run_status as run_status_module
from evalbench.cloud import ssm as ssm_module
from evalbench.config import get_settings
from evalbench.models import MetricRecord, ResultsResponse, RunConfig, SuiteResult
from evalbench.registry import get_suite, list_suites
from evalbench.runner import aggregate_records, execute_run
from evalbench.store import (
    SessionFactory,
    create_engine,
    create_session_factory,
    get_run_records,
    init_db,
    query_records,
)
from evalbench.suites.base import Suite


RunExecutor = Callable[..., Awaitable[SuiteResult]]
RunInvoker = Callable[[str, RunConfig], None]


class WindowDays(IntEnum):
    SEVEN = 7
    THIRTY = 30
    NINETY = 90

default_engine = create_engine()
default_session_factory = create_session_factory(default_engine)

# Personal single-user tool: requests are effectively sequential, so a single
# module-level cloud engine (disposed and replaced each request) is sufficient —
# no connection-pool cache with concurrency-safe eviction is needed here.
_cloud_engine = None
_CLOUD_DB_PATH = Path("/tmp/evalbench.db")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize the default store at startup and release it at shutdown."""
    await init_db(default_engine)
    try:
        yield
    finally:
        await default_engine.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_session_factory() -> SessionFactory:
    """Return a session factory, re-downloading the db from S3 first in cloud mode."""
    global _cloud_engine
    settings = get_settings()
    if not settings.db_bucket:
        return default_session_factory

    if _cloud_engine is not None:
        await _cloud_engine.dispose()

    db_sync.download_db(settings.db_bucket, settings.db_key, _CLOUD_DB_PATH)
    _cloud_engine = create_engine(f"sqlite+aiosqlite:///{_CLOUD_DB_PATH}")
    await init_db(_cloud_engine)
    return create_session_factory(_cloud_engine)


def get_run_executor() -> RunExecutor:
    """Return the default synchronous run dependency."""
    return execute_run


def get_run_invoker() -> RunInvoker:
    """Return the default async-runner-invocation dependency."""
    from evalbench.cloud.lambda_invoke import invoke_runner_async

    settings = get_settings()
    return lambda run_id, config: invoke_runner_async(
        settings.runner_function_name, run_id, config
    )


def verify_token(authorization: str | None = Header(default=None)) -> None:
    """No-op locally; requires a matching Bearer admin token in cloud mode."""
    settings = get_settings()
    if not settings.require_auth:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    expected = ssm_module.get_parameter(settings.admin_token_ssm_param)
    if authorization.split(" ", 1)[1] != expected:
        raise HTTPException(status_code=401, detail="Invalid token")


def _resolve_suite(name: str) -> Suite:
    try:
        return get_suite(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/suites")
async def suites() -> list[dict]:
    """Return stable metadata for every explicitly registered suite."""
    return [
        {
            "name": suite.name,
            "metric_keys": suite.metric_keys,
            "display_metrics": suite.display_metrics,
        }
        for suite in list_suites()
    ]


@app.post("/runs")
async def runs(
    config: RunConfig,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    run_executor: Annotated[RunExecutor, Depends(get_run_executor)],
    _: Annotated[None, Depends(verify_token)],
) -> dict[str, str]:
    """Execute one run to persistence before returning its identifier (local dev only)."""
    if get_settings().db_bucket:
        raise HTTPException(
            status_code=400, detail="Use /runs/async in cloud deployments"
        )
    _resolve_suite(config.suite)
    result = await run_executor(config, session_factory=session_factory)
    return {"run_id": result.run_id}


class MagicLinkRequest(BaseModel):
    email: str


@app.post("/api/auth/request")
def request_auth_link(req: MagicLinkRequest) -> dict[str, bool]:
    settings = get_settings()
    auth_module.request_magic_link(
        email=req.email,
        owner_email=settings.owner_email,
        table_name=settings.magic_token_table,
        base_url=settings.magic_link_base_url,
        sender_email=settings.ses_sender_email,
        ttl_seconds=settings.magic_token_ttl_seconds,
    )
    return {"sent": True}


@app.get("/api/auth/verify")
def verify_auth_link(token: str) -> dict[str, str]:
    settings = get_settings()
    if not auth_module.verify_magic_link(
        token=token, table_name=settings.magic_token_table
    ):
        raise HTTPException(
            status_code=401, detail="Invalid or expired magic link"
        )
    return {"admin_token": ssm_module.get_parameter(settings.admin_token_ssm_param)}


@app.post("/runs/async")
async def runs_async(
    config: RunConfig,
    run_invoker: Annotated[RunInvoker, Depends(get_run_invoker)],
    _: Annotated[None, Depends(verify_token)],
) -> dict[str, str]:
    """Kick off an asynchronous run on the runner Lambda and return its identifier."""
    suite = _resolve_suite(config.suite)
    tasks = suite.load_tasks(config.domain)
    models = list(dict.fromkeys(config.models))
    total = len(tasks) * len(models)
    run_id = str(uuid.uuid4())

    settings = get_settings()
    run_status_module.create_status(settings.run_status_table, run_id, total)
    run_invoker(run_id, config)
    return {"run_id": run_id}


@app.get("/runs/{run_id}/status")
async def run_status(run_id: str) -> dict[str, Any]:
    settings = get_settings()
    item = run_status_module.get_status(settings.run_status_table, run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return item


@app.get("/results", response_model=ResultsResponse)
async def results(
    suite: str,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    domain: Literal[
        "overall", "software", "finance", "legal", "medical", "physics"
    ] = "overall",
    window_days: Annotated[WindowDays | None, Query()] = None,
    exclude_refusals: bool = False,
    families: Annotated[list[str] | None, Query()] = None,
) -> ResultsResponse:
    """Query filtered raw records and return their aggregate dashboard shapes."""
    selected_suite = _resolve_suite(suite)
    records = await query_records(
        session_factory,
        suite=suite,
        domain=domain,
        window_days=window_days,
        exclude_refusals=exclude_refusals,
        families=families or (),
    )
    return aggregate_records(
        suite=selected_suite,
        records=records,
        domain=domain,
        exclude_refusals=exclude_refusals,
    )


@app.get("/runs/{run_id}", response_model=list[MetricRecord])
async def raw_run(
    run_id: str,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> list[MetricRecord]:
    """Return the persisted records for one run."""
    records = await get_run_records(session_factory, run_id)
    if not records:
        raise HTTPException(status_code=404, detail="Run not found")
    return records
```

Route safety note: `/runs/{run_id}/status` (3 path segments) and `/runs/{run_id}` (2 segments) never collide regardless of declaration order, since Starlette matches on exact segment count when there's no catch-all — a request to `/runs/run-9/status` only matches the 3-segment route. `POST /runs/async` is likewise safe from `/runs/{run_id}` because that route is GET-only.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_runner.py backend/tests/test_cloud.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/evalbench/config.py backend/evalbench/api/app.py .env.example backend/tests/test_runner.py
git commit -m "feat: add magic-link auth routes, async run trigger, and cloud db freshness"
```

---

### Task 7: Lambda invoke helper and both Lambda entry points

**Files:**
- Create: `backend/evalbench/cloud/lambda_invoke.py`
- Create: `backend/evalbench/runner_lambda.py`
- Create: `backend/evalbench/lambda_handler.py`
- Modify: `backend/tests/test_cloud.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `db_sync.download_db`/`upload_db` (Task 1), `run_status.set_running`/`increment_completed`/`set_done`/`set_error` (Task 2), `execute_run(..., run_id=..., on_progress=...)` (Task 5), `evalbench.api.app.app` (Task 6).
- Produces: `lambda_invoke.invoke_runner_async(function_name: str, run_id: str, config: RunConfig) -> None`; `runner_lambda.handler(event: dict, context: object) -> dict`; `lambda_handler.handler` (Mangum-wrapped ASGI callable).

- [ ] **Step 1: Add mangum dependency**

Edit `pyproject.toml`, add `"mangum"` to `dependencies`.

Run: `uv sync`

- [ ] **Step 2: Write the failing tests**

Append to `backend/tests/test_cloud.py`:

```python
import json

from evalbench.cloud import lambda_invoke
from evalbench.models import RunConfig


@mock_aws
def test_invoke_runner_async_invokes_with_event_type_and_json_payload():
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
```

Create `backend/tests/test_runner_lambda.py`:

```python
"""Unit tests for the runner Lambda's orchestration of a suite run."""

from pathlib import Path
from typing import Any

import pytest

import evalbench.runner_lambda as runner_lambda_module
from evalbench.cloud import run_status as run_status_module
from evalbench.config import Settings
from evalbench.models import MetricRecord, RunConfig, SuiteResult


def _make_record(run_id: str) -> MetricRecord:
    from datetime import datetime, timezone

    return MetricRecord(
        id="rec-1",
        run_id=run_id,
        suite="fake",
        domain="software",
        model="openai/gpt-4o",
        provider="openai",
        model_family="OpenAI",
        task_id="task-1",
        latency_ms=10.0,
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0.0,
        error=None,
        refused=False,
        metrics={},
        created_at=datetime.now(timezone.utc),
    )


async def test_handler_runs_downloads_uploads_and_marks_done(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        db_bucket="evalbench-dev-db",
        db_key="evalbench.db",
        run_status_table="evalbench-dev-run-status",
    )
    monkeypatch.setattr(runner_lambda_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runner_lambda_module, "_LOCAL_DB_PATH", tmp_path / "run.db")

    calls: list[str] = []
    monkeypatch.setattr(
        runner_lambda_module.db_sync,
        "download_db",
        lambda bucket, key, path: calls.append(f"download:{bucket}/{key}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.db_sync,
        "upload_db",
        lambda bucket, key, path: calls.append(f"upload:{bucket}/{key}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "set_running",
        lambda table, run_id: calls.append(f"running:{run_id}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "increment_completed",
        lambda table, run_id: calls.append(f"progress:{run_id}"),
    )
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "set_done",
        lambda table, run_id: calls.append(f"done:{run_id}"),
    )

    async def fake_execute_run(config: RunConfig, **kwargs: Any) -> SuiteResult:
        kwargs["on_progress"](1, 1)
        return SuiteResult(run_id=kwargs["run_id"], records=[_make_record(kwargs["run_id"])])

    monkeypatch.setattr(runner_lambda_module, "execute_run", fake_execute_run)

    result = runner_lambda_module.handler(
        {
            "run_id": "run-abc",
            "config": {
                "suite": "fake",
                "domain": "software",
                "models": ["openai/gpt-4o"],
            },
        },
        None,
    )

    assert result == {"run_id": "run-abc"}
    assert calls == [
        "download:evalbench-dev-db/evalbench.db",
        "running:run-abc",
        "progress:run-abc",
        "upload:evalbench-dev-db/evalbench.db",
        "done:run-abc",
    ]


async def test_handler_marks_error_and_reraises_on_failure(monkeypatch, tmp_path) -> None:
    settings = Settings(
        db_bucket="evalbench-dev-db",
        db_key="evalbench.db",
        run_status_table="evalbench-dev-run-status",
    )
    monkeypatch.setattr(runner_lambda_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runner_lambda_module, "_LOCAL_DB_PATH", tmp_path / "run.db")
    monkeypatch.setattr(runner_lambda_module.db_sync, "download_db", lambda *a: None)
    monkeypatch.setattr(runner_lambda_module.db_sync, "upload_db", lambda *a: None)
    monkeypatch.setattr(runner_lambda_module.run_status, "set_running", lambda *a: None)

    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner_lambda_module.run_status,
        "set_error",
        lambda table, run_id, message: errors.append((run_id, message)),
    )

    async def failing_execute_run(config: RunConfig, **kwargs: Any) -> SuiteResult:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(runner_lambda_module, "execute_run", failing_execute_run)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        runner_lambda_module.handler(
            {
                "run_id": "run-fail",
                "config": {
                    "suite": "fake",
                    "domain": "software",
                    "models": ["openai/gpt-4o"],
                },
            },
            None,
        )

    assert errors == [("run-fail", "synthetic failure")]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_cloud.py backend/tests/test_runner_lambda.py -v`
Expected: FAIL — `ModuleNotFoundError` for `lambda_invoke` and `runner_lambda`

- [ ] **Step 4: Implement lambda_invoke.py**

Create `backend/evalbench/cloud/lambda_invoke.py`:

```python
"""Fire-and-forget async invocation of the runner Lambda."""

import json

import boto3

from evalbench.models import RunConfig


def invoke_runner_async(function_name: str, run_id: str, config: RunConfig) -> None:
    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"run_id": run_id, "config": config.model_dump()}).encode(),
    )
```

- [ ] **Step 5: Implement runner_lambda.py**

Create `backend/evalbench/runner_lambda.py`:

```python
"""Lambda entry point for asynchronous suite runs (invoked only by the api Lambda)."""

import asyncio
from pathlib import Path
from typing import Any

from evalbench.cloud import db_sync
from evalbench.cloud import run_status
from evalbench.config import get_settings
from evalbench.models import RunConfig
from evalbench.runner import execute_run
from evalbench.store import create_engine, create_session_factory, init_db

_LOCAL_DB_PATH = Path("/tmp/evalbench.db")


def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    return asyncio.run(_run(event["run_id"], RunConfig(**event["config"])))


async def _run(run_id: str, config: RunConfig) -> dict[str, str]:
    settings = get_settings()
    db_sync.download_db(settings.db_bucket, settings.db_key, _LOCAL_DB_PATH)
    run_status.set_running(settings.run_status_table, run_id)

    engine = create_engine(f"sqlite+aiosqlite:///{_LOCAL_DB_PATH}")
    await init_db(engine)
    factory = create_session_factory(engine)

    def on_progress(_completed: int, _total: int) -> None:
        run_status.increment_completed(settings.run_status_table, run_id)

    try:
        await execute_run(
            config,
            session_factory=factory,
            run_id=run_id,
            on_progress=on_progress,
        )
    except Exception as exc:
        run_status.set_error(settings.run_status_table, run_id, str(exc))
        raise
    finally:
        await engine.dispose()

    db_sync.upload_db(settings.db_bucket, settings.db_key, _LOCAL_DB_PATH)
    run_status.set_done(settings.run_status_table, run_id)
    return {"run_id": run_id}
```

- [ ] **Step 6: Implement lambda_handler.py**

Create `backend/evalbench/lambda_handler.py`:

```python
"""Mangum entry point wrapping the FastAPI app for the api Lambda."""

from mangum import Mangum

from evalbench.api.app import app

handler = Mangum(app)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_cloud.py backend/tests/test_runner_lambda.py -v`
Expected: all pass

- [ ] **Step 8: Run the full backend test suite**

Run: `uv run pytest backend/tests/ -v`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock backend/evalbench/cloud/lambda_invoke.py backend/evalbench/runner_lambda.py backend/evalbench/lambda_handler.py backend/tests/test_cloud.py backend/tests/test_runner_lambda.py
git commit -m "feat: add runner and api Lambda entry points"
```

---

### Task 8: Lambda deployment zip builder

**Files:**
- Create: `backend/deploy.py`

**Interfaces:**
- Consumes: nothing new — packages the `evalbench` package (Tasks 1–7) and its dependencies.
- Produces: `backend/lambda-deployment.zip`, shared by both the `api` and `runner` Terraform Lambda resources (they differ only in `handler`, not in code — `evalbench.lambda_handler.handler` vs `evalbench.runner_lambda.handler`).

- [ ] **Step 1: Implement the zip builder**

Create `backend/deploy.py`:

```python
"""Build the Lambda deployment package shared by the api and runner functions.

Requires Docker (to install dependencies against the Lambda Python 3.12
runtime image, matching its manylinux ABI) and `uv`.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

BACKEND_DIR = Path(__file__).parent
BUILD_DIR = BACKEND_DIR / "lambda-package"
ZIP_PATH = BACKEND_DIR / "lambda-deployment.zip"


def main() -> None:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    BUILD_DIR.mkdir()

    requirements_path = BUILD_DIR / "requirements.txt"
    subprocess.run(
        [
            "uv", "export", "--no-dev", "--no-hashes", "--no-emit-project",
            "-o", str(requirements_path),
        ],
        check=True,
        cwd=BACKEND_DIR.parent,
    )

    print("Installing dependencies for the Lambda runtime...")
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{BUILD_DIR}:/var/task",
            "--platform", "linux/amd64",
            "--entrypoint", "",
            "public.ecr.aws/lambda/python:3.12",
            "/bin/sh", "-c",
            "pip install --target /var/task -r /var/task/requirements.txt "
            "--platform manylinux2014_x86_64 --only-binary=:all: --upgrade",
        ],
        check=True,
    )
    requirements_path.unlink()

    print("Copying application code...")
    shutil.copytree(BACKEND_DIR / "evalbench", BUILD_DIR / "evalbench")

    print("Creating zip file...")
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in BUILD_DIR.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(BUILD_DIR))

    print(f"Built {ZIP_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it builds a valid zip**

Run: `docker info >/dev/null 2>&1 && uv run python backend/deploy.py || echo "Docker not available — skip until CI"`
Expected: if Docker is available, `backend/lambda-deployment.zip` is created and `unzip -l backend/lambda-deployment.zip | grep evalbench/lambda_handler.py` finds the file. If Docker isn't available in this environment, this step runs for real the first time in GitHub Actions (Task 14), which always has Docker.

- [ ] **Step 3: Ignore build artifacts**

Add to `.gitignore`:

```
backend/lambda-package/
backend/lambda-deployment.zip
```

- [ ] **Step 4: Commit**

```bash
git add backend/deploy.py .gitignore
git commit -m "feat: add Lambda deployment zip builder"
```

---

### Task 9: Terraform skeleton (providers, backend, variables)

**Files:**
- Create: `terraform/versions.tf`
- Create: `terraform/backend.tf`
- Create: `terraform/variables.tf`
- Create: `terraform/dev.tfvars`

**Interfaces:**
- Produces: Terraform variables consumed by every resource file in Tasks 10–13: `var.project_name`, `var.environment`, `var.aws_region`, `var.owner_email`, `var.admin_token_ssm_param`.

- [ ] **Step 1: Create versions.tf**

Create `terraform/versions.tf`:

```hcl
terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
```

- [ ] **Step 2: Create backend.tf**

Create `terraform/backend.tf`:

```hcl
terraform {
  backend "s3" {
    # Set via: terraform init -backend-config="bucket=<your-state-bucket>"
    key     = "evalbench/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}
```

- [ ] **Step 3: Create variables.tf**

Create `terraform/variables.tf`:

```hcl
variable "project_name" {
  type    = string
  default = "evalbench"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "owner_email" {
  type        = string
  description = "Only email allowed to request a magic sign-in link."
  default     = "ahadagal@alumni.iu.edu"
}

variable "admin_token_ssm_param" {
  type        = string
  description = "SSM parameter name holding the admin bearer token (value set manually, not by Terraform)."
  default     = "/evalbench/dev/admin-token"
}

locals {
  name_prefix = "${var.project_name}-${var.environment}"
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

data "aws_caller_identity" "current" {}
```

- [ ] **Step 4: Create dev.tfvars**

Create `terraform/dev.tfvars`:

```hcl
project_name = "evalbench"
environment  = "dev"
aws_region   = "us-east-1"
owner_email  = "ahadagal@alumni.iu.edu"
```

- [ ] **Step 5: Verify the skeleton is syntactically valid**

Run: `cd terraform && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.` (no resources reference each other yet, so this only checks HCL syntax)

- [ ] **Step 6: Commit**

```bash
git add terraform/versions.tf terraform/backend.tf terraform/variables.tf terraform/dev.tfvars
git commit -m "feat: add Terraform skeleton for cloud deployment"
```

---

### Task 10: Terraform — DynamoDB tables, S3 db bucket, SSM param placeholder

**Files:**
- Create: `terraform/main.tf`

**Interfaces:**
- Consumes: `local.name_prefix`, `local.common_tags`, `var.admin_token_ssm_param` (Task 9).
- Produces: `aws_dynamodb_table.magic_tokens`, `aws_dynamodb_table.run_status`, `aws_s3_bucket.db` — referenced by IAM policies and Lambda env vars in Task 11.

- [ ] **Step 1: Create main.tf with the DynamoDB tables and db bucket**

Create `terraform/main.tf`:

```hcl
# ── Magic-link tokens ─────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "magic_tokens" {
  name         = "${local.name_prefix}-magic-tokens"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "token"
  tags         = local.common_tags

  attribute {
    name = "token"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# ── Async run progress ──────────────────────────────────────────────────────

resource "aws_dynamodb_table" "run_status" {
  name         = "${local.name_prefix}-run-status"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  tags         = local.common_tags

  attribute {
    name = "run_id"
    type = "S"
  }
}

# ── SQLite metric-records file ──────────────────────────────────────────────

resource "aws_s3_bucket" "db" {
  bucket = "${local.name_prefix}-db-${data.aws_caller_identity.current.account_id}"
  tags   = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "db" {
  bucket = aws_s3_bucket.db.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── admin_token placeholder ─────────────────────────────────────────────────
# The value is set manually via `aws ssm put-parameter` (one-time setup) so it
# never lands in Terraform state. This resource only reserves + documents the
# name; `lifecycle.ignore_changes` stops `apply` from clobbering the real value.

resource "aws_ssm_parameter" "admin_token" {
  name  = var.admin_token_ssm_param
  type  = "SecureString"
  value = "REPLACED_MANUALLY"
  tags  = local.common_tags

  lifecycle {
    ignore_changes = [value]
  }
}
```

- [ ] **Step 2: Verify**

Run: `cd terraform && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add terraform/main.tf
git commit -m "feat: add Terraform DynamoDB tables, db bucket, and admin-token placeholder"
```

---

### Task 11: Terraform — IAM roles, Lambda functions, API Gateway

**Files:**
- Modify: `terraform/main.tf`
- Create: `terraform/api_keys.tf`

**Interfaces:**
- Consumes: `aws_dynamodb_table.magic_tokens`/`run_status`, `aws_s3_bucket.db`, `aws_ssm_parameter.admin_token` (Task 10); `backend/lambda-deployment.zip` (Task 8).
- Produces: `aws_lambda_function.api`, `aws_lambda_function.runner`, `aws_apigatewayv2_api.main` — referenced by the frontend's `NEXT_PUBLIC_API_BASE_URL` and outputs (Task 12).

- [ ] **Step 1: Read provider keys from SSM at apply time**

Create `terraform/api_keys.tf` — these `data` sources read values Task 17's one-time setup will have already written via `aws ssm put-parameter`. They're consumed only by the `runner` Lambda's environment (Design correction 4: no runtime SSM read needed for these).

```hcl
data "aws_ssm_parameter" "openai_api_key" {
  name            = "/evalbench/${var.environment}/openai-api-key"
  with_decryption = true
}

data "aws_ssm_parameter" "anthropic_api_key" {
  name            = "/evalbench/${var.environment}/anthropic-api-key"
  with_decryption = true
}

data "aws_ssm_parameter" "gemini_api_key" {
  name            = "/evalbench/${var.environment}/gemini-api-key"
  with_decryption = true
}

data "aws_ssm_parameter" "openrouter_api_key" {
  name            = "/evalbench/${var.environment}/openrouter-api-key"
  with_decryption = true
}

data "aws_ssm_parameter" "xai_api_key" {
  name            = "/evalbench/${var.environment}/xai-api-key"
  with_decryption = true
}

data "aws_ssm_parameter" "judge_model" {
  name            = "/evalbench/${var.environment}/judge-model"
  with_decryption = true
}
```

- [ ] **Step 2: Append IAM roles and Lambda functions to main.tf**

Append to `terraform/main.tf`:

```hcl
# ── api Lambda IAM ───────────────────────────────────────────────────────────

resource "aws_iam_role" "api_lambda_role" {
  name = "${local.name_prefix}-api-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_lambda_basic" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  role       = aws_iam_role.api_lambda_role.name
}

resource "aws_iam_role_policy" "api_lambda_ssm" {
  name = "${local.name_prefix}-api-ssm-policy"
  role = aws_iam_role.api_lambda_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = aws_ssm_parameter.admin_token.arn
    }]
  })
}

resource "aws_iam_role_policy" "api_lambda_dynamodb" {
  name = "${local.name_prefix}-api-dynamodb-policy"
  role = aws_iam_role.api_lambda_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteItem"]
      Resource = [
        aws_dynamodb_table.magic_tokens.arn,
        aws_dynamodb_table.run_status.arn,
      ]
    }]
  })
}

resource "aws_iam_role_policy" "api_lambda_s3" {
  name = "${local.name_prefix}-api-s3-policy"
  role = aws_iam_role.api_lambda_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "${aws_s3_bucket.db.arn}/*"
    }]
  })
}

resource "aws_iam_role_policy" "api_lambda_ses" {
  name = "${local.name_prefix}-api-ses-policy"
  role = aws_iam_role.api_lambda_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ses:SendEmail"]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "api_lambda_invoke_runner" {
  name = "${local.name_prefix}-api-invoke-runner-policy"
  role = aws_iam_role.api_lambda_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.runner.arn
    }]
  })
}

# ── runner Lambda IAM ────────────────────────────────────────────────────────

resource "aws_iam_role" "runner_lambda_role" {
  name = "${local.name_prefix}-runner-role"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "runner_lambda_basic" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  role       = aws_iam_role.runner_lambda_role.name
}

resource "aws_iam_role_policy" "runner_lambda_dynamodb" {
  name = "${local.name_prefix}-runner-dynamodb-policy"
  role = aws_iam_role.runner_lambda_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
      Resource = aws_dynamodb_table.run_status.arn
    }]
  })
}

resource "aws_iam_role_policy" "runner_lambda_s3" {
  name = "${local.name_prefix}-runner-s3-policy"
  role = aws_iam_role.runner_lambda_role.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = "${aws_s3_bucket.db.arn}/*"
    }]
  })
}

# ── Lambda functions ─────────────────────────────────────────────────────────

resource "aws_lambda_function" "api" {
  filename         = "${path.module}/../backend/lambda-deployment.zip"
  function_name    = "${local.name_prefix}-api"
  role             = aws_iam_role.api_lambda_role.arn
  handler          = "evalbench.lambda_handler.handler"
  source_code_hash = filebase64sha256("${path.module}/../backend/lambda-deployment.zip")
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  timeout          = 29
  tags             = local.common_tags

  environment {
    variables = {
      REQUIRE_AUTH          = "true"
      ADMIN_TOKEN_SSM_PARAM  = var.admin_token_ssm_param
      OWNER_EMAIL            = var.owner_email
      SES_SENDER_EMAIL       = var.owner_email
      MAGIC_LINK_BASE_URL    = "https://${aws_cloudfront_distribution.main.domain_name}/run"
      MAGIC_TOKEN_TABLE      = aws_dynamodb_table.magic_tokens.name
      RUN_STATUS_TABLE       = aws_dynamodb_table.run_status.name
      DB_BUCKET              = aws_s3_bucket.db.id
      RUNNER_FUNCTION_NAME   = aws_lambda_function.runner.function_name
    }
  }
}

resource "aws_lambda_function" "runner" {
  filename         = "${path.module}/../backend/lambda-deployment.zip"
  function_name    = "${local.name_prefix}-runner"
  role             = aws_iam_role.runner_lambda_role.arn
  handler          = "evalbench.runner_lambda.handler"
  source_code_hash = filebase64sha256("${path.module}/../backend/lambda-deployment.zip")
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  timeout          = 900
  memory_size      = 512
  tags             = local.common_tags

  environment {
    variables = {
      DB_BUCKET           = aws_s3_bucket.db.id
      RUN_STATUS_TABLE    = aws_dynamodb_table.run_status.name
      OPENAI_API_KEY      = data.aws_ssm_parameter.openai_api_key.value
      ANTHROPIC_API_KEY   = data.aws_ssm_parameter.anthropic_api_key.value
      GEMINI_API_KEY      = data.aws_ssm_parameter.gemini_api_key.value
      OPENROUTER_API_KEY  = data.aws_ssm_parameter.openrouter_api_key.value
      XAI_API_KEY         = data.aws_ssm_parameter.xai_api_key.value
      JUDGE_MODEL         = data.aws_ssm_parameter.judge_model.value
    }
  }
}

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# ── API Gateway HTTP API ─────────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"
  tags          = local.common_tags

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["*"]
  }
}

resource "aws_apigatewayv2_integration" "api_lambda" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "api_proxy" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
}

resource "aws_apigatewayv2_route" "api_root" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "ANY /"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true
  tags        = local.common_tags
}
```

Note: `MAGIC_LINK_BASE_URL` references `aws_cloudfront_distribution.main`, created in Task 12 — Terraform resolves this fine via its dependency graph regardless of file order, since all `.tf` files in a directory are merged before evaluation.

- [ ] **Step 3: Verify (structural, not a real apply)**

Run: `cd terraform && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.` (will still fail until Task 12 defines `aws_cloudfront_distribution.main` — that's fine, re-run validate after Task 12)

- [ ] **Step 4: Commit**

```bash
git add terraform/main.tf terraform/api_keys.tf
git commit -m "feat: add Terraform IAM roles, Lambda functions, and API Gateway"
```

---

### Task 12: Terraform — SES identity, CloudFront + frontend bucket, outputs

**Files:**
- Modify: `terraform/main.tf`
- Create: `terraform/outputs.tf`

**Interfaces:**
- Consumes: `local.name_prefix`, `local.common_tags`, `var.owner_email` (Task 9); `aws_lambda_function.api`, `aws_apigatewayv2_api.main` (Task 11).
- Produces: `aws_cloudfront_distribution.main` (referenced by Task 11's `MAGIC_LINK_BASE_URL`), Terraform outputs consumed by the GitHub Actions workflow (Task 14) and one-time setup (Task 17).

- [ ] **Step 1: Append SES identity and frontend hosting to main.tf**

Append to `terraform/main.tf`:

```hcl
# ── SES identity for magic-link emails ──────────────────────────────────────

resource "aws_ses_email_identity" "owner" {
  email = var.owner_email
}

# ── Frontend static hosting ──────────────────────────────────────────────────

resource "aws_s3_bucket" "frontend" {
  bucket = "${local.name_prefix}-frontend-${data.aws_caller_identity.current.account_id}"
  tags   = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_website_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  index_document { suffix = "index.html" }
  error_document { key = "404.html" }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadGetObject"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  default_root_object = "index.html"
  tags                = local.common_tags

  origin {
    domain_name = aws_s3_bucket_website_configuration.frontend.website_endpoint
    origin_id   = "frontend-s3"

    custom_origin_config {
      http_port              = 80
      https_port              = 443
      origin_protocol_policy  = "http-only"
      origin_ssl_protocols    = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods          = ["GET", "HEAD"]
    target_origin_id        = "frontend-s3"
    viewer_protocol_policy  = "redirect-to-https"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }
  }

  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/404.html"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
```

- [ ] **Step 2: Create outputs.tf**

Create `terraform/outputs.tf`:

```hcl
output "frontend_url" {
  value = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "api_url" {
  value = aws_apigatewayv2_api.main.api_endpoint
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.id
}

output "db_bucket" {
  value = aws_s3_bucket.db.id
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.main.id
}
```

- [ ] **Step 3: Verify**

Run: `cd terraform && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add terraform/main.tf terraform/outputs.tf
git commit -m "feat: add Terraform SES identity, CloudFront frontend hosting, and outputs"
```

---

### Task 13: Terraform — GitHub OIDC provider and deploy role

**Files:**
- Create: `terraform/github_oidc.tf`
- Modify: `terraform/variables.tf`

**Interfaces:**
- Consumes: `local.name_prefix`, `data.aws_caller_identity.current` (Task 9).
- Produces: `aws_iam_role.github_deploy` — its ARN becomes the `AWS_ROLE_ARN` GitHub secret consumed by Task 14's workflow.

- [ ] **Step 1: Add a github_repo variable**

Append to `terraform/variables.tf`:

```hcl
variable "github_repo" {
  type        = string
  description = "owner/repo allowed to assume the deploy role via OIDC."
  default     = "akashpersetti/evalbench"
}
```

- [ ] **Step 2: Create github_oidc.tf**

Create `terraform/github_oidc.tf`:

```hcl
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name_prefix}-github-deploy"
  tags = local.common_tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "${local.name_prefix}-github-deploy-policy"
  role = aws_iam_role.github_deploy.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunction",
          "lambda:CreateFunction",
          "lambda:DeleteFunction",
          "lambda:AddPermission",
          "lambda:RemovePermission",
          "lambda:InvokeFunction",
          "lambda:TagResource",
          "lambda:ListTags",
        ]
        Resource = "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:${local.name_prefix}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:*"]
        Resource = [
          "arn:aws:s3:::${local.name_prefix}-*",
          "arn:aws:s3:::${local.name_prefix}-*/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:*"]
        Resource = "arn:aws:dynamodb:*:${data.aws_caller_identity.current.account_id}:table/${local.name_prefix}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["apigateway:*"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudfront:*"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:*"]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter", "ssm:AddTagsToResource"]
        Resource = "arn:aws:ssm:*:${data.aws_caller_identity.current.account_id}:parameter/evalbench/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ses:GetEmailIdentity", "ses:VerifyEmailIdentity", "ses:TagResource"]
        Resource = "*"
      },
    ]
  })
}
```

- [ ] **Step 3: Verify**

Run: `cd terraform && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add terraform/github_oidc.tf terraform/variables.tf
git commit -m "feat: add GitHub OIDC provider and scoped deploy role"
```

---

### Task 14: GitHub Actions deploy workflow

**Files:**
- Create: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: `backend/deploy.py` (Task 8), `terraform/` (Tasks 9–13), `web/` build (existing), Terraform outputs `frontend_bucket`/`cloudfront_distribution_id` (Task 12).
- Requires GitHub repository secrets `AWS_ROLE_ARN` (Task 13's `aws_iam_role.github_deploy` ARN), `AWS_ACCOUNT_ID`, `AWS_REGION`, and `TF_STATE_BUCKET` — set manually in Task 17.

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy EvalBench

on:
  push:
    branches: [main]
  workflow_dispatch: {}

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ${{ secrets.AWS_REGION }}

      - name: Install uv
        uses: astral-sh/setup-uv@v3

      - name: Build Lambda deployment package
        run: uv run python backend/deploy.py

      - uses: hashicorp/setup-terraform@v3

      - name: Terraform init
        working-directory: terraform
        run: terraform init -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}"

      - name: Terraform apply
        working-directory: terraform
        run: terraform apply -auto-approve -var-file=dev.tfvars

      - name: Set frontend build outputs
        id: tf_outputs
        working-directory: terraform
        run: |
          echo "api_url=$(terraform output -raw api_url)" >> "$GITHUB_OUTPUT"
          echo "frontend_bucket=$(terraform output -raw frontend_bucket)" >> "$GITHUB_OUTPUT"
          echo "distribution_id=$(terraform output -raw cloudfront_distribution_id)" >> "$GITHUB_OUTPUT"
          echo "frontend_url=$(terraform output -raw frontend_url)" >> "$GITHUB_OUTPUT"

      - name: Install frontend dependencies
        working-directory: web
        run: npm ci

      - name: Build frontend
        working-directory: web
        env:
          NEXT_PUBLIC_API_BASE_URL: ${{ steps.tf_outputs.outputs.api_url }}
        run: npm run build

      - name: Sync frontend to S3
        run: aws s3 sync web/out "s3://${{ steps.tf_outputs.outputs.frontend_bucket }}" --delete

      - name: Invalidate CloudFront cache
        run: aws cloudfront create-invalidation --distribution-id "${{ steps.tf_outputs.outputs.distribution_id }}" --paths "/*"

      - name: Print deployment summary
        run: echo "Deployed to ${{ steps.tf_outputs.outputs.frontend_url }}"
```

Note: this assumes `next.config` is (or will be, in Task 16) set to `output: "export"` so `npm run build` produces a static `web/out` directory — Next.js's static-export mode, required since there's no Node server running in this deployment.

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "feat: add GitHub Actions deploy workflow (OIDC, Terraform, S3, CloudFront)"
```

---

### Task 15: Frontend API client additions

**Files:**
- Modify: `web/lib/api.ts`

**Interfaces:**
- Consumes: nothing new from earlier tasks (calls the routes added in Task 6 directly over HTTP).
- Produces: `requestMagicLink(email: string) -> Promise<void>`, `verifyMagicLink(token: string) -> Promise<string>` (returns the admin token), `startRun(config: RunRequest, adminToken: string) -> Promise<string>` (returns run_id), `fetchRunStatus(runId: string) -> Promise<RunStatus>`, `fetchRun(runId: string) -> Promise<MetricRecord[]>` — consumed by Task 16's `/run` page.

- [ ] **Step 1: Append the new types and functions**

Append to `web/lib/api.ts`:

```ts
export type RunRequest = {
  suite: string;
  domain: Domain;
  models: string[];
  judgeModel?: string;
};

export type RunStatus = {
  run_id: string;
  status: "pending" | "running" | "done" | "error";
  completed: number;
  total: number;
  error?: string;
};

export type MetricRecord = {
  id: string;
  run_id: string;
  suite: string;
  domain: string;
  model: string;
  provider: string;
  model_family: string;
  task_id: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  error: string | null;
  refused: boolean;
  metrics: Record<string, number>;
  created_at: string;
};

function isRunStatus(value: unknown): value is RunStatus {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    (value.status === "pending" ||
      value.status === "running" ||
      value.status === "done" ||
      value.status === "error") &&
    isFiniteNumber(value.completed) &&
    isFiniteNumber(value.total)
  );
}

async function postJson(
  path: string,
  body: unknown,
  headers?: Record<string, string>,
): Promise<unknown> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  try {
    return await response.json();
  } catch {
    throw new ApiError(500, "Malformed API response");
  }
}

export async function requestMagicLink(email: string): Promise<void> {
  await postJson("/api/auth/request", { email });
}

export async function verifyMagicLink(token: string): Promise<string> {
  const payload = await fetchJson(
    `/api/auth/verify?token=${encodeURIComponent(token)}`,
  );

  if (
    !isRecord(payload) ||
    typeof payload.admin_token !== "string" ||
    !payload.admin_token
  ) {
    throw new ApiError(500, "Magic-link response did not include an admin token");
  }

  return payload.admin_token;
}

export async function startRun(
  config: RunRequest,
  adminToken: string,
): Promise<string> {
  const payload = await postJson(
    "/runs/async",
    {
      suite: config.suite,
      domain: config.domain,
      models: config.models,
      ...(config.judgeModel ? { judge_model: config.judgeModel } : {}),
    },
    { Authorization: `Bearer ${adminToken}` },
  );

  if (!isRecord(payload) || typeof payload.run_id !== "string") {
    throw new ApiError(500, "Malformed API response");
  }

  return payload.run_id;
}

export async function fetchRunStatus(runId: string): Promise<RunStatus> {
  const payload = await fetchJson(`/runs/${encodeURIComponent(runId)}/status`);

  if (!isRunStatus(payload)) {
    throw new ApiError(500, "Malformed API response");
  }

  return payload;
}

export async function fetchRun(runId: string): Promise<MetricRecord[]> {
  const payload = await fetchJson(`/runs/${encodeURIComponent(runId)}`);

  if (!Array.isArray(payload)) {
    throw new ApiError(500, "Malformed API response");
  }

  return payload as MetricRecord[];
}
```

- [ ] **Step 2: Verify it type-checks**

Run: `cd web && npx tsc --noEmit`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add web/lib/api.ts
git commit -m "feat: add frontend API client functions for magic-link auth and async runs"
```

---

### Task 16: `/run` page

**Files:**
- Create: `web/app/run/page.tsx`
- Modify: `web/next.config.ts` (or `.js`/`.mjs` — match whatever extension already exists)

**Interfaces:**
- Consumes: `requestMagicLink`, `verifyMagicLink`, `startRun`, `fetchRunStatus`, `fetchRun`, `fetchSuites` (Task 15 + existing `web/lib/api.ts`).

- [ ] **Step 1: Enable static export**

Check which config file exists (`web/next.config.ts`, `.mjs`, or `.js`) and add `output: "export"` to it. If it's `web/next.config.ts`:

```ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
};

export default nextConfig;
```

- [ ] **Step 2: Create the page**

Create `web/app/run/page.tsx`:

```tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  fetchRun,
  fetchRunStatus,
  fetchSuites,
  requestMagicLink,
  startRun,
  verifyMagicLink,
  type Domain,
  type MetricRecord,
  type RunStatus,
  type SuiteDescriptor,
} from "@/lib/api";

const DOMAINS: Domain[] = [
  "overall",
  "software",
  "finance",
  "legal",
  "medical",
  "physics",
];

const OWNER_EMAIL = "ahadagal@alumni.iu.edu";

function errorMessage(reason: unknown): string {
  return reason instanceof ApiError
    ? reason.message
    : "Unable to reach the EvalBench API.";
}

export default function RunPage() {
  const [token, setToken] = useState("");
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState("");
  const [linkSent, setLinkSent] = useState(false);
  const [sendingLink, setSendingLink] = useState(false);
  const [email, setEmail] = useState(OWNER_EMAIL);
  const authInitialized = useRef(false);

  const [suites, setSuites] = useState<SuiteDescriptor[]>([]);
  const [suite, setSuite] = useState("");
  const [domain, setDomain] = useState<Domain>("overall");
  const [modelsInput, setModelsInput] = useState("openai/gpt-4o");
  const [judgeModel, setJudgeModel] = useState("");
  const [submitError, setSubmitError] = useState("");

  const [runId, setRunId] = useState<string | null>(null);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [records, setRecords] = useState<MetricRecord[] | null>(null);

  useEffect(() => {
    if (authInitialized.current) return;
    authInitialized.current = true;

    async function initializeAuth() {
      const url = new URL(window.location.href);
      const magicToken = url.searchParams.get("magic");

      if (magicToken) {
        try {
          const adminToken = await verifyMagicLink(magicToken);
          localStorage.setItem("run_token", adminToken);
          setToken(adminToken);
        } catch {
          setAuthError("This magic link is invalid or has expired.");
        } finally {
          url.searchParams.delete("magic");
          history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
          setAuthLoading(false);
        }
        return;
      }

      const saved = localStorage.getItem("run_token");
      if (saved) {
        setToken(saved);
      }
      setAuthLoading(false);
    }

    void initializeAuth();
  }, []);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    fetchSuites(controller.signal)
      .then((loaded) => {
        setSuites(loaded);
        setSuite((current) => current || loaded[0]?.name || "");
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [token]);

  useEffect(() => {
    if (!runId || status?.status === "done" || status?.status === "error") return;

    const interval = setInterval(() => {
      fetchRunStatus(runId)
        .then(setStatus)
        .catch(() => undefined);
    }, 3000);

    return () => clearInterval(interval);
  }, [runId, status?.status]);

  useEffect(() => {
    if (status?.status !== "done" || !runId) return;
    fetchRun(runId)
      .then(setRecords)
      .catch(() => undefined);
  }, [status?.status, runId]);

  async function handleMagicLinkRequest(event: React.FormEvent) {
    event.preventDefault();
    setAuthError("");
    setSendingLink(true);
    try {
      await requestMagicLink(email);
      setLinkSent(true);
    } catch {
      setAuthError("Unable to send a magic link. Please try again.");
    } finally {
      setSendingLink(false);
    }
  }

  function handleSignOut() {
    localStorage.removeItem("run_token");
    setToken("");
    setRunId(null);
    setStatus(null);
    setRecords(null);
    setLinkSent(false);
  }

  const handleSubmit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSubmitError("");

      const models = modelsInput
        .split(",")
        .map((model) => model.trim())
        .filter(Boolean);

      if (models.length === 0) {
        setSubmitError("Enter at least one model.");
        return;
      }

      try {
        const newRunId = await startRun(
          { suite, domain, models, judgeModel: judgeModel.trim() || undefined },
          token,
        );
        setRunId(newRunId);
        setStatus({ run_id: newRunId, status: "pending", completed: 0, total: 0 });
        setRecords(null);
      } catch (reason) {
        setSubmitError(errorMessage(reason));
      }
    },
    [suite, domain, modelsInput, judgeModel, token],
  );

  if (authLoading) {
    return (
      <main className="min-h-screen bg-[#f7f5ef] text-[#202822]">
        <div className="mx-auto max-w-2xl px-5 py-16">
          <p className="text-sm text-[#62675f]">Loading…</p>
        </div>
      </main>
    );
  }

  if (!token) {
    return (
      <main className="min-h-screen bg-[#f7f5ef] text-[#202822]">
        <div className="mx-auto max-w-2xl px-5 py-16">
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#777970]">
            EvalBench
          </p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">Run a suite</h1>
          <p className="mt-2 text-sm text-[#62675f]">
            Sign in with a magic link to trigger a run.
          </p>

          {authError && <p className="mt-4 text-sm text-[#bd6b65]">{authError}</p>}

          {linkSent ? (
            <p className="mt-6 text-sm text-[#62675f]">
              Check your email for a sign-in link.
            </p>
          ) : (
            <form onSubmit={handleMagicLinkRequest} className="mt-6 flex gap-3">
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className="flex-1 rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
              />
              <button
                type="submit"
                disabled={sendingLink}
                className="rounded-md bg-[#283b32] px-4 py-2 text-sm font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32] disabled:opacity-50"
              >
                {sendingLink ? "Sending…" : "Send sign-in link"}
              </button>
            </form>
          )}
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-[#f7f5ef] text-[#202822]">
      <div className="mx-auto max-w-2xl px-5 py-10">
        <header className="mb-7 flex items-end justify-between gap-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#777970]">
              EvalBench
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight">Run a suite</h1>
          </div>
          <button
            type="button"
            onClick={handleSignOut}
            className="text-sm text-[#777970] underline"
          >
            Sign out
          </button>
        </header>

        <form onSubmit={handleSubmit} className="space-y-4 border-y border-[#dedbd2] py-6">
          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">Suite</span>
            <select
              value={suite}
              onChange={(event) => setSuite(event.target.value)}
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            >
              {suites.map((descriptor) => (
                <option key={descriptor.name} value={descriptor.name}>
                  {descriptor.name}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">Domain</span>
            <select
              value={domain}
              onChange={(event) => setDomain(event.target.value as Domain)}
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            >
              {DOMAINS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">
              Models (comma-separated)
            </span>
            <input
              type="text"
              value={modelsInput}
              onChange={(event) => setModelsInput(event.target.value)}
              placeholder="openai/gpt-4o,anthropic/claude-sonnet-4-5"
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            />
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-[#777970]">
              Judge model (optional)
            </span>
            <input
              type="text"
              value={judgeModel}
              onChange={(event) => setJudgeModel(event.target.value)}
              placeholder="anthropic/claude-sonnet-4-5"
              className="w-full rounded-md border border-[#cbc8be] bg-[#fbfaf6] px-3 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32]"
            />
          </label>

          {submitError && <p className="text-sm text-[#bd6b65]">{submitError}</p>}

          <button
            type="submit"
            disabled={!suite || (status !== null && status.status !== "done" && status.status !== "error")}
            className="rounded-md bg-[#283b32] px-4 py-2 text-sm font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#283b32] disabled:opacity-50"
          >
            Run
          </button>
        </form>

        {status && (
          <section className="mt-6 border-b border-[#dedbd2] py-6">
            <p className="text-xs font-bold uppercase tracking-[0.14em] text-[#777970]">
              Status
            </p>
            {status.status === "error" ? (
              <p className="mt-2 text-sm text-[#bd6b65]">
                Run failed: {status.error}
              </p>
            ) : status.status === "done" ? (
              <p className="mt-2 text-sm text-[#6f9f76]">
                Run complete — {status.total} records.
              </p>
            ) : (
              <p className="mt-2 text-sm text-[#62675f]">
                {status.status === "pending" ? "Starting…" : "Running…"}{" "}
                {status.completed} of {status.total || "?"} complete.
              </p>
            )}
          </section>
        )}

        {records && records.length > 0 && (
          <section className="mt-6">
            <p className="text-xs font-bold uppercase tracking-[0.14em] text-[#777970]">
              Results
            </p>
            <ul className="mt-3 space-y-2 text-sm">
              {records.map((record) => (
                <li
                  key={record.id}
                  className="border-b border-[#e4e1d9] py-2 text-[#30352f]"
                >
                  {record.model} · {record.task_id} ·{" "}
                  {record.error ? `error: ${record.error}` : "ok"}
                </li>
              ))}
            </ul>
            <a
              href={`/?suite=${encodeURIComponent(suite)}`}
              className="mt-4 inline-block text-sm text-[#283b32] underline"
            >
              View in the main dashboard
            </a>
          </section>
        )}
      </div>
    </main>
  );
}
```

- [ ] **Step 3: Manually verify in the browser**

Run: `make api` in one terminal, `make web` in another.
Navigate to `http://localhost:3000/run`. Since `REQUIRE_AUTH` is unset locally, `verify_magic_link`/SSM calls aren't exercised — to manually test the authenticated form, temporarily run `localStorage.setItem("run_token", "anything")` in the browser console (local `verify_token` no-ops when `require_auth` is false, so any token value in the request header works locally too, or omit the header entirely and call `/runs/async` directly since local mode doesn't enforce it). Confirm: the suite dropdown populates from `GET /suites`, the form submits to `/runs/async`, and if a `runner_function_name` isn't configured locally the call will fail — that's expected until cloud deployment (Task 17); the goal of this manual check is confirming the page renders correctly and matches the existing dashboard's visual style, not exercising a live run.

Expected: page renders with the same palette as `/` (off-white background, dark green heading, muted labels, bordered sections), no console errors, suite dropdown populated.

- [ ] **Step 4: Verify it builds**

Run: `cd web && npm run build`
Expected: succeeds, produces `web/out/run/index.html` (static export)

- [ ] **Step 5: Commit**

```bash
git add web/app/run/page.tsx web/next.config.ts
git commit -m "feat: add magic-link-gated interactive run suite page"
```

---

### Task 17: One-time manual setup and db migration

**Files:**
- Create: `docs/cloud-deploy.md`

**Interfaces:**
- Consumes: outputs and resource names from Tasks 9–13 (`db_bucket`, `admin_token_ssm_param`, SES identity, GitHub OIDC role).

- [ ] **Step 1: Write the setup checklist**

Create `docs/cloud-deploy.md`:

```markdown
# EvalBench cloud deployment — one-time setup

Run these once, before the first `git push` to `main` triggers the automated
deploy (`.github/workflows/deploy.yml`).

## 1. Terraform state bucket

    aws s3 mb s3://evalbench-terraform-state-$(aws sts get-caller-identity --query Account --output text) \
      --region us-east-1

Note the bucket name — it's the `TF_STATE_BUCKET` GitHub secret in step 6.

## 2. Migrate the existing local database

    aws s3 mb s3://evalbench-dev-db-$(aws sts get-caller-identity --query Account --output text) \
      --region us-east-1
    aws s3 cp evalbench.db s3://evalbench-dev-db-$(aws sts get-caller-identity --query Account --output text)/evalbench.db

(If Task 10's Terraform apply already created the `db` bucket with a different
exact name, use `terraform output db_bucket` instead of recomputing it.)

## 3. Verify the sender/owner email in SES

    aws ses verify-email-identity --email-address ahadagal@alumni.iu.edu --region us-east-1

Click the verification link AWS emails you. SES starts in sandbox mode, which
only allows sending to verified addresses — since the magic-link sender and
recipient are the same address, this is sufficient; no production-access
request needed.

## 4. Set the admin bearer token

    openssl rand -hex 32
    aws ssm put-parameter \
      --name /evalbench/dev/admin-token \
      --type SecureString \
      --value "<paste the generated token>" \
      --overwrite \
      --region us-east-1

## 5. Set provider API keys and judge model in SSM

    for name in openai anthropic gemini openrouter xai; do
      aws ssm put-parameter \
        --name "/evalbench/dev/${name}-api-key" \
        --type SecureString \
        --value "<your ${name} key>" \
        --overwrite \
        --region us-east-1
    done

    aws ssm put-parameter \
      --name /evalbench/dev/judge-model \
      --type SecureString \
      --value "anthropic/claude-sonnet-4-5" \
      --overwrite \
      --region us-east-1

These are read by Terraform at `apply` time and injected as `runner` Lambda
environment variables — see design correction 4 in the implementation plan.

## 6. GitHub repository secrets

In the repo's Settings → Secrets and variables → Actions, set:

| Secret | Value |
|---|---|
| `AWS_ROLE_ARN` | ARN of `terraform.aws_iam_role.github_deploy` (`terraform output` after first manual apply, or construct as `arn:aws:iam::<account-id>:role/evalbench-dev-github-deploy`) |
| `AWS_ACCOUNT_ID` | Your 12-digit AWS account ID |
| `AWS_REGION` | `us-east-1` |
| `TF_STATE_BUCKET` | The bucket created in step 1 |

## 7. First deploy

The very first `terraform apply` needs to run once locally (with your own AWS
credentials) before the GitHub Actions OIDC role exists to do it — this is a
bootstrapping chicken-and-egg step:

    cd terraform
    terraform init -backend-config="bucket=<state-bucket-from-step-1>"
    terraform apply -var-file=dev.tfvars

After this, every `git push` to `main` re-applies automatically via GitHub
Actions using the OIDC role this first apply created.
```

- [ ] **Step 2: Commit**

```bash
git add docs/cloud-deploy.md
git commit -m "docs: add one-time cloud deployment setup checklist"
```

---

## Post-plan verification

After all 17 tasks:

- [ ] `uv run pytest backend/tests/ -v` — full backend suite passes
- [ ] `cd web && npx tsc --noEmit && npm run build` — frontend type-checks and builds
- [ ] `cd terraform && terraform validate` — full Terraform config is syntactically valid
- [ ] `make api` + `make web` still work exactly as before (local dev unaffected)
- [ ] Walk through `docs/cloud-deploy.md` end to end once, on a real AWS account, before relying on the automated GitHub Actions deploy
