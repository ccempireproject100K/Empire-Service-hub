"""Provider-abstracted AI front line.

Selection is key-gated (see lib.config): OpenAI when ``OPENAI_API_KEY`` is
set, else Gemini when ``GEMINI_API_KEY`` is set, else the deterministic demo
provider. Every reply carries ``confidence`` (grounded in knowledge-base
retrieval strength, never invented by the model) and a ``demo`` flag so no
surface ever mistakes demo output for a live model.
"""
import os

import httpx

import db as db_module
from lib import config, knowledge

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


async def _openai_reply(tenant: dict, entries: list[dict], history: list[dict], text: str) -> str:
    messages = [
        {"role": "system", "content": _SYSTEM_TEMPLATE.format(
            name=tenant["name"], articles=_articles_text(entries))},
    ]
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


async def _gemini_reply(tenant: dict, entries: list[dict], history: list[dict], text: str) -> str:
    model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    prompt = _SYSTEM_TEMPLATE.format(name=tenant["name"], articles=_articles_text(entries))
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


async def generate_reply(database, tenant: dict, conversation_id: str, customer_text: str) -> dict:
    """Produce the AI reply for a customer message.

    Returns {text, confidence, demo, provider, citations: [entry titles]}.
    """
    entries = await database[db_module.KNOWLEDGE].find(
        {"tenant_id": tenant["_id"]}
    ).to_list(length=500)
    matches, confidence = knowledge.retrieve(entries, customer_text)
    history = await database[db_module.MESSAGES].find(
        {"conversation_id": conversation_id}
    ).sort("created_at", 1).to_list(length=50)

    provider = config.ai_provider_name()
    if provider == "openai":
        text = await _openai_reply(tenant, matches, history, customer_text)
    elif provider == "gemini":
        text = await _gemini_reply(tenant, matches, history, customer_text)
    else:
        text = _demo_reply(tenant, matches, confidence)

    return {
        "text": text,
        "confidence": confidence,
        "demo": provider == "demo",
        "provider": provider,
        "citations": [e["title"] for e in matches],
    }
