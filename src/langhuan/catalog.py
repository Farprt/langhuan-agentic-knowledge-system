from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CatalogCollection, Settings
from .reader import FRONTMATTER_RE, PARSER_VERSION, normalize_path, parse_frontmatter, read_markdown


CATALOG_VERSION = 2
CATALOG_PROJECTION_VERSION = 1
LEDGER_VERSION = "reading-ledger/v1"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
NOTE_ID_RE = re.compile(r"^note_[0-9a-f]{32}$")
LEDGER_PROCESSING_STATUSES = {
    "captured",
    "extracted",
    "integrated",
    "verified",
    "blocked",
    "retired",
}
LEDGER_CLEANUP_STATUSES = {
    "not-ready",
    "ready-for-cleanup",
    "raw-and-draft-cleaned",
    "not-applicable",
    "unknown",
}
CONTEXT_MAX_STRING = 512
CONTEXT_MAX_FIELDS = 24
CONTEXT_MAX_REGISTRY = 64
CONTEXT_MAX_ACTIVE_REGISTRY = 24
CONTEXT_MAX_ISSUES = 24
CONTEXT_MAX_LIMIT = 24
EVALUATION_MAX_NAMES = 64
EVALUATION_MAX_ENVELOPES = 12


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _under_prefix(path: str, prefix: str) -> bool:
    path_key = path.replace("\\", "/").strip("/").casefold()
    prefix_key = prefix.replace("\\", "/").strip("/").casefold()
    return prefix_key in {"", "."} or path_key == prefix_key or path_key.startswith(
        prefix_key + "/"
    )


