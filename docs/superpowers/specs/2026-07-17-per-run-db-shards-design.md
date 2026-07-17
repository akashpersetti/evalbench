# Per-run DB shards — design

## Problem

`db_sync.py` shares one S3 object (`evalbench.db`) across every run. Each
runner Lambda downloads the whole file, writes locally, then does a blind
`upload_file` back to the same key. Under concurrent runs (exactly what the
batch-run feature produces — `docs/superpowers/specs/2026-07-17-batch-run-design.md`)
this is last-writer-wins: whichever run finishes last clobbers every other
run's uploaded records with no merge. Confirmed against real batch data —
three concurrent runs (software/finance/legal) left only the
last-to-finish run's 8 records; the other two runs' records were completely
gone (`GET /runs/{run_id}` → 404).

Any batch with more than one concurrently-running run is unsafe today.

## Goals

- Concurrent runs never lose each other's data, regardless of finish order.
- No new locking, retry, or conditional-write logic to get right.
- `query_records`/`aggregate_records` and the `/results` aggregate view keep
  working unchanged across all historical runs.

## Non-goals

- No caching/incremental-merge layer for `/results` in this pass — see
  Follow-ups.
- No change to `run_status` (DynamoDB) — that's already per-run and
  unaffected by this race.
- No retention/archival of old run shards — deferred, see Follow-ups.

## Design

### Write path: one S3 key per run

Each run writes to its own object: `runs/{run_id}.db` instead of the shared
`evalbench.db`. Different runs never touch the same S3 object, so the race
is eliminated by construction — no locking, no ETag/conditional-write
handling needed.

`runner_lambda.py` no longer calls `download_db` before running.
`runner.py:execute_run` only calls `save_records` once, at the end
(confirmed — no code path reads pre-existing rows mid-run), so there is
nothing to download first. `init_db` creates the schema on the fresh local
file. The final `upload_db` call targets `runs/{run_id}.db`.

### Read path: single run

`GET /runs/{run_id}` (`raw_run` in `app.py`) downloads only
`runs/{run_id}.db` and queries it directly — no merge across other runs.
This is the exact endpoint that 404'd in the incident; it now can't be
affected by any other run's upload, in flight or otherwise.

If the run hasn't finished yet (no shard uploaded), `download_db` no-ops as
it does today and the query returns no rows — `raw_run` keeps its existing
"no records → 404" behavior, which is correct (poll `/runs/{run_id}/status`
for in-progress state; `raw_run` is only meant for completed runs).

### Read path: aggregate (`/results`)

`/results` filters across every historical run matching a suite (and
optionally domain/window/family). It needs the union of all run shards, not
one.

New `db_sync.py` helper:

```python
def merge_all_runs(bucket: str, prefix: str, local_path: Path) -> None:
    """Download every run shard under prefix and merge into one local SQLite file."""
```

Behavior:

1. `list_objects_v2` under `prefix` (`runs/`) to get every shard key.
2. Download all shards to temp files, bounded concurrency (16 parallel —
   matches the workflow-tool concurrency cap elsewhere in this codebase as a
   reasonable default, not a hard requirement).
3. Create `local_path` fresh, run `init_db` on it for schema.
4. For each downloaded shard: `ATTACH DATABASE '<shard>' AS shard;
   INSERT INTO metric_records SELECT * FROM shard.metric_records; DETACH
   DATABASE shard;` — primary-key `id` is a UUID per record
   (`MetricRecordRow.id`), so no collision risk across shards.
5. Return; caller points a normal engine/session factory at `local_path` and
   uses `query_records`/`aggregate_records` completely unchanged.

`get_session_factory` in `app.py` calls `merge_all_runs` instead of the old
single-key `download_db` when building the session factory used by
`/results`. `raw_run` gets its own narrower dependency that downloads just
the one shard (see Read path: single run above) rather than the merged
view — it never needed cross-run data.

No caching between requests: every `/results` call re-lists and re-merges
every shard, same "always fresh, always fully re-fetched" behavior the
current single-file design already has. Cost is driven by S3 API call count
(list + N parallel GETs), not row volume, since each shard is small (one
run's records). Estimated: ~1s at ~100 accumulated runs, ~5-10s at ~1,000,
degrading past that. Acceptable at current scale; see Follow-ups.

### Config / infra

- `backend/evalbench/config.py`: `s3_db_key: str = "evalbench.db"` →
  `s3_db_prefix: str = "runs/"`.
- `terraform/main.tf:403,448`: `S3_DB_KEY = "evalbench.db"` →
  `S3_DB_PREFIX = "runs/"` on both the api Lambda and runner Lambda
  environment blocks.

## Error handling

- Shard missing (run not finished / never started) — `raw_run` returns 404,
  same as today's "no records for this run" path.
- `merge_all_runs` with zero shards under the prefix (fresh deployment) —
  `local_path` still gets `init_db`'d, so `/results` gets an empty-but-valid
  database rather than an error. Matches current no-op behavior of
  `download_db` against a missing key.
- A shard download failing mid-merge (transient S3 error) — let it raise;
  `/results` returns 500, same failure visibility as any other unhandled
  exception in this codebase today (no existing retry wrapper to match).

## Testing

Extend `backend/tests/test_cloud.py` (moto, same pattern as existing
`db_sync` tests):

- Uploading two different runs' shards writes two distinct S3 objects
  (`runs/{run_id_a}.db`, `runs/{run_id_b}.db`) — proves no shared-key
  collision.
- `merge_all_runs` against N uploaded shards produces a local SQLite file
  whose `metric_records` is the union of all N shards' rows.
- `merge_all_runs` against zero shards produces a valid, empty, queryable
  database (schema present, no rows) rather than raising.
- Downloading a shard for a `run_id` with no uploaded object no-ops (mirrors
  existing `test_download_db_leaves_local_path_absent_when_object_missing`).

`_start_run`/batch fan-out tests (`test_cloud.py` coverage described in
`2026-07-17-batch-run-design.md`) are untouched — this change is isolated
to the sync layer.

Frontend: no changes required — `/runs/{run_id}` and `/results` response
shapes are unchanged, only their backing storage is.

## Follow-ups (not in this pass)

- **Retention/archival for `/results` merge cost.** Once accumulated run
  count approaches ~1-2k, list+merge-every-request will start to show up as
  latency. Cheapest fix: bound the merge to a retention window (e.g. only
  list/merge shards from the last N days — `/results` already accepts
  `window_days`) or periodically archive old shards out of the `runs/`
  prefix. Not built now — deferred until it's a measured problem, per this
  design's non-goals.
- **Judge failure on `error=JudgeResponseError`** (software suite, ~75% of
  records in the incident batch) is a separate, pre-existing bug in the
  judge response path, unrelated to storage. Needs raw judge-response
  logging added before it can be diagnosed. Out of scope for this design.
