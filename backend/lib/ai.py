"""Provider-abstracted AI front line.

Selection is key-gated (see lib.config): OpenAI when ``OPENAI_API_KEY`` is
set, else Gemini when ``GEMINI_API_KEY`` is set, else the deterministic demo
provider. Every reply carries ``confidence`` (grounded in knowledge-base
retrieval strength, never invented by the model) and a ``demo`` flag so no
surface ever mistakes demo output for a live model.

v2 additions: retrieval is hybrid (KB articles + ingested document chunks,
semantic boost when embeddings are configured); the prompt includes
*precedent* — similar past cases and how they were resolved — plus the
customer's own prior history; replies are produced in the customer's
detected language (bounded by tenant languages); every reply passes an
outbound PII check and is masked if the model echoed personal data; token
usage estimates are recorded for cost-per-resolution accounting.
"""
import os

import httpx

import db as db_module
from lib import analytics, config, guardrails, knowledge, language as lang_lib

_SYSTEM_TEMPLATE = (
    "You are the customer-service assistant for {name}. Answer the customer "
    "using ONLY the help articles below. If the articles do not cover the "
    "question, say so briefly and offer to bring in a human teammate. "
    "Be concise and friendly. Help articles:\n{articles}"
)


def _articles_text(entries: list[dict]) -> str:
    if not entries:
        return "(none matched this question)"
    return "\n".join(f"- {e['title']}: {e['content']}" for e in entries)


def _context_block(context: dict | None) -> str:
    """Render precedent / customer-history / language context for prompts."""
    if not context:
        return ""
    parts = []
    precedents = context.get("precedents") or []
    if precedents:
        parts.append("Similar past cases and how they were resolved:\n"
                     + "\n".join(precedents))
    history = context.get("customer_history") or []
    if history:
        parts.append("This customer's earlier conversations:\n"
                     + "\n".join(history))
    language = context.get("language")
    if language and language != "en":
        name = lang_lib.LANGUAGE_NAMES.get(language, language)
        parts.append(f"Reply in {name}.")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


async def _openai_reply(tenant: dict, entries: list[dict], history: list[dict],
                        text: str, context: dict | None) -> str:
    system = _SYSTEM_TEMPLATE.format(
        name=tenant["name"], articles=_articles_text(entries))
    system += _context_block(context)
    messages = [{"role": "system", "content": system}]
    for msg in history[-8:]:
        role = "user" if msg["role"] == "customer" else "assistant"
        messages.append({"role": role, "content": msg["text"]})
    messages.append({"role": "user", "content": text})
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.openai_api_key()}"},
            json={
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": messages,
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def _gemini_reply(tenant: dict, entries: list[dict], history: list[dict],
                        text: str, context: dict | None) -> str:
    model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    prompt = _SYSTEM_TEMPLATE.format(name=tenant["name"], articles=_articles_text(entries))
    prompt += _context_block(context)
    prompt += f"\n\nCustomer: {text}\nAssistant:"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": config.gemini_api_key()},
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _demo_reply(tenant: dict, entries: list[dict], confidence: float) -> str:
    """Deterministic structured reply — same inputs, same output, always."""
    greeting = (tenant.get("branding") or {}).get("name") or tenant["name"]
    if entries:
        top = entries[0]
        body = top["content"].strip()
        if len(body) > 600:
            body = body[:600].rsplit(" ", 1)[0] + "…"
        return (
            f"Thanks for reaching out to {greeting}. Based on our help article "
            f"\"{top['title']}\": {body}"
        )
    return (
        f"Thanks for reaching out to {greeting}. I couldn't find a help article "
        "that covers that yet, so I'm bringing in a teammate who can help "
        "directly — they'll pick this up from here."
    )


async def generate_reply(database, tenant: dict, conversation_id: str,
                         customer_text: str, context: dict | None = None) -> dict:
    """Produce the AI reply for a customer message.

    Returns {text, confidence, demo, provider, citations, usage,
    pii_redacted}. ``context`` may carry {language, precedents,
    customer_history} — assembled by the message pipeline.
    """
    matches, confidence = await knowledge.search_hybrid(
        database, tenant["_id"], customer_text
    )
    history = await database[db_module.MESSAGES].find(
        {"conversation_id": conversation_id}
    ).sort("created_at", 1).to_list(length=50)

    provider = config.ai_provider_name()
    if provider == "openai":
        text = await _openai_reply(tenant, matches, history, customer_text, context)
    elif provider == "gemini":
        text = await _gemini_reply(tenant, matches, history, customer_text, context)
    else:
        text = _demo_reply(tenant, matches, confidence)

    # Outbound guardrail: never let a reply echo PII back to the customer.
    checked = guardrails.check_outbound(text)
    text = checked["text"]

    rates = config.ai_cost_rates()
    est_tokens = analytics.estimate_tokens(customer_text) + \
        analytics.estimate_tokens(text)
    usage = {
        "provider": provider,
        "est_tokens": est_tokens,
        "est_cost_usd": round(est_tokens / 1000.0 * rates.get(provider, 0.0), 6),
    }
    return {
        "text": text,
        "confidence": confidence,
        "demo": provider == "demo",
        "provider": provider,
        "citations": [e["title"] for e in matches],
        "usage": usage,
        "pii_redacted": checked["redacted"],
    }