def _relative_argument(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a relative path inside the vault")
    raw = value.replace("\\", "/")
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or ".." in path.parts
    ):
        raise ValueError(f"{name} must be a relative path inside the vault")
    normalized = raw.strip("/")
    if not normalized:
        raise ValueError(f"{name} must be a relative path inside the vault")
    return normalize_path(normalized)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _CatalogLock:
    def __init__(self, path: Path, timeout: float = 15.0) -> None:
        self.path = path
        self.timeout = timeout
        self.handle: Any = None

    def __enter__(self) -> "_CatalogLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise RuntimeError(
                        f"Timed out waiting for catalog writer lock after {self.timeout:g}s"
                    ) from exc
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_note_bytes(path: Path, expected: bytes, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != expected:
            raise RuntimeError("The note changed while its stable ID was being assigned")
        os.chmod(temporary, path.stat().st_mode)
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    deadline = time.monotonic() + 2.0
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _collection_payload(collection: CatalogCollection) -> dict[str, Any]:
    return {
        "paths": list(collection.paths),
        "role": collection.role,
        "usage": collection.usage,
        "workflow": collection.workflow,
        "processor": collection.processor,
        "entrypoints": list(collection.entrypoints),
        "related": list(collection.related),
        "fields": list(collection.fields),
    }


def _scope_fingerprint(settings: Settings) -> str:
    ledger = ""
    if settings.catalog.reading_ledger is not None:
        ledger = normalize_path(settings.catalog.reading_ledger.relative_to(settings.vault))
    vault_identity = hashlib.sha256(
        os.path.normcase(str(settings.vault.resolve())).encode("utf-8")
    ).hexdigest()
    value = {
        "catalog_version": CATALOG_VERSION,
        "projection_version": CATALOG_PROJECTION_VERSION,
        "parser_version": PARSER_VERSION,
        "vault_identity": vault_identity,
        "include": settings.catalog.include,
        "exclude": settings.catalog.exclude,
        "identity_paths": settings.catalog.identity_paths,
        "include_non_markdown": settings.catalog.include_non_markdown,
        "metadata_fields": settings.catalog.metadata_fields,
        "collection_order": list(settings.catalog.collections),
        "collections": {
            name: _collection_payload(collection)
            for name, collection in settings.catalog.collections.items()
        },
        "reading_ledger": ledger,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _empty_catalog(settings: Settings) -> dict[str, Any]:
    return {
        "version": CATALOG_VERSION,
        "scope_fingerprint": _scope_fingerprint(settings),
        "revision": "",
        "content_digest": "",
        "updated_at": "",
        "files": {},
        "identity_index": {},
        "identity_summary": {
            "stable": 0,
            "legacy": 0,
            "malformed": 0,
            "managed_legacy": 0,
        },
        "issues": [],
    }


def load_catalog(settings: Settings) -> dict[str, Any]:
    if not settings.catalog_path.is_file():
        return _empty_catalog(settings)
    try:
        value = _read_json(settings.catalog_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Cannot read the local catalog JSON; remove it and rebuild") from exc
    if isinstance(value, dict) and value.get("version") == 1:
        rebuilt = _empty_catalog(settings)
        rebuilt["scope_fingerprint"] = ""
        return rebuilt
    if not isinstance(value, dict) or value.get("version") != CATALOG_VERSION:
        raise RuntimeError("Unsupported catalog version; remove the local catalog and rebuild")
    if not isinstance(value.get("files"), dict):
        raise RuntimeError("Invalid catalog JSON: files must be an object")
    return value


def _specificity(prefix: str) -> int:
    prefix = prefix.strip("/")
    return 0 if prefix in {"", "."} else prefix.count("/") + 1


def _matching_collections(
    relative_path: str, collections: dict[str, CatalogCollection]
) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for order, (name, collection) in enumerate(collections.items()):
        matched = [
            _specificity(prefix)
            for prefix in collection.paths
            if _under_prefix(relative_path, prefix)
        ]
        if matched:
            matches.append((-max(matched), order, name))
    return [name for _score, _order, name in sorted(matches)]


def _data_dir_prefix(settings: Settings) -> str | None:
    try:
        return normalize_path(settings.data_dir.resolve().relative_to(settings.vault.resolve()))
    except ValueError:
        return None


def _scan_paths(settings: Settings) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    current: dict[str, Path] = {}
    issues: list[dict[str, Any]] = []
    data_prefix = _data_dir_prefix(settings)
    vault = settings.vault.resolve()
    for path in settings.vault.rglob("*"):
        try:
            if not path.is_file():
                continue
            relative = normalize_path(path.relative_to(settings.vault))
            resolved = path.resolve()
            try:
                resolved.relative_to(vault)
            except ValueError:
                issues.append(
                    {
                        "severity": "error",
                        "code": "path_outside_vault",
                        "path": relative,
                        "message": "Skipped a path whose resolved target is outside the vault.",
                    }
                )
                continue
            if resolved != path.absolute():
                issues.append(
                    {
                        "severity": "warning",
                        "code": "path_alias_skipped",
                        "path": relative,
                        "message": "Skipped a symbolic link, junction or other path alias.",
                    }
                )
                continue
        except OSError:
            continue
        if path.name.casefold() in {".gitkeep", ".ds_store", "thumbs.db"}:
            continue
        if "__pycache__" in {part.casefold() for part in Path(relative).parts}:
            continue
        if data_prefix and _under_prefix(relative, data_prefix):
            continue
        if any(_under_prefix(relative, prefix) for prefix in settings.catalog.exclude):
            continue
        if not any(_under_prefix(relative, prefix) for prefix in settings.catalog.include):
            continue
        if not settings.catalog.include_non_markdown and path.suffix.casefold() != ".md":
            continue
        current[relative] = resolved
    return dict(sorted(current.items(), key=lambda item: item[0].casefold())), issues


def _metadata_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_metadata_value(item) for item in value]
    return str(value)


def _managed_identity_path(settings: Settings, relative: str) -> bool:
    return bool(settings.catalog.identity_paths) and any(
        _under_prefix(relative, prefix) for prefix in settings.catalog.identity_paths
    )


def _identity_fields(relative: str, raw_id: Any, *, markdown: bool) -> dict[str, str]:
    if not markdown:
        return {"id": "", "identity_state": "not-applicable", "identity_key": ""}
    note_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if raw_id is None:
        state = "legacy"
    elif NOTE_ID_RE.fullmatch(note_id):
        state = "stable"
    else:
        state = "malformed"
    return {
        "id": note_id,
        "identity_state": state,
        "identity_key": note_id if state == "stable" else f"legacy-path:{relative}",
    }


def _record_once(settings: Settings, relative: str, path: Path) -> dict[str, Any]:
    stat = path.stat()
    collections = _matching_collections(relative, settings.catalog.collections)
    base: dict[str, Any] = {
        "kind": "markdown" if path.suffix.casefold() == ".md" else "file",
        "extension": path.suffix.casefold(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": "",
        "title": path.stem,
        "aliases": [],
        "type": "",
        "status": "",
        "metadata": {},
        "collections": collections,
        "primary_collection": collections[0] if collections else "",
        "outlinks": [],
        "embeds": [],
        **_identity_fields(relative, None, markdown=False),
    }
    if base["kind"] != "markdown":
        return base

    document = read_markdown(path, settings.vault)
    identity = _identity_fields(
        relative,
        document.frontmatter.get("id") if "id" in document.frontmatter else None,
        markdown=True,
    )
    aliases = document.frontmatter.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    fields = list(settings.catalog.metadata_fields)
    for name in collections:
        fields.extend(settings.catalog.collections[name].fields)
    metadata = {
        key: _metadata_value(document.frontmatter[key])
        for key in dict.fromkeys(fields)
        if key in document.frontmatter
    }
    base.update(
        {
            "sha256": _sha256(path),
            "title": document.metadata["title"],
            "aliases": [str(alias) for alias in aliases],
            "type": document.metadata["type"],
            "status": document.metadata["status"],
            "metadata": metadata,
            "outlinks": json.loads(document.metadata["outlinks_json"]),
            "embeds": json.loads(document.metadata["embeds_json"]),
            **identity,
        }
    )
    return base


def _build_record(settings: Settings, relative: str, path: Path) -> dict[str, Any]:
    for _attempt in range(2):
        before = path.stat()
        record = _record_once(settings, relative, path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
            return record
    raise RuntimeError("file changed while it was being cataloged")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _structural_projection(
    fingerprint: str, files: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    projected: list[dict[str, Any]] = []
    for relative, record in sorted(files.items(), key=lambda item: item[0].casefold()):
        projected.append(
            {
                "relative_path": relative,
                "id": record.get("id", ""),
                "identity_state": record.get("identity_state", ""),
                "title": record.get("title", ""),
                "aliases": record.get("aliases", []),
                "kind": record.get("kind", ""),
                "extension": record.get("extension", ""),
                "type": record.get("type", ""),
                "status": record.get("status", ""),
                "metadata": record.get("metadata", {}),
                "collections": record.get("collections", []),
                "primary_collection": record.get("primary_collection", ""),
                "outlinks": record.get("outlinks", []),
                "embeds": record.get("embeds", []),
            }
        )
    return {
        "catalog_version": CATALOG_VERSION,
        "projection_version": CATALOG_PROJECTION_VERSION,
        "scope_fingerprint": fingerprint,
        "files": projected,
    }


def _revision(fingerprint: str, files: dict[str, dict[str, Any]]) -> str:
    return _canonical_digest(_structural_projection(fingerprint, files))


def _content_digest(files: dict[str, dict[str, Any]]) -> str:
    content = [
        [record.get("identity_key", f"legacy-path:{relative}"), record.get("sha256", "")]
        for relative, record in sorted(files.items(), key=lambda item: item[0].casefold())
        if record.get("kind") == "markdown"
    ]
    return _canonical_digest(content)


def _identity_state(
    settings: Settings, files: dict[str, dict[str, Any]]
) -> tuple[dict[str, str], dict[str, int], list[dict[str, Any]]]:
    by_id: dict[str, list[str]] = defaultdict(list)
    issues: list[dict[str, Any]] = []
    summary = {"stable": 0, "legacy": 0, "malformed": 0, "managed_legacy": 0}
    managed_legacy: list[str] = []
    for relative, record in files.items():
        state = str(record.get("identity_state", ""))
        if state in summary:
            summary[state] += 1
        if state == "stable":
            by_id[str(record["id"])].append(relative)
        elif state == "malformed":
            issues.append(
                {
                    "severity": "error",
                    "code": "malformed_note_id",
                    "path": relative,
                    "message": "Frontmatter id must match note_<32 lowercase hexadecimal characters>.",
                }
            )
        if (
            state == "legacy"
            and record.get("kind") == "markdown"
            and _managed_identity_path(settings, relative)
        ):
            managed_legacy.append(relative)
    summary["managed_legacy"] = len(managed_legacy)
    identity_index: dict[str, str] = {}
    for note_id, paths in sorted(by_id.items()):
        ordered = sorted(paths, key=str.casefold)
        if len(ordered) == 1:
            identity_index[note_id] = ordered[0]
        else:
            issues.append(
                {
                    "severity": "error",
                    "code": "duplicate_note_id",
                    "path": ordered[0],
                    "target": note_id,
                    "message": f"A stable note ID is used by {len(ordered)} files.",
                }
            )
    if managed_legacy:
        issues.append(
            {
                "severity": "warning",
                "code": "missing_stable_id",
                "path": sorted(managed_legacy, key=str.casefold)[0],
                "message": (
                    f"{len(managed_legacy)} managed Markdown files still use legacy path identity."
                ),
            }
        )
    return identity_index, summary, issues


def sync_catalog(
    settings: Settings, *, verify: bool = False, dry_run: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not settings.vault.is_dir():
        raise RuntimeError(f"Vault directory does not exist: {settings.vault}")
    with _CatalogLock(settings.catalog_lock_path):
        old = load_catalog(settings)
        fingerprint = _scope_fingerprint(settings)
        reset = old.get("scope_fingerprint") != fingerprint
        old_files: dict[str, dict[str, Any]] = {} if reset else old.get("files", {})
        current, issues = _scan_paths(settings)
        files = dict(old_files)
        deleted = sorted(set(old_files) - set(current), key=str.casefold)
        for relative in deleted:
            files.pop(relative, None)

        changed = 0
        unchanged = 0
        for relative, path in current.items():
            previous = old_files.get(relative)
            try:
                stat = path.stat()
            except OSError:
                issues.append(
                    {
                        "severity": "error",
                        "code": "stat_failed",
                        "path": relative,
                        "message": "The file could not be inspected.",
                    }
                )
                continue
            if (
                not verify
                and previous
                and previous.get("size") == stat.st_size
                and previous.get("mtime_ns") == stat.st_mtime_ns
            ):
                unchanged += 1
                continue
            try:
                record = _build_record(settings, relative, path)
            except (OSError, UnicodeError, ValueError, RuntimeError):
                issues.append(
                    {
                        "severity": "error",
                        "code": "catalog_read_failed",
                        "path": relative,
                        "message": "The file could not be read or parsed; the previous record was kept.",
                    }
                )
                continue
            if verify and previous and record.get("sha256") == previous.get("sha256"):
                if record != previous:
                    files[relative] = record
                    changed += 1
                else:
                    unchanged += 1
            elif record != previous:
                files[relative] = record
                changed += 1
            else:
                unchanged += 1

        if not reset and deleted:
            new_paths = sorted(set(current) - set(old_files), key=str.casefold)
            deleted_by_hash: dict[str, list[str]] = defaultdict(list)
            new_by_hash: dict[str, list[str]] = defaultdict(list)
            for relative in deleted:
                record = old_files[relative]
                if (
                    record.get("kind") == "markdown"
                    and record.get("identity_state") == "legacy"
                    and record.get("sha256")
                    and _managed_identity_path(settings, relative)
                ):
                    deleted_by_hash[str(record["sha256"])].append(relative)
            for relative in new_paths:
                record = files.get(relative, {})
                if (
                    record.get("kind") == "markdown"
                    and record.get("identity_state") == "legacy"
                    and record.get("sha256")
                    and _managed_identity_path(settings, relative)
                ):
                    new_by_hash[str(record["sha256"])].append(relative)
            for content_hash in sorted(set(deleted_by_hash) & set(new_by_hash)):
                old_paths = deleted_by_hash[content_hash]
                current_paths = new_by_hash[content_hash]
                if len(old_paths) == len(current_paths) == 1:
                    issues.append(
                        {
                            "severity": "warning",
                            "code": "possible_legacy_move",
                            "path": current_paths[0],
                            "target": old_paths[0],
                            "message": (
                                "A unique content hash suggests a legacy note moved. "
                                "This is not an identity assertion; assign stable IDs before moves."
                            ),
                        }
                    )

        files = dict(sorted(files.items(), key=lambda item: item[0].casefold()))
        identity_index, identity_summary, identity_issues = _identity_state(
            settings, files
        )
        issues.extend(identity_issues)
        issues = sorted(
            issues,
            key=lambda item: (
                str(item.get("severity", "")),
                str(item.get("code", "")),
                str(item.get("path", "")).casefold(),
            ),
        )
        revision = _revision(fingerprint, files)
        content_digest = _content_digest(files)
        changed_state = (
            reset
            or changed > 0
            or bool(deleted)
            or issues != old.get("issues", [])
            or identity_index != old.get("identity_index", {})
            or revision != old.get("revision", "")
            or content_digest != old.get("content_digest", "")
            or not settings.catalog_path.exists()
        )
        catalog = {
            "version": CATALOG_VERSION,
            "scope_fingerprint": fingerprint,
            "revision": revision,
            "content_digest": content_digest,
            "updated_at": _utc_now() if changed_state else old.get("updated_at", ""),
            "files": files,
            "identity_index": identity_index,
            "identity_summary": identity_summary,
            "issues": issues,
        }
        summary = {
            "version": CATALOG_VERSION,
            "scanned": len(current),
            "files": len(files),
            "changed": changed,
            "unchanged": unchanged,
            "deleted": len(deleted),
            "errors": sum(item["severity"] == "error" for item in issues),
            "possible_legacy_moves": sum(
                item.get("code") == "possible_legacy_move" for item in issues
            ),
            "reset": reset,
            "verify": verify,
            "dry_run": dry_run,
            "revision": revision,
            "catalog_revision": revision,
            "content_digest": content_digest,
            "identity_summary": identity_summary,
        }
        if not dry_run and changed_state:
            _atomic_json(settings.catalog_path, catalog)
        return catalog, summary


def ensure_note_id(settings: Settings, path: str) -> dict[str, Any]:
    relative = _relative_argument(path, "path")
    if Path(relative).suffix.casefold() != ".md":
        raise ValueError("Stable IDs can only be assigned to Markdown files")
    if settings.catalog.identity_paths and not _managed_identity_path(
        settings, relative
    ):
        raise ValueError("The note is outside catalog.identity_paths")
    if not any(_under_prefix(relative, prefix) for prefix in settings.catalog.include):
        raise ValueError("The note is outside catalog.include")
    if any(_under_prefix(relative, prefix) for prefix in settings.catalog.exclude):
        raise ValueError("The note is excluded from the catalog")

    target = settings.vault / relative
    vault = settings.vault.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as exc:
        raise ValueError("The note must stay inside the vault") from exc
    if resolved != target.absolute():
        raise ValueError("Stable IDs cannot be assigned through a path alias")
    if not target.is_file():
        raise ValueError(f"Markdown note not found: {relative}")

    with _CatalogLock(settings.catalog_lock_path):
        original = target.read_bytes()
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("The Markdown note must be UTF-8") from exc
        frontmatter_match = FRONTMATTER_RE.match(text)
        if text.startswith("---") and frontmatter_match is None:
            raise ValueError("The Markdown note has malformed frontmatter")
        frontmatter, _body = parse_frontmatter(text)
        if "id" in frontmatter:
            existing = frontmatter["id"]
            if isinstance(existing, str) and NOTE_ID_RE.fullmatch(existing):
                return {
                    "path": relative,
                    "id": existing,
                    "identity_state": "stable",
                    "changed": False,
                }
            raise ValueError(
                "Existing frontmatter id must match note_<32 lowercase hexadecimal characters>"
            )

        note_id = f"note_{uuid.uuid4().hex}"
        newline = "\r\n" if "\r\n" in text else "\n"
        if frontmatter_match is not None:
            opening_end = text.find("\n") + 1
            updated = text[:opening_end] + f"id: {note_id}{newline}" + text[opening_end:]
        else:
            updated = (
                f"---{newline}id: {note_id}{newline}---{newline}{newline}{text}"
            )
        _atomic_note_bytes(target, original, updated.encode("utf-8"))
        return {
            "path": relative,
            "id": note_id,
            "identity_state": "stable",
            "changed": True,
        }


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _bounded_value(value: Any, *, list_limit: int = 12, depth: int = 0) -> Any:
    if isinstance(value, str):
        return (
            value
            if len(value) <= CONTEXT_MAX_STRING
            else value[: CONTEXT_MAX_STRING - 3] + "..."
        )
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if depth >= 4:
        return _bounded_value(str(value), list_limit=list_limit, depth=depth + 1)
    if isinstance(value, list):
        result = [
            _bounded_value(item, list_limit=list_limit, depth=depth + 1)
            for item in value[:list_limit]
        ]
        if len(value) > list_limit:
            result.append({"_truncated": True, "total_items": len(value)})
        return result
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]).casefold())
        result = {
            str(key): _bounded_value(item, list_limit=list_limit, depth=depth + 1)
            for key, item in items[:CONTEXT_MAX_FIELDS]
        }
        if len(items) > CONTEXT_MAX_FIELDS:
            result["_truncated"] = True
            result["_total_fields"] = len(items)
        return result
    return _bounded_value(str(value), list_limit=list_limit, depth=depth + 1)


def _public_record(relative: str, record: dict[str, Any]) -> dict[str, Any]:
    return _bounded_value({
        "relative_path": relative,
        "id": record.get("id", ""),
        "identity_state": record.get("identity_state", ""),
        "identity_key": record.get("identity_key", ""),
        "title": record.get("title", ""),
        "aliases": record.get("aliases", []),
        "kind": record.get("kind", ""),
        "extension": record.get("extension", ""),
        "type": record.get("type", ""),
        "status": record.get("status", ""),
        "metadata": record.get("metadata", {}),
        "collections": record.get("collections", []),
        "primary_collection": record.get("primary_collection", ""),
    })


def _find_catalog_matches(
    catalog: dict[str, Any],
    query: str,
    *,
    collection: str | None = None,
    under: str | None = None,
    note_type: str | None = None,
    mode: str = "broad",
) -> list[dict[str, Any]]:
    if not query.strip():
        raise ValueError("Catalog query cannot be empty")
    if mode not in {"broad", "exact"}:
        raise ValueError("mode must be 'broad' or 'exact'")
    query_key = _key(query)
    query_path_key = _key(query.replace("\\", "/").strip("/"))
    under_path = _relative_argument(under, "under") if under else None
    results: list[dict[str, Any]] = []
    for relative, record in catalog.get("files", {}).items():
        if collection and collection not in record.get("collections", []):
            continue
        if under_path and not _under_prefix(relative, under_path):
            continue
        if note_type and _key(str(record.get("type", ""))) != _key(note_type):
            continue
        path_without_suffix = relative[: -len(Path(relative).suffix)] if Path(relative).suffix else relative
        exact_path_values = {_key(relative), _key(path_without_suffix)}
        stem_key = _key(Path(relative).stem)
        title_key = _key(str(record.get("title", "")))
        alias_keys = {_key(str(alias)) for alias in record.get("aliases", [])}
        exact_name_values = {stem_key, title_key, *alias_keys}
        note_id = _key(str(record.get("id", "")))
        searchable = exact_path_values | exact_name_values
        if note_id and query_key == note_id:
            score = 110
            match_type = "exact_id"
        elif query_path_key in exact_path_values:
            score = 100
            match_type = "exact_path"
        elif query_key == title_key:
            score = 98
            match_type = "exact_title"
        elif query_key in alias_keys:
            score = 97
            match_type = "exact_alias"
        elif query_key == stem_key:
            score = 96
            match_type = "exact_stem"
        elif mode == "exact":
            continue
        elif any(value.startswith(query_key) for value in searchable):
            score = 80
            match_type = "prefix"
        elif any(query_key in value for value in searchable):
            score = 60
            match_type = "contains"
        else:
            continue
        result = _public_record(relative, record)
        result["score"] = score
        result["match_type"] = match_type
        results.append(result)
    results.sort(
        key=lambda item: (
            -int(item["score"]),
            str(item["relative_path"]).casefold(),
            str(item["relative_path"]),
        )
    )
    return results


def find_catalog_page(
    catalog: dict[str, Any],
    query: str,
    *,
    collection: str | None = None,
    under: str | None = None,
    note_type: str | None = None,
    limit: int = 20,
    mode: str = "broad",
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    effective_limit = min(limit, CONTEXT_MAX_LIMIT)
    matches = _find_catalog_matches(
        catalog,
        query,
        collection=collection,
        under=under,
        note_type=note_type,
        mode=mode,
    )
    if mode == "exact" and matches:
        best_score = int(matches[0]["score"])
        matches = [item for item in matches if int(item["score"]) == best_score]
    results = matches[:effective_limit]
    status = (
        "not_found"
        if not matches
        else "unique"
        if mode == "exact" and len(matches) == 1
        else "ambiguous"
        if mode == "exact"
        else "matches"
    )
    return {
        "schema": "vault-agent/v1",
        "command": "find",
        "status": status,
        "match_type": results[0]["match_type"] if len(matches) == 1 else None,
        "warnings": [],
        "results": results,
        "total": len(matches),
        "truncated": len(matches) > len(results),
        "limit_requested": limit,
        "limit_effective": effective_limit,
    }


def find_in_catalog(
    catalog: dict[str, Any],
    query: str,
    *,
    collection: str | None = None,
    under: str | None = None,
    note_type: str | None = None,
    limit: int = 20,
    mode: str = "broad",
) -> list[dict[str, Any]]:
    return find_catalog_page(
        catalog,
        query,
        collection=collection,
        under=under,
        note_type=note_type,
        limit=limit,
        mode=mode,
    )["results"]


def catalog_inventory(catalog: dict[str, Any]) -> dict[str, Any]:
    """Return an explicit complete path inventory without note bodies."""
    paths = sorted(catalog.get("files", {}), key=str.casefold)
    return {
        "schema": "vault-agent/v1",
        "command": "list",
        "status": "complete",
        "catalog_revision": catalog.get("revision", ""),
        "content_digest": catalog.get("content_digest", ""),
        "paths": paths,
        "total": len(paths),
        "truncated": False,
    }


def _resolved_note(relative: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id", ""),
        "path": relative,
        "title": record.get("title", ""),
        "identity_state": record.get("identity_state", ""),
        "type": record.get("type", ""),
        "collections": record.get("collections", []),
    }


def resolve_catalog(
    catalog: dict[str, Any],
    *,
    note_id: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    if not note_id and not path:
        raise ValueError("resolve requires --id or --path")
    files: dict[str, dict[str, Any]] = catalog.get("files", {})
    id_matches: list[str] = []
    path_matches: list[str] = []
    if note_id:
        id_key = _key(note_id)
        id_matches = sorted(
            [
                relative
                for relative, record in files.items()
                if _key(str(record.get("id", ""))) == id_key
            ],
            key=str.casefold,
        )
    if path:
        normalized = _relative_argument(path, "path")
        path_key = normalized.casefold()
        path_matches = sorted(
            [
                relative
                for relative in files
                if relative.casefold() == path_key
                or (
                    not Path(normalized).suffix
                    and relative.casefold() == (path_key + ".md")
                )
            ],
            key=str.casefold,
        )

    warnings: list[dict[str, Any]] = []
    matches = id_matches if note_id and not path else path_matches
    match_type = "stable_id" if note_id and not path else "path"
    status = "not_found"
    if note_id and path:
        if len(id_matches) > 1 or len(path_matches) > 1:
            status = "ambiguous"
            matches = sorted(set([*id_matches, *path_matches]), key=str.casefold)
        elif id_matches and path_matches and id_matches[0] != path_matches[0]:
            status = "id_path_conflict"
            matches = [id_matches[0], path_matches[0]]
        elif id_matches or path_matches:
            status = "unique" if id_matches and path_matches else "not_found"
            matches = id_matches or path_matches
            match_type = "stable_id_and_path"
    elif len(matches) == 1:
        status = "unique"
    elif len(matches) > 1:
        status = "ambiguous"

    return {
        "schema": "vault-agent/v1",
        "command": "resolve",
        "status": status,
        "match_type": match_type if status == "unique" else None,
        "note": _resolved_note(matches[0], files[matches[0]])
        if status == "unique"
        else None,
        "matches": [
            _resolved_note(relative, files[relative]) for relative in matches
        ]
        if status in {"ambiguous", "id_path_conflict"}
        else [],
        "warnings": warnings,
    }


def _immediate_child_counts(
    files: dict[str, dict[str, Any]], prefixes: tuple[str, ...]
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for relative in files:
        matching = [prefix for prefix in prefixes if _under_prefix(relative, prefix)]
        if not matching:
            continue
        prefix = max(matching, key=_specificity).strip("/")
        remainder = relative if prefix in {"", "."} else relative[len(prefix) :].lstrip("/")
        child = remainder.split("/", 1)[0]
        if child:
            counts[child] += 1
    return [
        {"name": name, "files": count}
        for name, count in sorted(counts.items(), key=lambda item: item[0].casefold())
    ]


def _collection_summary(
    name: str,
    collection: CatalogCollection,
    files: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    records = [record for record in files.values() if name in record.get("collections", [])]
    markdown = sum(record.get("kind") == "markdown" for record in records)
    result: dict[str, Any] = {
        "name": name,
        "role": collection.role,
        "files": len(records),
        "markdown": markdown,
        "other_files": len(records) - markdown,
    }
    top_level_paths = tuple(
        path
        for path in collection.paths
        if path not in {"", "."} and "/" not in path and not Path(path).suffix
    )
    if top_level_paths:
        children = _immediate_child_counts(files, top_level_paths)
        result.update(
            {
                "children": children[:12],
                "total_children": len(children),
                "truncated": len(children) > 12,
            }
    )
    return result


def _lookup(files: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    lookup: dict[str, set[str]] = defaultdict(set)
    for relative, record in files.items():
        suffix = Path(relative).suffix
        without_suffix = relative[: -len(suffix)] if suffix else relative
        values = [
            relative,
            without_suffix,
            Path(relative).stem,
            str(record.get("id", "")),
            str(record.get("title", "")),
            *(str(alias) for alias in record.get("aliases", [])),
        ]
        for value in values:
            if value:
                lookup[_key(value)].add(relative)
    return {
        key: sorted(values, key=str.casefold)
        for key, values in sorted(lookup.items(), key=lambda item: item[0])
    }


def _resolve_link(
    source: str,
    target: str,
    files: dict[str, dict[str, Any]],
    lookup: dict[str, list[str]],
) -> list[str]:
    target = target.replace("\\", "/").strip().strip("/")
    if not target:
        return [source]
    suffix = Path(target).suffix
    without_suffix = target[: -len(suffix)] if suffix.casefold() == ".md" else target
    candidates: set[str] = set(lookup.get(_key(without_suffix), []))
    if "/" in target:
        rooted = without_suffix + ("" if suffix.casefold() == ".md" else "")
        candidates.update(lookup.get(_key(rooted), []))
        relative = normalize_path(Path(source).parent / without_suffix)
        candidates.update(lookup.get(_key(relative), []))
    return sorted((item for item in candidates if item in files), key=str.casefold)


def _backlinks(
    target_path: str,
    files: dict[str, dict[str, Any]],
    lookup: dict[str, list[str]],
    limit: int,
) -> list[str]:
    values: list[str] = []
    for source, record in files.items():
        for link in record.get("outlinks", []):
            if target_path in _resolve_link(source, str(link.get("target", "")), files, lookup):
                values.append(source)
                break
    return sorted(values, key=str.casefold)[:limit]


def _ledger_relative(settings: Settings) -> str:
    if settings.catalog.reading_ledger is None:
        return ""
    return normalize_path(settings.catalog.reading_ledger.relative_to(settings.vault))


def _ledger_issue(
    severity: str,
    code: str,
    message: str,
    *,
    unit_id: str = "",
    path: str = "",
    target: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if unit_id:
        value["unit_id"] = unit_id
    if path:
        value["path"] = path
    if target:
        value["target"] = target
    return value


def _load_ledger(
    settings: Settings,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    path = settings.catalog.reading_ledger
    if path is None:
        return None, [], "disabled"
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None, [
            _ledger_issue(
                "error",
                "ledger_missing",
                "The configured reading ledger does not exist.",
                path=_ledger_relative(settings),
            )
        ], "missing"
    except OSError:
        return None, [
            _ledger_issue(
                "error",
                "ledger_unreadable",
                "The configured reading ledger cannot be read.",
                path=_ledger_relative(settings),
            )
        ], "unreadable"
    digest = hashlib.sha256(content).hexdigest()
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, [
            _ledger_issue(
                "error",
                "ledger_invalid_json",
                "The configured reading ledger is not valid UTF-8 JSON.",
                path=_ledger_relative(settings),
            )
        ], f"invalid:{digest}"
    if not isinstance(value, dict):
        return None, [
            _ledger_issue("error", "ledger_invalid_root", "Ledger root must be an object.")
        ], f"invalid:{digest}"
    return value, [], f"sha256:{digest}"


def _reading_ledger_revision(settings: Settings) -> str:
    _ledger, _issues, revision = _load_ledger(settings)
    return revision


def _ledger_input_entries(
    raw: Any, unit_id: str, issues: list[dict[str, Any]]
) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        issues.append(
            _ledger_issue(
                "error",
                "ledger_paths_not_array",
                "inputs must be an array.",
                unit_id=unit_id,
            )
        )
        return []
    entries: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_path_invalid",
                    "inputs entries must be objects.",
                    unit_id=unit_id,
                )
            )
            continue
        role = item.get("role", "")
        path_text = item.get("path", "")
        presence = item.get("presence", "")
        sha256 = item.get("sha256", "")
        if not isinstance(role, str) or not role.strip():
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_role_missing",
                    "Each input needs a non-empty role.",
                    unit_id=unit_id,
                )
            )
            continue
        if not isinstance(path_text, str) or not path_text.strip():
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_path_missing",
                    "An input is missing a path.",
                    unit_id=unit_id,
                )
            )
            continue
        try:
            relative = _relative_argument(path_text, "inputs.path")
        except ValueError:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_path_unsafe",
                    "An input path must stay inside the vault.",
                    unit_id=unit_id,
                )
            )
            continue
        if not isinstance(presence, str) or presence not in {
            "present",
            "removed",
            "unknown",
        }:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_presence_invalid",
                    "Input presence must be present, removed or unknown.",
                    unit_id=unit_id,
                    path=relative,
                )
            )
            continue
        if not isinstance(sha256, str) or (sha256 and not SHA256_RE.fullmatch(sha256)):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_hash_invalid",
                    "Input sha256 must contain 64 hexadecimal characters.",
                    unit_id=unit_id,
                    path=relative,
                )
            )
            continue
        entries.append(
            {
                "role": role.strip(),
                "path": relative,
                "presence": presence,
                "sha256": sha256.lower(),
            }
        )
    return entries


