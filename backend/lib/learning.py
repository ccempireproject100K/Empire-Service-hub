"""The learning loop, closed (v2, section A) — the hub's headline feature.

v1 archived every escalation + human resolution but nothing fed back.
v2 closes the loop three ways:

1. **Resolution → KB draft.** Resolving an escalated conversation turns
   the question + resolution into a draft knowledge article in a
   per-tenant review queue. An operator edits/approves (it publishes into
   the live KB) or discards it. With an LLM key the draft is written by
   the model; in demo mode a deterministic template produces it.
2. **Similar-case retrieval.** New escalations (and AI answer attempts)
   pull the closest past cases from the archive — keyword overlap, with
   an embedding boost when configured — and surface them to the AI as
   precedent and to the operator as "solved before like this".
3. **Learning metrics.** Deflection rate (AI-resolved without ever
   escalating), repeat-issue rate, and their trend — the numbers that
   prove the loop is working.
"""
import httpx

import db as db_module
from lib import config, embeddings, knowledge
from lib.util import new_id, now

# ------------------------------------------------------------------ drafts
async def _llm_draft(tenant: dict, question: str, resolution: str) -> dict | None:
    """Ask the live LLM to write the KB article. None → caller templates it."""
    provider = config.ai_provider_name()
    if provider == "demo":
        return None
    prompt = (
        f"You maintain the help knowledge base for {tenant['name']}. A customer "
        f"asked:\n\n{question}\n\nA human resolved it with:\n\n{resolution}\n\n"
        "Write a reusable help article as JSON with keys 'title' and 'content'. "
        "Generalize away from this one customer; no names or personal details."
    )
    try:
        if provider == "openai":
            import os

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {config.openai_api_key()}"},
                    json={
                        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                import json as _json

                data = _json.loads(resp.json()["choices"][0]["message"]["content"])
        else:  # gemini — no strict JSON mode; parse leniently
            import json as _json
            import os
            import re

            model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent",
                    params={"key": config.gemini_api_key()},
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                )
                resp.raise_for_status()
                raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            data = _json.loads(match.group(0)) if match else {}
        title = str(data.get("title") or "").strip()
        content = str(data.get("content") or "").strip()
        if title and content:
            return {"title": title[:160], "content": content[:4000], "source": provider}
    except Exception:
        return None
    return None


def _template_draft(question: str, resolution: str) -> dict:
    short_q = " ".join((question or "Customer question").split())[:80]
    title = f"How to handle: {short_q}"
    content = (
        f"Customers may ask: {question.strip()}\n\n"
        f"What resolved it: {resolution.strip()}\n\n"
        "(Draft generated from a resolved escalation — review and edit "
        "before this becomes a general help article.)"
    )
    return {"title": title[:160], "content": content[:4000], "source": "template"}


async def draft_from_resolution(database, tenant: dict, conversation: dict,
                                archive_record: dict, resolution_text: str) -> dict:
    """Create a KB draft from a freshly resolved escalation."""
    transcript = archive_record.get("transcript") or []
    question = next(
        (m["text"] for m in transcript if m.get("role") == "customer"),
        conversation.get("subject") or "",
    )
    draft_body = await _llm_draft(tenant, question, resolution_text)
    if draft_body is None:
        draft_body = _template_draft(question, resolution_text)
    draft = {
        "_id": new_id("draft_"),
        "tenant_id": tenant["_id"],
        "conversation_id": conversation["_id"],
        "archive_id": archive_record["_id"],
        "question": question,
        "resolution": resolution_text,
        "title": draft_body["title"],
        "content": draft_body["content"],
        "source": draft_body["source"],
        "status": "pending",  # pending | approved | discarded
        "created_at": now(),
        "updated_at": now(),
    }
    await database[db_module.KB_DRAFTS].insert_one(draft)
    return draft


# ----------------------------------------------------------- similar cases
def _case_text(record: dict) -> str:
    parts = []
    for msg in record.get("transcript") or []:
        if msg.get("role") == "customer":
            parts.append(msg.get("text", ""))
    return " ".join(parts)


