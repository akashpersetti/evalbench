# EvalBench Phase 5 — RAG Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add suite #3, `rag`, as a strictly additive embedder/chunk-strategy benchmark over a bundled ~200-document corpus with 15 auditable gold-query labels, standard retrieval metrics, and judge-scored faithfulness.

**Architecture:** Each run model is the comparison-row identifier `embedder::chunk_strategy`. RAG tasks set `requires_generation=False`; `evaluate` parses that identifier from the Phase 1 private execution context, chunks the fixed corpus, uses the context's metered embedding calls, retrieves/ranks chunks, computes deterministic retrieval metrics, then uses the generic Judge for grounded answer generation and faithfulness scoring. The shared runner/store/API/dashboard remain untouched.

**Tech Stack:** Existing suite/runner/Judge contracts; Python standard library math/text processing; LiteLLM embeddings through `ExecutionContext.embed`; JSONL corpus/query data; existing registry-driven tests.

## Global Constraints

- This phase may change only `backend/evalbench/suites/rag.py`, `backend/evalbench/registry.py`, and files under `backend/data/rag/`.
- Do not edit tests, core, API, store, dashboard, README, Makefile, package files, or another suite.
- `MetricRecord.model` remains exactly the requested `"{embedder}::{chunk_strategy}"`; never put embedder or strategy in `metrics`.
- `model_family` is derived by Phase 1 config from the embedder provider (OpenAI, Voyage, Cohere); do not override record fields in the suite.
- Allowed strategies are exactly `fixed_512`, `recursive`, and `semantic`.
- Metrics are exactly `recall_at_5`, `ndcg_at_10`, `mrr`, `context_precision`, and `faithfulness`, all floats in `[0,1]` and all displayed as percent/higher-is-better.
- Corpus has exactly 200 synthetic/static documents, 40 per domain. Queries have exactly 15 rows, three per domain. Every gold doc ID has its own nonempty justification note.
- Automated/generic tests and mechanical verification use fake embedding/judge callables; no test makes a provider call.
- To keep per-record universal metrics honest without a core pre-index lifecycle, v1 measures a cold retrieval execution: each query chunks and embeds the corpus plus query. This is expensive but gives every task comparable metered latency/tokens/cost and avoids charging indexing only to the first task. Add the assumption as a code comment; do not introduce an unmetered cache.
- Phase gate: tests/builds pass, dashboard renders RAG matrix without edits, exact model encoding is persisted, label audit passes, and allowed-path diff proves extensibility.
- `[STRONGER MODEL REVIEW]` marks the load-bearing corpus/gold-label work and faithfulness prompt judgment.

---

## Locked data and Python interfaces

Corpus JSONL:

```json
{"id":"software-doc-001","domain":"software","title":"...","text":"..."}
```

Query JSONL:

```json
{
  "id":"software-query-01",
  "domain":"software",
  "query":"...",
  "gold":[
    {"doc_id":"software-doc-001","note":"This document explicitly states ..."}
  ]
}
```

```python
ChunkStrategy = Literal["fixed_512", "recursive", "semantic"]

@dataclass(frozen=True)
class Document:
    id: str
    domain: str
    title: str
    text: str

@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    text: str

class RagSuite(Suite):
    name = "rag"
    metric_keys = ["recall_at_5", "ndcg_at_10", "mrr", "context_precision", "faithfulness"]
    display_metrics = [
        {"key":"recall_at_5", "label":"Recall@5", "format":"percent", "higher_is_better":True},
        {"key":"ndcg_at_10", "label":"nDCG@10", "format":"percent", "higher_is_better":True},
        {"key":"mrr", "label":"MRR", "format":"percent", "higher_is_better":True},
        {"key":"context_precision", "label":"Context precision", "format":"percent", "higher_is_better":True},
        {"key":"faithfulness", "label":"Faithfulness", "format":"percent", "higher_is_better":True},
    ]

    def __init__(self, data_dir: Path | None = None): ...
    def load_tasks(self, domain: str) -> list[Task]: ...
    def build_prompt(self, task: Task) -> list[dict]: ...
    def evaluate(self, task: Task, raw_output: str, judge: Judge) -> dict[str, float]: ...

def parse_pipeline_model(model: str) -> tuple[str, ChunkStrategy]: ...
def resolve_litellm_embedder(embedder: str) -> str: ...
def chunk_fixed_512(documents: Sequence[Document]) -> list[Chunk]: ...
def chunk_recursive(documents: Sequence[Document]) -> list[Chunk]: ...
def chunk_semantic(documents: Sequence[Document], context: Any, embedder: str) -> list[Chunk]: ...
def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float: ...
def rank_chunks(chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]],
                query_vector: Sequence[float]) -> list[Chunk]: ...
def retrieval_metrics(ranked_chunks: Sequence[Chunk], gold_doc_ids: set[str]) -> dict[str, float]: ...
def score_faithfulness(task: Task, chunks: Sequence[Chunk], judge: Judge) -> float: ...
```

