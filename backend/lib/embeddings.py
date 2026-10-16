"""Provider-abstracted text embeddings (key-gated, like the AI front line).

OpenAI when ``OPENAI_API_KEY`` is set, else Gemini when ``GEMINI_API_KEY``
is set, else **none** — callers must treat a ``None`` return as "semantic
retrieval unavailable" and fall back to keyword retrieval, exactly as the
rest of the hub degrades to demo mode without keys. Embeddings power the
*hybrid* half of knowledge search and similar-case retrieval; nothing in
the hub hard-depends on them.
"""
import math
import os

import httpx

from lib import config


def provider_name() -> str:
    if config.openai_api_key():
        return "openai"
    if config.gemini_api_key():
        return "gemini"
    return "none"


def available() -> bool:
    return provider_name() != "none"


async def _openai_embed(texts: list[str]) -> list[list[float]]:
    model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {config.openai_api_key()}"},
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [row["embedding"] for row in sorted(data, key=lambda r: r["index"])]


async def _gemini_embed(texts: list[str]) -> list[list[float]]:
    model = os.environ.get("GEMINI_EMBED_MODEL", "text-embedding-004")
    out = []
    async with httpx.AsyncClient(timeout=30) as client:
        for text in texts:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1/models/{model}:embedContent",
                params={"key": config.gemini_api_key()},
                json={
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text}]},
                },
            )
            resp.raise_for_status()
            out.append(resp.json()["embedding"]["values"])
    return out


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts, or None when no provider is configured.

    Provider errors also return None — semantic retrieval is an
    enhancement, never a hard dependency.
    """
    if not texts:
        return []
    provider = provider_name()
    if provider == "none":
        return None
    try:
        if provider == "openai":
            return await _openai_embed(texts)
        return await _gemini_embed(texts)
    except Exception:
        return None


async def embed_one(text: str) -> list[float] | None:
    vectors = await embed_texts([text])
    if not vectors:
        return None
    return vectors[0]


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two vectors (0.0 when either is unusable)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)