def _ledger_output_entries(
    raw: Any, unit_id: str, issues: list[dict[str, Any]]
) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        issues.append(
            _ledger_issue(
                "error", "ledger_outputs_not_array", "outputs must be an array.", unit_id=unit_id
            )
        )
        return []
    entries: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_output_invalid",
                    "outputs entries must be objects.",
                    unit_id=unit_id,
                )
            )
            continue
        role = item.get("role", "")
        path_text = item.get("path", "")
        extensions = item.get("extensions", {})
        if not isinstance(role, str) or not role.strip():
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_role_missing",
                    "Each output needs a non-empty role.",
                    unit_id=unit_id,
                )
            )
            continue
        if not isinstance(path_text, str) or not path_text.strip():
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_path_missing",
                    "An output is missing a path.",
                    unit_id=unit_id,
                )
            )
            continue
        try:
            relative = _relative_argument(path_text, "outputs.path")
        except ValueError:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_path_unsafe",
                    "An output path must stay inside the vault.",
                    unit_id=unit_id,
                )
            )
            continue
        if not isinstance(extensions, dict):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_output_extensions_invalid",
                    "Output extensions must be an object.",
                    unit_id=unit_id,
                    path=relative,
                )
            )
            extensions = {}
        note_id = extensions.get("note_id", "")
        if not isinstance(note_id, str) or (
            note_id and not NOTE_ID_RE.fullmatch(note_id)
        ):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_note_id_invalid",
                    "Output extensions.note_id must match note_<32 lowercase hexadecimal characters>.",
                    unit_id=unit_id,
                    path=relative,
                )
            )
            note_id = ""
        entries.append(
            {"role": role.strip(), "path": relative, "note_id": note_id}
        )
    return entries


def _normalized_sources(
    ledger: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if ledger is None:
        return {}, []
    if ledger.get("version") == 1 and isinstance(ledger.get("units"), list):
        source_ids = {
            str(unit.get("source_id", "")).strip()
            for unit in ledger["units"]
            if isinstance(unit, dict) and str(unit.get("source_id", "")).strip()
        }
        return {
            source_id: {
                "source_id": source_id,
                "kind": "legacy",
                "title": source_id,
                "canonical_key": source_id,
                "inbox_roots": [],
            }
            for source_id in sorted(source_ids)
        }, []
    raw_sources = ledger.get("sources", {})
    if not isinstance(raw_sources, dict):
        return {}, [
            _ledger_issue("error", "ledger_sources_not_object", "sources must be an object.")
        ]
    sources: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for source_id, raw_source in raw_sources.items():
        if not isinstance(source_id, str) or not source_id or not isinstance(raw_source, dict):
            issues.append(
                _ledger_issue(
                    "error", "ledger_source_invalid", "Each source must be a named object."
                )
            )
            continue
        required: dict[str, str] = {}
        invalid = False
        for field in ("kind", "title", "canonical_key"):
            value = raw_source.get(field, "")
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_source_field_missing",
                        f"Source {field} must be a non-empty string.",
                        unit_id=source_id,
                    )
                )
                invalid = True
            else:
                required[field] = value.strip()
        raw_roots = raw_source.get("inbox_roots", [])
        if not isinstance(raw_roots, list):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_source_roots_invalid",
                    "Source inbox_roots must be an array.",
                    unit_id=source_id,
                )
            )
            raw_roots = []
            invalid = True
        inbox_roots: list[str] = []
        for root in raw_roots:
            try:
                inbox_roots.append(_relative_argument(root, "inbox_roots"))
            except (TypeError, ValueError):
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_path_unsafe",
                        "Source inbox_roots must stay inside the vault.",
                        unit_id=source_id,
                    )
                )
                invalid = True
        optional_paths: dict[str, str] = {}
        for field in ("official_root", "entry_path"):
            value = raw_source.get(field)
            if value is None:
                continue
            try:
                optional_paths[field] = _relative_argument(value, field)
            except (TypeError, ValueError):
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_path_unsafe",
                        f"Source {field} must stay inside the vault.",
                        unit_id=source_id,
                    )
                )
                invalid = True
        if not invalid:
            sources[source_id] = {
                "source_id": source_id,
                **required,
                "inbox_roots": inbox_roots,
                **optional_paths,
            }
    return sources, issues


