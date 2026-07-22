from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from langhuan.config import ConfigError, load_config, render_config
from langhuan.events import log_event
from langhuan.index import sync_index
from langhuan.reader import extract_links, parse_frontmatter
from langhuan.search import search


class LanghuanCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        (self.vault / "Projects").mkdir(parents=True)
        (self.vault / "Projects" / "RAG.md").write_text(
            """---
title: Local RAG
type: project
---
# Local RAG
## Privacy
Local JSONL events do not include query content by default.
""",
            encoding="utf-8",
        )
        self.config_path = self.root / "langhuan.toml"
        self.config_path.write_text(render_config(self.vault), encoding="utf-8")
        self.settings = load_config(self.config_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_frontmatter_and_wikilinks(self) -> None:
        frontmatter, body = parse_frontmatter("---\ntags: [rag, agent]\n---\nSee [[RAG#Index|the index]].")
        vector_text, links, embeds = extract_links(body)
        self.assertEqual(frontmatter["tags"], ["rag", "agent"])
        self.assertEqual(links[0]["target"], "RAG")
        self.assertIn("the index", vector_text)
        self.assertEqual(embeds, [])

    def test_incremental_hybrid_search(self) -> None:
        first = sync_index(self.settings)
        second = sync_index(self.settings)
        results = search(self.settings, "query content privacy", top_k=2)
        self.assertEqual(first["changed"], 1)
        self.assertEqual(second["changed"], 0)
        self.assertTrue(first["audit"]["consistent"])
        self.assertEqual(results[0]["metadata"]["relative_path"], "Projects/RAG.md")

    def test_deleted_file_removes_its_chunks(self) -> None:
        first = sync_index(self.settings)
        self.assertEqual(first["audit"]["files"], 1)
        (self.vault / "Projects" / "RAG.md").unlink()
        second = sync_index(self.settings)
        self.assertEqual(second["deleted"], 1)
        self.assertEqual(second["audit"]["files"], 0)
        self.assertEqual(second["audit"]["chunks"], 0)
        self.assertTrue(second["audit"]["consistent"])

    def test_scope_limits_results_by_path(self) -> None:
        sync_index(self.settings)
        self.assertEqual(search(self.settings, "privacy", scope="reading"), [])
        results = search(self.settings, "privacy", scope="projects")
        self.assertEqual(results[0]["metadata"]["relative_path"], "Projects/RAG.md")

    def test_event_body_is_off_by_default(self) -> None:
        log_event(self.settings, "ask", {"results": 1}, query="private question")
        event = json.loads(self.settings.event_log_path.read_text(encoding="utf-8"))
        self.assertNotIn("query", event)
        self.assertEqual(event["query_length"], len("private question"))
        self.assertNotIn("query_sha256", event)

    def test_empty_include_is_rejected(self) -> None:
        content = self.config_path.read_text(encoding="utf-8").replace(
            'include = ["."]', "include = []"
        )
        self.config_path.write_text(content, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.config_path)


if __name__ == "__main__":
    unittest.main()
