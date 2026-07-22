from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when langhuan.toml is missing or invalid."""


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

    @property
    def index_path(self) -> Path:
        return self.data_dir / "index.json"

    @property
    def event_log_path(self) -> Path:
        return self.data_dir / "events.jsonl"


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a TOML table")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be an array of strings")
    return tuple(item.replace("\\", "/").strip("/") for item in value if item.strip("/"))


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
    candidate = path or os.environ.get("LANGHUAN_CONFIG") or "langhuan.toml"
    resolved = Path(candidate).expanduser().resolve()
    if not resolved.is_file():
        raise ConfigError(f"Configuration not found: {resolved}. Run `langhuan init --vault PATH`.")
    return resolved


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

    return Settings(
        config_path=config_path,
        vault=_path(vault.get("path"), base, "vault.path"),
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