def _normalized_legacy_units(
    ledger: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_units = ledger.get("units", [])
    if not isinstance(raw_units, list):
        return [], [
            _ledger_issue("error", "ledger_units_not_array", "Legacy units must be an array.")
        ]
    issues = [
        _ledger_issue(
            "warning",
            "ledger_legacy_format",
            "Legacy version 1 ledger is supported but should migrate to reading-ledger/v1.",
        )
    ]
    allowed_statuses = ledger.get("allowed_statuses")
    allowed_cleanup = ledger.get("allowed_cleanup_statuses")
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_unit in raw_units:
        if not isinstance(raw_unit, dict):
            issues.append(
                _ledger_issue("error", "ledger_unit_invalid", "Each ledger unit must be an object.")
            )
            continue
        unit_id = raw_unit.get("unit_id", "")
        source_id = raw_unit.get("source_id", "")
        if not isinstance(unit_id, str) or not unit_id.strip() or unit_id in seen:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_unit_id_invalid",
                    "Legacy unit_id must be non-empty and unique.",
                )
            )
            continue
        seen.add(unit_id)
        if not isinstance(source_id, str) or not source_id.strip():
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_source_id_missing",
                    "source_id must be a non-empty string.",
                    unit_id=unit_id,
                )
            )
            source_id = ""
        status = raw_unit.get("processing_status", "")
        cleanup = raw_unit.get("cleanup_status", "")
        if not isinstance(status, str) or not status or (
            isinstance(allowed_statuses, list) and status not in allowed_statuses
        ):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_status_unknown",
                    "Legacy processing_status is invalid.",
                    unit_id=unit_id,
                )
            )
            status = ""
        if not isinstance(cleanup, str) or not cleanup or (
            isinstance(allowed_cleanup, list) and cleanup not in allowed_cleanup
        ):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_cleanup_status_unknown",
                    "Legacy cleanup_status is invalid.",
                    unit_id=unit_id,
                )
            )
            cleanup = ""

        converted_inputs: list[dict[str, Any]] = []
        for field, role in (("raw_paths", "raw"), ("draft_paths", "draft")):
            raw_entries = raw_unit.get(field, [])
            if not isinstance(raw_entries, list):
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_paths_not_array",
                        f"{field} must be an array.",
                        unit_id=unit_id,
                    )
                )
                continue
            for item in raw_entries:
                if isinstance(item, str):
                    converted_inputs.append(
                        {"role": role, "path": item, "presence": "present"}
                    )
                elif isinstance(item, dict):
                    converted_inputs.append(
                        {
                            "role": str(item.get("role", role)),
                            "path": item.get("path", ""),
                            "presence": item.get("state", "present"),
                            "sha256": item.get("sha256", ""),
                        }
                    )
                else:
                    converted_inputs.append(item)

        converted_outputs: list[dict[str, Any]] = []
        raw_outputs = raw_unit.get("official_paths", [])
        if isinstance(raw_outputs, list):
            for item in raw_outputs:
                if isinstance(item, str):
                    converted_outputs.append({"role": "official", "path": item})
                elif isinstance(item, dict):
                    converted_outputs.append(
                        {
                            "role": str(item.get("role", "official")),
                            "path": item.get("path", ""),
                        }
                    )
                else:
                    converted_outputs.append(item)
        else:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_outputs_not_array",
                    "official_paths must be an array.",
                    unit_id=unit_id,
                )
            )
        units.append(
            {
                "unit_id": unit_id,
                "source_id": source_id,
                "kind": "legacy",
                "scope": {},
                "processing_status": status,
                "cleanup_status": cleanup,
                "provenance": {"basis": ["legacy-ledger"], "confidence": "unresolved"},
                "issues": [],
                "inputs": _ledger_input_entries(converted_inputs, unit_id, issues),
                "outputs": _ledger_output_entries(converted_outputs, unit_id, issues),
            }
        )
    return units, issues


def _normalized_units(
    ledger: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if ledger is None:
        return [], []
    if ledger.get("version") == 1:
        return _normalized_legacy_units(ledger)
    issues: list[dict[str, Any]] = []
    if ledger.get("schema_version") != LEDGER_VERSION:
        issues.append(
            _ledger_issue(
                "error",
                "ledger_version_invalid",
                f"Ledger schema_version must be {LEDGER_VERSION}.",
            )
        )
    raw_units = ledger.get("units", {})
    if not isinstance(raw_units, dict):
        return [], issues + [
            _ledger_issue("error", "ledger_units_not_object", "Ledger units must be an object.")
        ]
    units: list[dict[str, Any]] = []
    for unit_id, raw_unit in raw_units.items():
        if not isinstance(unit_id, str) or not unit_id:
            issues.append(
                _ledger_issue("error", "ledger_unit_id_missing", "Each unit needs a key.")
            )
            continue
        if not isinstance(raw_unit, dict):
            issues.append(
                _ledger_issue(
                    "error", "ledger_unit_invalid", "Each ledger unit must be an object."
                )
            )
            continue
        status = raw_unit.get("processing_status", "")
        cleanup = raw_unit.get("cleanup_status", "")
        if not isinstance(status, str) or status not in LEDGER_PROCESSING_STATUSES:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_status_unknown",
                    "processing_status is not valid for reading-ledger/v1.",
                    unit_id=unit_id,
                )
            )
            status = ""
        if not isinstance(cleanup, str) or cleanup not in LEDGER_CLEANUP_STATUSES:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_cleanup_status_unknown",
                    "cleanup_status is not valid for reading-ledger/v1.",
                    unit_id=unit_id,
                )
            )
            cleanup = ""
        source_id = raw_unit.get("source_id", "")
        kind = raw_unit.get("kind", "")
        if not isinstance(source_id, str) or not source_id:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_source_id_missing",
                    "source_id must be a non-empty string.",
                    unit_id=unit_id,
                )
            )
            source_id = ""
        if not isinstance(kind, str) or not kind:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_kind_missing",
                    "kind must be a non-empty string.",
                    unit_id=unit_id,
                )
            )
            kind = ""
        scope = raw_unit.get("scope", {})
        if not isinstance(scope, dict):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_scope_invalid",
                    "scope must be an object.",
                    unit_id=unit_id,
                )
            )
            scope = {}
        provenance = raw_unit.get("provenance", {})
        if not isinstance(provenance, dict):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_provenance_invalid",
                    "provenance must be an object.",
                    unit_id=unit_id,
                )
            )
            provenance = {}
        basis = provenance.get("basis", [])
        confidence = provenance.get("confidence", "")
        if not isinstance(basis, list) or not all(isinstance(item, str) for item in basis):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_provenance_basis_invalid",
                    "provenance.basis must be an array of strings.",
                    unit_id=unit_id,
                )
            )
            basis = []
        if not isinstance(confidence, str) or confidence not in {
            "exact",
            "reviewed",
            "unresolved",
        }:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_provenance_confidence_invalid",
                    "provenance.confidence is invalid.",
                    unit_id=unit_id,
                )
            )
            confidence = ""
        declared_issues = raw_unit.get("issues", [])
        if not isinstance(declared_issues, list) or not all(
            isinstance(item, str) for item in declared_issues
        ):
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_issues_invalid",
                    "issues must be an array of strings.",
                    unit_id=unit_id,
                )
            )
            declared_issues = []
        units.append(
            {
                "unit_id": unit_id,
                "source_id": source_id,
                "kind": kind,
                "scope": scope,
                "processing_status": status,
                "cleanup_status": cleanup,
                "provenance": {"basis": basis, "confidence": confidence},
                "issues": declared_issues,
                "inputs": _ledger_input_entries(
                    raw_unit.get("inputs", []), unit_id, issues
                ),
                "outputs": _ledger_output_entries(
                    raw_unit.get("outputs", []), unit_id, issues
                ),
            }
        )
    return units, issues


def _ledger_output_path(
    entry: dict[str, Any], identity_index: dict[str, str]
) -> str:
    note_id = str(entry.get("note_id", ""))
    return identity_index.get(note_id, "") if note_id else str(entry.get("path", ""))


def _matching_reading_units(
    settings: Settings,
    catalog: dict[str, Any],
    focus_path: str | None,
    query: str | None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    str,
]:
    ledger, load_issues, ledger_revision = _load_ledger(settings)
    sources, source_issues = _normalized_sources(ledger)
    units, unit_issues = _normalized_units(ledger)
    identity_index: dict[str, str] = catalog.get("identity_index", {})
    if focus_path:
        exact_file = Path(focus_path).suffix != ""

        def path_matches(path: str) -> bool:
            return path.casefold() == focus_path.casefold() or (
                not exact_file and _under_prefix(path, focus_path)
            )

        units = [
            unit
            for unit in units
            if (
                any(path_matches(entry["path"]) for entry in unit["inputs"])
                or any(
                    path_matches(_ledger_output_path(entry, identity_index))
                    for entry in unit["outputs"]
                    if _ledger_output_path(entry, identity_index)
                )
            )
            or (
                unit["source_id"] in sources
                and any(
                    path_matches(source_path)
                    for source_path in [
                        *sources[unit["source_id"]].get("inbox_roots", []),
                        sources[unit["source_id"]].get("official_root", ""),
                        sources[unit["source_id"]].get("entry_path", ""),
                    ]
                    if source_path
                )
            )
        ]
    if query and not focus_path:
        query_key = _key(query)
        units = [
            unit
            for unit in units
            if query_key in _key(unit["unit_id"]) or query_key in _key(unit["source_id"])
        ]
    matched_source_ids = {unit["source_id"] for unit in units}
    matched_sources = [
        sources[source_id]
        for source_id in sorted(matched_source_ids, key=str.casefold)
        if source_id in sources
    ]
    return (
        units,
        load_issues + source_issues + unit_issues,
        matched_sources,
        ledger_revision,
    )


ENVELOPE_WORKFLOWS = {"auto", "process-input", "update-note", "update-project"}
ENVELOPE_ACTIONS = {"read", "create", "update", "move", "delete"}
PROJECT_HUB_TYPES = {"project", "project-readme"}


def _ancestor_readmes(
    relative_path: str, files: dict[str, dict[str, Any]]
) -> list[str]:
    current = Path(relative_path)
    if current.suffix:
        current = current.parent
    candidates: list[str] = []
    parts = current.parts
    for size in range(1, len(parts) + 1):
        candidate = normalize_path(Path(*parts[:size]) / "README.md")
        if candidate in files:
            candidates.append(candidate)
    return candidates


def _envelope_checks(
    workflow: str, action: str, processor: str, *, ledger_matched: bool
) -> list[str]:
    if workflow == "process-input":
        checks = [
            "read_target",
            "read_entrypoints",
            "ledger_exact",
            "dedupe_exact",
            "extract_candidates",
            "resolve_candidates",
            "update_ledger",
            "validate",
        ]
        if not ledger_matched:
            checks.insert(3, "register_ledger_input")
    elif workflow == "update-project":
        checks = [
            "read_project_hub",
            "validate_hub_references",
            "read_declared_evidence",
            "sync_indexes",
            "validate",
        ]
    else:
        checks = [
            "resolve_target",
            "read_target",
            "read_entrypoints",
            "inspect_direct_links",
            "extract_candidates",
            "resolve_candidates",
            "sync_indexes",
            "validate",
        ]
    if action == "create":
        checks.extend(["dedupe_exact", "ensure_stable_id"])
    if action in {"move", "delete"}:
        checks.extend(
            ["compare_target_hash", "inspect_backlinks_embeds_hubs_ledger"]
        )
    processor_checks = {
        "book-input": ["select_reading_protocol", "preserve_source_coordinates"],
        "paper-input": ["dedupe_citekey_doi"],
        "web-article-input": ["dedupe_url_author"],
        "problem-input": ["dedupe_problem_id_url", "check_patterns_related_problems_concepts"],
        "project-input": ["read_project_hub"],
        "history-source": ["check_people_events_time_concepts"],
        "technical-source": ["separate_source_implementation_experiment_adoption"],
        "leetcode-note": ["check_problem_id_patterns_related_problems_concepts"],
    }
    checks.extend(processor_checks.get(processor, []))
    return list(dict.fromkeys(checks))


