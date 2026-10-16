"""Lexicon sentiment scoring (demo-mode default).

Pattern harvested from Momo's conversation-intelligence work: curated
positive/negative word lists with word-boundary matching, frustration phrases
weighted double. A real model can replace ``score_text`` behind the same
return shape: {score: -1..1, label, positive_hits, negative_hits}.
"""
import re

POSITIVE_WORDS = [
    "great", "awesome", "excellent", "perfect", "thanks", "thank you",
    "appreciate", "helpful", "happy", "pleased", "wonderful", "fantastic",
    "love", "good", "nice", "amazing", "resolved", "fixed",
    # Spanish (v2: lexicons cover EN + ES; FR/ZH are detection-tagged only)
    "gracias", "excelente", "perfecto", "maravilloso", "fantástico",
    "contento", "feliz", "bueno", "buena", "amable", "resuelto", "arreglado",
]

NEGATIVE_WORDS = [
    "bad", "terrible", "awful", "horrible", "angry", "furious", "frustrated",
    "annoyed", "upset", "disappointed", "unacceptable", "ridiculous",
    "useless", "broken", "wrong", "fail", "failed", "failure", "scam",
    "refund", "cancel", "hate", "worst", "poor", "slow", "never", "again",
    "complaint", "lawsuit", "lawyer", "bbb", "not working", "doesn't work",
    "does not work", "still waiting", "nobody", "ignore", "ignored",
    # Spanish
    "malo", "mala", "pésimo", "enojado", "enojada", "furioso", "frustrado",
    "molesto", "decepcionado", "inaceptable", "ridículo", "inútil", "roto",
    "equivocado", "falla", "fallo", "estafa", "reembolso", "cancelar",
    "odio", "peor", "lento", "nunca", "queja", "demanda", "abogado",
    "no funciona", "sigo esperando", "ignorado",
]

# Frustration phrases count double — they signal a customer about to churn.
FRUSTRATION_PHRASES = [
    "this is ridiculous", "not working", "doesn't work", "does not work",
    "still waiting", "speak to a human", "talk to a human", "real person",
    "cancel my", "refund my", "i want a refund",
    "es ridículo", "no funciona", "sigo esperando", "hablar con un humano",
    "quiero un reembolso", "quiero cancelar",
]


def _compile(phrases: list[str]) -> list[re.Pattern]:
    return [re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE) for p in phrases]


_POS_RX = _compile(POSITIVE_WORDS)
_NEG_RX = _compile(NEGATIVE_WORDS)
_FRUSTRATION_RX = _compile(FRUSTRATION_PHRASES)


def score_text(text: str) -> dict:
    text = text or ""
    pos = sum(1 for rx in _POS_RX if rx.search(text))
    neg = sum(1 for rx in _NEG_RX if rx.search(text))
    neg += sum(1 for rx in _FRUSTRATION_RX if rx.search(text))  # extra weight
    total = pos + neg
    score = (pos - neg) / total if total else 0.0
    if score <= -0.34:
        label = "negative"
    elif score >= 0.34:
        label = "positive"
    else:
        label = "neutral"
    return {
        "score": round(score, 3),
        "label": label,
        "positive_hits": pos,
        "negative_hits": neg,
    }
