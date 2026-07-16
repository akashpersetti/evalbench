"""Audited RAG task loading and composite model encoding."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from evalbench.suites.base import Suite, Task

if TYPE_CHECKING:
    from evalbench.judge import Judge


_DOMAINS = ("software", "finance", "legal", "medical", "physics")
_CORPUS_KEYS = frozenset({"id", "domain", "title", "text"})
_QUERY_KEYS = frozenset({"id", "domain", "query", "gold"})
_GOLD_KEYS = frozenset({"doc_id", "note"})
_ALLOWED_STRATEGIES = ("fixed_512", "recursive", "semantic")

ChunkStrategy = Literal["fixed_512", "recursive", "semantic"]


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    domain: str
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class GoldLabel:
    doc_id: str
    note: str


@dataclass(frozen=True, slots=True)
class Query:
    id: str
    domain: str
    query: str
    gold: tuple[GoldLabel, ...]


class RagSuite(Suite):
    """Load the fixed RAG corpus and expose nongenerative RAG tasks."""

    name = "rag"
    metric_keys = [
        "recall_at_5",
        "ndcg_at_10",
        "mrr",
        "context_precision",
        "faithfulness",
    ]
    display_metrics = [
        {
            "key": "recall_at_5",
            "label": "Recall@5",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "ndcg_at_10",
            "label": "nDCG@10",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "mrr",
            "label": "MRR",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "context_precision",
            "label": "Context precision",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "faithfulness",
            "label": "Faithfulness",
            "format": "percent",
            "higher_is_better": True,
        },
    ]

    def __init__(self, data_root: Path | None = None) -> None:
        self.data_root = data_root or (
            Path(__file__).resolve().parents[2] / "data" / "rag"
        )
        # Only immutable parsed source text is retained. Embeddings and chunks
        # are deliberately not suite state; later retrieval is a cold run.
        self.documents, self.queries = _load_dataset(self.data_root)

    def load_tasks(self, domain: str) -> list[Task]:
        """Return all audited queries or the three queries in one domain."""
        if domain != "overall" and domain not in _DOMAINS:
            raise ValueError(f"unknown rag domain {domain!r}")

        queries = (
            self.queries
            if domain == "overall"
            else tuple(query for query in self.queries if query.domain == domain)
        )
        return [
            Task(
                id=query.id,
                domain=query.domain,
                prompt=query.query,
                payload={
                    "gold": [
                        {"doc_id": label.doc_id, "note": label.note}
                        for label in query.gold
                    ]
                },
                requires_generation=False,
            )
            for query in sorted(queries, key=lambda item: (item.domain, item.id))
        ]

    def build_prompt(self, task: Task) -> list[dict]:
        # requires_generation=False means the runner does not send this prompt
        # to the composite embedder model; it preserves the fixed Suite API.
        return [
            {
                "role": "system",
                "content": (
                    "Answer the user's question using only grounded retrieval "
                    "evidence. If the evidence is insufficient, say so."
                ),
            },
            {"role": "user", "content": task.prompt},
        ]

    def evaluate(
        self, task: Task, raw_output: str, judge: Judge
    ) -> dict[str, float]:
        # Retrieval and later judge answer generation must use task.prompt as
        # the same query. The chunking/evaluation stages are intentionally not
        # part of this loading-and-encoding task.
        raise NotImplementedError("RAG evaluation is outside Task 2")


def parse_pipeline_model(model: str) -> tuple[str, ChunkStrategy]:
    """Parse the exact ``embedder::chunk_strategy`` row identifier."""
    if not isinstance(model, str):
        raise ValueError("pipeline model must be a string")
    if model.count("::") != 1:
        raise ValueError("pipeline model must contain exactly one '::'")

    embedder, strategy = model.split("::")
    if not embedder.strip():
        raise ValueError("pipeline model embedder must be non-blank")
    if strategy not in _ALLOWED_STRATEGIES:
        raise ValueError(f"unknown chunk strategy {strategy!r}")
    return embedder, strategy  # type: ignore[return-value]


def resolve_litellm_embedder(embedder: str) -> str:
    """Resolve only the two short aliases that LiteLLM does not call directly."""
    if not isinstance(embedder, str) or not embedder.strip():
        raise ValueError("embedder must be a non-blank string")
    return {
        "openai/text-embedding-3-small": "openai/text-embedding-3-small",
        "voyage-3": "voyage/voyage-3",
        "cohere": "cohere/embed-v4.0",
    }.get(embedder, embedder)


def _load_dataset(data_root: Path) -> tuple[tuple[Document, ...], tuple[Query, ...]]:
    corpus_path = data_root / "corpus.jsonl"
    query_path = data_root / "queries.jsonl"
    corpus_rows = _read_jsonl(corpus_path, "corpus")
    query_rows = _read_jsonl(query_path, "query")

    documents: list[Document] = []
    document_ids: set[str] = set()
    for line_number, row in corpus_rows:
        _require_object_keys(row, _CORPUS_KEYS, corpus_path, line_number, "corpus")
        document = Document(
            id=_required_text(row["id"], "id", corpus_path, line_number, "corpus"),
            domain=_required_text(
                row["domain"], "domain", corpus_path, line_number, "corpus"
            ),
            title=_required_text(
                row["title"], "title", corpus_path, line_number, "corpus"
            ),
            text=_required_text(
                row["text"], "text", corpus_path, line_number, "corpus"
            ),
        )
        if document.domain not in _DOMAINS:
            _row_error(
                corpus_path,
                line_number,
                "corpus",
                f"unknown domain {document.domain!r}",
            )
        if document.id in document_ids:
            _row_error(
                corpus_path,
                line_number,
                "corpus",
                f"duplicate id {document.id!r}",
            )
        document_ids.add(document.id)
        documents.append(document)

    _require_count(corpus_path, "corpus", len(documents), 200, "rows")
    _require_domain_counts(corpus_path, "corpus", [item.domain for item in documents], 40)
    documents_by_id = {document.id: document for document in documents}

    queries: list[Query] = []
    query_ids: set[str] = set()
    for line_number, row in query_rows:
        _require_object_keys(row, _QUERY_KEYS, query_path, line_number, "query")
        query_id = _required_text(row["id"], "id", query_path, line_number, "query")
        domain = _required_text(
            row["domain"], "domain", query_path, line_number, "query"
        )
        query_text = _required_text(
            row["query"], "query", query_path, line_number, "query"
        )
        if domain not in _DOMAINS:
            _row_error(query_path, line_number, "query", f"unknown domain {domain!r}")
        if query_id in query_ids:
            _row_error(query_path, line_number, "query", f"duplicate id {query_id!r}")
        query_ids.add(query_id)

        raw_gold = row["gold"]
        if not isinstance(raw_gold, list) or not 1 <= len(raw_gold) <= 4:
            _row_error(query_path, line_number, "query", "gold must contain 1 to 4 items")
        labels: list[GoldLabel] = []
        gold_ids: set[str] = set()
        for gold_index, raw_label in enumerate(raw_gold, start=1):
            if not isinstance(raw_label, dict) or set(raw_label) != _GOLD_KEYS:
                _row_error(
                    query_path,
                    line_number,
                    "query",
                    f"gold item {gold_index} must contain exactly doc_id and note",
                )
            doc_id = _required_text(
                raw_label["doc_id"],
                f"gold[{gold_index}].doc_id",
                query_path,
                line_number,
                "query",
            )
            note = _required_text(
                raw_label["note"],
                f"gold[{gold_index}].note",
                query_path,
                line_number,
                "query",
            )
            if len(note) < 40:
                _row_error(
                    query_path,
                    line_number,
                    "query",
                    f"gold note for {doc_id!r} must be at least 40 characters",
                )
            if doc_id in gold_ids:
                _row_error(
                    query_path,
                    line_number,
                    "query",
                    f"duplicate gold doc_id {doc_id!r}",
                )
            document = documents_by_id.get(doc_id)
            if document is None:
                _row_error(
                    query_path,
                    line_number,
                    "query",
                    f"gold doc_id {doc_id!r} does not exist",
                )
            if document.domain != domain:
                _row_error(
                    query_path,
                    line_number,
                    "query",
                    f"gold doc_id {doc_id!r} is outside query domain {domain!r}",
                )
            gold_ids.add(doc_id)
            labels.append(GoldLabel(doc_id=doc_id, note=note))

        queries.append(
            Query(id=query_id, domain=domain, query=query_text, gold=tuple(labels))
        )

    _require_count(query_path, "query", len(queries), 15, "rows")
    _require_domain_counts(query_path, "query", [item.domain for item in queries], 3)
    if {item.domain for item in queries if len(item.gold) > 1} != set(_DOMAINS):
        _dataset_error(query_path, "each domain must have a multi-gold query")
    for query in queries:
        query_folded = query.query.casefold()
        if any(query_folded in document.text.casefold() for document in documents):
            _dataset_error(
                query_path,
                f"query {query.id!r} must not be a substring of corpus text",
            )

    return tuple(documents), tuple(queries)


def _read_jsonl(path: Path, kind: str) -> list[tuple[int, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        detail = error.strerror or str(error)
        raise ValueError(f"unable to read {path.name}: {detail}") from error

    rows: list[tuple[int, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rows.append((line_number, json.loads(line)))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{path.name}:{line_number}: invalid {kind} JSON: {error.msg}"
            ) from error
    return rows


def _require_object_keys(
    row: Any,
    expected: frozenset[str],
    path: Path,
    line_number: int,
    kind: str,
) -> None:
    if not isinstance(row, dict):
        _row_error(path, line_number, kind, "row must be an object")
    if set(row) != expected:
        _row_error(
            path,
            line_number,
            kind,
            f"row must contain exactly {sorted(expected)}",
        )


def _required_text(
    value: Any,
    field: str,
    path: Path,
    line_number: int,
    kind: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        _row_error(path, line_number, kind, f"{field} must be a non-blank string")
    return value


def _require_count(
    path: Path, kind: str, actual: int, expected: int, label: str
) -> None:
    if actual != expected:
        _dataset_error(path, f"expected exactly {expected} {kind} {label}, found {actual}")


def _require_domain_counts(
    path: Path, kind: str, domains: list[str], expected: int
) -> None:
    counts = Counter(domains)
    if set(counts) != set(_DOMAINS) or any(
        counts[domain] != expected for domain in _DOMAINS
    ):
        _dataset_error(
            path,
            f"expected {expected} {kind} rows per domain, found {dict(counts)}",
        )


def _row_error(path: Path, line_number: int, kind: str, detail: str) -> None:
    raise ValueError(f"{path.name}:{line_number}: invalid {kind} row: {detail}")


def _dataset_error(path: Path, detail: str) -> None:
    raise ValueError(f"{path.name}: invalid dataset: {detail}")