`fixed_512` uses deterministic lexical tokens (`re.findall(r"\S+", text)`) because the suite must compare embedders without adding an embedder-specific tokenizer dependency. “512” therefore means 512 whitespace-delimited tokens in v1; include this explicit assumption in a code comment.

## Task 1: Create the corpus and gold-label protocol

**Files:**

- Create: `backend/data/rag/corpus.jsonl`
- Create: `backend/data/rag/queries.jsonl`
- Create: `backend/data/rag/LABELING.md`

**Responsibilities:** Provide a fixed, non-time-sensitive, auditable retrieval dataset whose relevance labels can be independently checked from document text.

- [ ] **Step 1: Write the labeling protocol before content** `[STRONGER MODEL REQUIRED: label quality is load-bearing]`

`LABELING.md` must define: relevance means the document contains evidence needed to answer the query, not merely topical keyword overlap; each gold ID needs a query-specific note quoting/paraphrasing the evidence location; two-pass review checks both false negatives and false positives; near-duplicate documents are intentional hard negatives only when clearly labeled; queries cannot require outside/current knowledge; and no professional advice or real sensitive data appears. Record reviewer/date fields in this dataset-local file.

- [ ] **Step 2: Author exactly 200 corpus documents** `[STRONGER MODEL REQUIRED]`

Create IDs `software-doc-001`…`040`, `finance-doc-001`…`040`, `legal-doc-001`…`040`, `medical-doc-001`…`040`, and `physics-doc-001`…`040`. Each document is 120–350 words, self-contained, static, synthetic or public-domain factual, and has one clear title. Within each domain, create clusters of related documents so retrieval must distinguish details rather than keywords. Medical/legal/finance texts are synthetic educational records/policies, not advice. Do not embed queries, gold markers, or relevance notes in corpus text.

- [ ] **Step 3: Author exactly 15 queries and notes** `[STRONGER MODEL REQUIRED]`

Create IDs `<domain>-query-01`…`03`, three per domain. Each query has 1–4 gold documents. For every `gold` item, write a distinct note that names the specific evidence in that document. Include a mix of single-document fact lookup, multi-document synthesis, and hard-negative discrimination. Ensure at least one multi-gold query per domain so recall@5 can take nonbinary values and the dashboard treats RAG as continuous/matrix data.

- [ ] **Step 4: Perform two-pass label audit** `[STRONGER MODEL REQUIRED]`

Pass A: read each query and all its gold docs, verifying every note. Pass B: keyword/topic scan all 200 docs for plausible omitted relevant docs, then either add with a note or document why the near match is not relevant. Reject any query whose answer depends on an unstated inference. Complete reviewer/date entries in `LABELING.md`.

- [ ] **Step 5: Run a standalone mechanical audit**

Run a `uv run python -` script that loads both files and asserts: exactly 200/15 rows; exact key sets; unique IDs; 40 docs and 3 queries per domain; all fields nonblank; each query has 1–4 unique gold IDs; every gold ID exists and matches query domain; every note is nonblank and at least 40 characters; each domain has a multi-gold query; and no query text is a substring of a corpus document. Expected: `RAG dataset shape valid: 200 docs, 15 queries`.

- [ ] **Step 6: Commit dataset**

```bash
git add backend/data/rag
git commit -m "data: add audited rag corpus and gold queries"
```

## Task 2: Implement loading and exact model encoding

**Files:**

- Create: `backend/evalbench/suites/rag.py`

**Responsibilities:** Validate/load dataset content, create nongenerative generic tasks, and parse the model row identifier without storing configuration in metrics.

