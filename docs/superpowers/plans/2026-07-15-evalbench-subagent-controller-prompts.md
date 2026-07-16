# EvalBench Subagent-Driven Controller Prompts

Copy one prompt into a fresh Codex controller session. Run the phases in order and use `/clear` between them. Each controller prompt is self-contained and deliberately ends at its phase boundary. The controller is running in the checked-out repository root; resolve all paths relative to that root and do not assume a particular filesystem location.

Global model requirement for every prompt below: use GPT-5.6 Luna with high reasoning effort for the controller, every implementer, every reviewer, every fixer, and every whole-phase review. Treat this requirement as higher priority than model-routing language in the phase plan. Request that model explicitly on every dispatch. If the cloud surface does not expose a model selector or rejects that exact model label, record the limitation in the durable report and use the strongest available model with high reasoning effort rather than silently selecting a lower tier.

If Superpowers or the `superpowers:subagent-driven-development` skill is unavailable in the cloud session, manually reproduce its workflow with the available subagent mechanism: one fresh implementer per numbered task, one fresh spec-and-quality reviewer after each task, a fixer plus re-review for every Critical/Important finding, and one strongest available whole-phase reviewer at the end. Do not skip the loop merely because the skill is unavailable.

## Phase 1 controller prompt

```text
/using-superpowers
/subagent-driven-development

Model requirement: use GPT-5.6 Luna with high reasoning effort for this controller and every implementer, reviewer, fixer, and whole-phase reviewer. Request it explicitly on every dispatch, regardless of the phase plan's routing. If the cloud surface cannot select or recognize that exact model, record the limitation and use the strongest available model with high reasoning effort.

If Superpowers or the named skill is unavailable, manually execute the same loop with the available subagent mechanism: fresh implementer per task, fresh reviewer after each task, fixer plus re-review for every Critical/Important finding, and strongest available whole-phase reviewer.

You are the controller for EvalBench Phase 1 only. Work in the checked-out repository root.

Read these files in full before any implementation action:
1. SPEC.md
2. docs/superpowers/plans/2026-07-15-evalbench-phase-1-core.md
3. The complete superpowers:subagent-driven-development SKILL.md and every prompt/template it requires.

Use superpowers:subagent-driven-development for the entire phase. You are a controller, not the implementer. Dispatch a fresh implementer agent for every numbered Task in the Phase 1 plan, followed by a fresh task reviewer that checks both spec compliance and code quality. Critical or Important findings must go to a fixer and then through re-review before the task is marked complete. After all tasks, dispatch one strongest-available whole-phase reviewer against SPEC.md and the Phase 1 plan. Do not pause between tasks unless genuinely blocked, a plan/spec conflict requires user judgment, or the phase is complete.

Preflight:
- Inspect git status and branch without changing user work.
- Inspect HEAD before Task 1. If there is no valid HEAD, create a baseline commit containing only SPEC.md and docs/superpowers/plans/*.md, with message: docs: add evalbench specification and execution plans. If HEAD already exists, preserve it and do not create a redundant baseline commit. Do not include implementation files in a new baseline.
- Scan the Phase 1 plan once for internal conflicts. Batch any genuine conflicts into one question before dispatching Task 1. If clean, proceed without asking.
- Initialize or resume .superpowers/sdd/progress.md. Trust completed ledger entries and git history; never redispatch completed tasks.
- Record the phase baseline SHA after the documentation baseline commit.

For every Task N:
1. Record BASE_SHA=$(git rev-parse HEAD).
2. Use the subagent-driven-development task-brief script to extract Task N from the Phase 1 plan. The brief is the implementer's complete requirements; do not paste the whole plan into the dispatch.
3. Dispatch a fresh implementer with the official implementer template, exact brief/report paths, repository directory, and only the earlier interfaces that this task consumes. Require TDD wherever the task says so, focused RED/GREEN evidence, full relevant suite before commit, a commit, self-review, and a durable report file.
4. Generate review-package from the recorded BASE_SHA to current HEAD. Dispatch a fresh reviewer with the official task-reviewer template, brief/report/diff paths, and the Phase 1 Global Constraints copied verbatim.
5. Resolve every “cannot verify” item yourself. Send all Critical/Important findings to a fixer with the covering tests named; require appended test evidence and re-review. Record Minor findings in the ledger for the final reviewer.
6. Mark the task complete in the ledger only after clean spec and quality verdicts.

Model routing when selectable:
- Use GPT-5.6 Luna with high reasoning effort for every controller and subagent dispatch, including mechanical edits and all reviews. Do not downgrade any dispatch based on task size.

Non-negotiable execution rules:
- SPEC.md is the source of truth. Do not redesign or expand scope.
- Preserve the exact MetricRecord and Suite contracts and exact repository structure from the plan.
- No real LiteLLM calls in tests. Do not make a paid external model call unless the user separately authorizes spend and the needed environment keys already exist.
- Do not log or expose secrets. Never inspect or print secret values.
- Preserve unrelated/user changes. Never use destructive git commands.
- Commit each task as the plan requires. Do not push or create a PR during the task loop unless the controller explicitly authorizes publication.

Whole-phase gate:
- Run every Phase 1 verification command fresh, including backend tests, frontend lint/build, git diff checks, secret hygiene, and separate startup smoke tests for make api and make web.
- Create a whole-phase review package from PHASE_BASE_SHA to HEAD and dispatch the strongest-available final reviewer against SPEC.md plus the Phase 1 plan.
- Send the complete final-review findings list to one fixer, re-run affected/full gates, and re-review until no Critical/Important findings remain.
- Confirm the working tree contains no accidental implementation changes beyond Phase 1 scope. Do not hide the untracked database or env file by staging it.
- Do not create a branch or PR merely for ceremony. Publish only when the controller or user explicitly requests it.

Final response must report: DONE or BLOCKED; final commit hash; task ledger summary; exact test/lint/build/smoke commands and results; whole-phase review verdict; changed paths; remaining Minor findings; and any real API calls made (normally none). Then stop. Do not begin Phase 2. Tell the user to /clear and use the Phase 2 controller prompt.
```



