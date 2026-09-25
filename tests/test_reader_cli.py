from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langhuan.cli import build_parser, main
from langhuan.config import render_config
from langhuan.reader import read_sections


class ReadSectionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "Concepts").mkdir()
        (self.root / "Concepts" / "Example.md").write_text(
            """---
title: Example
---
Intro text.

## Parent
Parent body.
### Child
Child body.
## Sibling
Sibling body.
<!--
## Hidden Comment Heading
Not part of the document outline.
-->
```markdown
# Not a heading
```
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reads_current_source_section_without_frontmatter_or_siblings(self) -> None:
        result = read_sections(self.root, "Concepts/Example.md", ["Parent > Child"])
        self.assertEqual(result["status"], "ok")
        body = result["sections"][0]["text"]
        self.assertIn("Child body", body)
        self.assertNotIn("Sibling body", body)
        self.assertNotIn("title: Example", body)
        self.assertNotIn("# Not a heading", body)
        hidden = read_sections(self.root, "Concepts/Example.md", ["Hidden Comment Heading"])
        self.assertEqual(hidden["status"], "not_found")

    def test_parent_and_child_selection_does_not_duplicate_child(self) -> None:
        result = read_sections(
            self.root, "Concepts/Example.md", ["Parent", "Parent > Child"]
        )
        self.assertEqual(len(result["sections"]), 1)
        self.assertEqual(result["sections"][0]["heading"], "Parent")

    def test_duplicate_heading_requires_full_path_and_missing_is_explicit(self) -> None:
        path = self.root / "Concepts" / "Example.md"
        path.write_text("# First\n## Shared\n# Second\n## Shared\n", encoding="utf-8")
        ambiguous = read_sections(self.root, "Concepts/Example.md", ["Shared"])
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(ambiguous["ambiguous_headings"]["Shared"], ["First > Shared", "Second > Shared"])
        exact = read_sections(self.root, "Concepts/Example.md", ["Second > Shared"])
        self.assertEqual(exact["status"], "ok")
        missing = read_sections(self.root, "Concepts/Example.md", ["Absent"])
        self.assertEqual(missing["status"], "not_found")

    def test_reading_is_bounded_and_paths_cannot_escape_vault(self) -> None:
        result = read_sections(self.root, "Concepts/Example.md", [""], max_chars=20)
        self.assertEqual(result["returned_chars"], 20)
        self.assertTrue(result["truncated"])
        opening = read_sections(self.root, "Concepts/Example.md", max_chars=20)
        self.assertEqual(opening["returned_chars"], 20)
        for unsafe in ("../outside.md", str(self.root / "Concepts" / "Example.md"), "C:\\outside.md"):
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                read_sections(self.root, unsafe)


class CatalogReadCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        (self.vault / "Concepts").mkdir(parents=True)
        (self.vault / "Concepts" / "Example.md").write_text(
            "# Example\n## Source\nCurrent source text about events and query content; also CFO synchronization and cyclic prefix.\n",
            encoding="utf-8",
        )
        self.config = self.root / "langhuan.toml"
        self.config.write_text(render_config(self.vault), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ask_candidates_can_be_selected_and_read_from_current_markdown(self) -> None:
        results = [{
            "metadata": {
                "relative_path": "Concepts/Example.md",
                "title": "Example",
                "heading_path": "Example > Source",
            },
            "score": 0.91,
            "text": "indexed excerpt",
        }]
        stdout = io.StringIO()
        with patch("langhuan.cli.search", return_value=results), contextlib.redirect_stdout(stdout):
            code = main(["ask", "current source", "--config", str(self.config), "--output", "candidates.json"])
        self.assertEqual(code, 0)
        save_result = json.loads(stdout.getvalue())
        candidate_path = self.root / ".langhuan" / "candidates.json"
        self.assertEqual(save_result["saved"], ".langhuan/candidates.json")
        self.assertEqual(save_result["sources"][0]["id"], 1)
        self.assertEqual(save_result["sources"][0]["path"], "Concepts/Example.md")
        self.assertIn("indexed excerpt", save_result["sources"][0]["preview"])
        candidate_data = json.loads(candidate_path.read_text(encoding="utf-8"))
        self.assertNotIn("query", candidate_data)
        self.assertEqual(candidate_data["sources"][0]["path"], "Concepts/Example.md")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([
                "catalog", "read", "--sources", "candidates.json", "--select", "1",
                "--config", str(self.config), "--compact",
            ])
        reading = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(reading["sections"][0]["path"], "Concepts/Example.md")
        self.assertIn("Current source text", reading["sections"][0]["text"])

    def test_real_index_ask_and_read_commands_work_together(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            indexed = main(["index", "--config", str(self.config)])
            searched = main([
                "ask", "events include query content", "--top-k", "1",
                "--output", "actual-candidates.json", "--config", str(self.config),
            ])
        self.assertEqual(indexed, 0)
        self.assertEqual(searched, 0)
        candidate = self.root / ".langhuan" / "actual-candidates.json"
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        self.assertTrue(payload["sources"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            read = main([
                "catalog", "read", "--sources", "actual-candidates.json", "--select", "1",
                "--config", str(self.config),
            ])
        result = json.loads(stdout.getvalue())
        self.assertEqual(read, 0)
        self.assertEqual(result["sections"][0]["path"], "Concepts/Example.md")
        self.assertIn("Current source text", result["sections"][0]["text"])

    def test_catalog_find_accepts_positional_and_flag_forms_with_json_alias(self) -> None:
        args = build_parser().parse_args(["catalog", "find", "--query", "Example", "--json"])
        self.assertEqual(args.query_option, "Example")
        self.assertTrue(args.json)
        stdout = io.StringIO()
        with (
            patch("langhuan.cli.sync_catalog", return_value=({"revision": "r", "content_digest": "d"}, {})),
            patch("langhuan.cli.find_catalog_page", return_value={"results": []}) as find_page,
            contextlib.redirect_stdout(stdout),
        ):
            code = main(["catalog", "find", "--query", "Example", "--config", str(self.config), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(find_page.call_args.args[1], "Example")
        self.assertEqual(json.loads(stdout.getvalue())["results"], [])

    def test_candidate_traversal_and_existing_output_are_rejected(self) -> None:
        data_dir = self.root / ".langhuan"
        data_dir.mkdir()
        (data_dir / "unsafe.json").write_text(json.dumps({
            "schema": "langhuan/source-candidates/v1", "status": "ok",
            "sources": [{"id": 1, "path": "../outside.md", "headings": [""]}],
        }), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["catalog", "read", "--sources", "unsafe.json", "--select", "1", "--config", str(self.config)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "partial")

        stdout = io.StringIO()
        with patch("langhuan.cli.search") as search_mock, contextlib.redirect_stderr(stdout):
            code = main(["ask", "query", "--config", str(self.config), "--output", "unsafe.json"])
        self.assertEqual(code, 2)
        search_mock.assert_not_called()

    def test_direct_and_candidate_reads_respect_configured_excludes(self) -> None:
        excluded = self.vault / "Inbox" / "Processing" / "Secret.md"
        excluded.parent.mkdir(parents=True)
        excluded.write_text("# Secret\nExcluded content.\n", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([
                "catalog", "read", "--path", "Inbox/Processing/Secret.md",
                "--config", str(self.config),
            ])
        self.assertEqual(code, 2)
        self.assertNotIn("Excluded content", stdout.getvalue())

        data_dir = self.root / ".langhuan"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "excluded.json").write_text(json.dumps({
            "schema": "langhuan/source-candidates/v1", "status": "ok",
            "sources": [{"id": 1, "path": "Inbox/Processing/Secret.md", "headings": [""]}],
        }), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([
                "catalog", "read", "--sources", "excluded.json", "--select", "1",
                "--config", str(self.config),
            ])
        self.assertEqual(code, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("Excluded content", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
