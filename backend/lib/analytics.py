"""Analytics & cost accounting (v2, section F).

*Topic clustering* answers "why are customers escalating this month?" —
each archived escalation is reduced to its dominant content keywords and
grouped, so the biggest drivers surface with counts and examples.
*Cost estimation* turns per-message token estimates (recorded by the AI
layer) into a cost-per-resolution figure using the configurable provider
rates in lib.config — estimates for margin visibility, never billing.
"""
from collections import Counter

import db as db_module
from lib import config, knowledge


def _dominant_tokens(text: str, top: int = 2) -> list[str]:
    tokens = knowledge.tokenize(text)
    counts = Counter(tokens)
    return [token for token, _ in counts.most_common(top)]


async def topic_clusters(database, tenant_id: str, limit: int = 10) -> list[dict]:
    """Group escalations by dominant keyword cluster, biggest first."""
    records = await database[db_module.ARCHIVE].find(
        {"tenant_id": tenant_id}
    ).to_list(length=5000)
    clusters: dict[str, dict] = {}
    for record in records:
        text = " ".join(
            m.get("text", "") for m in record.get("transcript") or []
            if m.get("role") == "customer"
        )
        dominant = _dominant_tokens(text)
        if not dominant:
            continue
        topic = " ".join(dominant)
        cluster = clusters.setdefault(topic, {
            "topic": topic, "count": 0, "resolved": 0, "examples": [],
        })
        cluster["count"] += 1
        if record.get("status") == "resolved":
            cluster["resolved"] += 1
        if len(cluster["examples"]) < 3:
            cluster["examples"].append({
                "conversation_id": record["conversation_id"],
                "question": text[:160],
            })
    out = sorted(clusters.values(), key=lambda c: c["count"], reverse=True)
    total = sum(c["count"] for c in out) or 1
    for cluster in out:
        cluster["share"] = round(cluster["count"] / total, 3)
    return out[:limit]


async def cost_summary(database, tenant_id: str) -> dict:
    """Estimated AI spend per tenant + per resolved conversation."""
    messages = await database[db_module.MESSAGES].find(
        {"tenant_id": tenant_id, "role": "ai"}
    ).to_list(length=50000)
    rates = config.ai_cost_rates()
    total_tokens = 0
    total_cost = 0.0
    by_provider: dict[str, dict] = {}
    for msg in messages:
        usage = msg.get("usage") or {}
        tokens = usage.get("est_tokens") or estimate_tokens(msg.get("text", ""))
        provider = usage.get("provider") or "demo"
        cost = usage.get("est_cost_usd")
        if cost is None:
            cost = tokens / 1000.0 * rates.get(provider, 0.0)
        total_tokens += tokens
        total_cost += cost
        bucket = by_provider.setdefault(
            provider, {"messages": 0, "est_tokens": 0, "est_cost_usd": 0.0})
        bucket["messages"] += 1
        bucket["est_tokens"] += tokens
        bucket["est_cost_usd"] = round(bucket["est_cost_usd"] + cost, 6)

    resolved = await database[db_module.CONVERSATIONS].count_documents(
        {"tenant_id": tenant_id, "status": "resolved"}
    )
    return {
        "ai_messages": len(messages),
        "est_tokens": total_tokens,
        "est_cost_usd": round(total_cost, 4),
        "resolved_conversations": resolved,
        "est_cost_per_resolution_usd": (
            round(total_cost / resolved, 4) if resolved else None
        ),
        "by_provider": by_provider,
        "rates_per_1k_tokens": rates,
    }


def estimate_tokens(text: str) -> int:
    """Rough token estimate (≈4 chars/token) used across the hub."""
    return max(1, (len(text or "") + 3) // 4)