def task_envelope(
    settings: Settings,
    catalog: dict[str, Any],
    *,
    path: str | None = None,
    note_id: str | None = None,
    workflow: str = "auto",
    action: str = "update",
    char_budget: int = 2000,
) -> dict[str, Any]:
    if workflow not in ENVELOPE_WORKFLOWS:
        raise ValueError("workflow must be auto, process-input, update-note or update-project")
    if action not in ENVELOPE_ACTIONS:
        raise ValueError("action must be read, create, update, move or delete")
    if char_budget < 1200:
        raise ValueError("char-budget must be at least 1200")
    if not path and not note_id:
        raise ValueError("envelope requires --path or --id")

    files: dict[str, dict[str, Any]] = catalog.get("files", {})
    blocks: list[str] = []
    warning_codes: list[str] = []
    resolved_path = ""
    record: dict[str, Any] | None = None
    if note_id:
        resolved = resolve_catalog(catalog, note_id=note_id, path=path)
        if resolved["status"] == "unique":
            resolved_path = str(resolved["note"]["path"])
            record = files.get(resolved_path)
        else:
            blocks.append(f"target_{resolved['status']}")
    elif path:
        normalized = _relative_argument(path, "path")
        candidates = [normalized]
        if not Path(normalized).suffix:
            candidates.append(normalized + ".md")
        resolved_path = next((item for item in candidates if item in files), normalized)
        record = files.get(resolved_path)
        if record is None and action != "create" and not (settings.vault / resolved_path).is_dir():
            blocks.append("target_not_found")

    target_collections = _matching_collections(
        resolved_path, settings.catalog.collections
    )
    collection_values = [
        settings.catalog.collections[name]
        for name in target_collections
        if name in settings.catalog.collections
    ]
    processors = [item.processor for item in collection_values if item.processor]
    processor = processors[0] if processors else ""
    declared_workflows = list(
        dict.fromkeys(item.workflow for item in collection_values if item.workflow)
    )
    record_type = str((record or {}).get("type", ""))
    derived_workflow = ""
    if record_type in PROJECT_HUB_TYPES:
        derived_workflow = "update-project"
        processor = "project"
    elif len(declared_workflows) == 1:
        derived_workflow = declared_workflows[0]
    elif len(declared_workflows) > 1:
        blocks.append("workflow_registry_conflict")
    elif record is not None:
        derived_workflow = "update-note"

    if workflow == "auto":
        selected_workflow = derived_workflow
        if action == "create":
            blocks.append("workflow_required_for_create")
        elif not selected_workflow:
            blocks.append("workflow_unresolved")
    else:
        selected_workflow = workflow
        declared_processors = set(processors)
        if workflow == "process-input" and "process-input" not in declared_workflows:
            blocks.append("workflow_target_mismatch")
        elif workflow == "update-project" and not (
            derived_workflow == "update-project" or "project-note" in declared_processors
        ):
            blocks.append("workflow_target_mismatch")
        elif workflow == "update-note" and derived_workflow == "process-input":
            blocks.append("workflow_target_mismatch")

    if not selected_workflow:
        selected_workflow = workflow if workflow != "auto" else "unresolved"

    units, ledger_issues, sources, ledger_revision = _matching_reading_units(
        settings, catalog, resolved_path or None, None
    )
    ledger_errors = [
        item for item in ledger_issues if item.get("severity") == "error"
    ]
    warning_codes.extend(
        str(item.get("code"))
        for item in ledger_issues
        if item.get("severity") != "error" and item.get("code")
    )
    if selected_workflow == "process-input" and ledger_errors:
        blocks.extend(
            str(item.get("code") or "ledger_invalid") for item in ledger_errors
        )
    if ledger_revision == "disabled":
        ledger_state = "disabled"
    elif ledger_revision == "missing":
        ledger_state = "missing"
    elif ledger_revision.startswith("invalid:") or ledger_errors:
        ledger_state = "invalid"
    elif units:
        ledger_state = "matched"
    else:
        ledger_state = "not_found"

    entrypoints = _ancestor_readmes(resolved_path, files) if resolved_path else []
    for collection in collection_values:
        entrypoints.extend(collection.entrypoints)
    entrypoints = sorted(
        dict.fromkeys(item for item in entrypoints if item),
        key=lambda item: (item.count("/"), item.casefold(), item),
    )
    unit_ids = [str(item.get("unit_id", "")) for item in units if item.get("unit_id")]
    official_outputs = list(
        dict.fromkeys(
            _ledger_output_path(output, catalog.get("identity_index", {}))
            for unit in units
            for output in unit.get("outputs", [])
            if _ledger_output_path(output, catalog.get("identity_index", {}))
        )
    )
    source_ids = [
        str(item.get("source_id", "")) for item in sources if item.get("source_id")
    ]
    required_checks = _envelope_checks(
        selected_workflow,
        action,
        processor,
        ledger_matched=bool(units),
    )
    links = {
        "outlinks": len((record or {}).get("outlinks", [])),
        "embeds": len((record or {}).get("embeds", [])),
        "backlinks": len(_backlinks(resolved_path, files, _lookup(files), 1000))
        if record is not None
        else 0,
    }
    target = {
        "path": resolved_path,
        "exists": record is not None or (settings.vault / resolved_path).is_dir(),
        "kind": (record or {}).get("kind", "directory" if (settings.vault / resolved_path).is_dir() else "note"),
        "id": (record or {}).get("id", ""),
        "type": record_type,
        "content_hash": (record or {}).get("sha256", ""),
        "collections": target_collections,
    }
    blocks = list(dict.fromkeys(blocks))
    warning_codes = list(dict.fromkeys(warning_codes))
    result: dict[str, Any] = {
        "schema": "vault-agent/task-envelope/v1",
        "command": "envelope",
        "status": "blocked" if blocks else "ready",
        "workflow": selected_workflow,
        "action": action,
        "processor": processor or selected_workflow,
        "target": target,
        "entrypoints": [],
        "ledger": {
            "state": ledger_state,
            "write_required": selected_workflow == "process-input",
            "unit_count": len(unit_ids),
            "unit_ids": [],
            "source_ids": [],
            "official_outputs": [],
        },
        "signals": links,
        "required_checks": required_checks,
        "preflight": {"blocks": blocks, "warnings": []},
        "watermarks": {
            "catalog_revision": catalog.get("revision", ""),
            "content_digest": catalog.get("content_digest", ""),
            "reading_ledger_revision": ledger_revision,
        },
        "truncated": True,
        "omitted_counts": {
            "entrypoints": len(entrypoints),
            "unit_ids": len(unit_ids),
            "source_ids": len(source_ids),
            "official_outputs": len(official_outputs),
            "warnings": len(warning_codes),
        },
        "budget": {"requested_chars": char_budget, "used_chars": 0},
    }
    if _compact_json_chars(result) > char_budget:
        raise ValueError("char-budget is too small for the mandatory envelope fields")
    sections: list[tuple[list[Any], list[Any], str]] = [
        (result["entrypoints"], entrypoints, "entrypoints"),
        (result["ledger"]["unit_ids"], unit_ids, "unit_ids"),
        (result["ledger"]["source_ids"], source_ids, "source_ids"),
        (result["ledger"]["official_outputs"], official_outputs, "official_outputs"),
        (result["preflight"]["warnings"], warning_codes, "warnings"),
    ]
    for destination, items, name in sections:
        for item in items:
            destination.append(_bounded_value(item))
            result["omitted_counts"][name] -= 1
            if _compact_json_chars(result) + 16 > char_budget:
                destination.pop()
                result["omitted_counts"][name] += 1
                break
    result["omitted_counts"] = {
        key: value for key, value in result["omitted_counts"].items() if value > 0
    }
    result["truncated"] = bool(result["omitted_counts"])
    result["budget"]["used_chars"] = _compact_json_chars(result)
    result["budget"]["used_chars"] = _compact_json_chars(result)
    return result


def catalog_context(
    settings: Settings,
    catalog: dict[str, Any],
    *,
    query: str | None = None,
    path: str | None = None,
    collection: str | None = None,
    global_view: bool = False,
    limit: int = 12,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    effective_limit = min(limit, CONTEXT_MAX_LIMIT)
    if collection and collection not in settings.catalog.collections:
        available = ", ".join(settings.catalog.collections) or "none"
        raise ValueError(f"Unknown catalog collection {collection!r}. Available: {available}")
    files: dict[str, dict[str, Any]] = catalog.get("files", {})
    focus_path = _relative_argument(path, "path") if path else None
    focus: dict[str, Any] | None = None
    focus_issues: list[dict[str, Any]] = []
    matched_collections: list[str] = [collection] if collection else []
    lookup = _lookup(files)
    if focus_path:
        exact = focus_path if focus_path in files else ""
        if not exact and focus_path + ".md" in files:
            exact = focus_path + ".md"
        if not exact:
            candidates = lookup.get(_key(focus_path), [])
            exact = candidates[0] if len(candidates) == 1 else ""
            if len(candidates) > 1:
                focus_issues.append(
                    {
                        "severity": "warning",
                        "code": "focus_ambiguous",
                        "path": focus_path,
                        "candidates": candidates[:effective_limit],
                        "candidate_count": len(candidates),
                    }
                )
        if exact:
            record = files[exact]
            matched_collections = list(
                dict.fromkeys([*matched_collections, *record.get("collections", [])])
            )
            parent = normalize_path(Path(exact).parent)
            siblings = [
                relative
                for relative in files
                if relative != exact and normalize_path(Path(relative).parent) == parent
            ]
            focus = {
                "record": _public_record(exact, record),
                "outlinks": record.get("outlinks", [])[:effective_limit],
                "outlinks_count": len(record.get("outlinks", [])),
                "outlinks_truncated": len(record.get("outlinks", [])) > effective_limit,
                "embeds": record.get("embeds", [])[:effective_limit],
                "embeds_count": len(record.get("embeds", [])),
                "embeds_truncated": len(record.get("embeds", [])) > effective_limit,
                "backlinks": _backlinks(exact, files, lookup, effective_limit),
                "siblings": sorted(siblings, key=str.casefold)[:effective_limit],
                "siblings_count": len(siblings),
                "siblings_truncated": len(siblings) > effective_limit,
            }
            focus_path = exact
        else:
            matched_collections = list(
                dict.fromkeys(
                    [
                        *matched_collections,
                        *_matching_collections(focus_path, settings.catalog.collections),
                    ]
                )
            )
            descendants = [
                relative for relative in files if _under_prefix(relative, focus_path)
            ]
            child_counts: Counter[str] = Counter()
            prefix = focus_path.strip("/")
            for relative in descendants:
                remainder = relative[len(prefix) :].lstrip("/")
                child = remainder.split("/", 1)[0]
                if child:
                    child_counts[child] += 1
            focus = {
                "directory": focus_path,
                "files": len(descendants),
                "children": [
                    {"name": name, "files": count}
                    for name, count in sorted(
                        child_counts.items(), key=lambda item: item[0].casefold()
                    )[:effective_limit]
                ],
                "total_children": len(child_counts),
                "children_truncated": len(child_counts) > effective_limit,
            }
            if not descendants and not (settings.vault / focus_path).exists():
                focus_issues.append(
                    {
                        "severity": "warning",
                        "code": "focus_not_found",
                        "path": focus_path,
                    }
                )
    match_page = (
        find_catalog_page(
            catalog,
            query,
            collection=collection,
            limit=effective_limit,
        )
        if query
        else {
            "results": [],
            "total": 0,
            "truncated": False,
            "limit_requested": effective_limit,
            "limit_effective": effective_limit,
        }
    )
    matches = match_page["results"]
    if query and not focus_path and not collection:
        matched_collections = list(
            dict.fromkeys(
                name
                for match in matches
                for name in match.get("collections", [])
            )
        )
    (
        reading_units,
        ledger_issues,
        reading_sources,
        reading_ledger_revision,
    ) = _matching_reading_units(
        settings, catalog, focus_path, query
    )
    source_collections = [
        name
        for source in reading_sources
        if source.get("entry_path") or source.get("official_root")
        for name in _matching_collections(
            str(source.get("entry_path") or source.get("official_root") or ""),
            settings.catalog.collections,
        )
    ]
    matched_collections = list(
        dict.fromkeys([*matched_collections, *source_collections])
    )
    related: list[str] = []
    for name in matched_collections:
        registered = settings.catalog.collections.get(name)
        if registered:
            related.extend(registered.related)
    related = list(dict.fromkeys(item for item in related if item not in matched_collections))
    coverage = {
        "match_mode": "lexical" if query else "structural",
        "semantic_search_performed": False,
        "completeness_guaranteed": False,
        "related_semantics": "priority-hints-not-whitelist",
        "structural_signals": [
            "path",
            "id",
            "title",
            "alias",
            "ledger",
            "wikilink",
            "backlink",
        ],
        "semantic_fallback": "rag-required-for-cross-domain-or-incomplete-discovery",
        "identity_counts": catalog.get("identity_summary", {}),
    }
    if (
        query
        and not focus_path
        and not collection
        and not matches
        and not reading_units
        and not reading_sources
    ):
        return {
            "schema": "vault-agent/v1",
            "command": "context",
            "profile": "discovery",
            "status": "no_match",
            "query": _bounded_value(query),
            "coverage": coverage,
            "matches": [],
            "matches_count": 0,
            "matches_truncated": False,
            "matched_collections": [],
            "related_collections": [],
            "registry": [],
            "active_registry": [],
            "reading_units": [],
            "reading_sources": [],
            "suggested_next": [
                "use_a_specific_title_path_or_alias",
                "run_catalog_find_for_a_known_name",
                "use_rag_for_semantic_candidate_discovery",
                "use_context_global_for_registry_navigation",
            ],
            "watermarks": {
                "catalog_revision": catalog.get("revision", ""),
                "content_digest": catalog.get("content_digest", ""),
                "reading_ledger_revision": reading_ledger_revision,
            },
        }
    registry_all = [
        _collection_summary(name, value, files)
        for name, value in settings.catalog.collections.items()
    ]
    detail_names = list(dict.fromkeys([*matched_collections, *related]))
    active_registry_all = [
        {
            "name": name,
            "paths": list(settings.catalog.collections[name].paths),
            "usage": settings.catalog.collections[name].usage,
            "entrypoints": list(settings.catalog.collections[name].entrypoints),
            "related": list(settings.catalog.collections[name].related),
        }
        for name in detail_names
        if name in settings.catalog.collections
    ]
    visible_registry = (
        registry_all
        if global_view
        else [item for item in registry_all if item.get("name") in detail_names]
    )
    registry = visible_registry[:CONTEXT_MAX_REGISTRY]
    active_registry = active_registry_all[:CONTEXT_MAX_ACTIVE_REGISTRY]
    reading_units_limited = reading_units[:effective_limit]
    reading_sources_limited = reading_sources[:effective_limit]
    ledger_issues_limited = ledger_issues[:CONTEXT_MAX_ISSUES]
    nested_limit = min(effective_limit, 12)
    return {
        "version": CATALOG_VERSION,
        "revision": catalog.get("revision", ""),
        "catalog_revision": catalog.get("revision", ""),
        "content_digest": catalog.get("content_digest", ""),
        "identity_summary": catalog.get("identity_summary", {}),
        "reading_ledger_revision": reading_ledger_revision,
        "reading_ledger_valid": not any(
            issue.get("severity") == "error" for issue in ledger_issues
        ),
        "reading_ledger_issue_count": len(ledger_issues),
        "schema": "vault-agent/v1",
        "command": "context",
        "profile": "global" if global_view else "discovery",
        "status": "global" if global_view else "matches" if query else "focused",
        "coverage": coverage,
        "query": _bounded_value(query or "", list_limit=nested_limit),
        "limits": {
            "requested": limit,
            "effective": effective_limit,
            "string_characters": CONTEXT_MAX_STRING,
            "nested_items": nested_limit,
        },
        "registry": [
            _bounded_value(item, list_limit=nested_limit) for item in registry
        ],
        "registry_count": len(visible_registry),
        "registry_truncated": len(visible_registry) > len(registry),
        "active_registry": [
            _bounded_value(item, list_limit=nested_limit)
            for item in active_registry
        ],
        "active_registry_count": len(active_registry_all),
        "active_registry_truncated": len(active_registry_all) > len(active_registry),
        "focus": _bounded_value(focus, list_limit=nested_limit),
        "focus_issues": [
            _bounded_value(item, list_limit=nested_limit)
            for item in focus_issues[:nested_limit]
        ],
        "focus_issue_count": len(focus_issues),
        "focus_issues_truncated": len(focus_issues) > nested_limit,
        "matched_collections": [
            _bounded_value(item)
            for item in matched_collections[:CONTEXT_MAX_ACTIVE_REGISTRY]
        ],
        "matched_collection_count": len(matched_collections),
        "matched_collections_truncated": len(matched_collections)
        > CONTEXT_MAX_ACTIVE_REGISTRY,
        "related_collections": [
            _bounded_value(item)
            for item in related[:CONTEXT_MAX_ACTIVE_REGISTRY]
        ],
        "related_collection_count": len(related),
        "related_collections_truncated": len(related)
        > CONTEXT_MAX_ACTIVE_REGISTRY,
        "matches": [
            _bounded_value(item, list_limit=nested_limit) for item in matches
        ],
        "matches_count": match_page["total"],
        "matches_truncated": match_page["truncated"],
        "reading_units": [
            _bounded_value(item, list_limit=nested_limit)
            for item in reading_units_limited
        ],
        "reading_unit_count": len(reading_units),
        "reading_units_truncated": len(reading_units) > len(reading_units_limited),
        "reading_sources": [
            _bounded_value(item, list_limit=nested_limit)
            for item in reading_sources_limited
        ],
        "reading_source_count": len(reading_sources),
        "reading_sources_truncated": len(reading_sources)
        > len(reading_sources_limited),
        "reading_ledger_issues": [
            _bounded_value(item, list_limit=nested_limit)
            for item in ledger_issues_limited
        ],
        "reading_ledger_issue_count": len(ledger_issues),
        "reading_ledger_issues_truncated": len(ledger_issues)
        > len(ledger_issues_limited),
    }


def _route_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id", ""),
        "path": record.get("relative_path", ""),
        "title": record.get("title", ""),
        "identity_state": record.get("identity_state", ""),
        "type": record.get("type", ""),
        "collections": record.get("collections", []),
    }


