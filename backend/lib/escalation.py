"""Escalation engine — decides when the AI hands a conversation to a human.

Rules, in priority order (first match wins, and the winning rule is recorded):
  a) customer_request — the customer explicitly asks for a human
  b) keyword          — a tenant-configured escalation keyword appears
  c) sentiment        — lexicon sentiment at/under the tenant's threshold
  d) sensitive_topic  — a tenant-configured sensitive topic appears (v2:
                        auto-escalates regardless of AI confidence)
  e) low_confidence   — AI confidence under the tenant's threshold

On escalation the conversation moves to the human queue and an archive
record is written (the learning loop) — see lib.archive. v2 also
auto-assigns an operator (lib.routing) and emits the
``escalation.created`` tenant webhook (lib.webhooks).
"""
import re

import db as db_module
from lib import archive, guardrails, routing, sentiment, webhooks
from lib.util import new_id, now

_HUMAN_REQUEST_RX = re.compile(
    r"\b(human|real person|live agent|agent|speak to someone|talk to someone|"
    r"representative|manager|supervisor|somebody real|actual person)\b",
    re.IGNORECASE,
)


async def _add_system_message(database, conversation_id: str, tenant_id: str, text: str) -> dict:
    msg = {
        "_id": new_id("msg_"),
        "conversation_id": conversation_id,
        "tenant_id": tenant_id,
        "role": "system",
        "text": text,
        "created_at": now(),
    }
    await database[db_module.MESSAGES].insert_one(msg)
    return msg


def evaluate(tenant: dict, customer_text: str, ai_result: dict | None) -> dict | None:
    """Pure rule evaluation. Returns a reason dict or None (no escalation)."""
    if _HUMAN_REQUEST_RX.search(customer_text or ""):
        return {"rule": "customer_request", "detail": "Customer asked for a human."}

    for keyword in tenant.get("escalation_keywords") or []:
        if re.search(r"\b" + re.escape(keyword) + r"\b", customer_text or "", re.IGNORECASE):
            return {"rule": "keyword", "detail": f"Keyword matched: {keyword}"}

    sentiment_result = sentiment.score_text(customer_text)
    threshold = tenant.get("sentiment_threshold", -0.34)
    if sentiment_result["label"] == "negative" and sentiment_result["score"] <= threshold:
        return {
            "rule": "sentiment",
            "detail": f"Sentiment {sentiment_result['score']} "
                      f"({sentiment_result['negative_hits']} negative hits).",
            "sentiment": sentiment_result,
        }

    sensitive = guardrails.match_sensitive_topic(tenant, customer_text)
    if sensitive:
        return {
            "rule": "sensitive_topic",
            "detail": f"Sensitive topic '{sensitive['topic']}' "
                      f"(keyword: {sensitive['keyword']}).",
            "topic": sensitive["topic"],
        }

    if ai_result is not None:
        confidence_threshold = tenant.get("confidence_threshold", 0.55)
        if ai_result["confidence"] < confidence_threshold:
            return {
                "rule": "low_confidence",
                "detail": f"AI confidence {ai_result['confidence']} "
                          f"below threshold {confidence_threshold}.",
                "confidence": ai_result["confidence"],
            }
    return None


async def maybe_escalate(database, tenant: dict, conversation: dict,
                         customer_text: str, ai_result: dict | None) -> dict | None:
    """Evaluate rules; on a hit, escalate + archive. Returns the reason or None."""
    if conversation.get("status") not in ("open", "ai_handling"):
        return None
    reason = evaluate(tenant, customer_text, ai_result)
    if not reason:
        # Keep the latest sentiment on the conversation for the console.
        await database[db_module.CONVERSATIONS].update_one(
            {"_id": conversation["_id"]},
            {"$set": {"last_sentiment": sentiment.score_text(customer_text),
                      "updated_at": now()}},
        )
        return None

    reason["sentiment"] = reason.get("sentiment") or sentiment.score_text(customer_text)
    await database[db_module.CONVERSATIONS].update_one(
        {"_id": conversation["_id"]},
        {"$set": {
            "status": "escalated",
            "escalation": {**reason, "at": now()},
            "last_sentiment": reason["sentiment"],
            "updated_at": now(),
        }},
    )
    conversation["status"] = "escalated"
    conversation["escalation"] = reason
    await archive.write_escalation(database, tenant, conversation, reason)
    await _add_system_message(
        database, conversation["_id"], tenant["_id"],
        "This conversation has been escalated to a human teammate.",
    )
    # v2: route to an operator + notify subscribed products.
    await routing.auto_assign(database, tenant, conversation)
    await webhooks.emit(database, tenant, "escalation.created", {
        "conversation_id": conversation["_id"],
        "rule": reason["rule"],
        "detail": reason.get("detail"),
        "channel": conversation.get("channel"),
        "external_ref": conversation.get("external_ref"),
    })
    return reason
