from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHOWCASE = ROOT / "showcase"


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag in {"a", "link", "script", "img"}:
            target = values.get("href") or values.get("src")
            if target:
                self.links.append(target)


class ShowcaseTests(unittest.TestCase):
    def test_static_showcase_has_required_content_and_assets(self) -> None:
        html = (SHOWCASE / "index.html").read_text(encoding="utf-8")
        self.assertIn("让 Agent 使用你的知识库", html)
        self.assertIn("langhuan demo", html)
        self.assertIn("开源 v0.1", html)
        self.assertNotIn("single-writer", html)
        self.assertNotIn("react-loading-skeleton", html)

        parser = _DocumentParser()
        parser.feed(html)
        self.assertIn("main-content", parser.ids)
        for relative in ("styles.css", "app.js", "public/favicon.svg", "public/og.png"):
            self.assertTrue((SHOWCASE / relative).is_file(), relative)

    def test_showcase_has_no_package_manager_surface(self) -> None:
        self.assertFalse((SHOWCASE / "package.json").exists())
        self.assertFalse((SHOWCASE / "node_modules").exists())


if __name__ == "__main__":
    unittest.main()
