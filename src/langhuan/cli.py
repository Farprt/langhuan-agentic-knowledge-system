from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import __version__
from .catalog import (
    CATALOG_VERSION,
    catalog_inventory,
    catalog_status,
    catalog_context,
    evaluate_agent_cases,
    evaluate_catalog,
    ensure_note_id,
    find_catalog_page,
    resolve_catalog,
    render_evaluation_markdown,
    route_catalog_context,
    sync_catalog,
    task_envelope,
    validate_catalog,
)
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


def _json(data: object, *, compact: bool = False) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
        sort_keys=True,
    )


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
            checks["index_audit"] = audit_index(load_index(settings), settings)
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


def cmd_catalog_sync(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    _catalog, summary = sync_catalog(
        settings, verify=args.verify, dry_run=args.dry_run
    )
    print(_json(summary, compact=args.compact))
    return 0


def cmd_catalog_find(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    if args.collection and args.collection not in settings.catalog.collections:
        available = ", ".join(settings.catalog.collections) or "none"
        raise ValueError(
            f"Unknown catalog collection {args.collection!r}. Available: {available}"
        )
    catalog, sync = sync_catalog(settings)
    page = find_catalog_page(
        catalog,
        args.query,
        collection=args.collection,
        under=args.under,
        note_type=args.type,
        limit=args.limit,
        mode=args.mode,
    )
    print(
        _json(
            {
                "version": CATALOG_VERSION,
                "revision": catalog["revision"],
                "catalog_revision": catalog["revision"],
                "content_digest": catalog["content_digest"],
                "sync": sync,
                **page,
            },
            compact=args.compact,
        )
    )
    return 0


def cmd_catalog_context(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    scopes = [bool(args.query), bool(args.path), bool(args.collection), args.global_view]
    if sum(scopes) != 1:
        raise ValueError("context requires exactly one of --query, --path, --collection or --global")
    catalog, _sync = sync_catalog(settings)
    result = catalog_context(
        settings,
        catalog,
        query=args.query,
        path=args.path,
        collection=args.collection,
        global_view=args.global_view,
        limit=args.limit,
    )
    if args.route or args.char_budget is not None:
        result = route_catalog_context(
            result,
            char_budget=args.char_budget or 6000,
        )
    print(_json(result, compact=args.compact))
    return 0


def cmd_catalog_list(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    catalog, _sync = sync_catalog(settings)
    print(_json(catalog_inventory(catalog), compact=args.compact))
    return 0


def cmd_catalog_envelope(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    catalog, _sync = sync_catalog(settings)
    result = task_envelope(
        settings,
        catalog,
        path=args.path,
        note_id=args.id,
        workflow=args.workflow,
        action=args.action,
        char_budget=args.char_budget,
    )
    print(_json(result, compact=args.compact))
    return 0 if result["status"] == "ready" else 1


def cmd_catalog_status(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    catalog, _sync = sync_catalog(settings, verify=True)
    result = catalog_status(settings, catalog, verbose=args.verbose)
    print(_json(result, compact=args.compact))
    return 0 if result["status"] == "ready" else 1


def cmd_catalog_resolve(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    catalog, _sync = sync_catalog(settings)
    result = resolve_catalog(catalog, note_id=args.id, path=args.path)
    print(_json(result, compact=args.compact))
    return 0 if result["status"] == "unique" else 1


def cmd_catalog_validate(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    catalog, sync = sync_catalog(settings, verify=True)
    result = validate_catalog(settings, catalog, strict=args.strict)
    result["sync"] = sync
    print(_json(result, compact=args.compact))
    return 0 if result["valid"] else 1


def cmd_catalog_evaluate(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    catalog, sync = sync_catalog(settings, verify=True)
    result = evaluate_catalog(settings, catalog, strict=args.strict)
    result["sync"] = sync
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_evaluation_markdown(result), encoding="utf-8")
        result["report"] = args.report
    print(_json(result, compact=args.compact))
    return 0 if result["valid"] else 1


def cmd_catalog_evaluate_agent(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    catalog, sync = sync_catalog(settings, verify=True)
    cases = json.loads(Path(args.cases).expanduser().read_text(encoding="utf-8"))
    submissions = [
        json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        for path in args.submission
    ]
    result = evaluate_agent_cases(settings, catalog, cases, submissions)
    result["sync"] = sync
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_json(result), encoding="utf-8")
        result["report"] = args.report
    print(_json(result, compact=args.compact))
    return 0 if result["valid"] else 1


def cmd_catalog_ensure_id(args: argparse.Namespace) -> int:
    settings = load_config(args.config)
    if args.path and args.path_option:
        raise ValueError("Use either the positional path or --path, not both")
    target_path = args.path_option or args.path
    if not target_path:
        raise ValueError("A Markdown path is required")
    result = ensure_note_id(settings, target_path)
    catalog, sync = sync_catalog(settings, verify=True)
    if catalog.get("identity_index", {}).get(result["id"]) != result["path"]:
        raise RuntimeError(
            "The assigned note ID is missing or duplicated in the refreshed catalog"
        )
    print(
        _json(
            {
                "version": CATALOG_VERSION,
                "revision": catalog["revision"],
                "catalog_revision": catalog["revision"],
                "content_digest": catalog["content_digest"],
                "sync": sync,
                **result,
            },
            compact=args.compact,
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

    catalog = commands.add_parser(
        "catalog", help="Inspect the live vault structure without reading note bodies into output."
    )
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)

    catalog_sync = catalog_commands.add_parser(
        "sync", help="Incrementally synchronize structural metadata."
    )
    catalog_sync.add_argument("--config", default=None)
    catalog_sync.add_argument("--verify", action="store_true")
    catalog_sync.add_argument("--dry-run", action="store_true")
    catalog_sync.add_argument("--compact", action="store_true")
    catalog_sync.set_defaults(function=cmd_catalog_sync)

    catalog_find = catalog_commands.add_parser(
        "find", help="Find files by relative path, stem, title or alias."
    )
    catalog_find.add_argument("query")
    catalog_find.add_argument("--config", default=None)
    catalog_find.add_argument("--collection", default=None)
    catalog_find.add_argument("--under", default=None)
    catalog_find.add_argument("--type", default=None)
    catalog_find.add_argument("--limit", type=int, default=20)
    catalog_find.add_argument(
        "--mode", choices=("broad", "exact"), default="broad"
    )
    catalog_find.add_argument("--compact", action="store_true")
    catalog_find.set_defaults(function=cmd_catalog_find)

    catalog_resolve = catalog_commands.add_parser(
        "resolve", help="Resolve one current note by stable ID or exact relative path."
    )
    catalog_resolve.add_argument("--id", default=None)
    catalog_resolve.add_argument("--path", default=None)
    catalog_resolve.add_argument("--config", default=None)
    catalog_resolve.add_argument("--compact", action="store_true")
    catalog_resolve.set_defaults(function=cmd_catalog_resolve)

    catalog_status_parser = catalog_commands.add_parser(
        "status", help="Return compact Catalog and Reading Ledger health."
    )
    catalog_status_parser.add_argument("--config", default=None)
    catalog_status_parser.add_argument("--verbose", action="store_true")
    catalog_status_parser.add_argument("--compact", action="store_true")
    catalog_status_parser.set_defaults(function=cmd_catalog_status)

    catalog_list = catalog_commands.add_parser(
        "list", help="Explicitly return the complete cataloged relative-path inventory."
    )
    catalog_list.add_argument("--all", action="store_true", required=True)
    catalog_list.add_argument("--config", default=None)
    catalog_list.add_argument("--compact", action="store_true")
    catalog_list.set_defaults(function=cmd_catalog_list)

    catalog_context_parser = catalog_commands.add_parser(
        "context", help="Build a bounded structural context capsule for an agent."
    )
    catalog_context_parser.add_argument(
        "--query", default=None, help="Lexically locate candidates by name or identifier."
    )
    catalog_context_parser.add_argument("--config", default=None)
    catalog_context_parser.add_argument("--path", default=None)
    catalog_context_parser.add_argument("--collection", default=None)
    catalog_context_parser.add_argument(
        "--global",
        dest="global_view",
        action="store_true",
        help="Explicitly return the complete Collection Registry overview.",
    )
    catalog_context_parser.add_argument("--limit", type=int, default=12)
    catalog_context_parser.add_argument(
        "--route",
        action="store_true",
        help="Return the deterministic task-routing capsule instead of the full context view.",
    )
    catalog_context_parser.add_argument(
        "--char-budget",
        type=int,
        default=None,
        help="Bound the route capsule by compact JSON characters; implies --route.",
    )
    catalog_context_parser.add_argument("--compact", action="store_true")
    catalog_context_parser.set_defaults(function=cmd_catalog_context)

    catalog_envelope = catalog_commands.add_parser(
        "envelope",
        help="Build the deterministic task contract for one target note.",
    )
    catalog_envelope.add_argument("--path", default=None)
    catalog_envelope.add_argument("--id", default=None)
    catalog_envelope.add_argument(
        "--workflow",
        choices=("auto", "process-input", "update-note", "update-project"),
        default="auto",
    )
    catalog_envelope.add_argument(
        "--action",
        choices=("read", "create", "update", "move", "delete"),
        default="update",
    )
    catalog_envelope.add_argument("--char-budget", type=int, default=2000)
    catalog_envelope.add_argument("--config", default=None)
    catalog_envelope.add_argument("--compact", action="store_true")
    catalog_envelope.set_defaults(function=cmd_catalog_envelope)

    catalog_validate = catalog_commands.add_parser(
        "validate", help="Verify catalog, registry, wikilinks and the optional reading ledger."
    )
    catalog_validate.add_argument("--config", default=None)
    catalog_validate.add_argument("--strict", action="store_true")
    catalog_validate.add_argument("--compact", action="store_true")
    catalog_validate.set_defaults(function=cmd_catalog_validate)

    catalog_evaluate = catalog_commands.add_parser(
        "evaluate",
        help="Run body-free structural regression checks derived from explicit vault facts.",
    )
    catalog_evaluate.add_argument("--config", default=None)
    catalog_evaluate.add_argument("--strict", action="store_true")
    catalog_evaluate.add_argument(
        "--report", default=None, help="Optionally write a Markdown scorecard."
    )
    catalog_evaluate.add_argument("--compact", action="store_true")
    catalog_evaluate.set_defaults(function=cmd_catalog_evaluate)

    catalog_evaluate_agent = catalog_commands.add_parser(
        "evaluate-agent",
        help="Validate explicit agent cases and score evidence-only submissions.",
    )
    catalog_evaluate_agent.add_argument("--cases", required=True)
    catalog_evaluate_agent.add_argument("--submission", action="append", default=[])
    catalog_evaluate_agent.add_argument("--config", default=None)
    catalog_evaluate_agent.add_argument("--report", default=None)
    catalog_evaluate_agent.add_argument("--compact", action="store_true")
    catalog_evaluate_agent.set_defaults(function=cmd_catalog_evaluate_agent)

    catalog_ensure_id = catalog_commands.add_parser(
        "ensure-id", help="Assign a stable frontmatter ID to one existing Markdown note."
    )
    catalog_ensure_id.add_argument("path", nargs="?", default=None)
    catalog_ensure_id.add_argument("--path", dest="path_option", default=None)
    catalog_ensure_id.add_argument("--config", default=None)
    catalog_ensure_id.add_argument("--compact", action="store_true")
    catalog_ensure_id.set_defaults(function=cmd_catalog_ensure_id)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.function(args))
    except (ConfigError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
