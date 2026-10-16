"""Conversation quality self-review (v2, section F).

Every resolved conversation gets a heuristic quality score — resolution
completeness, tone of the closing exchange, and policy flags (PII left in
an AI reply, an escalated case closed with no resolution summary, a
dissatisfied CSAT). Low scores land in an operator review queue so a
human spot-checks the weak cases instead of reading everything. The
scorer is deterministic and explainable: every point lost names its flag.
"""
import db as db_module
from lib import config, pii, sentiment
from lib.util import new_id, now


def score_conversation(conversation: dict, messages: list[dict],
                       archive_record: dict | None,
                       csat: dict | None) -> dict:
    score = 70
    flags: list[str] = []
    breakdown: dict[str, int] = {}

    resolution_text = ""
    if archive_record and archive_record.get("resolution"):
        resolution_text = archive_record["resolution"].get("text") or ""
    if conversation.get("escalation") or archive_record:
        if len(resolution_text) >= 40:
            score += 15
            breakdown["resolution_detail"] = 15
        elif resolution_text:
            score += 5
            breakdown["resolution_detail"] = 5
        else:
            score -= 20
            flags.append("no_resolution_summary")
            breakdown["resolution_detail"] = -20
    else:
        score += 10  # AI handled it end-to-end without escalation
        breakdown["ai_self_served"] = 10

    human_messages = [m for m in messages if m["role"] == "operator"]
    if human_messages:
        tone = sentiment.score_text(human_messages[-1].get("text", ""))
        if tone["label"] == "negative":
            score -= 15
            flags.append("negative_operator_tone")
            breakdown["tone"] = -15

    ai_messages = [m for m in messages if m["role"] == "ai"]
    if any(pii.contains_pii(m.get("text", "")) for m in ai_messages):
        score -= 20
        flags.append("pii_in_ai_reply")
        breakdown["pii"] = -20

    if csat:
        if csat["rating"] >= 4:
            score += 10
            breakdown["csat"] = 10
        elif csat["rating"] <= 2:
            score -= 15
            flags.append("low_csat")
            breakdown["csat"] = -15

    score = max(0, min(100, score))
    return {"score": score, "flags": flags, "breakdown": breakdown}


async def review_on_resolve(database, tenant: dict, conversation: dict) -> dict:
    """Score a just-resolved conversation and file it for review if weak."""
    messages = await database[db_module.MESSAGES].find(
        {"conversation_id": conversation["_id"]}
    ).sort("created_at", 1).to_list(length=1000)
    record = await database[db_module.ARCHIVE].find_one(
        {"conversation_id": conversation["_id"]}
    )
    csat = await database[db_module.CSAT].find_one(
        {"conversation_id": conversation["_id"]}
    )
    result = score_conversation(conversation, messages, record, csat)
    review = {
        "_id": new_id("qr_"),
        "tenant_id": tenant["_id"],
        "conversation_id": conversation["_id"],
        "score": result["score"],
        "flags": result["flags"],
        "breakdown": result["breakdown"],
        "status": "queued" if result["score"] < config.quality_review_threshold()
                  else "passed",
        "reviewed_by": None,
        "created_at": now(),
    }
    await database[db_module.QUALITY_REVIEWS].insert_one(review)
    return review
