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


async def search_hybrid(database, tenant_id: str, query: str,
                        limit: int = 3) -> tuple[list[dict], float]:
    """Hybrid retrieval over KB articles *and* ingested document chunks.

    Keyword scoring always runs (v1 behavior, fully deterministic). When an
    embeddings provider is configured, chunks/articles carrying vectors get
    a semantic boost blended into their keyword score, so paraphrases match
    even with zero shared keywords. With no provider configured this is
    exactly the v1 keyword path — same matches, same confidence.
    """
    from lib import embeddings  # local import: keeps module import graph flat

    import db as db_module

    entries = await database[db_module.KNOWLEDGE].find(
        {"tenant_id": tenant_id}
    ).to_list(length=1000)
    chunks = await database[db_module.DOC_CHUNKS].find(
        {"tenant_id": tenant_id}
    ).to_list(length=5000)
    pool = list(entries) + [
        {"title": c.get("title", ""), "content": c.get("content", ""),
         "tags": [], "embedding": c.get("embedding"),
         "source": "document", "document_id": c.get("document_id")}
        for c in chunks
    ]
    if not pool:
        return [], 0.0

    query_tokens = set(tokenize(query))
    scored: list[tuple[float, dict]] = []
    for entry in pool:
        scored.append((float(score_entry(query_tokens, entry)), entry))

    query_vector = await embeddings.embed_one(query) if embeddings.available() else None
    if query_vector:
        blended = []
        for score, entry in scored:
            vector = entry.get("embedding")
            if not vector:
                # Articles ingested before embeddings existed: embed lazily
                # and cache the vector on the document for next time.
                vector = await embeddings.embed_one(
                    (entry.get("title", "") + " " + entry.get("content", ""))[:2000]
                )
                if vector and entry.get("_id"):
                    await database[db_module.KNOWLEDGE].update_one(
                        {"_id": entry["_id"]}, {"$set": {"embedding": vector}}
                    )
                    entry["embedding"] = vector
            sim = embeddings.cosine(query_vector, vector)
            if sim > 0.25:
                score += 10.0 * sim  # strong semantic match rivals a title hit
            blended.append((score, entry))
        scored = blended

    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return [], 0.0
    confidence = min(1.0, scored[0][0] / 8.0)
    return [e for _, e in scored[:limit]], round(confidence, 3)


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
