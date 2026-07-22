from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Protocol


TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    raw = TOKEN_RE.findall(text.lower())
    cjk = [token for token in raw if len(token) == 1 and "\u4e00" <= token <= "\u9fff"]
    return raw + ["".join(cjk[index : index + 2]) for index in range(len(cjk) - 1)]


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic offline embedding for demos, tests and small vaults."""

    name = "hash"

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text) or [text.lower()]:
            digest = hashlib.sha256(token.encode("utf-8", errors="ignore")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            vector[index] += -1.0 if digest[4] & 1 else 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [round(value / norm, 7) for value in vector]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]


class SentenceTransformerEmbedder:
    def __init__(self, model: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("Install model support with `pip install 'langhuan[models]'`.") from exc
        model_path = Path(model).expanduser()
        if not model_path.exists():
            raise RuntimeError(
                "Non-hash models must be local paths. Download the model explicitly, then set "
                "retrieval.embedding_model to that directory."
            )
        self.name = str(model_path.resolve())
        self._model = SentenceTransformer(self.name, local_files_only=True)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [[round(float(value), 7) for value in vector] for vector in vectors]


def make_embedder(model: str) -> Embedder:
    return HashEmbedder() if model.lower() == "hash" else SentenceTransformerEmbedder(model)


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))
