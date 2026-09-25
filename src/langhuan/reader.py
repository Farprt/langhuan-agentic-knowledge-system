from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
OBSIDIAN_LINK_RE = re.compile(r"(!)?\[\[([^\]]+)\]\]")
WIKILINK_RE = re.compile(r"^\[\[(.+?)\]\]$")
PARSER_VERSION = 2


@dataclass(frozen=True)
class ObsidianDocument:
    source_path: Path
    relative_path: str
    body: str
    vector_text: str
    frontmatter: dict[str, Any]
    metadata: dict[str, Any]


def normalize_path(path: Path | str) -> str:
    return Path(path).as_posix().removeprefix("./")


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
    in_comment = False
    fence: tuple[str, int] | None = None

    def replace(match: re.Match[str]) -> str:
        link = _split_link(match.group(2))
        (embeds if match.group(1) else outlinks).append(link)
        return link["display"]

    def replace_visible(line: str) -> str:
        nonlocal in_comment
        pieces: list[str] = []
        cursor = 0
        while cursor < len(line):
            if in_comment:
                end = line.find("-->", cursor)
                if end < 0:
                    pieces.append(line[cursor:])
                    break
                pieces.append(line[cursor : end + 3])
                cursor = end + 3
                in_comment = False
                continue
            start = line.find("<!--", cursor)
            if start < 0:
                pieces.append(OBSIDIAN_LINK_RE.sub(replace, line[cursor:]))
                break
            pieces.append(OBSIDIAN_LINK_RE.sub(replace, line[cursor:start]))
            pieces.append("<!--")
            cursor = start + 4
            in_comment = True
        return "".join(pieces)

    rendered: list[str] = []
    for line in body.splitlines(keepends=True):
        candidate: tuple[str, int, str] | None = None
        if not in_comment:
            match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
            if match:
                run, tail = match.groups()
                candidate = (run[0], len(run), tail)
        if fence:
            rendered.append(line)
            if (
                candidate
                and candidate[0] == fence[0]
                and candidate[1] >= fence[1]
                and not candidate[2].strip()
            ):
                fence = None
            continue
        if candidate and not (candidate[0] == "`" and "`" in candidate[2]):
            fence = (candidate[0], candidate[1])
            rendered.append(line)
            continue
        rendered.append(replace_visible(line))
    return "".join(rendered), outlinks, embeds


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


def read_sections(
    vault_root: Path,
    relative_path: str,
    headings: list[str] | tuple[str, ...] = (),
    *,
    max_chars: int = 12_000,
    include: tuple[str, ...] = (".",),
    exclude: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Read selected Markdown sections from the current vault, never from an index."""
    raw_path = relative_path.replace("\\", "/")
    candidate = Path(raw_path)
    if (
        not raw_path.strip()
        or candidate.is_absolute()
        or raw_path.startswith("/")
        or re.match(r"^[A-Za-z]:", raw_path)
        or ".." in candidate.parts
    ):
        raise ValueError("path must be a relative Markdown path inside the vault")
    if max_chars < 1 or max_chars > 100_000:
        raise ValueError("max_chars must be between 1 and 100000")

    root = vault_root.resolve()
    path = (root / candidate).resolve()
    if not path.is_relative_to(root) or path.suffix.casefold() != ".md" or not path.is_file():
        raise ValueError("path must resolve to a Markdown file inside the vault")
    resolved_relative = normalize_path(path.relative_to(root))
    if (include and not any(_under_prefix(resolved_relative, prefix) for prefix in include)) or any(
        _under_prefix(resolved_relative, prefix) for prefix in exclude
    ):
        raise ValueError("path is outside the configured retrieval scope")
    document = read_markdown(path, root)
    lines = document.body.splitlines()

    records: list[dict[str, Any]] = []
    parents: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    heading_re = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
    for index, line in enumerate(lines):
        if fence:
            fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if (
                fence_match
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= fence[1]
                and not fence_match.group(2).strip()
            ):
                fence = None
            continue
        visible: list[str] = []
        cursor = 0
        while cursor < len(line):
            if in_comment:
                end = line.find("-->", cursor)
                if end < 0:
                    cursor = len(line)
                    continue
                cursor = end + 3
                in_comment = False
                continue
            start = line.find("<!--", cursor)
            if start < 0:
                visible.append(line[cursor:])
                break
            visible.append(line[cursor:start])
            cursor = start + 4
            in_comment = True
        visible_line = "".join(visible)
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", visible_line)
        if fence_match and not (fence_match.group(1)[0] == "`" and "`" in fence_match.group(2)):
            fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            continue
        match = heading_re.match(visible_line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        while parents and parents[-1][0] >= level:
            parents.pop()
        parents.append((level, title))
        records.append(
            {
                "start": index,
                "level": level,
                "title": title,
                "path": " > ".join(parent[1] for parent in parents),
            }
        )

    requested = list(headings)
    if not requested or any(not str(query).strip() for query in requested):
        text = "\n".join(lines).strip()
        passage = text[:max_chars]
        truncated = len(passage) < len(text)
        return {
            "schema": "langhuan/source-reading/v1",
            "status": "ok",
            "path": document.relative_path,
            "sections": [{"heading": "", "text": passage, "truncated": truncated}],
            "max_chars": max_chars,
            "returned_chars": len(passage),
            "truncated": truncated,
        }
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for query in requested:
        key = query.strip().casefold()
        exact_path = [item for item in records if item["path"].casefold() == key]
        matches = exact_path or [item for item in records if item["title"].casefold() == key]
        if not matches:
            missing.append(query)
        elif len(matches) > 1:
            ambiguous[query] = [item["path"] for item in matches]
        else:
            selected.append(matches[0])
    if ambiguous:
        return {
            "schema": "langhuan/source-reading/v1",
            "status": "ambiguous",
            "path": document.relative_path,
            "ambiguous_headings": ambiguous,
            "missing_headings": missing,
            "sections": [],
            "truncated": False,
        }
    if missing:
        return {
            "schema": "langhuan/source-reading/v1",
            "status": "not_found",
            "path": document.relative_path,
            "missing_headings": missing,
            "sections": [],
            "truncated": False,
        }
    selected = sorted(
        {item["start"]: item for item in selected}.values(),
        key=lambda item: item["start"],
    )
    # A selected parent section already contains all of its child headings.
    # Avoid returning the same text twice when both are requested.
    selected = [
        item
        for item in selected
        if not any(
            parent["start"] < item["start"]
            and item["path"].startswith(parent["path"] + " > ")
            for parent in selected
        )
    ]
    passages: list[dict[str, Any]] = []
    remaining = max_chars
    for item in selected:
        end = len(lines)
        for candidate_heading in records:
            if candidate_heading["start"] > item["start"] and candidate_heading["level"] <= item["level"]:
                end = candidate_heading["start"]
                break
        text = "\n".join(lines[item["start"] : end]).strip()
        piece = text[:remaining]
        passages.append(
            {"heading": item["path"], "text": piece, "truncated": len(piece) < len(text)}
        )
        remaining -= len(piece)
        if remaining <= 0:
            break
    return {
        "schema": "langhuan/source-reading/v1", "status": "ok", "path": document.relative_path,
        "sections": passages, "max_chars": max_chars,
        "returned_chars": sum(len(item["text"]) for item in passages),
        "truncated": any(item["truncated"] for item in passages) or len(passages) < len(selected),
    }


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
