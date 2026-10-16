"""PII masking for logs and for the escalation-archive corpus.

The archive is a long-lived learning asset, so transcripts are masked *at
write time*: the live conversation keeps the original text for the operator
handling it, the archive only ever stores the masked copy.
"""
import re

_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s])\d{3}[-.\s]\d{4}(?!\d)")
_LONG_DIGITS_RX = re.compile(r"(?<!\d)\d{9,}(?!\d)")  # card / SSN-like runs


def mask_text(text: str | None) -> str:
    if not text:
        return ""
    out = _EMAIL_RX.sub("[email]", text)
    out = _PHONE_RX.sub("[phone]", out)
    out = _LONG_DIGITS_RX.sub("[number]", out)
    return out


def find_pii(text: str | None) -> list[str]:
    """Return the kinds of PII present in text: 'email', 'phone', 'number'."""
    if not text:
        return []
    found = []
    if _EMAIL_RX.search(text):
        found.append("email")
    if _PHONE_RX.search(text):
        found.append("phone")
    if _LONG_DIGITS_RX.search(text):
        found.append("number")
    return found


def contains_pii(text: str | None) -> bool:
    return bool(find_pii(text))


def mask_transcript(messages: list[dict]) -> list[dict]:
    """Return a masked copy of a message list for archive storage."""
    masked = []
    for msg in messages:
        copy = dict(msg)
        if isinstance(copy.get("text"), str):
            copy["text"] = mask_text(copy["text"])
        masked.append(copy)
    return masked