- [ ] **Step 1: Implement constants, dataclasses, and dataset loader**

Default data root is `Path(__file__).resolve().parents[2] / "data" / "rag"`. Parse both files with line-numbered errors and enforce the mechanical audit invariants at load time. Cache only parsed immutable document/query text in the suite instance; never cache embeddings or chunks. Task payload contains `gold` and no embedder/strategy. Set `requires_generation=False`. `overall` returns all 15 sorted by `(domain,id)`; domain returns its three.

- [ ] **Step 2: Implement composite model parsing**

Require exactly one `::`. Left side nonblank; right side one of the three allowed strategies. Return the original left side unchanged for record identity. `resolve_litellm_embedder` maps `openai/text-embedding-3-small` to itself, `voyage-3` to LiteLLM's direct `voyage/voyage-3`, and `cohere` to LiteLLM's direct `cohere/embed-v4.0`; any other nonblank embedder passes through unchanged so LiteLLM can support registered providers. These two aliases may produce an authentication error record when their direct provider credentials are unavailable; do not add keys beyond the fixed `.env.example` list or silently substitute a different embedder. Never mutate `context.model` or return the resolved call model as MetricRecord.model.

- [ ] **Step 3: Implement the required `build_prompt` despite nongenerative execution**

Return system/user messages asking for a grounded answer to `task.prompt`, but note in a code comment that `requires_generation=False` means the runner does not send them to the composite embedder model. `evaluate` uses the same query for retrieval and later judge answer generation. This preserves the fixed Suite interface without a bogus completion call.

- [ ] **Step 4: Standalone verification and commit**

Run a `uv run python -` script asserting 15/3 task counts, every task nongenerative, valid parses for all nine embedder/strategy combinations, exact left-side preservation, and rejection of missing/multiple separators or unknown strategies. Expected: `RAG loading/model encoding checks passed`.

```bash
git add backend/evalbench/suites/rag.py
git commit -m "feat: load rag tasks and pipeline models"
```

## Task 3: Implement three deterministic chunk strategies

**Files:**

- Modify: `backend/evalbench/suites/rag.py`

**Responsibilities:** Turn the same documents into stable chunks under three strategies with traceable document IDs and no external text library.

- [ ] **Step 1: Implement fixed lexical chunks**

Tokenize with `re.findall(r"\S+", text)`. Emit windows of 512 tokens with 64-token overlap (`next_start = end - 64`, stop at end). Prefix title to chunk text but do not count title toward the 512 body-token limit. IDs are `f"{doc.id}::fixed_512::{index:04d}"`. Every nonempty document emits at least one chunk.

- [ ] **Step 2: Implement recursive chunks**

Split recursively by `"\n\n"`, `"\n"`, sentence boundary regex `(?<=[.!?])\s+`, then whitespace until each unit is ≤512 lexical tokens. Greedily pack adjacent units up to 512, carrying the final 64 tokens as overlap. IDs use `recursive`. Preserve text order, no empty chunks, and joining non-overlap content must retain every source token in order.

- [ ] **Step 3: Implement semantic chunks**

Split each document into nonempty sentences. Batch-embed its sentences through `context.embed(..., embedder=resolved_embedder)`. Start a new chunk before a sentence when either adding it would exceed 512 lexical tokens or cosine similarity between adjacent sentence vectors is `< 0.65` after the current chunk has at least 3 sentences. Never exceed 512; a single overlong sentence is split by fixed windows. IDs use `semantic`. This embedding work is metered by the runner context.

- [ ] **Step 4: Implement cosine safely**

Require equal nonzero dimensions. Return dot product divided by norm product. For a zero vector return `0.0`; for dimension mismatch raise `ValueError` so the runner records an error instead of silently ranking invalid vectors.

- [ ] **Step 5: Mechanically verify without test edits**

Run a terminal script with small synthetic documents and a fake context that returns known vectors. Assert deterministic IDs/order, fixed chunk max/overlap, recursive token coverage/max, semantic split at low similarity and cap, empty documents rejected by loader, cosine known values, and fake embed call count. Expected: `RAG chunking checks passed`.

- [ ] **Step 6: Commit**

```bash
git add backend/evalbench/suites/rag.py
git commit -m "feat: add rag chunk strategies"
```

