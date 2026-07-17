# RAG Dataset Labeling Protocol

## Scope and schema

This dataset is a fixed, non-time-sensitive, synthetic educational retrieval set. A corpus row has exactly `id`, `domain`, `title`, and `text`. A query row has exactly `id`, `domain`, `query`, and `gold`; each `gold` entry has exactly `doc_id` and `note`.

## Relevance rule

Relevance means that the document contains evidence needed to answer the query. A document is not relevant merely because it shares topical keywords with the query. A gold document must support an answer from its own text without importing current facts, professional judgment, or an unstated inference. If a document is included in `gold`, its note must quote or paraphrase the specific evidence location and explain why that evidence answers this query. Notes are intentionally query-specific, not generic summaries of the document.

Each query has between one and four gold documents. Multi-document queries require combining evidence across the listed documents; a single document must not be labeled gold only because it is topically adjacent. Queries are limited to facts and relationships stated in the corpus and cannot require outside or current knowledge. The software, finance, legal, medical, and physics domains are separate; a gold ID must match its query's domain. Medical, legal, and finance documents are synthetic educational records or policies, not advice. No real sensitive data or professional advice appears in this dataset.

## Hard negatives

Near-duplicate documents are intentional hard negatives only when the distinction is clearly labeled by the documents' concrete details. A near match remains non-gold when it has the same topic but lacks the fact, identifier, threshold, sequence, or relationship asked for. Keyword overlap alone is never enough for a gold label.

## Review protocol

Pass A is a false-negative check from the query outward: read each query, read every gold document, verify that every gold note points to evidence that supports the answer, and reject any query whose answer depends on an unstated inference. Then scan the corpus for evidence that should be represented by the query and add an omitted document only when it is necessary to answer the query.

Pass B is a false-positive and omission check from the corpus inward: keyword/topic-scan all documents for plausible omitted relevant documents, inspect every plausible near match, and either add it with a distinct query-specific note or record why it is not relevant. The reason must identify the missing or conflicting detail, rather than simply saying that the document is different. Both passes check false negatives and false positives; a label is retained only when the document evidence and the query-specific note agree.

## Reviewer record

- Reviewer: Codex (GPT-5.6 Luna, high reasoning effort)
- Review date: 2026-07-16
- Pass A status: completed; 15/15 queries and 20/20 gold notes verified against explicit document evidence
- Pass B status: completed; all 200 corpus rows topic-scanned, 131 plausible same-domain candidates reviewed, and no omitted relevant document identified
- Mechanical audit command: the standalone `uv run python -` assertion script specified in the task brief