async def similar_cases(database, tenant_id: str, query_text: str, limit: int = 3,
                        exclude_conversation_id: str | None = None) -> list[dict]:
    """Closest past archive cases for a question, best first.

    Keyword overlap is the base score; when the query and a case both have
    embeddings, cosine similarity boosts it. Resolved cases rank above
    unresolved ones — precedent is only useful when it includes the fix.
    """
    if not query_text:
        return []
    records = await database[db_module.ARCHIVE].find(
        {"tenant_id": tenant_id}
    ).to_list(length=1000)
    query_tokens = set(knowledge.tokenize(query_text))
    query_vector = await embeddings.embed_one(query_text) if embeddings.available() else None
    scored = []
    for record in records:
        if record.get("conversation_id") == exclude_conversation_id:
            continue
        case_text = _case_text(record)
        if not case_text:
            continue
        overlap = len(query_tokens & set(knowledge.tokenize(case_text)))
        score = float(overlap)
        vector = record.get("embedding")
        if query_vector and vector:
            sim = embeddings.cosine(query_vector, vector)
            if sim > 0.25:
                score += 10.0 * sim
        if record.get("status") == "resolved":
            score += 0.5
        if score <= 0:
            continue
        resolution = (record.get("resolution") or {}).get("text")
        scored.append((score, {
            "archive_id": record["_id"],
            "conversation_id": record["conversation_id"],
            "question": case_text[:240],
            "resolution": resolution,
            "rule": (record.get("reason") or {}).get("rule"),
            "status": record.get("status"),
            "score": round(score, 3),
        }))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [case for _, case in scored[:limit]]


def precedent_lines(cases: list[dict]) -> list[str]:
    """Compact precedent lines for the AI prompt."""
    lines = []
    for case in cases:
        if case.get("resolution"):
            lines.append(
                f"- A similar customer asked \"{case['question']}\" and it was "
                f"resolved with: {case['resolution']}"
            )
    return lines


# ----------------------------------------------------------------- metrics
async def learning_metrics(database, tenant_id: str, days: int = 30) -> dict:
    """Deflection / repeat-issue rates + daily trend for one tenant."""
    from datetime import timedelta

    cutoff = now() - timedelta(days=days)
    conversations = await database[db_module.CONVERSATIONS].find(
        {"tenant_id": tenant_id}
    ).to_list(length=20000)
    conversations = [c for c in conversations
                     if (c.get("created_at") or cutoff) >= cutoff]

    total = len(conversations)
    resolved = [c for c in conversations if c.get("status") == "resolved"]
    escalated_ids = {
        r["conversation_id"]
        for r in await database[db_module.ARCHIVE].find(
            {"tenant_id": tenant_id}
        ).to_list(length=20000)
    }
    ai_resolved = [c for c in resolved if c["_id"] not in escalated_ids]
    deflection_rate = round(len(ai_resolved) / total, 3) if total else None

    by_customer: dict[str, int] = {}
    for c in conversations:
        if c.get("customer_key"):
            by_customer[c["customer_key"]] = by_customer.get(c["customer_key"], 0) + 1
    repeaters = sum(1 for n in by_customer.values() if n > 1)
    repeat_issue_rate = (
        round(repeaters / len(by_customer), 3) if by_customer else None
    )

    trend_by_day: dict[str, dict] = {}
    for c in conversations:
        day = (c.get("created_at") or cutoff).date().isoformat()
        bucket = trend_by_day.setdefault(
            day, {"date": day, "total": 0, "escalated": 0, "ai_resolved": 0})
        bucket["total"] += 1
        if c["_id"] in escalated_ids:
            bucket["escalated"] += 1
        elif c.get("status") == "resolved":
            bucket["ai_resolved"] += 1
    trend = []
    for day in sorted(trend_by_day):
        bucket = trend_by_day[day]
        bucket["deflection_rate"] = (
            round(bucket["ai_resolved"] / bucket["total"], 3) if bucket["total"] else None
        )
        trend.append(bucket)

    return {
        "window_days": days,
        "conversations": total,
        "resolved": len(resolved),
        "ai_resolved": len(ai_resolved),
        "escalated": len([c for c in conversations if c["_id"] in escalated_ids]),
        "deflection_rate": deflection_rate,
        "repeat_customers": repeaters,
        "repeat_issue_rate": repeat_issue_rate,
        "trend": trend,
    }
