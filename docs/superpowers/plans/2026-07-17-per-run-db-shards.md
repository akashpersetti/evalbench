# Per-run DB shards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop concurrent batch runs from clobbering each other's data by giving every run its own S3 object instead of sharing one SQLite file.

**Architecture:** Each runner Lambda writes its run's records to `runs/{run_id}.db` in S3 instead of a single shared `evalbench.db` key — different runs can never collide because they never touch the same object. `GET /runs/{run_id}` downloads just that one shard. `GET /results` (which aggregates across every historical run) downloads every shard under the `runs/` prefix and merges them into one local SQLite file via `ATTACH DATABASE` + `INSERT ... SELECT`, then hands that file to the existing unchanged query/aggregate functions.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async + aiosqlite, boto3 (S3), sqlite3 stdlib (for the merge step), pytest + moto for tests, Terraform for Lambda env vars.

## Global Constraints

- `s3_db_key: str = "evalbench.db"` in `backend/evalbench/config.py` becomes `s3_db_prefix: str = "runs/"` — spec: [2026-07-17-per-run-db-shards-design.md](../specs/2026-07-17-per-run-db-shards-design.md), "Config / infra".
- Merge concurrency for parallel shard downloads: 16 (spec: "Read path: aggregate").
- No caching/incremental-merge layer in this pass — every `/results` call re-lists and re-merges every shard (spec non-goals).
- No retention/archival of old shards in this pass (spec non-goals / follow-ups).
- `metric_records.id` is a UUID per record (confirmed at `backend/evalbench/runner.py:549`), so merged inserts across shards can never collide on primary key.
- `runner.py:execute_run` only calls `save_records` once, at the end (confirmed at `backend/evalbench/runner.py:640`) — no code path reads pre-existing rows mid-run, so the runner Lambda's write path needs no download-before-write step.

---

### Task 1: `db_sync.py` — per-run key helper and shard merge

**Files:**
- Modify: `backend/evalbench/cloud/db_sync.py`
- Test: `backend/tests/test_cloud.py`

**Interfaces:**
- Consumes: nothing new (boto3, stdlib only).
- Produces:
  - `run_db_key(prefix: str, run_id: str) -> str` — returns `f"{prefix}{run_id}.db"`. Used by Task 2 (runner_lambda upload) and Task 3 (app.py single-run download).
  - `merge_all_runs(bucket: str, prefix: str, local_path: Path) -> None` — downloads every object under `prefix`, merges their `metric_records` tables into `local_path`. If no objects exist under `prefix`, leaves `local_path` absent (same no-op contract as the existing `download_db`). Used by Task 3.
  - `download_db(bucket: str, key: str, local_path: Path) -> None` — unchanged, existing function.
  - `upload_db(bucket: str, key: str, local_path: Path) -> None` — unchanged, existing function.

- [ ] **Step 1: Write the failing test for `run_db_key`**

Add to `backend/tests/test_cloud.py` (near the top, after the existing `download_db`/`upload_db` tests):

```python
def test_run_db_key_joins_prefix_and_run_id():
    assert db_sync.run_db_key("runs/", "abc-123") == "runs/abc-123.db"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest backend/tests/test_cloud.py::test_run_db_key_joins_prefix_and_run_id -v`
Expected: FAIL with `AttributeError: module 'evalbench.cloud.db_sync' has no attribute 'run_db_key'`

- [ ] **Step 3: Implement `run_db_key`**

In `backend/evalbench/cloud/db_sync.py`, add after `download_db`/`upload_db`:

```python
def run_db_key(prefix: str, run_id: str) -> str:
    """Return the S3 key for one run's shard file."""
    return f"{prefix}{run_id}.db"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest backend/tests/test_cloud.py::test_run_db_key_joins_prefix_and_run_id -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for `merge_all_runs` with zero shards**

Add to `backend/tests/test_cloud.py`:

```python
@mock_aws
def test_merge_all_runs_no_shards_leaves_local_path_absent(tmp_path):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

    local_path = tmp_path / "merged.db"
    db_sync.merge_all_runs(BUCKET, "runs/", local_path)

    assert not local_path.exists()
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/pytest backend/tests/test_cloud.py::test_merge_all_runs_no_shards_leaves_local_path_absent -v`
Expected: FAIL with `AttributeError: module 'evalbench.cloud.db_sync' has no attribute 'merge_all_runs'`

- [ ] **Step 7: Write the failing test for `merge_all_runs` with multiple shards**

Add to `backend/tests/test_cloud.py`. This test builds two tiny real SQLite files (each with one row of a minimal `metric_records`-shaped table) and asserts the merge produces the union:

```python
import sqlite3


