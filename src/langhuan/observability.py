from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import secrets
from typing import Any
import uuid


EVENT_SCHEMA = "langhuan/trace-event/v1"
CONTEXT_SCHEMA = "langhuan/trace-context/v1"


def observability_enabled() -> bool:
    return os.environ.get("LANGHUAN_TRACE_DISABLED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def infer_agent(explicit: str | None = None) -> str:
    if explicit and explicit != "unknown":
        return explicit
    configured = os.environ.get("LANGHUAN_AGENT", "").strip()
    if configured:
        return configured
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("HERMES_HOME"):
        return "hermes"
    return explicit or "unknown"


def infer_session_id(agent: str, explicit: str | None = None) -> str:
    for value in (
        explicit,
        os.environ.get("LANGHUAN_SESSION_ID"),
        os.environ.get("CODEX_THREAD_ID"),
        os.environ.get("HERMES_SESSION_ID"),
    ):
        if value and value.strip():
            return value.strip()
    return f"{agent}:process:{os.getppid()}"


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _root(data_dir: Path) -> Path:
    return data_dir / "observability"


def _active_path(data_dir: Path, session_key: str) -> Path:
    return _root(data_dir) / "active" / f"{session_key}.json"


def _event_path(data_dir: Path, timestamp: str) -> Path:
    day = timestamp[:10]
    return _root(data_dir) / f"events_{day}.jsonl"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_event(data_dir: Path, event: dict[str, Any]) -> None:
    path = _event_path(data_dir, str(event["timestamp"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def _public_context(context: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
    return {
        "schema": context["schema"],
        "trace_id": context["trace_id"],
        "run_id": context["run_id"],
        "root_span_id": context["root_span_id"],
        "agent": context["agent"],
        "session_key": context["session_key"],
        "start_mode": context["start_mode"],
        "started_at": context["started_at"],
        "reused": reused,
    }


def current_trace(
    data_dir: Path,
    *,
    agent: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    resolved_agent = infer_agent(agent)
    key = _session_key(infer_session_id(resolved_agent, session_id))
    path = _active_path(data_dir, key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if payload.get("schema") == CONTEXT_SCHEMA else None


def _make_event(
    context: dict[str, Any],
    *,
    component: str,
    operation: str,
    status: str,
    duration_ms: float | None = None,
    parent_span_id: str | None = None,
    target_path: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "schema": EVENT_SCHEMA,
        "event_id": uuid.uuid4().hex,
        "trace_id": context["trace_id"],
        "run_id": context["run_id"],
        "span_id": secrets.token_hex(8),
        "parent_span_id": parent_span_id or context["root_span_id"],
        "timestamp": timestamp,
        "agent": context["agent"],
        "session_key": context["session_key"],
        "component": component,
        "operation": operation,
        "status": status,
        "duration_ms": round(float(duration_ms), 3) if duration_ms is not None else None,
        "target": {"path": target_path} if target_path else None,
        "attributes": attributes or {},
        "privacy": {"content_included": False},
    }


def start_trace(
    data_dir: Path,
    *,
    agent: str | None = None,
    session_id: str | None = None,
    target_path: str | None = None,
    workflow: str | None = None,
    action: str | None = None,
    start_mode: str = "explicit",
    reuse: bool = True,
) -> dict[str, Any] | None:
    if not observability_enabled():
        return None
    resolved_agent = infer_agent(agent)
    resolved_session = infer_session_id(resolved_agent, session_id)
    key = _session_key(resolved_session)
    active = current_trace(data_dir, agent=resolved_agent, session_id=resolved_session)
    if reuse and active and active.get("target_path") == target_path:
        return _public_context(active, reused=True)
    if active:
        superseded = _make_event(
            active,
            component="task",
            operation="task.superseded",
            status="cancelled",
            target_path=active.get("target_path"),
        )
        _append_event(data_dir, superseded)
    timestamp = datetime.now(timezone.utc).isoformat()
    context = {
        "schema": CONTEXT_SCHEMA,
        "trace_id": uuid.uuid4().hex,
        "run_id": uuid.uuid4().hex,
        "root_span_id": secrets.token_hex(8),
        "agent": resolved_agent,
        "session_key": key,
        "start_mode": start_mode,
        "started_at": timestamp,
        "target_path": target_path,
        "workflow": workflow,
        "action": action,
    }
    _write_json_atomic(_active_path(data_dir, key), context)
    started = _make_event(
        context,
        component="task",
        operation="task.start",
        status="ok",
        parent_span_id=context["root_span_id"],
        target_path=target_path,
        attributes={"workflow": workflow, "action": action, "start_mode": start_mode},
    )
    started["span_id"] = context["root_span_id"]
    started["parent_span_id"] = None
    _append_event(data_dir, started)
    return _public_context(context)


def emit_event(
    data_dir: Path,
    *,
    component: str,
    operation: str,
    status: str = "ok",
    duration_ms: float | None = None,
    target_path: str | None = None,
    attributes: dict[str, Any] | None = None,
    agent: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    if not observability_enabled():
        return None
    context = current_trace(data_dir, agent=agent, session_id=session_id)
    if context is None:
        start_trace(
            data_dir,
            agent=agent,
            session_id=session_id,
            target_path=target_path,
            start_mode="implicit",
        )
        context = current_trace(data_dir, agent=agent, session_id=session_id)
    if context is None:
        return None
    event = _make_event(
        context,
        component=component,
        operation=operation,
        status=status,
        duration_ms=duration_ms,
        target_path=target_path or context.get("target_path"),
        attributes=attributes,
    )
    _append_event(data_dir, event)
    return event


def finish_trace(
    data_dir: Path,
    *,
    status: str,
    agent: str | None = None,
    session_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    clear: bool = True,
) -> dict[str, Any] | None:
    context = current_trace(data_dir, agent=agent, session_id=session_id)
    if context is None:
        return None
    event = _make_event(
        context,
        component="task",
        operation="task.finish",
        status=status,
        target_path=context.get("target_path"),
        attributes=attributes,
    )
    _append_event(data_dir, event)
    if clear:
        _active_path(data_dir, context["session_key"]).unlink(missing_ok=True)
    return event