def _route_ledger_unit(unit: dict[str, Any]) -> dict[str, Any]:
    return _bounded_value(
        {
            "unit_id": unit.get("unit_id", ""),
            "source_id": unit.get("source_id", ""),
            "kind": unit.get("kind", ""),
            "scope": unit.get("scope", {}),
            "processing_status": unit.get("processing_status", ""),
            "cleanup_status": unit.get("cleanup_status", ""),
            "issues": unit.get("issues", []),
            "inputs": [
                {
                    "role": item.get("role", ""),
                    "path": item.get("path", ""),
                    "presence": item.get("presence", ""),
                }
                for item in unit.get("inputs", [])
            ],
            "outputs": [
                {
                    "role": item.get("role", ""),
                    "path": item.get("path", ""),
                    "note_id": item.get("note_id", ""),
                }
                for item in unit.get("outputs", [])
            ],
        },
        list_limit=8,
    )


def _compact_json_chars(value: dict[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def route_catalog_context(
    context: dict[str, Any], *, char_budget: int = 6000
) -> dict[str, Any]:
    if char_budget < 2000:
        raise ValueError("char-budget must be at least 2000")
    focus = context.get("focus") or {}
    focus_issues = list(context.get("focus_issues") or [])
    ledger_issues = list(context.get("reading_ledger_issues") or [])
    issue_codes = {str(item.get("code", "")) for item in focus_issues}
    status = (
        "no_match"
        if context.get("status") == "no_match"
        else "global"
        if context.get("status") == "global"
        else "ambiguous"
        if "focus_ambiguous" in issue_codes
        else "not_found"
        if "focus_not_found" in issue_codes
        else "ready"
    )
    target: dict[str, Any] | None = None
    if focus.get("record"):
        target = {"kind": "note", **_route_record(focus["record"])}
    elif focus.get("directory"):
        target = {
            "kind": "directory",
            "path": focus.get("directory"),
            "files": focus.get("files", 0),
        }

    route_items = [
        _bounded_value(
            {
                "name": item.get("name", ""),
                "usage": item.get("usage", ""),
                "entrypoints": item.get("entrypoints", []),
            },
            list_limit=8,
        )
        for item in context.get("active_registry", [])
    ]
    collection_items = (
        []
        if context.get("matched_collections")
        else [
            _bounded_value(
                {"name": item.get("name", ""), "role": item.get("role", "")},
                list_limit=4,
            )
            for item in context.get("registry", [])
        ]
    )
    reading_units = [
        _route_ledger_unit(item) for item in context.get("reading_units", [])
    ]
    reading_sources = [
        _bounded_value(item, list_limit=8)
        for item in context.get("reading_sources", [])
    ]
    matches = [
        _route_record(item) for item in context.get("matches", [])
    ]
    warnings = [
        _bounded_value(item, list_limit=8)
        for item in [*focus_issues, *ledger_issues]
    ]
    links = {
        name: list(focus.get(name, []) or [])
        for name in ("outlinks", "backlinks", "embeds", "siblings")
    }
    counts = {
        "routes": int(context.get("active_registry_count", len(route_items))),
        "collection_index": int(
            context.get("registry_count", len(collection_items))
        )
        if collection_items
        else 0,
        "matches": int(context.get("matches_count", len(matches))),
        "reading_sources": int(
            context.get("reading_source_count", len(reading_sources))
        ),
        "reading_units": int(
            context.get("reading_unit_count", len(reading_units))
        ),
        "warnings": int(context.get("focus_issue_count", len(focus_issues)))
        + int(context.get("reading_ledger_issue_count", len(ledger_issues))),
        **{
            name: int(focus.get(f"{name}_count", len(items)))
            for name, items in links.items()
        },
    }
    omitted = dict(counts)
    omitted["matched_collections"] = max(
        0,
        int(context.get("matched_collection_count", 0))
        - len(context.get("matched_collections", [])),
    )
    omitted["related_collections"] = max(
        0,
        int(context.get("related_collection_count", 0))
        - len(context.get("related_collections", [])),
    )
    result: dict[str, Any] = {
        "schema": "vault-agent/v1",
        "command": "context",
        "profile": "route",
        "status": status,
        "query": context.get("query", ""),
        "target": target,
        "watermarks": {
            "catalog_revision": context.get("catalog_revision", ""),
            "content_digest": context.get("content_digest", ""),
            "reading_ledger_revision": context.get(
                "reading_ledger_revision", ""
            ),
        },
        "coverage": context.get("coverage", {}),
        "matched_collections": context.get("matched_collections", []),
        "related_collections": context.get("related_collections", []),
        "routes": [],
        "collection_index": [],
        "reading_units": [],
        "reading_sources": [],
        "matches": [],
        "links": {name: [] for name in links},
        "warnings": [],
        "suggested_next": context.get("suggested_next", []),
        "counts": counts,
        "truncated": True,
        "omitted_counts": omitted,
        "budget": {"requested_chars": char_budget, "used_chars": 0},
    }
    if _compact_json_chars(result) > char_budget:
        raise ValueError("char-budget is too small for the mandatory route fields")

    sections: list[tuple[str, list[Any], int]] = [
        ("reading_units", reading_units, counts["reading_units"]),
        ("routes", route_items, counts["routes"]),
        ("reading_sources", reading_sources, counts["reading_sources"]),
        ("matches", matches, counts["matches"]),
        ("warnings", warnings, counts["warnings"]),
        ("collection_index", collection_items, counts["collection_index"]),
    ]
    for name, items, _total in sections:
        destination = result[name]
        for item in items:
            destination.append(item)
            result["omitted_counts"][name] = max(
                0, result["omitted_counts"][name] - 1
            )
            if _compact_json_chars(result) + 16 > char_budget:
                destination.pop()
                result["omitted_counts"][name] += 1
                break
    for name, items in links.items():
        destination = result["links"][name]
        for item in items:
            destination.append(_bounded_value(item, list_limit=8))
            result["omitted_counts"][name] = max(
                0, result["omitted_counts"][name] - 1
            )
            if _compact_json_chars(result) + 16 > char_budget:
                destination.pop()
                result["omitted_counts"][name] += 1
                break

    result["omitted_counts"] = {
        key: value
        for key, value in result["omitted_counts"].items()
        if value > 0
    }
    result["truncated"] = bool(result["omitted_counts"])
    result["budget"]["used_chars"] = _compact_json_chars(result)
    result["budget"]["used_chars"] = _compact_json_chars(result)
    return result


def catalog_status(
    settings: Settings, catalog: dict[str, Any], *, verbose: bool = False
) -> dict[str, Any]:
    validation = validate_catalog(settings, catalog)
    issues = validation["issues"]
    ledger_errors = [
        item
        for item in issues
        if item.get("severity") == "error"
        and str(item.get("code", "")).startswith("ledger_")
    ]
    catalog_errors = [
        item
        for item in issues
        if item.get("severity") == "error"
        and not str(item.get("code", "")).startswith("ledger_")
    ]
    if settings.catalog.reading_ledger is None:
        ledger_state = "disabled"
    else:
        ledger_state = "invalid" if ledger_errors else "valid"
    catalog_state = "invalid" if catalog_errors else "ready"
    result: dict[str, Any] = {
        "schema": "vault-agent/v1",
        "command": "status",
        "status": "ready"
        if catalog_state == "ready" and ledger_state in {"valid", "disabled"}
        else "invalid",
        "catalog": catalog_state,
        "ledger": ledger_state,
        "warnings": validation["summary"]["warnings"],
        "watermarks": {
            "catalog_revision": catalog.get("revision", ""),
            "content_digest": catalog.get("content_digest", ""),
            "reading_ledger_revision": _reading_ledger_revision(settings),
        },
    }
    if verbose:
        result.update(
            {
                "summary": validation["summary"],
                "warning_codes": sorted(
                    {
                        str(item.get("code"))
                        for item in issues
                        if item.get("severity") == "warning" and item.get("code")
                    }
                ),
            }
        )
    return result


def _issue(
    severity: str,
    code: str,
    message: str,
    *,
    path: str = "",
    target: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if path:
        value["path"] = path
    if target:
        value["target"] = target
    return value


def _validate_ledger(
    settings: Settings,
    files: dict[str, dict[str, Any]],
    identity_index: dict[str, str],
) -> list[dict[str, Any]]:
    ledger, issues, _revision = _load_ledger(settings)
    sources, source_issues = _normalized_sources(ledger)
    units, normalized_issues = _normalized_units(ledger)
    issues.extend(source_issues)
    issues.extend(normalized_issues)
    for source_id, source in sources.items():
        for root in source.get("inbox_roots", []):
            if not (settings.vault / root).exists():
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_source_root_missing",
                        "A source inbox_root does not exist.",
                        unit_id=source_id,
                        path=root,
                    )
                )
        official_root = source.get("official_root", "")
        if official_root and not (settings.vault / official_root).exists():
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_source_root_missing",
                    "A source official_root does not exist.",
                    unit_id=source_id,
                    path=official_root,
                )
            )
        entry_path = source.get("entry_path", "")
        if entry_path and entry_path not in files:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_source_entry_missing",
                    "A source entry_path is not cataloged.",
                    unit_id=source_id,
                    path=entry_path,
                )
            )
    for unit in units:
        if unit["source_id"] not in sources:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_source_unknown",
                    "The unit source_id is not declared in sources.",
                    unit_id=unit["unit_id"],
                )
            )
        official_outputs = [
            entry for entry in unit["outputs"] if entry.get("role") == "official"
        ]
        if unit["processing_status"] in {"integrated", "verified"} and not official_outputs:
            issues.append(
                _ledger_issue(
                    "error",
                    "ledger_integrated_without_output",
                    "Integrated or verified units require an official output.",
                    unit_id=unit["unit_id"],
                )
            )
        if unit["cleanup_status"] in {
            "ready-for-cleanup",
            "raw-and-draft-cleaned",
        }:
            cleanup_inputs = [
                entry
                for entry in unit["inputs"]
                if entry.get("role") in {"raw", "draft"}
            ]
            unhashed_cleanup_inputs = [
                entry
                for entry in cleanup_inputs
                if entry.get("presence") not in {"present", "removed"}
                or not entry.get("sha256")
            ]
            if not cleanup_inputs or unhashed_cleanup_inputs:
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_cleanup_without_raw_hash",
                        "Every raw or draft cleanup input requires a content hash or tombstone hash.",
                        unit_id=unit["unit_id"],
                    )
                )
            if unit["processing_status"] not in {"integrated", "verified"}:
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_cleanup_before_integration",
                        "Cleanup-ready units must be integrated or verified first.",
                        unit_id=unit["unit_id"],
                    )
                )
            if not official_outputs:
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_cleanup_without_output",
                        "Cleanup-ready units require an official output.",
                        unit_id=unit["unit_id"],
                    )
                )
            if unit["cleanup_status"] == "raw-and-draft-cleaned" and any(
                entry.get("presence") != "removed" for entry in cleanup_inputs
            ):
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_cleaned_input_still_present",
                        "Cleaned units require every raw and draft input to be a removed tombstone.",
                        unit_id=unit["unit_id"],
                    )
                )
        for entry in unit["inputs"]:
            relative = entry["path"]
            record = files.get(relative)
            if entry["presence"] == "present":
                if record is None:
                    issues.append(
                        _ledger_issue(
                            "error",
                            "ledger_path_missing",
                            "A present input path is not cataloged.",
                            unit_id=unit["unit_id"],
                            path=relative,
                        )
                    )
                elif entry["sha256"]:
                    if not record.get("sha256"):
                        issues.append(
                            _ledger_issue(
                                "warning",
                                "ledger_hash_unverifiable",
                                "The path is cataloged without content hashing.",
                                unit_id=unit["unit_id"],
                                path=relative,
                            )
                        )
                    elif entry["sha256"] != record["sha256"]:
                        issues.append(
                            _ledger_issue(
                                "error",
                                "ledger_hash_mismatch",
                                "The ledger hash does not match the current file.",
                                unit_id=unit["unit_id"],
                                path=relative,
                            )
                        )
            elif entry["presence"] == "removed" and record is not None:
                issues.append(
                    _ledger_issue(
                        "warning",
                        "ledger_removed_path_exists",
                        "A removed input path still exists.",
                        unit_id=unit["unit_id"],
                        path=relative,
                    )
                )
        for entry in unit["outputs"]:
            note_id = entry.get("note_id", "")
            if note_id:
                resolved = identity_index.get(note_id, "")
                if not resolved:
                    issues.append(
                        _ledger_issue(
                            "error",
                            "ledger_note_id_missing",
                            "An output note_id does not resolve to one current catalog path.",
                            unit_id=unit["unit_id"],
                            path=entry["path"],
                            target=note_id,
                        )
                    )
                    continue
                if entry["path"] != resolved:
                    current = files.get(entry["path"])
                    if current is None:
                        issues.append(
                            _ledger_issue(
                                "warning",
                                "ledger_output_path_stale",
                                "The output path is stale; note_id resolves the current path.",
                                unit_id=unit["unit_id"],
                                path=entry["path"],
                                target=resolved,
                            )
                        )
                    elif current.get("id") != note_id:
                        issues.append(
                            _ledger_issue(
                                "error",
                                "ledger_output_identity_mismatch",
                                "The output path and note_id identify different notes.",
                                unit_id=unit["unit_id"],
                                path=entry["path"],
                                target=resolved,
                            )
                        )
            elif entry["path"] not in files:
                issues.append(
                    _ledger_issue(
                        "error",
                        "ledger_output_missing",
                        "An output path is not cataloged.",
                        unit_id=unit["unit_id"],
                        path=entry["path"],
                    )
                )
    return issues