## Phase 2 controller prompt

```text
/using-superpowers
/subagent-driven-development

Model requirement: use GPT-5.6 Luna with high reasoning effort for this controller and every implementer, reviewer, fixer, and whole-phase reviewer. Request it explicitly on every dispatch, regardless of the phase plan's routing. If the cloud surface cannot select or recognize that exact model, record the limitation and use the strongest available model with high reasoning effort.

If Superpowers or the named skill is unavailable, manually execute the same loop with the available subagent mechanism: fresh implementer per task, fresh reviewer after each task, fixer plus re-review for every Critical/Important finding, and strongest available whole-phase reviewer.

You are the controller for EvalBench Phase 2 only. Work in the checked-out repository root.

Read these files in full before any implementation action:
1. SPEC.md
2. docs/superpowers/plans/2026-07-15-evalbench-phase-2-structured.md
3. The complete superpowers:subagent-driven-development SKILL.md and every prompt/template it requires.

Use superpowers:subagent-driven-development for the entire phase. Dispatch a fresh implementer for every numbered Task, a fresh spec-and-quality reviewer after every task, fix/re-review all Critical or Important findings, then run one strongest-available whole-phase review. Continue without human check-ins unless blocked, a real plan/spec conflict requires judgment, or Phase 2 is complete.

Preflight:
- Inspect git status/branch and preserve user work.
- Require a completed, green Phase 1 with a valid HEAD. Run the Phase 1 backend test and frontend build gates before changing files. If they fail, diagnose and report; do not silently fold Phase 1 repairs into Phase 2.
- Scan the Phase 2 plan once for conflicts and batch genuine conflicts into one question before Task 1.
- Initialize or resume .superpowers/sdd/progress.md for Phase 2. Do not redispatch tasks marked complete with matching commits.
- Record PHASE_BASE_SHA=$(git rev-parse HEAD).

For every Task N, follow the official subagent-driven-development loop exactly:
1. Record task BASE_SHA.
2. Generate a task brief from the Phase 2 plan with the skill script.
3. Dispatch a fresh implementer using the official template and a durable report file. Require the plan's TDD RED/GREEN evidence, focused tests while iterating, full relevant tests before commit, self-review, and a task commit.
4. Generate review-package from the recorded BASE_SHA to HEAD.
5. Dispatch a fresh reviewer with brief/report/diff paths and the Phase 2 Global Constraints verbatim.
6. Resolve “cannot verify” items. Send Critical/Important findings to a fixer, require covering-test evidence appended to the report, and re-review. Record Minor items in the ledger.
7. Mark complete only after both spec and quality approval.

Non-negotiable rules:
- Implement only suite #2 structured and its Phase 2 integration/documentation. Do not redesign core contracts or dashboard.
- Exactly 40 dataset rows: 8 per domain and the exact adversarial distribution in the plan. This is a dataset count, not a requirement to invent 40 implementation tasks.
- Field accuracy must retain partial credit from parseable but schema-invalid JSON.
- Target and judge calls are mocked in all tests; no real API calls or spend without separate user authorization.
- Never print keys/prompts/outputs containing secrets. Preserve unrelated work. No destructive git.
- Commit each task in the current repository workspace. Do not push or create a PR during the task loop unless the controller explicitly authorizes publication.

Whole-phase gate:
- Run all Phase 2 backend tests, frontend lint/build, make api and make web startup smokes, /suites metadata check, schema/core no-diff check, secret hygiene, and git diff --check.
- Build a whole-phase review package from PHASE_BASE_SHA to HEAD. The final reviewer must compare the full change to SPEC.md and the Phase 2 plan and specifically audit dataset quality, retry accounting, MetricRecord persistence, mock-only provider behavior, and absence of core contract changes.
- Use one fixer for all final Critical/Important findings and re-review until approved.

Final response must report: DONE or BLOCKED; final commit hash; task ledger; exact verification evidence; dataset counts/adversarial split; final review verdict; changed paths; remaining Minor findings; and real API calls made (normally none). Then stop. Do not begin Phase 3. Tell the user to /clear and use the Phase 3 controller prompt.
```



