# Cloud deployment + interactive Run Suite page

**Status:** Approved design, pending implementation plan
**Date:** 2026-07-17

## Goal

Today EvalBench only runs locally (`make api`, `make web`, `make run-suite`). This spec covers:

1. Deploying the existing API + web dashboard to AWS at near-zero cost.
2. Adding a new `/run` page that lets you trigger a suite run interactively from the browser (equivalent to `make run-suite SUITE=... DOMAIN=... MODELS=...`), gated so only you can use it.
3. Migrating the existing local `evalbench.db` (189 records, 26 runs, real API spend) into the cloud store without losing it.

Reference architecture: `akashpersetti/twin` (specifically the blog admin's magic-link auth and the overall Lambda/API Gateway/S3/CloudFront/Terraform/GitHub-OIDC deployment pattern). Local dev (`make api`, `make web`, `make run-suite`) is unaffected — everything below is additive.

## Architecture overview

```
                              AWS Cloud (evalbench)

  CloudFront --> S3 (frontend)   Next.js static export
                                   - "/"    results dashboard (public, read-only)
                                   - "/run" interactive run trigger (magic-link gated)

  Browser
    |
    |-- GET  /suites, /results, /runs/{id}, /runs/{id}/status   -- public, read-only
    |-- POST /api/auth/request, GET /api/auth/verify            -- magic link (email-gated)
    `-- POST /runs, POST /runs/async                            -- Bearer admin_token required
              |
              v
     API Gateway (HTTP API) --> Lambda "api"  (FastAPI/Mangum -- existing app.py, extended)
                                     |
                                     | async ("Event") invoke, fire-and-forget
                                     v
                            Lambda "runner"  (execute_run, up to 15 min timeout)
                                     |
                     +---------------+--------------------+
                     v                                     v
           DynamoDB run_status                     S3 evalbench.db (SQLite)
           (run_id, status,                         (pulled at start, pushed
            completed, total)                        back on completion)

  DynamoDB magic_tokens (TTL)   SSM Parameter Store (admin_token, provider API keys, judge model)
  SES (send magic link email)   IAM roles scoped per Lambda
```

Two Lambdas, not one: the **api** Lambda answers HTTP requests fast (must return well under API Gateway's 29s cap). The **runner** Lambda does the actual multi-model eval work in the background (up to 15 min) and is never reachable directly — only the api Lambda can invoke it.

## Auth flow (magic link)

Mirrors twin's blog admin auth exactly:

1. `/run` page loads, checks `localStorage.run_token`. If present, sends it as `Authorization: Bearer <token>` on protected routes.
2. No token: shows an email input (prefilled `ahadagal@alumni.iu.edu`) and a "Send sign-in link" button. Submits `POST /api/auth/request {email}`.
3. Backend checks `email == OWNER_EMAIL` ("ahadagal@alumni.iu.edu"). If it matches, generates a random token, writes `{token, expires_at}` to DynamoDB `magic_tokens` (15 min TTL), and emails the link via SES. Always returns `{"sent": true}` regardless of match, to avoid leaking whether the address is the owner's.
4. Clicking the emailed link (`https://<cloudfront-domain>/run?magic=<token>`) loads the page, which reads `?magic=` and calls `GET /api/auth/verify?token=...`.
5. Backend does a one-time, race-safe delete-if-exists lookup against DynamoDB. If valid and unexpired, returns the static `admin_token` from SSM.
6. Page stores `admin_token` in `localStorage`, strips `?magic=` from the URL, and unlocks the run form.
7. Every `POST /runs/async` call sends `Authorization: Bearer <admin_token>`. The api Lambda's `verify_token` dependency compares it against the same SSM param.

**Gap closed in review:** the existing synchronous `POST /runs` is unauthenticated today, which is fine for localhost-only trust but would leave a public, cost-incurring endpoint open once deployed — anyone with the API Gateway URL could trigger runs and spend provider API credits, bypassing the magic-link gate entirely. Fix: `verify_token` gates `POST /runs` too, but only when `REQUIRE_AUTH=true` — an env var set on the Lambda (via Terraform) and left unset for local `make api`. Local dev stays frictionless; the cloud deployment requires the same Bearer token on every mutating route, sync or async.

SES starts in sandbox mode, which only allows sending to verified addresses. Since sender and recipient are both `ahadagal@alumni.iu.edu`, this works without requesting SES production access — just a one-time email verification click.

## Async run execution + storage

**`POST /runs/async`** (Bearer-protected):
1. Validates `RunConfig` (suite/domain/models/judge_model) — same validation as today's synchronous `/runs`.
2. Generates `run_id`.
3. Writes a DynamoDB `run_status` item: `{run_id, status: "pending", completed: 0, total: len(tasks)*len(models), created_at}`.
4. Invokes the runner Lambda asynchronously (`InvocationType: Event`).
5. Returns `{"run_id": ...}` immediately.

**Runner Lambda** (new handler wrapping `execute_run`):
1. Sets `run_status.status = "running"`.
2. Downloads `evalbench.db` from S3 to `/tmp/evalbench.db`, points `DATABASE_URL` at it.
3. Runs `execute_run` — unchanged core logic and concurrency semaphore.
4. As each task/model pair completes, increments `run_status.completed` in DynamoDB (single-item update — cheap, no file contention).
5. On completion: `save_records` writes to the local SQLite file, uploads it back to S3, sets `run_status.status = "done"`. On unhandled exception: `status = "error"` with the error message stored on the item.

**`GET /runs/{run_id}/status`** (public): returns the DynamoDB item — `{status, completed, total, error?}`. The `/run` page polls this every few seconds while pending/running, then does one final `GET /runs/{run_id}` for the finished records.

Why DynamoDB for progress instead of re-uploading SQLite per record: progress lives in a single-item DynamoDB update (no race, low latency). The SQLite file itself is touched once per run (download at start, upload at end), so the round-trip concurrency risk only materializes if two runs are uploading at literally the same instant — made unlikely by the fact that only one person (you) can trigger runs at all.

## `/run` page (frontend)

New route `web/app/run/page.tsx`, client component, following the shape of twin's `blog/page.tsx`.

**Unauthenticated state:** email input (prefilled `ahadagal@alumni.iu.edu`), "Send sign-in link" button. After submit, shows "Check your email" with no success/failure distinction.

**Authenticated state**, a form mirroring `make run-suite`'s knobs:
- **Suite** — dropdown from `GET /suites`
- **Domain** — dropdown: overall / software / finance / legal / medical / physics
- **Models** — comma-separated free-text input (matches `MODELS="openai/gpt-4o,anthropic/claude-sonnet-4-5"` today)
- **Judge model** — optional override, defaults to the configured judge model
- **Run** button — `POST /runs/async` with Bearer token, receives `run_id`

Client-side validation: reject model names not present in `MODEL_PRICING` (`config.py`) before submit, so a typo doesn't silently run untracked-cost calls.

**While running:** progress bar / "N of M complete" from polling `GET /runs/{run_id}/status`. Sign-out button (clears `localStorage`) always visible.

**On completion:** renders the finished run inline, reusing the existing `Leaderboard`/`ModelBarChart` components against `GET /runs/{run_id}`, with a link back to the main dashboard filtered to this suite.

**Visual design:** matches the existing dashboard exactly — no new design system. Warm off-white background (`#f7f5ef`), dark green-gray text (`#202822`), muted labels (`#777970`/`#62675f`), thin borders (`#dedbd2`/`#cbc8be`/`#e4e1d9`), dark green accent/buttons (`#283b32`), uppercase-tracked eyebrow labels, `rounded-md` inputs with the same `focus-visible:outline-[#283b32]` treatment already used in `page.tsx`'s suite selector and filter controls. Plain Tailwind utilities, no component library, consistent with the rest of `web/`.

## Infra / Terraform / CI-CD

New `terraform/` directory (mirrors twin's layout: `main.tf`, `variables.tf`, `outputs.tf`, `backend.tf`, `versions.tf`, `dev.tfvars`).

**Resources:**
- `aws_s3_bucket.frontend` + `aws_cloudfront_distribution.main` — static Next.js export, HTTPS
- `aws_s3_bucket.db` — private, holds `evalbench.db`
- `aws_dynamodb_table.magic_tokens` — PAY_PER_REQUEST, TTL on `expires_at`
- `aws_dynamodb_table.run_status` — PAY_PER_REQUEST, hash key `run_id`
- `aws_apigatewayv2_api.main` — HTTP API, CORS enabled, proxy integration to the `api` Lambda
- `aws_lambda_function.api` — FastAPI/Mangum; handles `/suites`, `/results`, `/runs/{id}`, `/runs/{id}/status`, `/runs`, `/runs/async`, `/api/auth/*`; `REQUIRE_AUTH=true` set here
- `aws_lambda_function.runner` — the async `execute_run` worker, 15 min timeout, invoked only by the api Lambda (no API Gateway route)
- `aws_ses_email_identity` — `ahadagal@alumni.iu.edu`
- SSM `SecureString` params: `admin_token` (set manually, not in Terraform state), `openai_api_key`, `anthropic_api_key`, `gemini_api_key`, `openrouter_api_key`, `xai_api_key`, `judge_model`
- IAM: `api` Lambda role (SSM read on its params, DynamoDB rw on both tables, S3 rw on db bucket, SES send, `lambda:InvokeFunction` on runner only); `runner` Lambda role (S3 rw on db bucket, DynamoDB rw on `run_status`, SSM read on provider keys + judge model only)
- GitHub OIDC provider + deploy role scoped to this repo

**`.github/workflows/deploy.yml`** — on push to `main`: checkout, assume AWS role via OIDC, build both Lambda zips, `terraform apply`, `npm run build` (Next static export), `aws s3 sync` frontend, CloudFront invalidation.

**Domain:** plain AWS URLs (CloudFront domain + API Gateway invoke URL) — no custom domain/Route 53/ACM for now.

## One-time manual setup

1. `aws s3 mb` the Terraform state bucket.
2. `aws s3 cp evalbench.db s3://<db-bucket>/evalbench.db` — migrates the existing 189 records / 26 runs. Same schema as the cloud store (`MetricRecordRow`), so no transformation needed.
3. Verify `ahadagal@alumni.iu.edu` in SES (click the verification email).
4. `aws ssm put-parameter` for `admin_token` (generate with `secrets.token_hex(32)`) and each provider API key.
5. Set GitHub secrets `AWS_ROLE_ARN`, `AWS_ACCOUNT_ID`, `AWS_REGION` for OIDC.

## Out of scope

- Custom domain (can be added later following twin's `prod.tfvars` pattern).
- Multi-user auth (single owner email only, matching twin's blog admin).
- Migrating away from SQLite to a different database engine.
- Changes to the existing synchronous `/runs` route or local dev workflow — both untouched.