## Task 4: Implement ranking and deterministic retrieval metrics

**Files:**

- Modify: `backend/evalbench/suites/rag.py`

**Responsibilities:** Embed/rank chunks, deduplicate document rankings where appropriate, and calculate standard metrics with exact denominators.

- [ ] **Step 1: Implement ranking**

Require one vector per chunk. Score cosine similarity to the query. Sort descending score with chunk ID ascending tie-break. Return ranked chunks. The caller embeds chunk texts in batches of at most 64 via `context.embed` and embeds the query separately.

- [ ] **Step 2: Implement document-level ranked list**

For recall/nDCG/MRR, deduplicate ranked chunks by `doc_id`, preserving the first/highest-ranked occurrence. This prevents a document with many chunks receiving repeated relevance credit. Use the first 10 unique documents for nDCG, first 5 for recall, and the full deduplicated list for MRR.

- [ ] **Step 3: Implement exact formulas**

- `recall_at_5 = |set(top_5_doc_ids) ∩ gold| / |gold|`.
- `dcg@10 = Σ(rel_i / log2(i+1))` for one-based ranks `i=1..10`, binary `rel_i`.
- `idcg@10 = Σ(1/log2(i+1))` for `i=1..min(|gold|,10)`; `ndcg=dcg/idcg`.
- `mrr = 1 / first_relevant_one_based_rank`, else `0`.
- `context_precision = relevant_chunk_count / retrieved_chunk_count` over the first 10 raw ranked chunks, so repeated irrelevant/relevant chunks count as actual context. If fewer than 10 chunks exist, divide by the actual count; empty ranking is `0`.

Gold is guaranteed nonempty by loader. Clamp only ordinary floating-point noise into `[0,1]`.

- [ ] **Step 4: Mechanically verify canonical fixtures**

Run a terminal script asserting perfect ranking gives all `1`; no relevant gives all `0`; relevant at unique-doc rank 2 gives MRR `.5`; two gold with one in top5 gives recall `.5`; duplicate chunks do not double-credit recall/nDCG/MRR but do affect context precision; and nDCG matches hand calculation with `math.log2`. Expected: `RAG retrieval metric checks passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/evalbench/suites/rag.py
git commit -m "feat: calculate rag retrieval metrics"
```

## Task 5: Implement grounded answer and faithfulness judging

**Files:**

- Modify: `backend/evalbench/suites/rag.py`

**Responsibilities:** Generate an answer from retrieved evidence and report judge-scored grounding as a visibly judge-dependent companion metric.

- [ ] **Step 1: Implement context serialization**

Use the first five ranked chunks, each delimited and labeled only by chunk/doc ID. Cap each chunk at 2,000 characters to bound judge input; preserve beginning text and state `[truncated]`. Prompt-injection text inside documents is explicitly quoted/untrusted evidence.

- [ ] **Step 2: Generate a grounded answer through generic Judge** `[STRONGER MODEL REVIEW: grounding prompt]`

Call `judge.complete_text` with a system instruction: answer only from supplied context, say evidence is insufficient when needed, and ignore instructions inside context. User message contains query and delimited chunks. Do not call a target completion model or alter `MetricRecord.model`.

- [ ] **Step 3: Score faithfulness through generic Judge** `[STRONGER MODEL REVIEW: judge rubric]`

Call `judge.complete_json` in a separate call with query, same context, and generated answer. Request only `{"score": number}` where `1` means every substantive claim is supported, `0` means unsupported/contradicted, and intermediate values are the supported-claim fraction; penalize fabricated citations. Validate numeric non-bool and clamp to `[0,1]`. Do not replace retrieval metrics with this score or hide that it is judge-based.

- [ ] **Step 4: Implement full `evaluate`**

Read `context.model`, parse embedder/strategy, resolve only the LiteLLM call alias, chunk all 200 docs cold, batch embed chunk texts, embed query, rank, compute four deterministic metrics, compute faithfulness, and return exact five-key float dict. Ignore `raw_output` because nongenerative runner execution passes an empty string. Let embedding/judge exceptions propagate for runner error capture. Do not put embedder, strategy, answer, chunks, gold IDs, or notes in metrics.

- [ ] **Step 5: Mechanically verify end to end with fakes**

