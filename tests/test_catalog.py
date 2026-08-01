from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langhuan.catalog import (
    CATALOG_VERSION,
    _CatalogLock,
    _atomic_json,
    _read_json,
    _scope_fingerprint,
    catalog_context,
    evaluate_agent_cases,
    evaluate_catalog,
    find_catalog_page,
    find_in_catalog,
    route_catalog_context,
    sync_catalog,
    task_envelope,
    validate_catalog,
)
from langhuan.cli import main
from langhuan.config import (
    CatalogCollection,
    ConfigError,
    find_config,
    load_config,
    render_config,
)


def _note(
    title: str, note_type: str, body: str = "", *, note_id: str = ""
) -> str:
    identity = f"id: {note_id}\n" if note_id else ""
    return f"""---
{identity}title: "{title}"
type: {note_type}
status: draft
---
# {title}

{body}
"""


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        for directory in (
            "Sources/Books/History",
            "Concepts",
            "Events",
            "People",
            "Time",
            "Projects",
            "Inbox/Books/WeRead",
        ):
            (self.vault / directory).mkdir(parents=True, exist_ok=True)
        (self.vault / "People" / "Ada.md").write_text(
            _note("Ada", "person"), encoding="utf-8"
        )
        (self.vault / "Events" / "Launch.md").write_text(
            _note("Launch", "historical-event"), encoding="utf-8"
        )
        (self.vault / "Time" / "2026.md").write_text(
            _note("2026", "year"), encoding="utf-8"
        )
        (self.vault / "Concepts" / "Agent.md").write_text(
            _note("Agent", "concept"), encoding="utf-8"
        )
        self.chapter = self.vault / "Sources" / "Books" / "History" / "Chapter.md"
        self.chapter.write_text(
            _note(
                "History Chapter",
                "source-section",
                """See [[Ada]].

```text
[[Code Example That Is Not A Link]]
```
<!-- [[Comment Example That Is Not A Link]] -->
""",
            ),
            encoding="utf-8",
        )
        self.raw = self.vault / "Inbox" / "Books" / "WeRead" / "Chapter raw.md"
        self.raw.write_text("raw source payload", encoding="utf-8")
        self.config_path = self.root / "langhuan.toml"
        self.ledger_path = self.vault / "System" / "Indexes" / "reading-ledger.json"
        self.ledger_path.parent.mkdir(parents=True)
        self._write_ledger()
        config = render_config(self.vault).replace(
            'metadata_fields = ["type", "status", "area", "subarea", "source_type", "book", "project", "year", "start_year", "end_year", "processing_unit", "processing_status", "official_note"]',
            'metadata_fields = ["type", "status", "area", "subarea", "source_type", "book", "project", "year", "start_year", "end_year", "processing_unit", "processing_status", "official_note"]\nreading_ledger = "System/Indexes/reading-ledger.json"',
        )
        config += """

[catalog.collections.concepts]
paths = ["Concepts"]
role = "Reusable concepts."
usage = "Check before creating a concept."

[catalog.collections.events]
paths = ["Events"]
role = "Historical events."
usage = "Check event identity and chronology."

[catalog.collections.people]
paths = ["People"]
role = "People entities."
usage = "Check names and aliases."

[catalog.collections.time]
paths = ["Time"]
role = "Years and periods."
usage = "Check temporal anchors."

[catalog.collections.history_sources]
paths = ["Sources/Books/History"]
role = "Curated historical reading."
usage = "Check concepts, events, people and time."
processor = "history-source"
related = ["concepts", "events", "people", "time"]

[catalog.collections.inbox]
paths = ["Inbox"]
role = "Unprocessed input."
usage = "Check the reading ledger before promotion."
workflow = "process-input"
processor = "input"

[catalog.collections.book_inbox]
paths = ["Inbox/Books"]
role = "Raw book input."
usage = "Preserve provenance and source coordinates."
processor = "book-input"

[catalog.collections.system]
paths = ["System"]
role = "Machine-readable workflow state."
usage = "Use as policy and ledger infrastructure."
"""
        self.config_path.write_text(config, encoding="utf-8")
        self.settings = load_config(self.config_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_ledger(self) -> None:
        raw_hash = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        self.ledger_path.write_text(
            json.dumps(
                {
                    "schema_version": "reading-ledger/v1",
                    "sources": {
                        "history-book": {
                            "kind": "book",
                            "title": "History Book",
                            "canonical_key": "book:history",
                            "inbox_roots": ["Inbox/Books/WeRead"],
                            "official_root": "Sources/Books/History",
                            "entry_path": "Sources/Books/History/Chapter.md",
                        }
                    },
                    "units": {
                        "history-chapter-01": {
                            "source_id": "history-book",
                            "kind": "book-section",
                            "scope": {"label": "Chapter 1"},
                            "processing_status": "integrated",
                            "cleanup_status": "ready-for-cleanup",
                            "inputs": [
                                {
                                    "role": "raw",
                                    "path": "Inbox/Books/WeRead/Chapter raw.md",
                                    "sha256": raw_hash,
                                    "presence": "present",
                                }
                            ],
                            "outputs": [
                                {
                                    "role": "official",
                                    "path": "Sources/Books/History/Chapter.md",
                                }
                            ],
                            "provenance": {
                                "basis": ["explicit-test-mapping"],
                                "confidence": "exact",
                            },
                            "issues": [],
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_incremental_create_modify_and_delete(self) -> None:
        first, first_summary = sync_catalog(self.settings)
        second, second_summary = sync_catalog(self.settings)
        self.assertGreater(first_summary["changed"], 0)
        self.assertEqual(second_summary["changed"], 0)
        self.assertEqual(first["revision"], second["revision"])

        note = self.vault / "Concepts" / "New.md"
        note.write_text(_note("New", "concept"), encoding="utf-8")
        _third, third_summary = sync_catalog(self.settings)
        self.assertEqual(third_summary["changed"], 1)

        note.write_text(_note("New version", "concept", "changed"), encoding="utf-8")
        fourth, fourth_summary = sync_catalog(self.settings)
        self.assertEqual(fourth_summary["changed"], 1)
        self.assertEqual(fourth["files"]["Concepts/New.md"]["title"], "New version")

        note.unlink()
        fifth, fifth_summary = sync_catalog(self.settings)
        self.assertEqual(fifth_summary["deleted"], 1)
        self.assertNotIn("Concepts/New.md", fifth["files"])

    def test_v1_catalog_rebuilds_and_digests_have_distinct_semantics(self) -> None:
        first, _summary = sync_catalog(self.settings, verify=True)
        first_revision = first["revision"]
        first_content = first["content_digest"]
        self.assertRegex(first_revision, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(first_content, r"^sha256:[0-9a-f]{64}$")

        self.chapter.write_text(
            self.chapter.read_text(encoding="utf-8") + "\nA plain body-only change.\n",
            encoding="utf-8",
        )
        changed, _summary = sync_catalog(self.settings, verify=True)
        self.assertEqual(changed["revision"], first_revision)
        self.assertNotEqual(changed["content_digest"], first_content)

        self.settings.catalog_path.write_text(
            json.dumps({"version": 1, "files": {"stale": {}}}),
            encoding="utf-8",
        )
        rebuilt, summary = sync_catalog(self.settings, verify=True)
        self.assertEqual(rebuilt["version"], CATALOG_VERSION)
        self.assertTrue(summary["reset"])
        self.assertIn("Sources/Books/History/Chapter.md", rebuilt["files"])

    def test_ensure_id_is_atomic_idempotent_and_findable_after_move(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "ensure-id",
                    "--path",
                    "Concepts/Agent.md",
                    "--config",
                    str(self.config_path),
                ]
            )
        self.assertEqual(code, 0)
        first = json.loads(output.getvalue())
        note_id = first["id"]
        self.assertRegex(note_id, r"^note_[0-9a-f]{32}$")
        self.assertTrue(first["changed"])

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "ensure-id",
                    "Concepts/Agent.md",
                    "--config",
                    str(self.config_path),
                ]
            )
        second = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(second["changed"])
        self.assertEqual(second["id"], note_id)

        before_move, _summary = sync_catalog(self.settings, verify=True)
        moved = self.vault / "Concepts" / "Renamed Agent.md"
        (self.vault / "Concepts" / "Agent.md").rename(moved)
        after_move, _summary = sync_catalog(self.settings, verify=True)
        self.assertNotEqual(after_move["revision"], before_move["revision"])
        self.assertEqual(after_move["content_digest"], before_move["content_digest"])
        self.assertEqual(after_move["identity_index"][note_id], "Concepts/Renamed Agent.md")
        self.assertEqual(
            find_in_catalog(after_move, note_id)[0]["relative_path"],
            "Concepts/Renamed Agent.md",
        )

        missing = self.vault / "Concepts" / "Missing.md"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "catalog",
                    "ensure-id",
                    "Concepts/Missing.md",
                    "--config",
                    str(self.config_path),
                ]
            )
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())

        raw_before = self.raw.read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "catalog",
                    "ensure-id",
                    "Inbox/Books/WeRead/Chapter raw.md",
                    "--config",
                    str(self.config_path),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(self.raw.read_bytes(), raw_before)

    def test_legacy_move_is_only_reported_as_a_hash_based_warning(self) -> None:
        first, _summary = sync_catalog(self.settings, verify=True)
        before_content = first["content_digest"]
        original = self.vault / "Concepts" / "Agent.md"
        moved = self.vault / "Concepts" / "Legacy Agent.md"
        original.rename(moved)

        second, _summary = sync_catalog(self.settings, verify=True)
        warnings = [
            issue
            for issue in second["issues"]
            if issue.get("code") == "possible_legacy_move"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["path"], "Concepts/Legacy Agent.md")
        self.assertEqual(warnings[0]["target"], "Concepts/Agent.md")
        self.assertNotEqual(second["content_digest"], before_content)

    def test_ensure_id_can_add_frontmatter_without_rewriting_the_body(self) -> None:
        plain = self.vault / "Concepts" / "Plain.md"
        plain.write_bytes(b"# Plain\r\n\r\nBody.\r\n")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "ensure-id",
                    "Concepts/Plain.md",
                    "--config",
                    str(self.config_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        updated = plain.read_bytes()
        self.assertIn(f"id: {payload['id']}\r\n".encode(), updated)
        self.assertTrue(updated.endswith(b"# Plain\r\n\r\nBody.\r\n"))

    def test_duplicate_and_malformed_ids_block_validation(self) -> None:
        duplicate = "note_" + "a" * 32
        (self.vault / "Concepts" / "Duplicate One.md").write_text(
            _note("Duplicate One", "concept", note_id=duplicate), encoding="utf-8"
        )
        (self.vault / "Concepts" / "Duplicate Two.md").write_text(
            _note("Duplicate Two", "concept", note_id=duplicate), encoding="utf-8"
        )
        malformed = self.vault / "Concepts" / "Malformed.md"
        malformed.write_text(
            _note("Malformed", "concept", note_id="note_NOT_HEX"),
            encoding="utf-8",
        )
        original = malformed.read_bytes()

        catalog, _summary = sync_catalog(self.settings, verify=True)
        result = validate_catalog(self.settings, catalog)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertFalse(result["valid"])
        self.assertIn("duplicate_note_id", codes)
        self.assertIn("malformed_note_id", codes)
        self.assertNotIn(duplicate, catalog["identity_index"])
        self.assertEqual(
            catalog["files"]["Concepts/Malformed.md"]["identity_state"],
            "malformed",
        )

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "catalog",
                    "ensure-id",
                    "Concepts/Malformed.md",
                    "--config",
                    str(self.config_path),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(malformed.read_bytes(), original)

    def test_catalog_scope_binds_vault_identity(self) -> None:
        first, _summary = sync_catalog(self.settings)
        other_vault = self.root / "other-vault"
        (other_vault / "Concepts").mkdir(parents=True)
        (other_vault / "Concepts" / "Other.md").write_text(
            _note("Other", "concept"), encoding="utf-8"
        )
        other_settings = replace(
            self.settings,
            vault=other_vault,
            catalog=replace(self.settings.catalog, reading_ledger=None),
        )
        second, summary = sync_catalog(other_settings)
        self.assertTrue(summary["reset"])
        self.assertNotEqual(first["scope_fingerprint"], second["scope_fingerprint"])
        self.assertEqual(set(second["files"]), {"Concepts/Other.md"})

    def test_collection_order_change_resets_primary_membership(self) -> None:
        shared = self.vault / "Shared"
        shared.mkdir()
        (shared / "Note.md").write_text(_note("Shared", "concept"), encoding="utf-8")
        first_collection = CatalogCollection(
            name="first", paths=("Shared",), role="First tie-breaker."
        )
        second_collection = CatalogCollection(
            name="second", paths=("Shared",), role="Second tie-breaker."
        )
        first_settings = replace(
            self.settings,
            catalog=replace(
                self.settings.catalog,
                collections={"first": first_collection, "second": second_collection},
                reading_ledger=None,
            ),
        )
        first, _summary = sync_catalog(first_settings)
        second_settings = replace(
            first_settings,
            catalog=replace(
                first_settings.catalog,
                collections={"second": second_collection, "first": first_collection},
            ),
        )
        second, summary = sync_catalog(second_settings)
        self.assertTrue(summary["reset"])
        self.assertNotEqual(
            _scope_fingerprint(first_settings), _scope_fingerprint(second_settings)
        )
        self.assertEqual(first["files"]["Shared/Note.md"]["primary_collection"], "first")
        self.assertEqual(second["files"]["Shared/Note.md"]["primary_collection"], "second")

    def test_history_context_and_reading_ledger(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        result = catalog_context(
            self.settings,
            catalog,
            path="Sources/Books/History/Chapter.md",
        )
        self.assertIn("history_sources", result["matched_collections"])
        self.assertTrue(
            {"concepts", "events", "people", "time"}.issubset(
                result["related_collections"]
            )
        )
        self.assertEqual(result["reading_units"][0]["unit_id"], "history-chapter-01")
        self.assertTrue(result["reading_ledger_revision"].startswith("sha256:"))
        self.assertEqual(
            result["coverage"]["related_semantics"],
            "priority-hints-not-whitelist",
        )
        self.assertIn("id", result["coverage"]["structural_signals"])
        self.assertEqual(
            result["coverage"]["semantic_fallback"],
            "rag-required-for-cross-domain-or-incomplete-discovery",
        )
        registry = {item["name"]: item for item in result["registry"]}
        self.assertEqual(registry["people"]["role"], "People entities.")
        self.assertEqual(registry["events"]["files"], 1)
        self.assertEqual(registry["concepts"]["children"][0]["name"], "Agent.md")
        active = {item["name"]: item for item in result["active_registry"]}
        self.assertEqual(
            active["history_sources"]["usage"],
            "Check concepts, events, people and time.",
        )

        matches = find_in_catalog(catalog, "Ada", collection="people")
        self.assertEqual(matches[0]["relative_path"], "People/Ada.md")

    def test_catalog_evaluation_uses_explicit_facts_and_reports_quality_gaps(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        report = evaluate_catalog(self.settings, catalog)
        self.assertTrue(report["valid"], report)
        self.assertEqual(
            report["metrics"]["known_relation_resolution"]["rate"], 1.0
        )
        self.assertGreater(
            report["metrics"]["unique_name_resolution"]["total"], 0
        )
        self.assertEqual(
            report["metrics"]["envelope_determinism"]["failed"], 0
        )

        self.chapter.write_text(
            _note("History Chapter", "source-section", "See [[Missing Person]]."),
            encoding="utf-8",
        )
        broken, _summary = sync_catalog(self.settings)
        non_strict = evaluate_catalog(self.settings, broken)
        strict = evaluate_catalog(self.settings, broken, strict=True)
        self.assertTrue(non_strict["valid"])
        self.assertGreater(non_strict["summary"]["quality_failed"], 0)
        self.assertFalse(strict["valid"])

    def test_cli_catalog_evaluate_writes_markdown_scorecard(self) -> None:
        report_path = self.root / "evaluation.md"
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "evaluate",
                    "--config",
                    str(self.config_path),
                    "--report",
                    str(report_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], "langhuan/catalog-evaluation/v1")
        self.assertIn("Structural Memory Evaluation", report_path.read_text("utf-8"))

    def test_agent_evaluation_scores_deterministic_cases_and_evidence(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        cases = {
            "schema": "langhuan/agent-evaluation-cases/v1",
            "cases": [
                {
                    "id": "book-envelope",
                    "kind": "envelope",
                    "target_path": "Inbox/Books/WeRead/Chapter raw.md",
                    "expected": {
                        "status": "ready",
                        "workflow": "process-input",
                        "processor": "book-input",
                        "ledger_state": "matched",
                        "ledger_unit_count": 1,
                    },
                    "required_checks": ["extract_candidates", "resolve_candidates"],
                },
                {
                    "id": "person-lookup",
                    "kind": "lookup",
                    "lookups": [
                        {"query": "Ada", "mode": "exact", "expected_path": "People/Ada.md"}
                    ],
                },
                {
                    "id": "ambiguous-target",
                    "kind": "agent",
                    "expected_status": "target_required",
                    "required_evidence": [],
                },
                {
                    "id": "history-relations",
                    "kind": "agent",
                    "target_path": "Sources/Books/History/Chapter.md",
                    "expected_status": "completed",
                    "must_find": ["People/Ada.md"],
                    "allowed_adopt": ["Events/Launch.md"],
                    "must_not_adopt": ["Concepts/Agent.md"],
                    "required_evidence": ["queries", "opened_paths", "decisions"],
                },
            ],
        }
        good = {
            "schema": "langhuan/agent-evaluation-submission/v1",
            "agent": "codex",
            "cases": {
                "ambiguous-target": {"status": "target_required"},
                "history-relations": {
                    "status": "completed",
                    "found_paths": ["People/Ada.md"],
                    "opened_paths": ["People/Ada.md"],
                    "queries": [
                        {
                            "query": "Ada",
                            "mode": "exact",
                            "exit_code": 0,
                            "returned_paths": ["People/Ada.md"],
                        }
                    ],
                    "decisions": [
                        {
                            "path": "People/Ada.md",
                            "decision": "adopt",
                            "reason": "The opened person note matches the source candidate.",
                        }
                    ],
                },
            },
        }
        other = json.loads(json.dumps(good))
        other["agent"] = "hermes"
        report = evaluate_agent_cases(self.settings, catalog, cases, [good, other])
        self.assertTrue(report["valid"])
        self.assertEqual(report["summary"]["deterministic_failed"], 0)
        self.assertEqual(report["submissions"][0]["metrics"]["must_find_recall"], 1.0)
        self.assertEqual(report["consistency"]["cases"][1]["adopted_jaccard"], 1.0)

        broken = json.loads(json.dumps(good))
        broken["cases"]["ambiguous-target"]["status"] = "completed"
        broken["cases"]["history-relations"]["found_paths"] = []
        broken["cases"]["history-relations"]["decisions"].append(
            {
                "path": "Concepts/Agent.md",
                "decision": "adopt",
                "reason": "Incorrectly adopted for the regression test.",
            }
        )
        failed = evaluate_agent_cases(self.settings, catalog, cases, [broken])
        self.assertFalse(failed["valid"])
        codes = {item["code"] for item in failed["submissions"][0]["failures"]}
        self.assertIn("ambiguous_target_guessed", codes)
        self.assertIn("must_find_missed", codes)
        self.assertIn("forbidden_relation_adopted", codes)

    def test_task_envelope_routes_input_and_requires_content_discovery(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        first = task_envelope(
            self.settings,
            catalog,
            path="Inbox/Books/WeRead/Chapter raw.md",
        )
        second = task_envelope(
            self.settings,
            catalog,
            path="Inbox/Books/WeRead/Chapter raw.md",
        )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["workflow"], "process-input")
        self.assertEqual(first["processor"], "book-input")
        self.assertEqual(first["ledger"]["state"], "matched")
        self.assertIn("extract_candidates", first["required_checks"])
        self.assertIn("resolve_candidates", first["required_checks"])
        self.assertIn("update_ledger", first["required_checks"])
        self.assertIn("preserve_source_coordinates", first["required_checks"])
        self.assertLessEqual(first["budget"]["used_chars"], 2000)
        self.assertNotIn("raw source payload", json.dumps(first))

    def test_task_envelope_specializes_history_and_project_workflows(self) -> None:
        project_hub = self.vault / "Projects" / "Knowledge.md"
        project_hub.write_text(_note("Knowledge", "project"), encoding="utf-8")
        project_note = self.vault / "Projects" / "Decision.md"
        project_note.write_text(_note("Decision", "project-note"), encoding="utf-8")
        catalog, _summary = sync_catalog(self.settings)

        history = task_envelope(
            self.settings,
            catalog,
            path="Sources/Books/History/Chapter.md",
        )
        self.assertEqual(history["workflow"], "update-note")
        self.assertEqual(history["processor"], "history-source")
        self.assertIn(
            "check_people_events_time_concepts", history["required_checks"]
        )

        hub = task_envelope(self.settings, catalog, path="Projects/Knowledge.md")
        self.assertEqual(hub["workflow"], "update-project")
        self.assertIn("read_project_hub", hub["required_checks"])

        note = task_envelope(self.settings, catalog, path="Projects/Decision.md")
        self.assertEqual(note["workflow"], "update-note")
        explicit = task_envelope(
            self.settings,
            catalog,
            path="Projects/Decision.md",
            workflow="update-project",
        )
        self.assertEqual(explicit["status"], "ready")

    def test_task_envelope_blocks_ambiguous_create_and_wrong_workflow(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        auto = task_envelope(
            self.settings,
            catalog,
            path="Concepts/New Concept.md",
            action="create",
        )
        self.assertEqual(auto["status"], "blocked")
        self.assertIn("workflow_required_for_create", auto["preflight"]["blocks"])

        explicit = task_envelope(
            self.settings,
            catalog,
            path="Concepts/New Concept.md",
            workflow="update-note",
            action="create",
        )
        self.assertEqual(explicit["status"], "ready")
        self.assertIn("ensure_stable_id", explicit["required_checks"])

        wrong = task_envelope(
            self.settings,
            catalog,
            path="Sources/Books/History/Chapter.md",
            workflow="process-input",
        )
        self.assertEqual(wrong["status"], "blocked")
        self.assertIn("workflow_target_mismatch", wrong["preflight"]["blocks"])

    def test_cli_task_envelope_is_compact_and_uses_exit_status(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "envelope",
                    "--path",
                    "Inbox/Books/WeRead/Chapter raw.md",
                    "--config",
                    str(self.config_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], "vault-agent/task-envelope/v1")
        self.assertNotIn("\n  ", output.getvalue())

        with redirect_stdout(io.StringIO()):
            code = main(
                [
                    "catalog",
                    "envelope",
                    "--path",
                    "Concepts/New Concept.md",
                    "--action",
                    "create",
                    "--config",
                    str(self.config_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 1)

    def test_catalog_status_includes_all_structural_watermarks(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "status",
                    "--config",
                    str(self.config_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["watermarks"]["catalog_revision"].startswith("sha256:"))
        self.assertTrue(payload["watermarks"]["content_digest"].startswith("sha256:"))
        self.assertTrue(
            payload["watermarks"]["reading_ledger_revision"].startswith("sha256:")
        )

    def test_collection_workflow_and_processor_are_validated(self) -> None:
        original = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(
            original.replace('workflow = "update-note"', 'workflow = "guess"', 1),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

        self.config_path.write_text(
            original.replace('processor = "project-note"', 'processor = "bad value"', 1),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_ledger_note_id_resolves_a_stale_output_path(self) -> None:
        note_id = "note_" + "b" * 32
        self.chapter.write_text(
            _note(
                "History Chapter",
                "source-section",
                "See [[Ada]].",
                note_id=note_id,
            ),
            encoding="utf-8",
        )
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        output = ledger["units"]["history-chapter-01"]["outputs"][0]
        output["extensions"] = {"note_id": note_id}
        moved_relative = "Sources/Books/History/Moved Chapter.md"
        ledger["sources"]["history-book"]["entry_path"] = moved_relative
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        moved = self.vault / moved_relative
        self.chapter.rename(moved)

        catalog, _summary = sync_catalog(self.settings, verify=True)
        self.assertEqual(catalog["identity_index"][note_id], moved_relative)
        result = validate_catalog(self.settings, catalog)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertTrue(result["valid"], result["issues"])
        self.assertIn("ledger_output_path_stale", codes)
        self.assertNotIn("ledger_output_missing", codes)
        context = catalog_context(self.settings, catalog, path=moved_relative)
        self.assertEqual(context["reading_units"][0]["unit_id"], "history-chapter-01")
        self.assertEqual(context["reading_units"][0]["outputs"][0]["note_id"], note_id)

        self.chapter.write_text(
            _note(
                "Replacement",
                "source-section",
                note_id="note_" + "c" * 32,
            ),
            encoding="utf-8",
        )
        catalog, _summary = sync_catalog(self.settings, verify=True)
        mismatch = validate_catalog(self.settings, catalog)
        self.assertFalse(mismatch["valid"])
        self.assertIn(
            "ledger_output_identity_mismatch",
            {issue["code"] for issue in mismatch["issues"]},
        )

    def test_raw_reading_routes_through_its_official_source_collection(self) -> None:
        technical_root = self.vault / "Sources" / "Books" / "Computer Science" / "AI"
        technical_root.mkdir(parents=True)
        technical_note = technical_root / "Technical.md"
        technical_note.write_text(
            _note("Technical Chapter", "source-section"), encoding="utf-8"
        )
        technical_raw = self.vault / "Inbox" / "Books" / "WeRead" / "Technical raw.md"
        technical_raw.write_text("technical raw payload", encoding="utf-8")
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        ledger["sources"]["technical-book"] = {
            "kind": "book",
            "title": "Technical Book",
            "canonical_key": "book:technical",
            "inbox_roots": ["Inbox/Books/WeRead"],
            "official_root": "Sources/Books/Computer Science/AI",
            "entry_path": "Sources/Books/Computer Science/AI/Technical.md",
        }
        ledger["units"]["technical-chapter-01"] = {
            "source_id": "technical-book",
            "kind": "book-section",
            "scope": {"label": "Technical Chapter"},
            "processing_status": "captured",
            "cleanup_status": "not-ready",
            "inputs": [
                {
                    "role": "raw",
                    "path": "Inbox/Books/WeRead/Technical raw.md",
                    "sha256": hashlib.sha256(technical_raw.read_bytes()).hexdigest(),
                    "presence": "present",
                }
            ],
            "outputs": [
                {
                    "role": "official",
                    "path": "Sources/Books/Computer Science/AI/Technical.md",
                }
            ],
            "provenance": {"basis": ["test-mapping"], "confidence": "exact"},
            "issues": [],
        }
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        technical_collection = CatalogCollection(
            name="technical_sources",
            paths=("Sources/Books/Computer Science",),
            role="Curated technical reading.",
            usage="Check concepts and projects.",
            related=("concepts", "projects"),
        )
        settings = replace(
            self.settings,
            catalog=replace(
                self.settings.catalog,
                collections={
                    **self.settings.catalog.collections,
                    "technical_sources": technical_collection,
                },
            ),
        )
        catalog, _summary = sync_catalog(settings)

        technical = catalog_context(
            settings, catalog, path="Inbox/Books/WeRead/Technical raw.md"
        )
        self.assertIn("technical_sources", technical["matched_collections"])
        self.assertTrue(
            {"concepts", "projects"}.issubset(technical["related_collections"])
        )
        self.assertNotIn("history_sources", technical["matched_collections"])
        self.assertTrue(
            {"events", "people", "time"}.isdisjoint(
                technical["related_collections"]
            )
        )
        technical_active = {
            item["name"] for item in technical["active_registry"]
        }
        self.assertIn("technical_sources", technical_active)
        self.assertNotIn("history_sources", technical_active)

        history = catalog_context(
            settings, catalog, path="Inbox/Books/WeRead/Chapter raw.md"
        )
        self.assertIn("history_sources", history["matched_collections"])
        self.assertTrue(
            {"events", "people", "time"}.issubset(history["related_collections"])
        )
        self.assertNotIn("technical_sources", history["matched_collections"])
        history_active = {item["name"] for item in history["active_registry"]}
        self.assertIn("history_sources", history_active)
        self.assertNotIn("technical_sources", history_active)

    def test_reading_ledger_has_an_independent_revision(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        original_read_bytes = Path.read_bytes
        ledger_reads = 0

        def tracked_read_bytes(path: Path) -> bytes:
            nonlocal ledger_reads
            if path == self.ledger_path:
                ledger_reads += 1
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", tracked_read_bytes):
            first = catalog_context(self.settings, catalog, path="Inbox/Books/WeRead")
        self.assertEqual(ledger_reads, 1)
        self.assertTrue(first["reading_ledger_valid"])
        catalog_revision = first["revision"]
        original = self.ledger_path.read_text(encoding="utf-8")
        self.ledger_path.write_text(original + "\n", encoding="utf-8")
        second = catalog_context(self.settings, catalog, path="Inbox/Books/WeRead")
        self.assertEqual(second["revision"], catalog_revision)
        self.assertNotEqual(
            second["reading_ledger_revision"], first["reading_ledger_revision"]
        )

        self.ledger_path.write_text("{invalid", encoding="utf-8")
        invalid = catalog_context(self.settings, catalog, path="Inbox/Books/WeRead")
        self.assertTrue(invalid["reading_ledger_revision"].startswith("invalid:"))
        self.ledger_path.unlink()
        missing = catalog_context(self.settings, catalog, path="Inbox/Books/WeRead")
        self.assertEqual(missing["reading_ledger_revision"], "missing")
        disabled_settings = replace(
            self.settings,
            catalog=replace(self.settings.catalog, reading_ledger=None),
        )
        disabled = catalog_context(disabled_settings, catalog, path="Inbox/Books/WeRead")
        self.assertEqual(disabled["reading_ledger_revision"], "disabled")

    def test_lexical_context_routes_from_matches(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        result = catalog_context(self.settings, catalog, query="History Chapter")
        self.assertEqual(result["status"], "matches")
        self.assertEqual(result["coverage"]["match_mode"], "lexical")
        self.assertFalse(result["coverage"]["semantic_search_performed"])
        self.assertIn("history_sources", result["matched_collections"])
        self.assertTrue(
            {"concepts", "events", "people", "time"}.issubset(
                result["related_collections"]
            )
        )

    def test_lexical_no_match_is_compact_and_global_registry_is_explicit(self) -> None:
        catalog, _summary = sync_catalog(self.settings)
        result = catalog_context(
            self.settings,
            catalog,
            query="处理一本关于智能体工程的技术书",
        )
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["coverage"]["match_mode"], "lexical")
        self.assertFalse(result["coverage"]["semantic_search_performed"])
        self.assertFalse(result["coverage"]["completeness_guaranteed"])
        self.assertEqual(result["registry"], [])
        self.assertEqual(result["active_registry"], [])
        self.assertIn(
            "use_rag_for_semantic_candidate_discovery", result["suggested_next"]
        )
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 1800)

        routed = route_catalog_context(result)
        self.assertEqual(routed["status"], "no_match")
        self.assertEqual(routed["collection_index"], [])
        self.assertIn(
            "use_rag_for_semantic_candidate_discovery", routed["suggested_next"]
        )

        global_view = catalog_context(self.settings, catalog, global_view=True)
        self.assertEqual(global_view["status"], "global")
        self.assertEqual(
            global_view["registry_count"], len(self.settings.catalog.collections)
        )
        self.assertGreater(len(global_view["registry"]), 0)

    def test_context_reports_ambiguous_and_missing_focus(self) -> None:
        for name in ("One", "Two"):
            (self.vault / "Concepts" / f"{name}.md").write_text(
                f"""---
title: "{name}"
aliases: [Shared Alias]
type: concept
---
""",
                encoding="utf-8",
            )
        catalog, _summary = sync_catalog(self.settings)
        ambiguous = catalog_context(self.settings, catalog, path="Shared Alias")
        self.assertIn(
            "focus_ambiguous", {issue["code"] for issue in ambiguous["focus_issues"]}
        )
        missing = catalog_context(self.settings, catalog, path="Does/Not/Exist")
        self.assertIn(
            "focus_not_found", {issue["code"] for issue in missing["focus_issues"]}
        )

    def test_validate_checks_ledger_hash(self) -> None:
        catalog, _summary = sync_catalog(self.settings, verify=True)
        valid = validate_catalog(self.settings, catalog)
        self.assertTrue(valid["valid"], valid["issues"])

        self.raw.write_text("raw source payload changed", encoding="utf-8")
        changed, _summary = sync_catalog(self.settings, verify=True)
        invalid = validate_catalog(self.settings, changed)
        self.assertFalse(invalid["valid"])
        self.assertIn(
            "ledger_hash_mismatch", {issue["code"] for issue in invalid["issues"]}
        )

    def test_legacy_array_ledger_remains_readable(self) -> None:
        self.ledger_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "allowed_statuses": ["organized"],
                    "allowed_cleanup_statuses": ["retained"],
                    "units": [
                        {
                            "unit_id": "legacy-unit",
                            "source_id": "legacy-source",
                            "processing_status": "organized",
                            "cleanup_status": "retained",
                            "raw_paths": ["Inbox/Books/WeRead/Chapter raw.md"],
                            "draft_paths": [],
                            "official_paths": [
                                "Sources/Books/History/Chapter.md"
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        catalog, _summary = sync_catalog(self.settings, verify=True)
        result = validate_catalog(self.settings, catalog)
        self.assertTrue(result["valid"], result["issues"])
        self.assertIn(
            "ledger_legacy_format", {issue["code"] for issue in result["issues"]}
        )

    def test_malformed_ledger_returns_structured_errors(self) -> None:
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        ledger["sources"]["history-book"]["inbox_roots"] = [123]
        unit = ledger["units"]["history-chapter-01"]
        unit["processing_status"] = []
        unit["cleanup_status"] = {}
        unit["inputs"][0]["path"] = 123
        unit["provenance"]["confidence"] = []
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        catalog, _summary = sync_catalog(self.settings, verify=True)
        result = validate_catalog(self.settings, catalog)
        self.assertFalse(result["valid"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("ledger_path_unsafe", codes)
        self.assertIn("ledger_status_unknown", codes)
        self.assertIn("ledger_provenance_confidence_invalid", codes)

    def test_cleanup_lifecycle_requires_hash_and_official_output(self) -> None:
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        unit = ledger["units"]["history-chapter-01"]
        unit["inputs"][0].pop("sha256")
        unit["outputs"] = []
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        catalog, _summary = sync_catalog(self.settings, verify=True)
        result = validate_catalog(self.settings, catalog)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("ledger_integrated_without_output", codes)
        self.assertIn("ledger_cleanup_without_raw_hash", codes)
        self.assertIn("ledger_cleanup_without_output", codes)

    def test_cleanup_lifecycle_requires_integrated_and_removed_state(self) -> None:
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        unit = ledger["units"]["history-chapter-01"]
        unit["processing_status"] = "captured"
        unit["cleanup_status"] = "raw-and-draft-cleaned"
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        catalog, _summary = sync_catalog(self.settings, verify=True)
        result = validate_catalog(self.settings, catalog)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("ledger_cleanup_before_integration", codes)
        self.assertIn("ledger_cleaned_input_still_present", codes)

        unit["inputs"][0]["presence"] = "unknown"
        unit["inputs"][0].pop("sha256")
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        catalog, _summary = sync_catalog(self.settings, verify=True)
        result = validate_catalog(self.settings, catalog)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("ledger_cleanup_without_raw_hash", codes)
        self.assertIn("ledger_cleaned_input_still_present", codes)

    def test_context_bounds_ledger_fields(self) -> None:
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        unit = ledger["units"]["history-chapter-01"]
        unit["issues"] = ["X" * 1000 for _ in range(30)]
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        catalog, _summary = sync_catalog(self.settings)
        result = catalog_context(
            self.settings,
            catalog,
            path="Inbox/Books/WeRead/Chapter raw.md",
        )
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("X" * 513, encoded)
        self.assertIn('"total_items": 30', encoded)
        self.assertEqual(result["limits"]["effective"], 12)

    def test_non_markdown_records_path_without_content(self) -> None:
        secret = "NON_MARKDOWN_BODY_MUST_NEVER_APPEAR"
        script = self.vault / "Projects" / "worker.py"
        script.write_text(secret, encoding="utf-8")
        (self.vault / "Projects" / ".gitkeep").write_text("", encoding="utf-8")
        cache = self.vault / "System" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "worker.pyc").write_bytes(b"cache")
        (self.vault / "Projects" / ".DS_Store").write_bytes(b"metadata")
        (self.vault / "Projects" / "Thumbs.db").write_bytes(b"metadata")
        catalog, _summary = sync_catalog(self.settings)
        self.assertNotIn("Projects/worker.py", catalog["files"])

        content = self.config_path.read_text(encoding="utf-8").replace(
            "include_non_markdown = false", "include_non_markdown = true"
        )
        self.config_path.write_text(content, encoding="utf-8")
        settings = load_config(self.config_path)
        catalog, _summary = sync_catalog(settings)
        record = catalog["files"]["Projects/worker.py"]
        self.assertEqual(record["kind"], "file")
        self.assertEqual(record["sha256"], "")
        self.assertNotIn("Projects/.gitkeep", catalog["files"])
        self.assertNotIn("Projects/.DS_Store", catalog["files"])
        self.assertNotIn("Projects/Thumbs.db", catalog["files"])
        self.assertNotIn("System/__pycache__/worker.pyc", catalog["files"])
        self.assertNotIn(secret, json.dumps(catalog, ensure_ascii=False))
        context = catalog_context(settings, catalog, collection="projects")
        projects = next(
            item for item in context["registry"] if item["name"] == "projects"
        )
        self.assertEqual(projects["files"], 1)
        self.assertEqual(projects["markdown"], 0)
        self.assertEqual(projects["other_files"], 1)

    def test_resolved_paths_cannot_escape_the_vault(self) -> None:
        external = self.root / "outside.md"
        external.write_text("must not be cataloged", encoding="utf-8")
        link = self.vault / "Concepts" / "Outside.md"
        link.write_text("simulated reparse-point child", encoding="utf-8")
        original_resolve = Path.resolve

        def simulated_resolve(path: Path, *args: object, **kwargs: object) -> Path:
            if path == link:
                return external
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", new=simulated_resolve):
            catalog, summary = sync_catalog(self.settings)
        self.assertNotIn("Concepts/Outside.md", catalog["files"])
        self.assertEqual(summary["errors"], 1)
        self.assertIn("path_outside_vault", {issue["code"] for issue in catalog["issues"]})

    def test_internal_path_alias_cannot_bypass_excludes(self) -> None:
        excluded = self.vault / ".git" / "secret.md"
        excluded.parent.mkdir()
        excluded.write_text("must remain excluded", encoding="utf-8")
        alias = self.vault / "Concepts" / "Alias.md"
        alias.write_text("simulated junction child", encoding="utf-8")
        original_resolve = Path.resolve

        def simulated_resolve(path: Path, *args: object, **kwargs: object) -> Path:
            if path == alias:
                return excluded
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", new=simulated_resolve):
            catalog, _summary = sync_catalog(self.settings)
        self.assertNotIn("Concepts/Alias.md", catalog["files"])
        self.assertNotIn(".git/secret.md", catalog["files"])
        self.assertIn("path_alias_skipped", {issue["code"] for issue in catalog["issues"]})

    def test_cli_json_contains_no_body_or_absolute_vault_path(self) -> None:
        secret = "MARKDOWN_BODY_MUST_NOT_BE_IN_CONTEXT"
        note = self.vault / "Concepts" / "Private.md"
        note.write_text(_note("Private title", "concept", secret), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "context",
                    "--config",
                    str(self.config_path),
                    "--path",
                    "Concepts/Private.md",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["focus"]["record"]["title"], "Private title")
        self.assertEqual(payload["profile"], "discovery")
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(str(self.vault), output.getvalue())

        compact_output = io.StringIO()
        with redirect_stdout(compact_output):
            code = main(
                [
                    "catalog",
                    "context",
                    "--config",
                    str(self.config_path),
                    "--path",
                    "Concepts/Private.md",
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        json.loads(compact_output.getvalue())
        self.assertNotIn("\n  ", compact_output.getvalue())

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(
                    [
                        "catalog",
                        "context",
                        "--task",
                        "removed interface",
                        "--config",
                        str(self.config_path),
                    ]
                )

    def test_cli_context_query_global_and_explicit_inventory(self) -> None:
        no_match_output = io.StringIO()
        with redirect_stdout(no_match_output):
            code = main(
                [
                    "catalog",
                    "context",
                    "--query",
                    "处理一本关于智能体工程的技术书",
                    "--config",
                    str(self.config_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        no_match = json.loads(no_match_output.getvalue())
        self.assertEqual(no_match["status"], "no_match")
        self.assertNotIn("sync", no_match)
        self.assertNotIn("registry_count", no_match)

        global_output = io.StringIO()
        with redirect_stdout(global_output):
            code = main(
                [
                    "catalog",
                    "context",
                    "--global",
                    "--config",
                    str(self.config_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(global_output.getvalue())["status"], "global")

        inventory_output = io.StringIO()
        with redirect_stdout(inventory_output):
            code = main(
                [
                    "catalog",
                    "list",
                    "--all",
                    "--config",
                    str(self.config_path),
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        inventory = json.loads(inventory_output.getvalue())
        self.assertEqual(inventory["status"], "complete")
        self.assertEqual(inventory["total"], len(inventory["paths"]))
        self.assertIn("Concepts/Agent.md", inventory["paths"])

    def test_find_has_hard_output_bounds(self) -> None:
        long_alias = "A" * 1000
        for number in range(30):
            (self.vault / "Concepts" / f"Needle {number}.md").write_text(
                f"""---
title: Needle {number}
aliases: [{long_alias}]
type: concept
---
""",
                encoding="utf-8",
            )
        catalog, _summary = sync_catalog(self.settings)
        page = find_catalog_page(catalog, "Needle", limit=999)
        self.assertEqual(len(page["results"]), 24)
        self.assertEqual(page["total"], 30)
        self.assertTrue(page["truncated"])
        self.assertNotIn("A" * 513, json.dumps(page))

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "catalog",
                    "find",
                    "Needle",
                    "--config",
                    str(self.config_path),
                    "--limit",
                    "999",
                    "--compact",
                ]
            )
        self.assertEqual(code, 0)
        cli_page = json.loads(output.getvalue())
        self.assertEqual(cli_page["total"], 30)
        self.assertTrue(cli_page["truncated"])

        for number in range(24, 30):
            (self.vault / "Concepts" / f"Needle {number}.md").unlink()
        catalog, _summary = sync_catalog(self.settings)
        exact_page = find_catalog_page(catalog, "Needle", limit=999)
        self.assertEqual(exact_page["total"], 24)
        self.assertEqual(len(exact_page["results"]), 24)
        self.assertFalse(exact_page["truncated"])
        context = catalog_context(self.settings, catalog, query="Needle", limit=999)
        self.assertEqual(context["matches_count"], 24)
        self.assertFalse(context["matches_truncated"])

    def test_single_writer_lock_releases_cleanly(self) -> None:
        lock_path = self.root / "lock"
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source_root)
        child = (
            "import sys\n"
            "from pathlib import Path\n"
            "from langhuan.catalog import _CatalogLock\n"
            "with _CatalogLock(Path(sys.argv[1]), timeout=0.1):\n"
            "    print('locked')\n"
        )
        with _CatalogLock(lock_path):
            with self.assertRaises(RuntimeError):
                with _CatalogLock(lock_path, timeout=0.05):
                    pass
            blocked = subprocess.run(
                [sys.executable, "-c", child, str(lock_path)],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
        with _CatalogLock(lock_path, timeout=0.05):
            pass
        released = subprocess.run(
            [sys.executable, "-c", child, str(lock_path)],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_atomic_json_survives_a_concurrent_process_reader(self) -> None:
        target = self.root / "atomic.json"
        stop = self.root / "stop"
        marker = "<initial>"
        _atomic_json(
            target,
            {"sequence": -1, "marker": marker, "payload": marker * 10_000},
        )
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source_root)
        writer = (
            "import sys\n"
            "from pathlib import Path\n"
            "from langhuan.catalog import _atomic_json\n"
            "target, stop = Path(sys.argv[1]), Path(sys.argv[2])\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "sequence = 0\n"
            "while not stop.exists():\n"
            "    marker = f'<{sequence}>'\n"
            "    _atomic_json(target, {'sequence': sequence, 'marker': marker, "
            "'payload': marker * 10000})\n"
            "    if sequence == 0:\n"
            "        print('started', flush=True)\n"
            "    sequence += 1\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", writer, str(target), str(stop)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            assert process.stdout is not None
            assert process.stdin is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            process.stdin.write("\n")
            process.stdin.flush()
            self.assertEqual(process.stdout.readline().strip(), "started")
            sequences: set[int] = set()
            reads = 0
            while len(sequences) < 3 and reads < 5_000:
                value = _read_json(target)
                self.assertEqual(value["payload"], value["marker"] * 10_000)
                sequences.add(int(value["sequence"]))
                reads += 1
            stop.write_text("stop", encoding="utf-8")
            _stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertGreaterEqual(len(sequences), 2)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_parent_directory_config_discovery_and_old_config(self) -> None:
        old = self.root / "old"
        nested = old / "one" / "two"
        nested.mkdir(parents=True)
        old_config = render_config(self.vault)
        start = old_config.index("[catalog]")
        end = old_config.index("[retrieval]")
        old_config = old_config[:start] + old_config[end:]
        (old / "langhuan.toml").write_text(old_config, encoding="utf-8")
        previous = Path.cwd()
        try:
            os.chdir(nested)
            discovered = find_config()
            settings = load_config()
        finally:
            os.chdir(previous)
        self.assertEqual(discovered, (old / "langhuan.toml").resolve())
        self.assertEqual(settings.catalog.include, (".",))
        self.assertEqual(settings.catalog.collections, {})


if __name__ == "__main__":
    unittest.main()
