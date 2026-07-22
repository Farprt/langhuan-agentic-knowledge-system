from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .chunking import chunk_document
from .config import Settings
from .embeddings import make_embedder
from .reader import iter_markdown_files, normalize_path, read_markdown


INDEX_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_id(relative_path: str, index: int) -> str:
    path_id = hashlib.sha1(relative_path.lower().encode("utf-8")).hexdigest()[:16]
    return f"{path_id}::{index:04d}"


def config_fingerprint(settings: Settings) -> str:
    value = f"{settings.embedding_model}|{settings.chunk_size}|{settings.chunk_overlap}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def empty_index(settings: Settings) -> dict[str, Any]:
    return {
        "version": INDEX_VERSION,
        "fingerprint": config_fingerprint(settings),
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
    if index.get("version") != INDEX_VERSION:
        raise RuntimeError("Unsupported index version. Remove the data directory and rebuild.")
    return index


def save_index(settings: Settings, index: dict[str, Any]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    temporary = settings.index_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(settings.index_path)


def audit_index(index: dict[str, Any]) -> dict[str, Any]:
    expected = {
        chunk_id
        for record in index.get("files", {}).values()
        for chunk_id in record.get("chunk_ids", [])
    }
    actual = set(index.get("chunks", {}))
    missing = expected - actual
    orphaned = actual - expected
    return {
        "files": len(index.get("files", {})),
        "chunks": len(actual),
        "missing_chunks": len(missing),
        "orphaned_chunks": len(orphaned),
        "consistent": not missing and not orphaned,
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
    reset = index.get("fingerprint") != config_fingerprint(settings)
    if reset:
        index = empty_index(settings)
        force = True

    paths = iter_markdown_files(settings.vault, settings.include, settings.exclude)
    current = {normalize_path(path.relative_to(settings.vault)): path for path in paths}
    hashes = {relative: _sha256(path) for relative, path in current.items()}
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
    }
    if dry_run:
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

    audit = audit_index(index)
    if not audit["consistent"]:
        raise RuntimeError(f"Index consistency audit failed: {audit}")
    save_index(settings, index)
    summary["audit"] = audit
    return summary