def _write_shard_db(path, row_id, run_id):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE metric_records (id TEXT PRIMARY KEY, run_id TEXT, value TEXT)"
    )
    connection.execute(
        "INSERT INTO metric_records VALUES (?, ?, ?)", (row_id, run_id, "x")
    )
    connection.commit()
    connection.close()


@mock_aws
def test_merge_all_runs_unions_rows_across_shards(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)

    shard_a = tmp_path / "a.db"
    shard_b = tmp_path / "b.db"
    _write_shard_db(shard_a, "row-a", "run-a")
    _write_shard_db(shard_b, "row-b", "run-b")
    client.upload_file(str(shard_a), BUCKET, "runs/run-a.db")
    client.upload_file(str(shard_b), BUCKET, "runs/run-b.db")

    local_path = tmp_path / "merged.db"
    db_sync.merge_all_runs(BUCKET, "runs/", local_path)

    connection = sqlite3.connect(local_path)
    rows = connection.execute(
        "SELECT id, run_id FROM metric_records ORDER BY id"
    ).fetchall()
    connection.close()

    assert rows == [("row-a", "run-a"), ("row-b", "run-b")]
```

- [ ] **Step 8: Run both new merge tests to verify they fail**

Run: `.venv/bin/pytest backend/tests/test_cloud.py -k merge_all_runs -v`
Expected: both FAIL with `AttributeError`

- [ ] **Step 9: Implement `merge_all_runs`**

In `backend/evalbench/cloud/db_sync.py`, replace the file contents with:

```python
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
                connection.execute("DETACH DATABASE shard")
            connection.commit()
        finally:
            connection.close()
```

- [ ] **Step 10: Run all `test_cloud.py` tests to verify they pass**

Run: `.venv/bin/pytest backend/tests/test_cloud.py -v`
Expected: all PASS (19 pre-existing + 3 new = 22)

- [ ] **Step 11: Commit**

```bash
git add backend/evalbench/cloud/db_sync.py backend/tests/test_cloud.py
git commit -m "feat: add per-run S3 key helper and shard merge to db_sync"
```

---

### Task 2: Config rename `s3_db_key` → `s3_db_prefix`

**Files:**
- Modify: `backend/evalbench/config.py:29`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.s3_db_prefix: str` (default `"runs/"`) — consumed by Task 3 (`app.py`) and Task 4 (`runner_lambda.py`).

- [ ] **Step 1: Rename the field**

In `backend/evalbench/config.py`, change:

```python
    s3_db_key: str = "evalbench.db"
```

to:

```python
    s3_db_prefix: str = "runs/"
```

- [ ] **Step 2: Search for any remaining `s3_db_key` references**

