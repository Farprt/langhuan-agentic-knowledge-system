from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .reader import ObsidianDocument


@dataclass(frozen=True)
class Chunk:
    text: str
    metadata: dict[str, Any]


def _header_sections(text: str) -> list[tuple[str, dict[str, str]]]:
    sections: list[tuple[str, dict[str, str]]] = []
    headings = {"h1": "", "h2": "", "h3": "", "h4": ""}
    buffer: list[str] = []
    in_code = False

    def flush() -> None:
        content = "\n".join(buffer).strip()
        if content:
            sections.append((content, dict(headings)))
        buffer.clear()

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            buffer.append(line)
            continue
        matched = False
        if not in_code:
            for level in range(4, 0, -1):
                prefix = "#" * level + " "
                if line.startswith(prefix):
                    flush()
                    key = f"h{level}"
                    headings[key] = line[len(prefix) :].strip()
                    for deeper in range(level + 1, 5):
                        headings[f"h{deeper}"] = ""
                    buffer.append(line)
                    matched = True
                    break
        if not matched:
            buffer.append(line)
    flush()
    return sections


def _split(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        pieces.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return [piece for piece in pieces if piece]


def chunk_document(doc: ObsidianDocument, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section, headings in _header_sections(doc.vector_text):
        metadata = dict(doc.metadata)
        metadata.update(headings)
        metadata["heading_path"] = " > ".join(headings[key] for key in headings if headings[key])
        context = metadata["heading_path"] or metadata["title"]
        tags = ", ".join(item for item in (metadata["area"], metadata["type"]) if item)
        prefix = f"[Source: {context}]" + (f" ({tags})" if tags else "")
        for sub_index, text in enumerate(_split(section, chunk_size, chunk_overlap)):
            chunk_metadata = dict(metadata)
            chunk_metadata["subchunk_index"] = sub_index
            chunks.append(Chunk(f"{prefix}\n{text}".strip(), chunk_metadata))
    return chunks
