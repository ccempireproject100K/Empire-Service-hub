"""Guardrails: sensitive topics in, PII leakage out.

Inbound: each tenant can configure ``sensitive_topics`` — subjects (legal
threats, medical questions, payment disputes, …) that must always reach a
human regardless of AI confidence. Format per topic: either a plain string
(``"chargeback"`` — the string itself is the keyword) or an object
``{"name": "legal", "keywords": ["lawsuit", "attorney"]}``.

Outbound: AI replies pass a PII check before they are stored/sent; any PII
the model echoed (say, repeating a card number back) is masked in place
and the reply is flagged so operators can review it.
"""
import re

from lib import pii


def _topic_keywords(topic) -> tuple[str, list[str]]:
    if isinstance(topic, str):
        return topic, [topic]
    if isinstance(topic, dict):
        name = topic.get("name") or "sensitive"
        keywords = topic.get("keywords") or ([name] if name else [])
        return name, [str(k) for k in keywords]
    return "sensitive", []


def match_sensitive_topic(tenant: dict, text: str | None) -> dict | None:
    """Return {topic, keyword} when the customer text hits a sensitive topic."""
    if not text:
        return None
    for topic in tenant.get("sensitive_topics") or []:
        name, keywords = _topic_keywords(topic)
        for keyword in keywords:
            if keyword and re.search(
                r"\b" + re.escape(keyword) + r"\b", text, re.IGNORECASE
            ):
                return {"topic": name, "keyword": keyword}
    return None


def check_outbound(text: str) -> dict:
    """PII-check an outbound (AI) reply. Returns text possibly masked + flags."""
    findings = pii.find_pii(text)
    if not findings:
        return {"text": text, "redacted": False, "findings": []}
    return {"text": pii.mask_text(text), "redacted": True, "findings": findings}