## Phase 3 controller prompt

```text
/using-superpowers
/subagent-driven-development

Model requirement: use GPT-5.6 Luna with high reasoning effort for this controller and every implementer, reviewer, fixer, and whole-phase reviewer. Request it explicitly on every dispatch, regardless of the phase plan's routing. If the cloud surface cannot select or recognize that exact model, record the limitation and use the strongest available model with high reasoning effort.

If Superpowers or the named skill is unavailable, manually execute the same loop with the available subagent mechanism: fresh implementer per task, fresh reviewer after each task, fixer plus re-review for every Critical/Important finding, and strongest available whole-phase reviewer.

You are the controller for EvalBench Phase 3 only. Work in the checked-out repository root.

Read these files in full before any implementation action:
1. SPEC.md
2. docs/superpowers/plans/2026-07-15-evalbench-phase-3-dashboard.md
3. The complete superpowers:subagent-driven-development SKILL.md and every prompt/template it requires.

Use superpowers:subagent-driven-development throughout Phase 3: fresh implementer per numbered Task, fresh spec-and-quality reviewer after each, fixer plus re-review for every Critical/Important issue, and one strongest-available whole-dashboard review at the end. Do not pause for routine approval between tasks.

Preflight:
- Inspect git status and preserve user work.
- Require green Phase 2: backend tests pass, structured is returned by /suites, and the placeholder frontend builds. Do not mix prior-phase repairs into dashboard commits without reporting a blocker.
- Scan the plan for conflicts before Task 1; ask at most one batched conflict question if needed.
- Initialize/resume .superpowers/sdd/progress.md for Phase 3 and record PHASE_BASE_SHA.

Per-task controller loop:
1. Record task BASE_SHA and generate the task brief with the SDD skill script.
2. Dispatch a fresh implementer with the official implementer template, brief/report paths, repository directory, and only relevant established API/types.
3. Require focused TypeScript/lint/build verification, self-review, and commit. Where the task requests browser/manual fixtures, require the report to describe viewport/data cases and observed results.
4. Generate review-package BASE_SHA..HEAD and dispatch a fresh task reviewer with Phase 3 Global Constraints verbatim.
5. Fix and re-review all Critical/Important findings. Resolve cross-task “cannot verify” items yourself. Preserve Minor items for final review.
6. Update the durable ledger only after clean task approval.

Non-negotiable rules:
- No suite-name conditionals and no structured-specific rendering logic. Everything comes from /suites metadata and /results shapes.
- Every model row shows row n; every matrix cell shows its own n and CI; no bare estimate.
- Under-8% stacked labels are hidden inline and shown in tooltip. Segment order and non-color redundancy are fixed.
- Follow the restrained visual rules exactly: off-white, muted colors, generous spacing, no gradients or decorative shadows.
- Do not redesign backend/core. Do not make real model calls. Preserve unrelated work and never use destructive git.
- Commit task changes in the current repository workspace; do not push or create a PR during the task loop unless the controller explicitly authorizes publication.

Whole-phase gate:
- Run fresh backend tests, frontend lint/build, git diff --check, and separate make api/make web startup smokes.
- Perform the plan's live-data control checks and visual checks at 1440x900, 1024x768, and 390x844. Route the aesthetic judgment to the strongest available reviewer; cheap agents must not sign off the BullshitBench look.
- Generate a whole-phase review package from PHASE_BASE_SHA to HEAD. Final review must cover dynamic-suite extensibility, filter-query correctness, CI/n rendering, accessibility, responsive/thin-slice behavior, and prohibited visual embellishments.
- Send all final Critical/Important findings to one fixer, rerun affected/full gates, and re-review until approved.

Final response must report: DONE or BLOCKED; final commit hash; ledger summary; test/lint/build/smoke results; viewport review results; whole-phase review verdict; changed paths; and remaining Minor findings. Then stop. Do not begin Phase 4. Tell the user to /clear and use the Phase 4 controller prompt.
```



