"""Tenant knowledge-base retrieval.

Deliberately simple, deterministic keyword scoring (title matches weigh most,
then tags, then body). The AI layer's confidence is derived from the best
match, so weak retrieval naturally routes to a human via the escalation
engine's low-confidence rule — no fake certainty.
"""
import re

_STOPWORDS = {
    "a", "an", "the", "is", "are", "do", "does", "i", "my", "me", "you",
    "your", "how", "what", "when", "where", "can", "to", "of", "for", "in",
    "on", "and", "or", "it", "this", "that", "with", "about", "please",
}


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 1]


def score_entry(query_tokens: set[str], entry: dict) -> int:
    title_tokens = set(tokenize(entry.get("title", "")))
    tag_tokens = set()
    for tag in entry.get("tags") or []:
        tag_tokens.update(tokenize(tag))
    body_tokens = set(tokenize(entry.get("content", "")))
    return (
        3 * len(query_tokens & title_tokens)
        + 2 * len(query_tokens & tag_tokens)
        + 1 * len(query_tokens & body_tokens)
    )


def retrieve(entries: list[dict], query: str, limit: int = 3) -> tuple[list[dict], float]:
    """Return (ranked matches, confidence 0..1) for a customer question."""
    query_tokens = set(tokenize(query))
    if not query_tokens or not entries:
        return [], 0.0
    scored = [(score_entry(query_tokens, e), e) for e in entries]
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return [], 0.0
    best = scored[0][0]
    # Confidence saturates: a strong multi-token title match approaches 1.
    confidence = min(1.0, best / 8.0)
    return [e for _, e in scored[:limit]], round(confidence, 3)
