from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Settings
from .embeddings import cosine, make_embedder, tokenize
from .index import audit_index, config_fingerprint, load_index


def _under_prefix(path: str, prefix: str) -> bool:
    prefix = prefix.replace("\\", "/").strip("/")
    return not prefix or path == prefix or path.startswith(prefix + "/")


def _bm25(query: str, chunks: dict[str, dict[str, Any]]) -> list[tuple[str, float]]:
    query_tokens = list(dict.fromkeys(tokenize(query)))
    documents = {chunk_id: tokenize(value["text"]) for chunk_id, value in chunks.items()}
    if not query_tokens or not documents:
        return []
    average_length = sum(len(tokens) for tokens in documents.values()) / len(documents) or 1.0
    document_frequency = {
        token: sum(token in set(tokens) for tokens in documents.values()) for token in query_tokens
    }
    scores: list[tuple[str, float]] = []
    for chunk_id, tokens in documents.items():
        counts = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = counts[token]
            if not frequency:
                continue
            df = document_frequency[token]
            inverse = math.log(1.0 + (len(documents) - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.5 * (1.0 - 0.75 + 0.75 * len(tokens) / average_length)
            score += inverse * frequency * 2.5 / denominator
        if score > 0:
            scores.append((chunk_id, score))
    return sorted(scores, key=lambda item: item[1], reverse=True)


def _rerank(model: str, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not model or not candidates:
        return candidates
    model_path = Path(model).expanduser()
    if not model_path.exists():
        raise RuntimeError("reranker_model must be a local path; Langhuan never downloads models implicitly.")
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise RuntimeError("Install model support with `pip install 'langhuan[models]'`.") from exc
    reranker = CrossEncoder(str(model_path.resolve()), local_files_only=True)
    scores = reranker.predict([(query, item["text"]) for item in candidates])
    for item, score in zip(candidates, scores):
        item["rerank_score"] = float(score)
    return sorted(candidates, key=lambda item: item["rerank_score"], reverse=True)


def search(
    settings: Settings,
    query: str,
    *,
    top_k: int | None = None,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    if not query.strip():
        raise ValueError("Query cannot be empty")
    index = load_index(settings)
    audit = audit_index(index)
    if not audit["consistent"] or not index["chunks"]:
        raise RuntimeError("Index is empty or inconsistent. Run `langhuan index` first.")
    if index.get("fingerprint") != config_fingerprint(settings):
        raise RuntimeError("Index configuration changed. Run `langhuan sync` before querying.")

    prefixes: tuple[str, ...] = ()
    if scope:
        if scope not in settings.scopes:
            raise ValueError(f"Unknown scope {scope!r}. Available: {', '.join(settings.scopes) or 'none'}")
        prefixes = settings.scopes[scope]
    chunks = {
        chunk_id: value
        for chunk_id, value in index["chunks"].items()
        if not prefixes
        or any(_under_prefix(value["metadata"]["relative_path"], prefix) for prefix in prefixes)
    }
    if not chunks:
        return []

    query_vector = make_embedder(settings.embedding_model).embed([query])[0]
    dense = sorted(
        ((chunk_id, cosine(query_vector, value["vector"])) for chunk_id, value in chunks.items()),
        key=lambda item: item[1],
        reverse=True,
    )[: settings.dense_candidates]
    sparse = _bm25(query, chunks)[: settings.sparse_candidates]

    rrf: dict[str, float] = {}
    for ranked in (dense, sparse):
        for rank, (chunk_id, _score) in enumerate(ranked, 1):
            rrf[chunk_id] = rrf.get(chunk_id, 0.0) + 1.0 / (settings.rrf_k + rank)
    candidates = []
    for chunk_id, score in sorted(rrf.items(), key=lambda item: item[1], reverse=True):
        value = chunks[chunk_id]
        candidates.append(
            {
                "chunk_id": chunk_id,
                "score": score,
                "text": value["text"],
                "metadata": value["metadata"],
            }
        )
    candidates = _rerank(settings.reranker_model, query, candidates)
    return candidates[: (top_k or settings.top_k)]