## Phase 4 controller prompt

```text
/using-superpowers
/subagent-driven-development

Model requirement: use GPT-5.6 Luna with high reasoning effort for this controller and every implementer, reviewer, fixer, and whole-phase reviewer. Request it explicitly on every dispatch, regardless of the phase plan's routing. If the cloud surface cannot select or recognize that exact model, record the limitation and use the strongest available model with high reasoning effort.

If Superpowers or the named skill is unavailable, manually execute the same loop with the available subagent mechanism: fresh implementer per task, fresh reviewer after each task, fixer plus re-review for every Critical/Important finding, and strongest available whole-phase reviewer.

You are the controller for EvalBench Phase 4 only. Work in the checked-out repository root.

Read these files in full before any implementation action:
1. SPEC.md
2. docs/superpowers/plans/2026-07-15-evalbench-phase-4-latency-cost.md
3. The complete superpowers:subagent-driven-development SKILL.md and every prompt/template it requires.

Use superpowers:subagent-driven-development for all Phase 4 tasks: fresh implementer per numbered Task, fresh spec-and-quality reviewer after each, fixes plus re-review for Critical/Important findings, then strongest-available whole-phase review. Continue without routine user prompts.

Preflight:
- Require green Phase 3 and a clean/understood working tree. Record PHASE_BASE_SHA=$(git rev-parse HEAD).
- Run backend tests and frontend lint/build before changes.
- Scan the Phase 4 plan once for conflicts and initialize/resume the Phase 4 SDD ledger.
- Record the exact allowed paths now: backend/evalbench/suites/latency_cost.py, backend/evalbench/registry.py, and backend/data/latency_cost/**.

For every task, use the official task-brief, implementer report, review-package, and task-reviewer flow. Every implementer/reviewer prompt must repeat the allowed-path constraint. If any agent changes tests, core, API, store, dashboard, README, Makefile, packages, or another suite, treat that as an Important spec violation and correct it before proceeding. One-off verification scripts must remain uncommitted.

Non-negotiable rules:
- Pairwise only: candidate win=1.0, tie=0.5, loss=0.0. No absolute quality scoring.
- Every judge call independently randomizes candidate A/B position and maps the anonymous verdict back correctly.
- Approximately 20% deterministic sample; exactly three calls on sampled rows; quality uses modal verdict; judge_variance is disagreement from the modal; unsampled rows omit judge_variance.
- Exactly 20 fixed-reference tasks with rubric and stored reference-model provenance.
- No real target/judge call or spend unless separately authorized. Existing tests must remain mock-only.
- No destructive git, no publication during task execution, and no scope expansion.

Whole-phase gate:
- Run all existing backend tests and frontend lint/build, API/dashboard synthetic smoke, make api/make web startup smokes, and git diff --check.
- Run both allowed-path diff commands from PHASE_BASE_SHA. The result must contain only the three allowed path groups. This is a hard gate.
- Generate a whole-phase review package and use the strongest reviewer to check pairwise neutrality, A/B mapping, modal/all-different behavior, sampling/metric n, fixed-reference quality, derived dashboard compatibility, and exact additive invariant.
- Fix/re-review all Critical/Important findings with one final fixer wave if needed.

Final response must report: DONE or BLOCKED; final hash; task ledger; exact verification results; sample-rate check; allowed-path output proving zero core/dashboard changes; final review verdict; remaining Minor findings; and whether any real API calls occurred. Do not publish unless the user explicitly requests it. Then stop. Do not begin Phase 5. Tell the user to /clear and use the Phase 5 controller prompt.
```