Run: `grep -rn "s3_db_key" backend/evalbench`
Expected: no output (Tasks 3 and 4 below update the two files that reference it; if this is run before those tasks, `app.py` and `runner_lambda.py` will still show matches — that's expected until those tasks land)

- [ ] **Step 3: Commit**

```bash
git add backend/evalbench/config.py
git commit -m "feat: rename s3_db_key setting to s3_db_prefix"
```

---

### Task 3: `app.py` — per-run read path and merged aggregate read path

**Files:**
- Modify: `backend/evalbench/api/app.py:87-107` (`get_session_factory`), `backend/evalbench/api/app.py:298-306` (`raw_run`)

**Interfaces:**
- Consumes: `db_sync.run_db_key(prefix, run_id)` and `db_sync.merge_all_runs(bucket, prefix, local_path)` from Task 1; `settings.s3_db_prefix` from Task 2.
- Produces: `get_run_session_factory(run_id: str) -> SessionFactory` — new FastAPI dependency, scoped to one run's shard. `get_session_factory` keeps its existing name and signature (used by `/results` and `/runs`) but its cloud branch now merges all shards instead of downloading one shared key.

- [ ] **Step 1: Update `get_session_factory`'s cloud branch to merge all shards**

In `backend/evalbench/api/app.py`, replace:

```python
async def get_session_factory() -> SessionFactory:
    """Return the session factory, re-downloading from S3 in cloud mode."""
    settings = get_settings()

    # In cloud mode, re-download the database on every request for freshness
    if settings.s3_db_bucket:
        from evalbench.cloud import db_sync

        db_path = Path(tempfile.gettempdir()) / "evalbench_cloud.db"
        db_sync.download_db(settings.s3_db_bucket, settings.s3_db_key, db_path)
        # Create an engine pointing to the just-downloaded database
        cloud_engine = create_engine(
            database_url=f"sqlite+aiosqlite:///{db_path}"
        )
        # download_db no-ops when the S3 object doesn't exist yet (fresh
        # deployment, or a run still in flight before its first upload), so
        # the schema may be missing on the just-downloaded file.
        await init_db(cloud_engine)
        return create_session_factory(cloud_engine)

    return default_session_factory
```

with:

```python
async def get_session_factory() -> SessionFactory:
    """Return a session factory over every run's merged data, in cloud mode."""
    settings = get_settings()

    # In cloud mode, re-merge every run's shard on every request for freshness
    if settings.s3_db_bucket:
        from evalbench.cloud import db_sync

        db_path = Path(tempfile.gettempdir()) / "evalbench_cloud.db"
        db_sync.merge_all_runs(settings.s3_db_bucket, settings.s3_db_prefix, db_path)
        # Create an engine pointing to the just-merged database
        cloud_engine = create_engine(
            database_url=f"sqlite+aiosqlite:///{db_path}"
        )
        # merge_all_runs no-ops when no shards exist yet (fresh deployment,
        # or every run still in flight before its first upload), so the
        # schema may be missing on the just-merged file.
        await init_db(cloud_engine)
        return create_session_factory(cloud_engine)

    return default_session_factory


async def get_run_session_factory(run_id: str) -> SessionFactory:
    """Return a session factory scoped to one run's S3 shard, in cloud mode."""
    settings = get_settings()

    if settings.s3_db_bucket:
        from evalbench.cloud import db_sync

        db_path = Path(tempfile.gettempdir()) / f"evalbench_run_{run_id}.db"
        db_sync.download_db(
            settings.s3_db_bucket,
            db_sync.run_db_key(settings.s3_db_prefix, run_id),
            db_path,
        )
        cloud_engine = create_engine(
            database_url=f"sqlite+aiosqlite:///{db_path}"
        )
        # download_db no-ops when this run's shard doesn't exist yet (run
        # still in flight, or never started), so the schema may be missing.
        await init_db(cloud_engine)
        return create_session_factory(cloud_engine)

    return default_session_factory
```

- [ ] **Step 2: Point `raw_run` at the new per-run dependency**

In `backend/evalbench/api/app.py`, find:

```python
@app.get("/runs/{run_id}", response_model=list[MetricRecord])
async def raw_run(
    run_id: str,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> list[MetricRecord]:
```

Change `Depends(get_session_factory)` to `Depends(get_run_session_factory)`:

```python
@app.get("/runs/{run_id}", response_model=list[MetricRecord])
async def raw_run(
    run_id: str,
    session_factory: Annotated[SessionFactory, Depends(get_run_session_factory)],
) -> list[MetricRecord]:
```

(The rest of the function body — calling `get_run_records` and raising 404 on empty — is unchanged.)

- [ ] **Step 3: Verify the module still imports cleanly**

Run: `.venv/bin/python -c "from evalbench.api import app"`
Expected: no output, exit code 0

- [ ] **Step 4: Run the full test suite to catch any regressions**

Run: `.venv/bin/pytest backend/tests -v`
Expected: all PASS (no test in this repo directly exercises `app.py` via `TestClient` today, so this mainly confirms nothing else broke)

- [ ] **Step 5: Commit**

```bash
git add backend/evalbench/api/app.py
git commit -m "feat: read /runs/{run_id} from its own shard, merge all shards for /results"
```

---

### Task 4: `runner_lambda.py` — write to per-run shard, drop pre-run download

**Files:**
- Modify: `backend/evalbench/runner_lambda.py`

**Interfaces:**
- Consumes: `db_sync.run_db_key(prefix, run_id)` from Task 1; `settings.s3_db_prefix` from Task 2.
- Produces: nothing new (this is the write-side endpoint of the flow; Task 3's `get_run_session_factory` reads what this task writes).

- [ ] **Step 1: Remove the pre-run download and switch upload to the per-run key**

Current `backend/evalbench/runner_lambda.py`:

```python
async def _run(run_id: str, config: RunConfig) -> dict[str, str]:
    settings = get_settings()
    db_sync.download_db(settings.s3_db_bucket, settings.s3_db_key, _LOCAL_DB_PATH)
    run_status.set_running(settings.dynamodb_run_status_table, run_id)

    engine = create_engine(f"sqlite+aiosqlite:///{_LOCAL_DB_PATH}")
    await init_db(engine)
    factory = create_session_factory(engine)

    def on_progress(_completed: int, _total: int) -> None:
        run_status.increment_completed(settings.dynamodb_run_status_table, run_id)

    try:
        await execute_run(
            config,
            session_factory=factory,
            run_id=run_id,
            on_progress=on_progress,
        )
    except Exception as exc:
        run_status.set_error(settings.dynamodb_run_status_table, run_id, str(exc))
        raise
    finally:
        await engine.dispose()

    db_sync.upload_db(settings.s3_db_bucket, settings.s3_db_key, _LOCAL_DB_PATH)
    run_status.set_done(settings.dynamodb_run_status_table, run_id)
    return {"run_id": run_id}
```

Replace with:

```python
async def _run(run_id: str, config: RunConfig) -> dict[str, str]:
    settings = get_settings()
    run_status.set_running(settings.dynamodb_run_status_table, run_id)

    engine = create_engine(f"sqlite+aiosqlite:///{_LOCAL_DB_PATH}")
    await init_db(engine)
    factory = create_session_factory(engine)

    def on_progress(_completed: int, _total: int) -> None:
        run_status.increment_completed(settings.dynamodb_run_status_table, run_id)

    try:
        await execute_run(
            config,
            session_factory=factory,
            run_id=run_id,
            on_progress=on_progress,
        )
    except Exception as exc:
        run_status.set_error(settings.dynamodb_run_status_table, run_id, str(exc))
        raise
    finally:
        await engine.dispose()

    db_sync.upload_db(
        settings.s3_db_bucket,
        db_sync.run_db_key(settings.s3_db_prefix, run_id),
        _LOCAL_DB_PATH,
    )
    run_status.set_done(settings.dynamodb_run_status_table, run_id)
    return {"run_id": run_id}
```

(No download is needed first: `execute_run` only calls `save_records` once, at the end, and `init_db` creates the schema fresh on `_LOCAL_DB_PATH` — there is nothing to download.)

- [ ] **Step 2: Verify the module still imports cleanly**

Run: `.venv/bin/python -c "from evalbench import runner_lambda"`
Expected: no output, exit code 0

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/pytest backend/tests -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add backend/evalbench/runner_lambda.py
git commit -m "feat: write run records to a per-run S3 shard instead of a shared key"
```

---

### Task 5: Terraform — rename Lambda env var on both functions

**Files:**
- Modify: `terraform/main.tf:403` (api Lambda), `terraform/main.tf:448` (runner Lambda)

**Interfaces:**
- Consumes: nothing.
- Produces: `S3_DB_PREFIX` env var on both Lambdas, matching `Settings.s3_db_prefix`'s env var name (pydantic-settings uppercases the field name by default — confirm this matches the existing `S3_DB_BUCKET`/`S3_DB_KEY` convention already in use).

- [ ] **Step 1: Update the api Lambda's environment block**

In `terraform/main.tf`, around line 403, change:

```hcl
      S3_DB_BUCKET                = aws_s3_bucket.db.id
      S3_DB_KEY                   = "evalbench.db"
```

to:

```hcl
      S3_DB_BUCKET                = aws_s3_bucket.db.id
      S3_DB_PREFIX                = "runs/"
```

- [ ] **Step 2: Update the runner Lambda's environment block**

Around line 448, change:

```hcl
      S3_DB_BUCKET              = aws_s3_bucket.db.id
      S3_DB_KEY                 = "evalbench.db"
```

to:

```hcl
      S3_DB_BUCKET              = aws_s3_bucket.db.id
      S3_DB_PREFIX              = "runs/"
```

- [ ] **Step 3: Validate Terraform syntax**

Run: `terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid.`

(If AWS credentials aren't configured in this environment and `validate` fails on provider init rather than syntax, run `terraform -chdir=terraform fmt -check` instead to confirm the HCL is well-formed, and note in the commit that `validate` needs to be run where credentials are available before `terraform apply`.)

- [ ] **Step 4: Commit**

```bash
git add terraform/main.tf
git commit -m "feat: rename S3_DB_KEY to S3_DB_PREFIX on api and runner Lambda env"
```

---

### Task 6: Final full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `.venv/bin/pytest backend/tests -v`
Expected: all PASS

- [ ] **Step 2: Confirm no remaining references to the old config field**

Run: `grep -rn "s3_db_key" backend/evalbench terraform`
Expected: no output

- [ ] **Step 3: Confirm no remaining references to the old shared-file constant usage pattern**

Run: `grep -rn '"evalbench.db"' backend/evalbench terraform`
Expected: no output outside of `config.py`'s local-mode `database_url` default (`sqlite+aiosqlite:///./evalbench.db`), which is unrelated to S3 sync and stays as-is

