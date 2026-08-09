from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

import httpx

from app.config import settings


class HashEmbeddingProvider:
    """Deterministic, offline bag-of-hashes embedding (dev/test only)."""

    name = "hash"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class HTTPEmbeddingProvider:
    """OpenAI-compatible embeddings endpoint (dedicated embedding model)."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        dim: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": self.model, "input": list(texts)},
            )
            resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda item: item["index"])
        return [item["embedding"] for item in data]


def get_embedder():
    if settings.embedding_provider == "http":
        if not settings.embedding_base_url:
            raise RuntimeError("EV_EMBEDDING_BASE_URL is required for http embedding provider")
        return HTTPEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )
    return HashEmbeddingProvider(dim=settings.embedding_dim)

