from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any


SAFE_ATTRIBUTE_KEYS = {
    "action",
    "case_id",
    "catalog_valid",
    "entrypoint_count",
    "entrypoint_chars",
    "error_type",
    "low_quality",
    "omitted_entrypoint_count",
    "payload_chars",
    "rag_ready",
    "required_check_count",
    "result_count",
    "start_mode",
    "task_kind",
    "top_distance",
    "token_estimate",
    "variant",
    "workflow",
}


def sanitize_event(
    event: dict[str, Any], *, include_local_context: bool = False
) -> dict[str, Any]:
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    safe_attributes = {
        key: value
        for key, value in attributes.items()
        if key in SAFE_ATTRIBUTE_KEYS
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    payload = {
        "name": f"{event.get('component', 'unknown')}.{event.get('operation', 'unknown')}",
        "timestamp": event.get("timestamp"),
        "trace_id": event.get("trace_id"),
        "run_id": event.get("run_id"),
        "span_id": event.get("span_id"),
        "parent_span_id": event.get("parent_span_id"),
        "agent": event.get("agent", "unknown"),
        "session_key": event.get("session_key"),
        "component": event.get("component", "unknown"),
        "operation": event.get("operation", "unknown"),
        "status": event.get("status", "unknown"),
        "duration_ms": event.get("duration_ms"),
        "attributes": safe_attributes,
        "content_included": False,
    }
    if include_local_context:
        payload["target"] = event.get("target")
    return payload


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"files": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def pending_events(
    event_dir: Path,
    state: dict[str, Any],
    *,
    max_events: int | None = None,
) -> list[tuple[Path, int, dict[str, Any]]]:
    pending: list[tuple[Path, int, dict[str, Any]]] = []
    file_state = state.setdefault("files", {})
    for path in sorted(event_dir.glob("events_*.jsonl")):
        sent_lines = int(file_state.get(path.name, 0))
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line_number <= sent_lines or not raw_line.strip():
                continue
            pending.append((path, line_number, json.loads(raw_line)))
            if max_events is not None and len(pending) >= max_events:
                return pending
    return pending


def _event_times(payload: dict[str, Any]) -> tuple[int, int]:
    from datetime import datetime, timezone

    raw = str(payload.get("timestamp") or "")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    start_ns = int(parsed.timestamp() * 1_000_000_000)
    duration_ms = max(0.0, float(payload.get("duration_ms") or 0.0))
    return start_ns, start_ns + int(duration_ms * 1_000_000)


def _otel_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    component = str(payload["component"])
    span_kind = "RETRIEVER" if component == "rag" else "TASK"
    result: dict[str, Any] = {
        "gen_ai.span.kind": span_kind,
        "gen_ai.operation.name": str(payload["operation"]),
        "langhuan.trace_id": str(payload["trace_id"]),
        "langhuan.run_id": str(payload["run_id"]),
        "langhuan.span_id": str(payload["span_id"]),
        "langhuan.agent": str(payload["agent"]),
        "langhuan.session_key": str(payload["session_key"]),
        "langhuan.component": component,
        "langhuan.status": str(payload["status"]),
        "langhuan.content_included": False,
    }
    if payload.get("parent_span_id"):
        result["langhuan.parent_span_id"] = str(payload["parent_span_id"])
    for key, value in payload["attributes"].items():
        if value is not None:
            result[f"langhuan.{key}"] = value
    target = payload.get("target")
    if isinstance(target, dict) and target.get("path"):
        result["langhuan.target_path"] = str(target["path"])
    return result


def _otel_parent_context(trace_id: str) -> Any:
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        set_span_in_context,
    )

    remote_parent = SpanContext(
        trace_id=int(trace_id, 16),
        span_id=1,
        is_remote=True,
        trace_flags=TraceFlags(0x01),
    )
    return set_span_in_context(NonRecordingSpan(remote_parent))