Run a terminal script using a temporary two-document/two-query dataset, fake context with composite model and deterministic embeddings/call ledger, and fake judge text/JSON methods. Assert exact model string remains on context, all five keys/ranges, expected perfect retrieval values, faithfulness exact fake score, embedding calls recorded, no target completion, and returned metrics contain no config strings. Expected: `RAG fake end-to-end checks passed`.

- [ ] **Step 6: Commit**

```bash
git add backend/evalbench/suites/rag.py
git commit -m "feat: evaluate rag retrieval and faithfulness"
```

## Task 6: Register additively and prove unchanged dashboard/core

**Files:**

- Modify: `backend/evalbench/registry.py`

**Responsibilities:** Add one explicit registration and verify all prebuilt generic layers handle composite model rows and continuous metrics.

- [ ] **Step 1: Add only import and registration lines**

Import `RagSuite` and call `register_suite(RagSuite())` after existing suites. Do not change registry functions/order semantics.

- [ ] **Step 2: Run existing automated gates**

Run `uv run pytest backend/tests -q`, `npm --prefix web run lint`, and `npm --prefix web run build`. Expected: all pass. Generic suite tests must now load/audit RAG using existing fakes; no test edit is permitted.

- [ ] **Step 3: Verify API/model encoding with fake records**

Use a one-off script to run one temporary RAG task with model `openai/text-embedding-3-small::fixed_512` and injected fake embedding/judge. Assert persisted record `suite="rag"`, model exact composite string, provider `openai`, family `OpenAI`, universal embedding tokens/cost/latency populated by runner, no error, and exact metric keys. Query `/results` and assert each metric estimate has `n`/Wilson CI and p95 latency has `n`/CI.

- [ ] **Step 4: Verify dashboard generically**

Start `make api`/`make web` with representative RAG records. Select RAG. Confirm no suite-specific frontend edit; model rows use composite strings; matrix lists five metrics plus p95; every cell/row shows `n` and CI; if no metric qualifies for stacked shape, the page gives the continuous-matrix message rather than failing.

- [ ] **Step 5: Prove allowed-path invariant**

Set `PHASE4_COMMIT` to the recorded Phase 4 final hash and run:

```bash
git diff --name-only "$PHASE4_COMMIT"..HEAD
git diff --name-only "$PHASE4_COMMIT"
```

Every path must be `backend/evalbench/suites/rag.py`, `backend/evalbench/registry.py`, or begin `backend/data/rag/`. If not, stop and remove/revert only this phase's accidental changes; never reset user work.

- [ ] **Step 6: Commit registry change**

```bash
git add backend/evalbench/registry.py
git commit -m "feat: register rag suite"
```

## Task 7: Final definition-of-done gate

**Files:** No file changes expected.

- [ ] **Step 1: Run complete clean verification**

Run `uv sync --dev`, `uv run pytest backend/tests -q`, `npm --prefix web ci`, `npm --prefix web run lint`, `npm --prefix web run build`, `git diff --check "$PHASE4_COMMIT"..HEAD`, and the standalone 200-doc/15-query label audit. Expected: all pass and no real provider call occurs in tests.

- [ ] **Step 2: Smoke all registered suites and required commands**

Start `make api` and `make web` separately. Verify `/suites` has structured, latency_cost, rag; dashboard suite/domain/time/refusal/family controls work for representative records; row/cell `n` and continuous CIs are visible; and no bare mean appears. Stop servers.

If credentials are intentionally available, run the spec's structured command and one minimal RAG query/config. Otherwise do not make external calls; record fake integration evidence.

- [ ] **Step 3: Verify security and schema invariants**

Run a secret-pattern scan excluding lock/spec, confirm `.env`/`evalbench.db` ignored, inspect DB schema for only universal columns + JSON metrics, and confirm logs contain no prompt/output/key. Confirm unknown pricing returns zero with warning and no run crash via existing test evidence.

- [ ] **Step 4: Capture additive proof and handoff**

Because there is no remote, include Phase 4 and Phase 5 allowed-path outputs and all verification commands in the final handoff instead of creating a PR. Explicitly state suites #1/#3 required zero runner/store/API/dashboard changes.

- [ ] **Step 5: Stop**

Record final main-branch commit hash and clean/dirty status. The five-phase spec is complete; do not invent a Phase 6.
