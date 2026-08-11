from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from langhuan.observability import current_trace, emit_event, finish_trace, start_trace
from langhuan.observability_export import (
    _CollectingSpanProcessor,
    _export_langfuse,
    _otel_parent_context,
    export_events,
    sanitize_event,
)


ROOT = Path(__file__).resolve().parents[1]


class ObservabilityTests(unittest.TestCase):
    def test_agentloop_example_uses_the_exporter_environment_names(self) -> None:
        path = ROOT / "integrations" / "loongsuite-agentloop" / ".env.example"
        names = {
            line.split("=", 1)[0]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }
        self.assertEqual(
            names,
            {
                "AGENTLOOP_ENDPOINT",
                "AGENTLOOP_LICENSE_KEY",
                "AGENTLOOP_PROJECT",
                "AGENTLOOP_SERVICE_NAME",
                "AGENTLOOP_WORKSPACE",
            },
        )

    def test_task_events_share_trace_and_finish_clears_active_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            started = start_trace(
                data_dir,
                agent="test-agent",
                session_id="session-1",
                target_path="Projects/Example.md",
                workflow="update-project",
                action="update",
            )
            reused = start_trace(
                data_dir,
                agent="test-agent",
                session_id="session-1",
                target_path="Projects/Example.md",
            )
            event = emit_event(
                data_dir,
                agent="test-agent",
                session_id="session-1",
                component="rag",
                operation="query",
                attributes={"result_count": 3},
            )
            self.assertEqual(started["trace_id"], reused["trace_id"])
            self.assertTrue(reused["reused"])
            self.assertEqual(event["trace_id"], started["trace_id"])
            self.assertEqual(event["run_id"], started["run_id"])

            finished = finish_trace(
                data_dir,
                agent="test-agent",
                session_id="session-1",
                status="passed",
            )
            self.assertEqual(finished["trace_id"], started["trace_id"])
            self.assertIsNone(
                current_trace(data_dir, agent="test-agent", session_id="session-1")
            )
            events = []
            for line in next((data_dir / "observability").glob("events_*.jsonl")).read_text(
                encoding="utf-8"
            ).splitlines():
                events.append(json.loads(line))
            self.assertEqual(
                [item["operation"] for item in events],
                ["task.start", "query", "task.finish"],
            )

    def test_export_is_preview_first_and_redacts_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_trace(
                data_dir,
                agent="codex",
                session_id="session-2",
                target_path="Projects/Private.md",
            )
            event_dir = data_dir / "observability"
            result = export_events(event_dir, provider="agentloop", send=False)
            self.assertEqual(result["status"], "preview")
            self.assertEqual(result["pending"], 1)
            self.assertNotIn("Projects/Private.md", json.dumps(result))

            raw_event = json.loads(
                next(event_dir.glob("events_*.jsonl")).read_text(encoding="utf-8")
            )
            raw_event.setdefault("attributes", {}).update(
                {
                    "entrypoint_chars": 1200,
                    "payload_chars": 400,
                    "required_check_count": 3,
                    "private_context": "must-not-export",
                }
            )
            redacted = sanitize_event(raw_event)
            opted_in = sanitize_event(raw_event, include_local_context=True)
            self.assertNotIn("target", redacted)
            self.assertEqual(redacted["attributes"]["entrypoint_chars"], 1200)
            self.assertEqual(redacted["attributes"]["payload_chars"], 400)
            self.assertEqual(redacted["attributes"]["required_check_count"], 3)
            self.assertNotIn("private_context", redacted["attributes"])
            self.assertEqual(opted_in["target"]["path"], "Projects/Private.md")

    def test_langfuse_export_reuses_the_external_trace_id(self) -> None:
        calls = []

        class Context:
            def __init__(self, value=None):
                self.value = value

            def __enter__(self):
                return self.value

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        class Span:
            def update(self, **kwargs):
                calls.append({"update": kwargs})

        class Client:
            def __init__(self, **kwargs):
                calls.append({"client": kwargs})

            def start_as_current_observation(self, **kwargs):
                calls.append({"observation": kwargs})
                return Context(Span())

            def flush(self):
                calls.append({"flush": True})

        fake = types.ModuleType("langfuse")
        fake.Langfuse = Client
        fake.propagate_attributes = lambda **kwargs: Context()
        payload = {
            "name": "rag.query",
            "timestamp": "2026-08-05T00:00:00+00:00",
            "trace_id": "a" * 32,
            "run_id": "b" * 32,
            "span_id": "c" * 16,
            "parent_span_id": "d" * 16,
            "agent": "codex",
            "session_key": "e" * 16,
            "component": "rag",
            "operation": "query",
            "status": "ok",
            "duration_ms": 3.0,
            "attributes": {},
            "content_included": False,
        }
        with patch.dict("sys.modules", {"langfuse": fake}):
            _export_langfuse([payload], 5.0)
        observation = next(item["observation"] for item in calls if "observation" in item)
        self.assertEqual(observation["trace_context"]["trace_id"], "a" * 32)

    def test_otel_parent_context_reuses_the_external_trace_id(self) -> None:
        try:
            from opentelemetry.trace import get_current_span
        except ImportError:
            self.skipTest("optional OpenTelemetry SDK is not installed")
        trace_id = "a" * 32
        context = _otel_parent_context(trace_id)
        self.assertEqual(
            get_current_span(context).get_span_context().trace_id,
            int(trace_id, 16),
        )

    def test_collecting_span_processor_supports_current_otel_end_hook(self) -> None:
        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            self.skipTest("optional OpenTelemetry SDK is not installed")

        processor = _CollectingSpanProcessor()
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(processor)
        span = provider.get_tracer("test").start_span("canary")
        span.end()
        provider.shutdown()
        self.assertEqual(len(processor.spans), 1)


if __name__ == "__main__":
    unittest.main()