class _CollectingSpanProcessor:
    def __init__(self) -> None:
        self.spans: list[Any] = []

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def _on_ending(self, span: Any) -> None:
        # OpenTelemetry 1.38+ invokes this hook before on_end. Keep this
        # collector compatible without importing the optional SDK at startup.
        pass

    def on_end(self, span: Any) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _export_agentloop(events: list[dict[str, Any]], timeout: float) -> None:
    required = (
        "AGENTLOOP_ENDPOINT",
        "AGENTLOOP_LICENSE_KEY",
        "AGENTLOOP_PROJECT",
        "AGENTLOOP_WORKSPACE",
    )
    config = {key: os.environ.get(key, "").strip() for key in required}
    missing = [key for key, value in config.items() if not value]
    if missing:
        raise RuntimeError("Missing AgentLoop environment: " + ", ".join(missing))

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.trace import Status, StatusCode

    resource = Resource.create(
        {
            "service.name": os.environ.get(
                "AGENTLOOP_SERVICE_NAME", "langhuan-observability"
            ),
            "service.version": "1",
            "deployment.environment": "local",
            "acs.cms.workspace": config["AGENTLOOP_WORKSPACE"],
            "acs.arms.service.feature": "genai_app",
        }
    )
    processor = _CollectingSpanProcessor()
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("langhuan-observability-exporter", "1")
    exporter = OTLPSpanExporter(
        endpoint=config["AGENTLOOP_ENDPOINT"],
        headers={
            "x-arms-license-key": config["AGENTLOOP_LICENSE_KEY"],
            "x-arms-project": config["AGENTLOOP_PROJECT"],
            "x-cms-workspace": config["AGENTLOOP_WORKSPACE"],
        },
        timeout=timeout,
    )
    try:
        processor.spans.clear()
        for payload in events:
            start_ns, end_ns = _event_times(payload)
            span = tracer.start_span(
                str(payload["name"]),
                context=_otel_parent_context(str(payload["trace_id"])),
                attributes=_otel_attributes(payload),
                start_time=start_ns,
            )
            if payload["status"] not in {"ok", "success", "ready", "passed"}:
                span.set_status(Status(StatusCode.ERROR))
            span.end(end_time=end_ns)
        if exporter.export(processor.spans) != SpanExportResult.SUCCESS:
            raise RuntimeError("AgentLoop OTLP export failed")
    finally:
        exporter.shutdown()
        provider.shutdown()


def _export_langfuse(events: list[dict[str, Any]], timeout: float) -> None:
    from langfuse import Langfuse, propagate_attributes

    client = Langfuse(timeout=int(timeout), tracing_enabled=True)
    for payload in events:
        metadata = {
            "trace_id": payload["trace_id"],
            "run_id": payload["run_id"],
            "span_id": payload["span_id"],
            "parent_span_id": payload["parent_span_id"],
            "agent": payload["agent"],
            "session_key": payload["session_key"],
            "component": payload["component"],
            "status": payload["status"],
            "content_included": False,
            **payload["attributes"],
        }
        if payload.get("target"):
            metadata["target"] = payload["target"]
        with client.start_as_current_observation(
            as_type="span",
            name=str(payload["name"]),
            metadata=metadata,
            trace_context={
                "trace_id": str(payload["trace_id"]),
                "parent_span_id": "0000000000000001",
            },
        ) as span:
            with propagate_attributes(
                trace_name="langhuan-task",
                session_id=str(payload["session_key"]),
                tags=["langhuan", str(payload["component"])],
            ):
                span.update(
                    output={
                        "status": payload["status"],
                        "duration_ms": payload["duration_ms"],
                    }
                )
    client.flush()


def export_events(
    event_dir: Path,
    *,
    provider: str,
    send: bool,
    include_local_context: bool = False,
    batch_size: int = 20,
    max_events: int | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    if provider not in {"agentloop", "langfuse"}:
        raise ValueError("provider must be agentloop or langfuse")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if max_events is not None and max_events < 1:
        raise ValueError("max_events must be at least 1")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    state_path = event_dir / f"{provider}_export_state.json"
    state = load_state(state_path)
    pending = pending_events(event_dir, state, max_events=max_events)
    preview = [
        sanitize_event(entry, include_local_context=include_local_context)
        for _, _, entry in pending[:5]
    ]
    if not send:
        return {"status": "preview", "provider": provider, "pending": len(pending), "preview": preview}

    started = time.perf_counter()
    exported = 0
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset : offset + batch_size]
        payloads = [
            sanitize_event(entry, include_local_context=include_local_context)
            for _, _, entry in batch
        ]
        if provider == "agentloop":
            _export_agentloop(payloads, timeout)
        else:
            _export_langfuse(payloads, timeout)
        for path, line_number, _ in batch:
            state["files"][path.name] = max(
                int(state["files"].get(path.name, 0)), line_number
            )
        save_state(state_path, state)
        exported += len(batch)
    return {
        "status": "success",
        "provider": provider,
        "exported": exported,
        "pending_before": len(pending),
        "duration_s": round(time.perf_counter() - started, 3),
    }
