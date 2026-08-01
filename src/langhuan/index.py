from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .chunking import CHUNKER_VERSION, chunk_document
from .config import Settings
from .embeddings import TOKENIZER_VERSION, make_embedder
from .reader import PARSER_VERSION, iter_markdown_files, normalize_path, read_markdown


INDEX_VERSION = 2
RAG_INPUT_CONTRACT = "langhuan-rag-input/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_id(relative_path: str, index: int) -> str:
    path_id = hashlib.sha1(relative_path.lower().encode("utf-8")).hexdigest()[:16]
    return f"{path_id}::{index:04d}"


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _model_identity(model: str) -> str:
    if model.lower() == "hash":
        return "hash-embedder/v1"
    path = Path(model).expanduser()
    if not path.exists():
        return f"unresolved:{path.name or 'model'}"
    resolved = path.resolve()
    return f"local-snapshot:{resolved.name}"


def pipeline_contract(settings: Settings) -> dict[str, Any]:
    return {
        "contract": RAG_INPUT_CONTRACT,
        "index_version": INDEX_VERSION,
        "include": sorted(settings.include),
        "exclude": sorted(settings.exclude),
        "parser_version": PARSER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "tokenizer_version": TOKENIZER_VERSION,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "embedding_model": settings.embedding_model,
        "model_identity": _model_identity(settings.embedding_model),
    }


def config_fingerprint(settings: Settings) -> str:
    return _canonical_sha256(pipeline_contract(settings))


def scan_input_hashes(settings: Settings) -> tuple[dict[str, Path], dict[str, str]]:
    paths = iter_markdown_files(settings.vault, settings.include, settings.exclude)
    current = {normalize_path(path.relative_to(settings.vault)): path for path in paths}
    return current, {relative: _sha256(path) for relative, path in current.items()}


def rag_input_digest(settings: Settings, hashes: dict[str, str]) -> str:
    return _canonical_sha256(
        {
            "pipeline": pipeline_contract(settings),
            "files": [
                {"path": relative, "sha256": hashes[relative]}
                for relative in sorted(hashes)
            ],
        }
    )


def empty_index(settings: Settings) -> dict[str, Any]:
    return {
        "version": INDEX_VERSION,
        "pipeline_fingerprint": config_fingerprint(settings),
        "input_contract": RAG_INPUT_CONTRACT,
        "indexed_input_digest": None,
        "embedding_model": settings.embedding_model,
        "files": {},
        "chunks": {},
    }


def load_index(settings: Settings) -> dict[str, Any]:
    if not settings.index_path.exists():
        return empty_index(settings)
    try:
        index = json.loads(settings.index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read index {settings.index_path}: {exc}") from exc
    return index


def save_index(settings: Settings, index: dict[str, Any]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    temporary = settings.index_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(settings.index_path)


def audit_index(
    index: dict[str, Any],
    settings: Settings | None = None,
    *,
    current_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    expected = {
        chunk_id
        for record in index.get("files", {}).values()
        for chunk_id in record.get("chunk_ids", [])
    }
    actual = set(index.get("chunks", {}))
    missing = expected - actual
    orphaned = actual - expected
    pipeline_current = bool(
        settings
        and index.get("version") == INDEX_VERSION
        and index.get("pipeline_fingerprint") == config_fingerprint(settings)
    )
    target_digest: str | None = None
    if settings is not None:
        if current_hashes is None:
            _current, current_hashes = scan_input_hashes(settings)
        target_digest = rag_input_digest(settings, current_hashes)
    indexed_digest = index.get("indexed_input_digest")
    storage_consistent = not missing and not orphaned
    fresh = bool(
        pipeline_current
        and indexed_digest
        and target_digest
        and indexed_digest == target_digest
    )
    return {
        "files": len(index.get("files", {})),
        "chunks": len(actual),
        "missing_chunks": len(missing),
        "orphaned_chunks": len(orphaned),
        "storage_consistent": storage_consistent,
        "consistent": storage_consistent,
        "pipeline_current": pipeline_current,
        "indexed_input_digest": indexed_digest,
        "target_input_digest": target_digest,
        "fresh": fresh,
        "ready": storage_consistent and fresh,
    }


def _source_group(metadata: dict[str, Any]) -> tuple[str, str]:
    if metadata.get("book"):
        return str(metadata["book"]), "book"
    if metadata.get("project"):
        return str(metadata["project"]), "project"
    if metadata.get("type") == "concept":
        return str(metadata["title"]), "concept"
    return str(metadata["title"]), "note"


def sync_index(settings: Settings, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    if not settings.vault.is_dir():
        raise RuntimeError(f"Vault directory does not exist: {settings.vault}")

    index = load_index(settings)
    reset = (
        index.get("version") != INDEX_VERSION
        or index.get("pipeline_fingerprint") != config_fingerprint(settings)
    )
    if reset:
        index = empty_index(settings)
        force = True

    current, hashes = scan_input_hashes(settings)
    target_digest = rag_input_digest(settings, hashes)
    old_files = index["files"]
    changed = [
        relative
        for relative in current
        if force or old_files.get(relative, {}).get("sha256") != hashes[relative]
    ]
    deleted = sorted(set(old_files) - set(current))
    summary: dict[str, Any] = {
        "scanned": len(current),
        "changed": len(changed),
        "deleted": len(deleted),
        "reset": reset,
        "chunks_upserted": 0,
        "dry_run": dry_run,
        "pipeline_fingerprint": config_fingerprint(settings),
        "target_input_digest": target_digest,
    }
    if dry_run:
        summary["audit"] = audit_index(index, settings, current_hashes=hashes)
        return summary

    for relative in deleted + changed:
        for chunk_id in old_files.get(relative, {}).get("chunk_ids", []):
            index["chunks"].pop(chunk_id, None)
        index["files"].pop(relative, None)

    embedder = make_embedder(settings.embedding_model) if changed else None
    for relative in changed:
        document = read_markdown(current[relative], settings.vault)
        chunks = chunk_document(document, settings.chunk_size, settings.chunk_overlap)
        texts = [chunk.text for chunk in chunks]
        vectors = embedder.embed(texts) if embedder and texts else []
        if len(vectors) != len(chunks):
            raise RuntimeError(
                f"Embedding backend returned {len(vectors)} vectors for {len(chunks)} chunks"
            )
        ids: list[str] = []
        for chunk_number, (chunk, vector) in enumerate(zip(chunks, vectors)):
            chunk_id = _chunk_id(relative, chunk_number)
            metadata = dict(chunk.metadata)
            metadata["chunk_index"] = chunk_number
            metadata["chunk_id"] = chunk_id
            metadata["source_group"], metadata["source_kind"] = _source_group(metadata)
            index["chunks"][chunk_id] = {
                "text": chunk.text,
                "metadata": metadata,
                "vector": vector,
            }
            ids.append(chunk_id)
        index["files"][relative] = {"sha256": hashes[relative], "chunk_ids": ids}
        summary["chunks_upserted"] += len(ids)

    storage_audit = audit_index(index)
    if not storage_audit["storage_consistent"]:
        raise RuntimeError(f"Index consistency audit failed: {storage_audit}")
    index["indexed_input_digest"] = target_digest
    save_index(settings, index)
    audit = audit_index(index, settings, current_hashes=hashes)
    if not audit["ready"]:
        raise RuntimeError(f"Index readiness audit failed: {audit}")
    summary["audit"] = audit
    return summary
