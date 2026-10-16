"""Lightweight customer-language detection (no external dependency).

Heuristic, deterministic: CJK character share identifies Chinese; small
distinctive stopword/function-word lists separate Spanish and French from
English. Returns an ISO tag ('en', 'es', 'fr', 'zh') or None when the text
is too short/ambiguous to call — callers then keep the conversation's
current language. Detection is *bounded by the tenant's configured
languages* at reply time (see ``reply_language``): the hub never answers
in a language the tenant hasn't enabled.
"""
import re

_ES_WORDS = {
    "hola", "gracias", "quiero", "necesito", "cuando", "donde", "como",
    "porque", "cuanto", "cuesta", "precio", "cuenta", "factura", "reembolso",
    "tarjeta", "horario", "abierto", "cerrado", "puedo", "tengo", "problema",
    "ayuda", "favor", "buenas", "dias", "tarde", "noche", "semana", "mes",
    "pago", "pagar", "envio", "pedido", "contraseña", "correo",
}

_FR_WORDS = {
    "bonjour", "merci", "veux", "voudrais", "quand", "comment", "pourquoi",
    "combien", "compte", "facture", "remboursement", "carte", "horaire",
    "ouvert", "ferme", "puis", "problème", "aide", "semaine", "mois",
    "paiement", "payer", "commande", "mot", "passe",
    "bonsoir", "madame", "monsieur",
}

_EN_WORDS = {
    "the", "please", "thanks", "want", "need", "when", "where", "how",
    "why", "account", "invoice", "refund", "card", "hours", "open",
    "closed", "problem", "help", "week", "month", "pay", "order",
    "password", "email", "hello", "veuillez",
}

_CJK_RX = re.compile(r"[一-鿿]")
_WORD_RX = re.compile(r"[a-záéíóúñüçàèêô']+", re.IGNORECASE)


def detect_language(text: str | None) -> str | None:
    """Detect 'en' | 'es' | 'fr' | 'zh', or None when undecidable."""
    if not text or len(text.strip()) < 3:
        return None
    cjk = len(_CJK_RX.findall(text))
    if cjk >= 2 and cjk / max(len(text), 1) > 0.15:
        return "zh"
    words = [w.lower() for w in _WORD_RX.findall(text)]
    if len(words) < 2:
        return None
    scores = {
        "es": sum(1 for w in words if w in _ES_WORDS),
        "fr": sum(1 for w in words if w in _FR_WORDS),
        "en": sum(1 for w in words if w in _EN_WORDS),
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return None
    # Require a strict lead so one borrowed word doesn't flip the language.
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] == ranked[1]:
        return None
    return best


def reply_language(tenant: dict, conversation: dict, detected: str | None) -> str:
    """Pick the language for the next AI reply, bounded by tenant languages."""
    allowed = tenant.get("languages") or ["en"]
    if detected and detected in allowed:
        return detected
    current = conversation.get("language")
    if current in allowed:
        return current
    return allowed[0]


# Human-readable names used in AI prompts ("Reply in Spanish.").
LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "zh": "Simplified Chinese",
}