## Phase 5 controller prompt

```text
/using-superpowers
/subagent-driven-development

Model requirement: use GPT-5.6 Luna with high reasoning effort for this controller and every implementer, reviewer, fixer, and whole-phase reviewer. Request it explicitly on every dispatch, regardless of the phase plan's routing. If the cloud surface cannot select or recognize that exact model, record the limitation and use the strongest available model with high reasoning effort.

If Superpowers or the named skill is unavailable, manually execute the same loop with the available subagent mechanism: fresh implementer per task, fresh reviewer after each task, fixer plus re-review for every Critical/Important finding, and strongest available whole-phase reviewer.

You are the controller for EvalBench Phase 5 only. Work in the checked-out repository root.

Read these files in full before any implementation action:
1. SPEC.md
2. docs/superpowers/plans/2026-07-15-evalbench-phase-5-rag.md
3. The complete superpowers:subagent-driven-development SKILL.md and every prompt/template it requires.

Use superpowers:subagent-driven-development for every Phase 5 task: fresh implementer, fresh task reviewer, fixes and re-review for all Critical/Important findings, then one strongest-available final whole-project review. Continue continuously unless genuinely blocked or user judgment is required.

Preflight:
- Require green Phase 4 and inspect/preserve the working tree. Record PHASE_BASE_SHA.
- Run backend tests and frontend lint/build before changes.
- Scan the Phase 5 plan for conflicts and initialize/resume its durable SDD ledger.
- Record allowed paths: backend/evalbench/suites/rag.py, backend/evalbench/registry.py, and backend/data/rag/**.

For every task, follow the official task-brief/report/review-package/reviewer workflow. Repeat the allowed-path constraint in every implementer and reviewer prompt. Any test/core/API/store/dashboard/README/Makefile/package/other-suite edit is an Important violation. Standalone verification scripts are temporary and uncommitted. Mark ledger completion only after clean spec and quality review.

Non-negotiable rules:
- Exactly 200 corpus docs, 40/domain; exactly 15 queries, 3/domain; each gold ID has its own substantive justification note and two-pass audit.
- Persist model exactly as embedder::chunk_strategy. Never put embedder or strategy in metrics and never silently substitute an embedder.
- Allowed strategies exactly fixed_512, recursive, semantic. Exact five metrics only.
- RAG is cold per query in v1 so universal embedding latency/tokens/cost remain comparable and runner-owned; no unmetered embedding cache.
- Tests and verification use fake embedding/judge calls. No paid call without separate user authorization and available keys.
- No destructive git, publication during task execution, or scope expansion.

Whole-phase/project gate:
- Run uv sync, all backend tests, npm ci/lint/build, git diff --check, make api and make web startup smokes, the 200/15 dataset audit, fake persisted-model integration, API results checks, and generic dashboard RAG checks.
- Run allowed-path diffs from PHASE_BASE_SHA; only rag.py, registry.py, and data/rag/** may appear.
- Generate the final review package from PHASE_BASE_SHA to HEAD for Phase 5, plus make the reviewer inspect the recorded Phase 4 allowed-path evidence and the complete five-phase Definition of Done.
- Strongest final reviewer must audit label quality, chunk invariants, deduplicated retrieval formulas, context precision denominator, faithfulness separation, composite model identity/provider/family behavior, universal metering/error capture, dynamic dashboard behavior, security, and zero Phase 4/5 core/dashboard edits.
- Send the complete final findings list to one fixer, rerun affected and full gates, then re-review until no Critical/Important issues remain.

Final response must report: DONE or BLOCKED; final hash and git status; all task ledger entries; exact verification evidence; corpus/query/domain/gold audit; model-encoding evidence; Phase 4 and Phase 5 allowed-path outputs; final reviewer verdict; remaining Minor findings; and any real API calls made. Do not publish unless the user explicitly requests it. Stop after Phase 5; do not invent further work.
```
