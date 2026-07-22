from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
OBSIDIAN_LINK_RE = re.compile(r"(!)?\[\[([^\]]+)\]\]")
WIKILINK_RE = re.compile(r"^\[\[(.+?)\]\]$")


@dataclass(frozen=True)
class ObsidianDocument:
    source_path: Path
    relative_path: str
    body: str
    vector_text: str
    frontmatter: dict[str, Any]
    metadata: dict[str, Any]


def normalize_path(path: Path | str) -> str:
    return Path(path).as_posix().lstrip("./")


def _clean_wikilink(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    match = WIKILINK_RE.match(value)
    if match:
        return match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
    return value


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if WIKILINK_RE.match(value):
        return _clean_wikilink(value)
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_clean_wikilink(item) for item in inner.split(",")]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in match.group(1).splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_key:
            current = frontmatter.setdefault(current_key, [])
            if not isinstance(current, list):
                current = [current]
                frontmatter[current_key] = current
            current.append(_parse_scalar(stripped[2:]))
        elif ":" in raw_line:
            key, value = raw_line.split(":", 1)
            current_key = key.strip()
            parsed = _parse_scalar(value)
            frontmatter[current_key] = [] if parsed == "" else parsed
    return frontmatter, text[match.end() :]


def _split_link(inner: str) -> dict[str, str]:
    target_and_heading, _, alias = inner.partition("|")
    target, _, heading = target_and_heading.partition("#")
    target, heading, alias = target.strip(), heading.strip(), alias.strip()
    return {
        "target": target,
        "heading": heading,
        "alias": alias,
        "display": alias or heading or Path(target).stem or target,
    }


def extract_links(body: str) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    outlinks: list[dict[str, str]] = []
    embeds: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        link = _split_link(match.group(2))
        (embeds if match.group(1) else outlinks).append(link)
        return link["display"]

    return OBSIDIAN_LINK_RE.sub(replace, body), outlinks, embeds


def _frontmatter_text(frontmatter: dict[str, Any], key: str) -> str:
    value = frontmatter.get(key, "")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value) if value is not None else ""


def read_markdown(path: Path, vault_root: Path) -> ObsidianDocument:
    raw = path.read_text(encoding="utf-8-sig")
    frontmatter, body = parse_frontmatter(raw)
    vector_text, outlinks, embeds = extract_links(body)
    relative_path = normalize_path(path.relative_to(vault_root))
    aliases = frontmatter.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    metadata = {
        "relative_path": relative_path,
        "title": _frontmatter_text(frontmatter, "title") or path.stem,
        "type": _frontmatter_text(frontmatter, "type"),
        "area": _frontmatter_text(frontmatter, "area"),
        "subarea": _frontmatter_text(frontmatter, "subarea"),
        "source_type": _frontmatter_text(frontmatter, "source_type"),
        "visibility": _frontmatter_text(frontmatter, "visibility"),
        "status": _frontmatter_text(frontmatter, "status"),
        "book": _clean_wikilink(_frontmatter_text(frontmatter, "book")),
        "project": _clean_wikilink(_frontmatter_text(frontmatter, "project")),
        "outlinks_json": json.dumps(outlinks, ensure_ascii=False),
        "embeds_json": json.dumps(embeds, ensure_ascii=False),
        "aliases_json": json.dumps(aliases if isinstance(aliases, list) else [], ensure_ascii=False),
    }
    return ObsidianDocument(path, relative_path, body, vector_text, frontmatter, metadata)


def _under_prefix(path: str, prefix: str) -> bool:
    prefix = prefix.strip("/")
    return prefix in {"", "."} or path == prefix or path.startswith(prefix + "/")


def iter_markdown_files(
    vault_root: Path, include: tuple[str, ...], exclude: tuple[str, ...]
) -> list[Path]:
    files: list[Path] = []
    for path in vault_root.rglob("*.md"):
        relative = normalize_path(path.relative_to(vault_root))
        if any(_under_prefix(relative, prefix) for prefix in exclude):
            continue
        if include and not any(_under_prefix(relative, prefix) for prefix in include):
            continue
        files.append(path)
    return sorted(files, key=lambda item: normalize_path(item.relative_to(vault_root)).lower())