def validate_catalog(
    settings: Settings, catalog: dict[str, Any], *, strict: bool = False
) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = catalog.get("files", {})
    issues: list[dict[str, Any]] = list(catalog.get("issues", []))
    for name, collection in settings.catalog.collections.items():
        for prefix in collection.paths:
            target = settings.vault if prefix == "." else settings.vault / prefix
            if not target.exists():
                issues.append(
                    _issue(
                        "error",
                        "collection_path_missing",
                        f"Collection {name!r} points to a missing path.",
                        path=prefix,
                    )
                )
        for entrypoint in collection.entrypoints:
            if entrypoint not in files and not (settings.vault / entrypoint).is_dir():
                issues.append(
                    _issue(
                        "error",
                        "collection_entrypoint_missing",
                        f"Collection {name!r} entrypoint is not cataloged.",
                        path=entrypoint,
                    )
                )
    unclassified = [
        relative for relative, record in files.items() if not record.get("collections")
    ]
    if unclassified:
        issues.append(
            _issue(
                "warning",
                "unclassified_files",
                f"{len(unclassified)} cataloged files do not match any collection.",
                path=unclassified[0],
            )
        )

    names: dict[str, set[str]] = defaultdict(set)
    for relative, record in files.items():
        for value in [
            Path(relative).stem,
            str(record.get("title", "")),
            *(str(alias) for alias in record.get("aliases", [])),
        ]:
            if value:
                names[_key(value)].add(relative)
    for value, paths in sorted(names.items()):
        if len(paths) > 1:
            ordered = sorted(paths, key=str.casefold)
            issues.append(
                _issue(
                    "warning",
                    "ambiguous_name",
                    f"A title, stem or alias resolves to {len(ordered)} files.",
                    path=ordered[0],
                    target=value,
                )
            )

    lookup = _lookup(files)
    for source, record in files.items():
        for kind in ("outlinks", "embeds"):
            for link in record.get(kind, []):
                target = str(link.get("target", ""))
                if not target:
                    continue
                suffix = Path(target).suffix.casefold()
                if kind == "embeds" and suffix and suffix != ".md" and not settings.catalog.include_non_markdown:
                    continue
                resolved = _resolve_link(source, target, files, lookup)
                if not resolved:
                    issues.append(
                        _issue(
                            "warning",
                            "unresolved_wikilink",
                            "A wikilink does not resolve to a cataloged path.",
                            path=source,
                            target=target,
                        )
                    )
                elif len(resolved) > 1:
                    issues.append(
                        _issue(
                            "warning",
                            "ambiguous_wikilink",
                            f"A wikilink resolves to {len(resolved)} files.",
                            path=source,
                            target=target,
                        )
                    )
    issues.extend(
        _validate_ledger(
            settings,
            files,
            catalog.get("identity_index", {}),
        )
    )
    issues = sorted(
        issues,
        key=lambda item: (
            str(item.get("severity", "")),
            str(item.get("code", "")),
            str(item.get("path", "")).casefold(),
            str(item.get("target", "")).casefold(),
        ),
    )
    errors = sum(item.get("severity") == "error" for item in issues)
    warnings = sum(item.get("severity") == "warning" for item in issues)
    valid = errors == 0 and (not strict or warnings == 0)
    return {
        "version": CATALOG_VERSION,
        "revision": catalog.get("revision", ""),
        "catalog_revision": catalog.get("revision", ""),
        "content_digest": catalog.get("content_digest", ""),
        "strict": strict,
        "valid": valid,
        "summary": {
            "files": len(files),
            "collections": len(settings.catalog.collections),
            "errors": errors,
            "warnings": warnings,
            "identities": catalog.get("identity_summary", {}),
        },
        "issues": issues,
    }


def evaluate_catalog(
    settings: Settings, catalog: dict[str, Any], *, strict: bool = False
) -> dict[str, Any]:
    """Run body-free regression checks derived from current explicit vault facts."""
    files: dict[str, dict[str, Any]] = catalog.get("files", {})
    validation = validate_catalog(settings, catalog, strict=False)
    core_failures: list[dict[str, Any]] = []
    quality_failures: list[dict[str, Any]] = []

    identity_total = 0
    identity_passed = 0
    for relative, record in files.items():
        note_id = str(record.get("id", ""))
        if not note_id:
            continue
        identity_total += 1
        resolved = resolve_catalog(catalog, note_id=note_id)
        if (
            resolved.get("status") == "unique"
            and (resolved.get("note") or {}).get("path") == relative
        ):
            identity_passed += 1
        else:
            core_failures.append(
                {
                    "code": "identity_regression",
                    "path": relative,
                    "target": note_id,
                }
            )

    name_owners: dict[str, set[str]] = defaultdict(set)
    record_names: dict[str, set[str]] = {}
    for relative, record in files.items():
        names = {
            value
            for value in [
                Path(relative).stem,
                str(record.get("title", "")),
                *(str(alias) for alias in record.get("aliases", [])),
            ]
            if value
        }
        record_names[relative] = names
        for value in names:
            name_owners[_key(value)].add(relative)

    exact_candidates: list[tuple[str, str]] = []
    exact_ambiguous = 0
    for relative, names in sorted(record_names.items(), key=lambda item: item[0].casefold()):
        for value in sorted(names, key=str.casefold):
            if len(name_owners[_key(value)]) != 1:
                exact_ambiguous += 1
                continue
            exact_candidates.append((relative, value))

    exact_samples = exact_candidates[:EVALUATION_MAX_NAMES]
    exact_total = len(exact_samples)
    exact_passed = 0
    exact_skipped = exact_ambiguous + max(0, len(exact_candidates) - exact_total)
    for relative, value in exact_samples:
        page = find_catalog_page(catalog, value, mode="exact", limit=2)
        if (
            page.get("status") == "unique"
            and page.get("results")
            and page["results"][0]["relative_path"] == relative
        ):
            exact_passed += 1
        else:
            core_failures.append(
                {
                    "code": "exact_lookup_regression",
                    "path": relative,
                    "target": value,
                }
            )

    lookup = _lookup(files)
    relation_total = 0
    relation_passed = 0
    for source, record in files.items():
        for kind in ("outlinks", "embeds"):
            for link in record.get(kind, []):
                target = str(link.get("target", ""))
                if not target:
                    continue
                suffix = Path(target).suffix.casefold()
                if (
                    kind == "embeds"
                    and suffix
                    and suffix != ".md"
                    and not settings.catalog.include_non_markdown
                ):
                    continue
                relation_total += 1
                resolved = _resolve_link(source, target, files, lookup)
                if len(resolved) == 1:
                    relation_passed += 1
                else:
                    quality_failures.append(
                        {
                            "code": (
                                "unresolved_known_relation"
                                if not resolved
                                else "ambiguous_known_relation"
                            ),
                            "path": source,
                            "target": target,
                        }
                    )

    envelope_total = 0
    envelope_passed = 0
    route_samples: dict[tuple[str, str], str] = {}
    fallback_samples: dict[tuple[Any, ...], str] = {}
    for relative, record in sorted(files.items(), key=lambda item: item[0].casefold()):
        if record.get("kind") != "markdown":
            continue
        collection_values = [
            settings.catalog.collections[name]
            for name in _matching_collections(relative, settings.catalog.collections)
            if name in settings.catalog.collections
        ]
        processors = [item.processor for item in collection_values if item.processor]
        processor = processors[0] if processors else ""
        workflows = list(
            dict.fromkeys(item.workflow for item in collection_values if item.workflow)
        )
        if str(record.get("type", "")) in PROJECT_HUB_TYPES:
            workflow = "update-project"
            processor = "project"
        elif len(workflows) == 1:
            workflow = workflows[0]
        else:
            workflow = "update-note" if not workflows else "conflict"
        route_samples.setdefault((workflow, processor), relative)
        fallback_signature = (
            relative.split("/", 1)[0],
            tuple(record.get("collections", [])),
            str(record.get("type", "")),
            str(record.get("primary_collection", "")),
        )
        fallback_samples.setdefault(fallback_signature, relative)
    envelope_samples = list(route_samples.values())
    for relative in fallback_samples.values():
        if len(envelope_samples) >= EVALUATION_MAX_ENVELOPES:
            break
        if relative not in envelope_samples:
            envelope_samples.append(relative)
    envelope_samples = envelope_samples[:EVALUATION_MAX_ENVELOPES]
    for relative in envelope_samples:
        envelope_total += 1
        first = task_envelope(settings, catalog, path=relative)
        second = task_envelope(settings, catalog, path=relative)
        if first == second and first.get("status") == "ready":
            envelope_passed += 1
        else:
            core_failures.append(
                {
                    "code": (
                        "envelope_nondeterministic"
                        if first != second
                        else "envelope_not_ready"
                    ),
                    "path": relative,
                }
            )

    validation_errors = [
        item for item in validation["issues"] if item.get("severity") == "error"
    ]
    validation_warnings = [
        item for item in validation["issues"] if item.get("severity") == "warning"
    ]
    core_failures.extend(validation_errors)

    ledger, ledger_load_issues, _revision = _load_ledger(settings)
    ledger_units, ledger_unit_issues = _normalized_units(ledger)
    ledger_issues = [*ledger_load_issues, *ledger_unit_issues]
    ledger_enabled = settings.catalog.reading_ledger is not None
    ledger_ok = not any(item.get("severity") == "error" for item in ledger_issues)

    def metric(total: int, passed: int, *, skipped: int = 0) -> dict[str, Any]:
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "skipped": skipped,
            "rate": round(passed / total, 4) if total else None,
        }

    metrics = {
        "stable_id_resolution": metric(identity_total, identity_passed),
        "unique_name_resolution": metric(
            exact_total, exact_passed, skipped=exact_skipped
        ),
        "known_relation_resolution": metric(relation_total, relation_passed),
        "envelope_determinism": metric(envelope_total, envelope_passed),
        "ledger_contract": metric(
            1 if ledger_enabled else 0,
            1 if ledger_enabled and ledger_ok else 0,
        ),
    }
    valid = not core_failures and (not strict or not quality_failures)
    return {
        "schema": "langhuan/catalog-evaluation/v1",
        "status": "passed" if valid else "failed",
        "strict": strict,
        "valid": valid,
        "watermarks": {
            "catalog_revision": catalog.get("revision", ""),
            "content_digest": catalog.get("content_digest", ""),
            "reading_ledger_revision": _reading_ledger_revision(settings),
        },
        "metrics": metrics,
        "summary": {
            "files": len(files),
            "exact_name_sample_limit": EVALUATION_MAX_NAMES,
            "envelope_sample_limit": EVALUATION_MAX_ENVELOPES,
            "ledger_units": len(ledger_units),
            "core_failed": len(core_failures),
            "quality_failed": len(quality_failures),
            "validation_warnings": len(validation_warnings),
        },
        "core_failures": core_failures[:CONTEXT_MAX_ISSUES],
        "core_failures_truncated": len(core_failures) > CONTEXT_MAX_ISSUES,
        "quality_failures": quality_failures[:CONTEXT_MAX_ISSUES],
        "quality_failures_truncated": len(quality_failures) > CONTEXT_MAX_ISSUES,
        "warning_codes": sorted(
            {
                str(item.get("code"))
                for item in validation_warnings
                if item.get("code")
            }
        ),
    }


