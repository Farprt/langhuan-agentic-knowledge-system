from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when langhuan.toml is missing or invalid."""


DEFAULT_CATALOG_FIELDS = (
    "type",
    "status",
    "area",
    "subarea",
    "source_type",
    "book",
    "project",
    "year",
    "start_year",
    "end_year",
    "processing_unit",
    "processing_status",
    "official_note",
)


@dataclass(frozen=True)
class CatalogCollection:
    name: str
    paths: tuple[str, ...]
    role: str
    usage: str = ""
    workflow: str = ""
    processor: str = ""
    entrypoints: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogProcessor:
    name: str
    required_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogSettings:
    include: tuple[str, ...] = (".",)
    exclude: tuple[str, ...] = (".git", ".obsidian", ".langhuan")
    identity_paths: tuple[str, ...] = ()
    include_non_markdown: bool = False
    metadata_fields: tuple[str, ...] = DEFAULT_CATALOG_FIELDS
    collections: dict[str, CatalogCollection] = field(default_factory=dict)
    processors: dict[str, CatalogProcessor] = field(default_factory=dict)
    reading_ledger: Path | None = None


@dataclass(frozen=True)
class Settings:
    config_path: Path
    vault: Path
    data_dir: Path
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    chunk_size: int
    chunk_overlap: int
    embedding_model: str
    reranker_model: str
    dense_candidates: int
    sparse_candidates: int
    top_k: int
    rrf_k: int
    log_events: bool
    include_content_in_events: bool
    scopes: dict[str, tuple[str, ...]]
    catalog: CatalogSettings = field(default_factory=CatalogSettings)

    @property
    def index_path(self) -> Path:
        return self.data_dir / "index.json"

    @property
    def event_log_path(self) -> Path:
        return self.data_dir / "events.jsonl"

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.json"

    @property
    def catalog_lock_path(self) -> Path:
        return self.data_dir / "catalog.lock"


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a TOML table")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be an array of strings")
    return tuple(item.replace("\\", "/").strip("/") for item in value if item.strip("/"))


def _identifiers(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be an array of strings")
    identifiers = tuple(item.strip() for item in value)
    if any(
        not item or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item)
        for item in identifiers
    ):
        raise ConfigError(
            f"{name} entries must use letters, digits, underscores, dots or hyphens"
        )
    return identifiers


def _relative_strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be an array of strings")
    paths: list[str] = []
    for raw in value:
        item = raw.replace("\\", "/")
        path = Path(item)
        if (
            path.is_absolute()
            or item.startswith("/")
            or re.match(r"^[A-Za-z]:", item)
            or ".." in path.parts
        ):
            raise ConfigError(f"{name} entries must be relative and stay inside the vault")
        cleaned = item.strip("/")
        if cleaned:
            paths.append(cleaned)
    return tuple(paths)


def _string(value: Any, name: str, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value.strip()


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _path(value: Any, base: Path, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty path")
    path = Path(os.path.expandvars(value)).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def find_config(path: str | Path | None = None) -> Path:
    explicit = path or os.environ.get("LANGHUAN_CONFIG")
    if explicit:
        resolved = Path(explicit).expanduser().resolve()
        if resolved.is_file():
            return resolved
        raise ConfigError(f"Configuration not found: {resolved}. Run `langhuan init --vault PATH`.")
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / "langhuan.toml"
        if candidate.is_file():
            return candidate.resolve()
    raise ConfigError(
        f"Configuration not found from {Path.cwd()}. Run `langhuan init --vault PATH`."
    )


def load_config(path: str | Path | None = None) -> Settings:
    config_path = find_config(path)
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read {config_path}: {exc}") from exc

    base = config_path.parent
    vault = _table(data, "vault")
    index = _table(data, "index")
    retrieval = _table(data, "retrieval")
    observability = _table(data, "observability")
    raw_scopes = _table(data, "scopes")
    raw_catalog = _table(data, "catalog")
    raw_collections = _table(raw_catalog, "collections")
    raw_processors = _table(raw_catalog, "processors")

    include = _strings(vault.get("include", ["."]), "vault.include")
    if not include:
        raise ConfigError("vault.include cannot be empty; use an explicit path or ['.']")
    exclude = _strings(
        vault.get("exclude", [".git", ".obsidian", ".langhuan", "Assets"]),
        "vault.exclude",
    )
    chunk_size = _positive_int(index.get("chunk_size", 1200), "index.chunk_size")
    chunk_overlap = int(index.get("chunk_overlap", 150))
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ConfigError("index.chunk_overlap must be >= 0 and smaller than chunk_size")

    scopes: dict[str, tuple[str, ...]] = {}
    for name, scope in raw_scopes.items():
        if not isinstance(scope, dict):
            raise ConfigError(f"scopes.{name} must be a TOML table")
        paths = _strings(scope.get("paths", []), f"scopes.{name}.paths")
        if not paths:
            raise ConfigError(f"scopes.{name}.paths cannot be empty")
        scopes[name] = paths

    embedding_model = str(retrieval.get("embedding_model", "hash")).strip()
    if not embedding_model:
        raise ConfigError("retrieval.embedding_model cannot be empty")

    vault_path = _path(vault.get("path"), base, "vault.path")
    catalog_include = _relative_strings(
        raw_catalog.get("include", ["."]), "catalog.include"
    )
    if not catalog_include:
        raise ConfigError("catalog.include cannot be empty; use an explicit path or ['.']")
    catalog_exclude = _relative_strings(
        raw_catalog.get("exclude", [".git", ".obsidian", ".langhuan"]),
        "catalog.exclude",
    )
    identity_paths = _relative_strings(
        raw_catalog.get("identity_paths", []), "catalog.identity_paths"
    )
    metadata_fields = _strings(
        raw_catalog.get("metadata_fields", list(DEFAULT_CATALOG_FIELDS)),
        "catalog.metadata_fields",
    )
    processors: dict[str, CatalogProcessor] = {}
    for name, raw_processor in raw_processors.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ConfigError(
                f"catalog processor {name!r} must use letters, digits, underscores or hyphens"
            )
        if not isinstance(raw_processor, dict):
            raise ConfigError(f"catalog.processors.{name} must be a TOML table")
        processors[name] = CatalogProcessor(
            name=name,
            required_checks=_identifiers(
                raw_processor.get("required_checks", []),
                f"catalog.processors.{name}.required_checks",
            ),
        )
    collections: dict[str, CatalogCollection] = {}
    for name, raw_collection in raw_collections.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ConfigError(
                f"catalog collection {name!r} must use letters, digits, underscores or hyphens"
            )
        if not isinstance(raw_collection, dict):
            raise ConfigError(f"catalog.collections.{name} must be a TOML table")
        paths = _relative_strings(
            raw_collection.get("paths", []), f"catalog.collections.{name}.paths"
        )
        if not paths:
            raise ConfigError(f"catalog.collections.{name}.paths cannot be empty")
        role = _string(
            raw_collection.get("role"), f"catalog.collections.{name}.role"
        )
        if not role:
            raise ConfigError(f"catalog.collections.{name}.role cannot be empty")
        collections[name] = CatalogCollection(
            name=name,
            paths=paths,
            role=role,
            usage=_string(
                raw_collection.get("usage"),
                f"catalog.collections.{name}.usage",
            ),
            workflow=_string(
                raw_collection.get("workflow"),
                f"catalog.collections.{name}.workflow",
            ),
            processor=_string(
                raw_collection.get("processor"),
                f"catalog.collections.{name}.processor",
            ),
            entrypoints=_relative_strings(
                raw_collection.get("entrypoints", []),
                f"catalog.collections.{name}.entrypoints",
            ),
            related=_strings(
                raw_collection.get("related", []),
                f"catalog.collections.{name}.related",
            ),
            fields=_strings(
                raw_collection.get("fields", []),
                f"catalog.collections.{name}.fields",
            ),
        )
        if collections[name].workflow not in {
            "",
            "process-input",
            "update-note",
            "update-project",
        }:
            raise ConfigError(
                f"catalog.collections.{name}.workflow must be process-input, "
                "update-note or update-project"
            )
        if collections[name].processor and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*", collections[name].processor
        ):
            raise ConfigError(
                f"catalog.collections.{name}.processor must use letters, digits, "
                "underscores or hyphens"
            )
        if (
            collections[name].processor
            and collections[name].processor not in processors
        ):
            raise ConfigError(
                f"catalog.collections.{name}.processor references undeclared processor "
                f"{collections[name].processor!r}"
            )
    for collection in collections.values():
        unknown = sorted(set(collection.related) - set(collections))
        if unknown:
            raise ConfigError(
                f"catalog.collections.{collection.name}.related contains unknown collections: "
                + ", ".join(unknown)
            )

    reading_ledger: Path | None = None
    raw_ledger = raw_catalog.get("reading_ledger")
    if raw_ledger is not None:
        ledger_text = _string(raw_ledger, "catalog.reading_ledger")
        if not ledger_text:
            raise ConfigError("catalog.reading_ledger cannot be empty")
        ledger_normalized = ledger_text.replace("\\", "/")
        ledger_relative = Path(ledger_normalized)
        if (
            ledger_relative.is_absolute()
            or ledger_normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", ledger_normalized)
            or ".." in ledger_relative.parts
        ):
            raise ConfigError("catalog.reading_ledger must be a relative path inside the vault")
        reading_ledger = (vault_path / ledger_relative).resolve()
        try:
            reading_ledger.relative_to(vault_path)
        except ValueError as exc:
            raise ConfigError("catalog.reading_ledger must stay inside the vault") from exc

    return Settings(
        config_path=config_path,
        vault=vault_path,
        data_dir=_path(index.get("data_dir", ".langhuan"), base, "index.data_dir"),
        include=include,
        exclude=exclude,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        embedding_model=embedding_model,
        reranker_model=str(retrieval.get("reranker_model", "")).strip(),
        dense_candidates=_positive_int(
            retrieval.get("dense_candidates", 40), "retrieval.dense_candidates"
        ),
        sparse_candidates=_positive_int(
            retrieval.get("sparse_candidates", 40), "retrieval.sparse_candidates"
        ),
        top_k=_positive_int(retrieval.get("top_k", 6), "retrieval.top_k"),
        rrf_k=_positive_int(retrieval.get("rrf_k", 60), "retrieval.rrf_k"),
        log_events=_boolean(observability.get("enabled", True), "observability.enabled"),
        include_content_in_events=_boolean(
            observability.get("include_content", False), "observability.include_content"
        ),
        scopes=scopes,
        catalog=CatalogSettings(
            include=catalog_include,
            exclude=catalog_exclude,
            identity_paths=identity_paths,
            include_non_markdown=_boolean(
                raw_catalog.get("include_non_markdown", False),
                "catalog.include_non_markdown",
            ),
            metadata_fields=metadata_fields,
            collections=collections,
            processors=processors,
            reading_ledger=reading_ledger,
        ),
    )


def render_config(vault: Path) -> str:
    escaped = str(vault.resolve()).replace("\\", "/").replace('"', '\\"')
    return f'''# Local paths and credentials belong in this untracked file.
[vault]
path = "{escaped}"
include = ["."]
exclude = [".git", ".obsidian", ".langhuan", "Assets", "Inbox/Processing"]

[index]
data_dir = ".langhuan"
chunk_size = 1200
chunk_overlap = 150

[catalog]
include = ["."]
exclude = [".git", ".obsidian", ".langhuan"]
identity_paths = ["Sources", "Concepts", "People", "Events", "Time", "Maps", "Areas", "Projects"]
include_non_markdown = false
metadata_fields = ["type", "status", "area", "subarea", "source_type", "book", "project", "year", "start_year", "end_year", "processing_unit", "processing_status", "official_note"]

[catalog.processors.history-source]
required_checks = ["check_people_events_time_concepts"]

[catalog.processors.technical-source]
required_checks = ["separate_source_implementation_experiment_adoption"]

[catalog.processors.leetcode-note]
required_checks = ["check_problem_id_patterns_related_problems_concepts"]

[catalog.processors.project-note]
required_checks = []

[catalog.processors.project]
required_checks = []

[catalog.processors.input]
required_checks = []

[catalog.processors.book-input]
required_checks = ["select_reading_protocol", "preserve_source_coordinates"]

[catalog.processors.paper-input]
required_checks = ["dedupe_citekey_doi"]

[catalog.processors.web-article-input]
required_checks = ["dedupe_url_author"]

[catalog.processors.problem-input]
required_checks = ["dedupe_problem_id_url", "check_patterns_related_problems_concepts"]

[catalog.processors.project-input]
required_checks = ["read_project_hub"]

[catalog.processors.processing-input]
required_checks = []

[catalog.collections.sources]
paths = ["Sources"]
role = "Curated source notes with traceable provenance."
usage = "Check before promoting Inbox material or creating reusable knowledge."
workflow = "update-note"
entrypoints = []
related = ["projects"]

[catalog.collections.projects]
paths = ["Projects"]
role = "Active outcomes, decisions and implementation evidence."
usage = "Use for task-local context; promote reusable knowledge elsewhere."
workflow = "update-note"
processor = "project-note"
entrypoints = []
related = ["sources"]

[retrieval]
embedding_model = "hash"
reranker_model = ""
dense_candidates = 40
sparse_candidates = 40
top_k = 6
rrf_k = 60

[observability]
enabled = true
include_content = false

[scopes.reading]
paths = ["Sources/Books", "Sources/Papers"]

[scopes.projects]
paths = ["Projects"]
'''
