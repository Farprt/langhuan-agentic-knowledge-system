from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, find_config, load_config, render_config
from .events import log_event
from .index import audit_index, load_index, sync_index
from .search import search


DEMO_NOTES = {
    "Home/Home.md": """---
title: 琅嬛演示库
type: map
---
# 琅嬛演示库

这个知识库使用结构化 Markdown、增量索引和混合检索。参见 [[Projects/RAG]]。
""",
    "Projects/RAG.md": """---
title: 本地优先 RAG
type: project
area: Knowledge Engineering
---
# 本地优先 RAG

## 检索流水线

系统组合确定性 Dense Retrieval、BM25 与 Reciprocal Rank Fusion，并保留标题上下文。

## 隐私边界

索引和事件默认只保存在本机；事件不记录查询正文，云端导出必须显式配置。
""",
}


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _write_config(path: Path, vault: Path, force: bool) -> None:
    if path.exists() and not force:
        raise RuntimeError(f"Refusing to overwrite {path}; pass --force if intentional.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(vault), encoding="utf-8")


def cmd_init(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise RuntimeError(f"Vault directory does not exist: {vault}")
    _write_config(config_path, vault, args.force)
    print(f"Created {config_path}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: dict[str, object] = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_supported": sys.version_info >= (3, 11),
    }
    try:
        config_path = find_config(args.config)
        settings = load_config(config_path)
        model_is_hash = settings.embedding_model == "hash"
        model_is_local = Path(settings.embedding_model).expanduser().exists()
        model_dependency = model_is_hash or importlib.util.find_spec("sentence_transformers") is not None
        checks.update(
            {
                "config": str(config_path),
                "config_valid": True,
                "vault_exists": settings.vault.is_dir(),
                "vault": str(settings.vault),
                "embedding_model": settings.embedding_model,
                "model_local": model_is_hash or model_is_local,
                "model_dependency_available": model_dependency,
                "index_exists": settings.index_path.is_file(),
            }
        )
        if settings.index_path.is_file():
            checks["index_audit"] = audit_index(load_index(settings))
    except ConfigError as exc:
        checks.update({"config_valid": False, "error": str(exc)})
    healthy = bool(
        checks["python_supported"]
        and checks.get("config_valid")
        and checks.get("vault_exists")
        and checks.get("model_local")
        and checks.get("model_dependency_available")
    )
    checks["healthy"] = healthy
    print(_json(checks))
    return 0 if healthy else 1


def _sync(args: argparse.Namespace, force: bool) -> int:
    settings = load_config(args.config)
    summary = sync_index(settings, force=force, dry_run=args.dry_run)
    if not args.dry_run:
        log_event(settings, "index" if force else "sync", summary)
    print(_json(summary))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    results = search(settings, args.query, top_k=args.top_k, scope=args.scope)
    log_event(
        settings,
        "ask",
        {"results": len(results), "scope": args.scope or ""},
        query=args.query,
    )
    if args.json:
        print(_json(results))
    elif not results:
        print("No matching context found.")
    else:
        for number, result in enumerate(results, 1):
            metadata = result["metadata"]
            print(f"[{number}] {metadata['relative_path']} :: {metadata.get('heading_path') or metadata['title']}")
            print(result["text"])
            print()
    return 0


def cmd_demo(_args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="langhuan-demo-") as directory:
        root = Path(directory)
        vault = root / "vault"
        for relative, content in DEMO_NOTES.items():
            path = vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        config_path = root / "langhuan.toml"
        config_path.write_text(render_config(vault), encoding="utf-8")
        settings = load_config(config_path)
        indexed = sync_index(settings)
        query = "事件不记录查询正文"
        results = search(settings, query, top_k=2)
        if not any("RAG.md" in result["metadata"]["relative_path"] for result in results):
            raise RuntimeError("Offline demo self-check failed")
        print(
            _json(
                {
                    "status": "ok",
                    "offline": True,
                    "indexed_files": indexed["audit"]["files"],
                    "indexed_chunks": indexed["audit"]["chunks"],
                    "query": query,
                    "top_result": results[0]["metadata"]["relative_path"],
                }
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="langhuan", description="Local-first Obsidian RAG.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create an untracked langhuan.toml.")
    init.add_argument("--vault", required=True)
    init.add_argument("--config", default="langhuan.toml")
    init.add_argument("--force", action="store_true")
    init.set_defaults(function=cmd_init)

    doctor = commands.add_parser("doctor", help="Run offline configuration checks.")
    doctor.add_argument("--config", default=None)
    doctor.set_defaults(function=cmd_doctor)

    for name, force, help_text in (
        ("index", True, "Build or rebuild the local index."),
        ("sync", False, "Incrementally synchronize changed Markdown files."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", default=None)
        command.add_argument("--dry-run", action="store_true")
        command.set_defaults(function=lambda args, force=force: _sync(args, force))

    ask = commands.add_parser("ask", help="Retrieve evidence; it does not generate an answer.")
    ask.add_argument("query")
    ask.add_argument("--config", default=None)
    ask.add_argument("--top-k", type=int, default=None)
    ask.add_argument("--scope", default=None)
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(function=cmd_ask)

    demo = commands.add_parser("demo", help="Run a self-contained offline smoke test.")
    demo.set_defaults(function=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.function(args))
    except (ConfigError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