def evaluate_agent_cases(
    settings: Settings,
    catalog: dict[str, Any],
    cases: dict[str, Any],
    submissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate small explicit agent cases and score evidence-only submissions."""
    if cases.get("schema") != "langhuan/agent-evaluation-cases/v1":
        raise ValueError("Unsupported agent evaluation case schema")
    raw_cases = cases.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Agent evaluation cases must be a non-empty array")

    files: dict[str, dict[str, Any]] = catalog.get("files", {})
    definition_failures: list[dict[str, Any]] = []
    deterministic_failures: list[dict[str, Any]] = []
    case_index: dict[str, dict[str, Any]] = {}

    def failure(bucket: list[dict[str, Any]], code: str, case_id: str, **extra: Any) -> None:
        bucket.append({"code": code, "case_id": case_id, **extra})

    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            failure(definition_failures, "case_not_object", "")
            continue
        case_id = str(raw_case.get("id", "")).strip()
        kind = str(raw_case.get("kind", "")).strip()
        if not case_id or case_id in case_index:
            failure(definition_failures, "case_id_invalid", case_id)
            continue
        if kind not in {"agent", "context", "envelope", "lookup"}:
            failure(definition_failures, "case_kind_invalid", case_id, kind=kind)
            continue
        case_index[case_id] = raw_case
        target_path = str(raw_case.get("target_path", "")).strip()
        if target_path:
            try:
                target_path = _relative_argument(target_path, "target_path")
            except ValueError:
                failure(definition_failures, "target_path_invalid", case_id)
                continue
            if target_path not in files:
                failure(definition_failures, "target_missing", case_id, path=target_path)

        relation_sets: dict[str, set[str]] = {}
        for field in ("must_find", "allowed_adopt", "must_not_adopt"):
            raw_values = raw_case.get(field, [])
            if not isinstance(raw_values, list) or not all(
                isinstance(value, str) and value for value in raw_values
            ):
                failure(definition_failures, "relation_list_invalid", case_id, field=field)
                raw_values = []
            relation_sets[field] = set(raw_values)
        if relation_sets["must_find"] & relation_sets["must_not_adopt"]:
            failure(definition_failures, "relation_sets_overlap", case_id)
        for path in relation_sets["must_find"] | relation_sets["allowed_adopt"]:
            if path not in files:
                failure(definition_failures, "expected_path_missing", case_id, path=path)

        if kind == "envelope" and target_path in files:
            envelope = task_envelope(settings, catalog, path=target_path, action="read")
            expected = raw_case.get("expected", {})
            actual = {
                "status": envelope.get("status"),
                "workflow": envelope.get("workflow"),
                "processor": envelope.get("processor"),
                "ledger_state": envelope.get("ledger", {}).get("state"),
                "ledger_unit_count": envelope.get("ledger", {}).get("unit_count"),
            }
            for field, value in expected.items():
                if actual.get(field) != value:
                    failure(
                        deterministic_failures,
                        "envelope_mismatch",
                        case_id,
                        field=field,
                        expected=value,
                        actual=actual.get(field),
                    )
            missing = sorted(
                set(raw_case.get("required_checks", []))
                - set(envelope.get("required_checks", []))
            )
            if missing:
                failure(
                    deterministic_failures,
                    "required_checks_missing",
                    case_id,
                    missing=missing,
                )
        elif kind == "context":
            result = catalog_context(settings, catalog, query=str(raw_case.get("query", "")))
            expected = raw_case.get("expected", {})
            actual = {
                "status": result.get("status"),
                "registry_count": len(result.get("registry", [])),
                "semantic_search_performed": result.get("coverage", {}).get(
                    "semantic_search_performed"
                ),
            }
            for field, value in expected.items():
                if field == "suggested_next_contains":
                    if value not in result.get("suggested_next", []):
                        failure(
                            deterministic_failures,
                            "context_suggestion_missing",
                            case_id,
                            expected=value,
                        )
                elif actual.get(field) != value:
                    failure(
                        deterministic_failures,
                        "context_mismatch",
                        case_id,
                        field=field,
                        expected=value,
                        actual=actual.get(field),
                    )
        elif kind == "lookup":
            for lookup_case in raw_case.get("lookups", []):
                query = str(lookup_case.get("query", ""))
                mode = str(lookup_case.get("mode", "broad"))
                expected_path = str(lookup_case.get("expected_path", ""))
                page = find_catalog_page(catalog, query, mode=mode, limit=20)
                returned = {item["relative_path"] for item in page.get("results", [])}
                if expected_path not in returned:
                    failure(
                        deterministic_failures,
                        "lookup_miss",
                        case_id,
                        query=query,
                        mode=mode,
                        expected_path=expected_path,
                    )

    scored_submissions: list[dict[str, Any]] = []
    normalized_by_agent: dict[str, dict[str, dict[str, Any]]] = {}
    for submission in submissions or []:
        if submission.get("schema") != "langhuan/agent-evaluation-submission/v1":
            raise ValueError("Unsupported agent evaluation submission schema")
        agent = str(submission.get("agent", "")).strip()
        raw_results = submission.get("cases", {})
        if not agent or not isinstance(raw_results, dict):
            raise ValueError("Each submission needs an agent and case result object")
        normalized_by_agent[agent] = {}
        failures: list[dict[str, Any]] = []
        must_total = must_found = must_opened = evidence_total = evidence_passed = 0
        forbidden_adopted = 0
        target_safety_total = target_safety_passed = 0
        for case_id, case in case_index.items():
            if case.get("kind") != "agent":
                continue
            result = raw_results.get(case_id)
            if not isinstance(result, dict):
                failure(failures, "submission_case_missing", case_id)
                continue
            expected_status = str(case.get("expected_status", "completed"))
            if result.get("status") != expected_status:
                failure(
                    failures,
                    "submission_status_mismatch",
                    case_id,
                    expected=expected_status,
                    actual=result.get("status"),
                )
            found_values = result.get("found_paths", [])
            opened_values = result.get("opened_paths", [])
            query_values = result.get("queries", [])
            decisions = result.get("decisions", [])
            found = set(found_values if isinstance(found_values, list) else [])
            opened = set(opened_values if isinstance(opened_values, list) else [])
            returned: set[str] = set()
            if isinstance(query_values, list):
                for query in query_values:
                    if not isinstance(query, dict) or not all(
                        (
                            isinstance(query.get("query"), str)
                            and query.get("query"),
                            query.get("mode") in {"exact", "broad"},
                            isinstance(query.get("exit_code"), int),
                            isinstance(query.get("returned_paths"), list),
                        )
                    ):
                        failure(failures, "query_evidence_invalid", case_id)
                        continue
                    returned.update(str(path) for path in query["returned_paths"])
            else:
                failure(failures, "query_evidence_invalid", case_id)
            for path in sorted(found | opened | returned):
                if path not in files:
                    failure(failures, "evidence_path_missing", case_id, path=path)
            if not isinstance(decisions, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or item.get("decision") not in {"adopt", "reject", "defer"}
                or not isinstance(item.get("reason"), str)
                or not item.get("reason")
                for item in decisions
            ):
                failure(failures, "decision_evidence_invalid", case_id)
                decisions = []
            adopted = {
                str(item.get("path"))
                for item in decisions
                if isinstance(item, dict) and item.get("decision") == "adopt"
            }
            normalized_by_agent[agent][case_id] = {
                "status": result.get("status"),
                "found": found,
                "opened": opened,
                "adopted": adopted,
            }
            if case.get("expected_status") == "target_required":
                target_safety_total += 1
                if result.get("status") == "target_required":
                    target_safety_passed += 1
                else:
                    failure(failures, "ambiguous_target_guessed", case_id)
            must = set(case.get("must_find", []))
            must_total += len(must)
            must_found += len(must & found)
            must_opened += len(must & opened)
            for path in sorted(must - found):
                failure(failures, "must_find_missed", case_id, path=path)
            for path in sorted(must - opened):
                failure(failures, "must_find_not_opened", case_id, path=path)
            for path in sorted(must - returned):
                failure(failures, "must_find_without_query_evidence", case_id, path=path)
            forbidden = set(case.get("must_not_adopt", [])) & adopted
            forbidden_adopted += len(forbidden)
            for path in sorted(forbidden):
                failure(failures, "forbidden_relation_adopted", case_id, path=path)
            for field in case.get("required_evidence", []):
                evidence_total += 1
                value = result.get(field)
                if isinstance(value, list) and value:
                    evidence_passed += 1
                else:
                    failure(failures, "evidence_missing", case_id, field=field)
        metrics = {
            "must_find_recall": round(must_found / must_total, 4) if must_total else None,
            "must_find_open_rate": round(must_opened / must_total, 4) if must_total else None,
            "forbidden_adopted": forbidden_adopted,
            "evidence_rate": round(evidence_passed / evidence_total, 4)
            if evidence_total
            else None,
            "target_safety_rate": round(target_safety_passed / target_safety_total, 4)
            if target_safety_total
            else None,
        }
        scored_submissions.append(
            {
                "agent": agent,
                "valid": not failures,
                "metrics": metrics,
                "failures": failures,
            }
        )

    consistency_cases: list[dict[str, Any]] = []
    agents = sorted(normalized_by_agent)
    if len(agents) > 1:
        for case_id, case in case_index.items():
            if case.get("kind") != "agent":
                continue
            results = [normalized_by_agent[agent].get(case_id, {}) for agent in agents]
            statuses = {result.get("status") for result in results}
            adopted_sets = [set(result.get("adopted", set())) for result in results]
            union = set().union(*adopted_sets)
            intersection = set(adopted_sets[0]).intersection(*adopted_sets[1:])
            consistency_cases.append(
                {
                    "case_id": case_id,
                    "status_agreement": len(statuses) == 1,
                    "adopted_jaccard": round(len(intersection) / len(union), 4)
                    if union
                    else 1.0,
                }
            )

    valid = not definition_failures and not deterministic_failures and all(
        item["valid"] for item in scored_submissions
    )
    return {
        "schema": "langhuan/agent-evaluation/v1",
        "status": "passed" if valid else "failed",
        "valid": valid,
        "watermarks": {
            "catalog_revision": catalog.get("revision", ""),
            "content_digest": catalog.get("content_digest", ""),
            "reading_ledger_revision": _reading_ledger_revision(settings),
        },
        "summary": {
            "cases": len(case_index),
            "definition_failed": len(definition_failures),
            "deterministic_failed": len(deterministic_failures),
            "submissions": len(scored_submissions),
            "submissions_failed": sum(not item["valid"] for item in scored_submissions),
        },
        "definition_failures": definition_failures,
        "deterministic_failures": deterministic_failures,
        "submissions": scored_submissions,
        "consistency": {"agents": agents, "cases": consistency_cases},
    }


def render_evaluation_markdown(report: dict[str, Any]) -> str:
    """Render a compact human-readable scorecard without note content."""
    lines = [
        "# Structural Memory Evaluation",
        "",
        f"- Status: **{report['status']}**",
        f"- Catalog revision: `{report['watermarks']['catalog_revision']}`",
        f"- Files: {report['summary']['files']}",
        f"- Core failures: {report['summary']['core_failed']}",
        f"- Quality failures: {report['summary']['quality_failed']}",
        "",
        "| Metric | Passed | Total | Rate | Skipped |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, value in report["metrics"].items():
        rate = "n/a" if value["rate"] is None else f"{value['rate']:.1%}"
        lines.append(
            f"| `{name}` | {value['passed']} | {value['total']} | {rate} | {value['skipped']} |"
        )
    for heading, key in (
        ("Core failures", "core_failures"),
        ("Quality failures", "quality_failures"),
    ):
        failures = report[key]
        if not failures:
            continue
        lines.extend(["", f"## {heading}", ""])
        for item in failures:
            detail = " → ".join(
                str(item.get(field))
                for field in ("path", "target")
                if item.get(field)
            )
            lines.append(f"- `{item['code']}`{': ' + detail if detail else ''}")
    return "\n".join(lines) + "\n"
