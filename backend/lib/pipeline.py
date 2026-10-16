"""The shared customer-message pipeline (v2).

Every inbound channel — widget chat, email, SMS, voice transcript — runs
the same core flow that v1 hard-coded in the widget router:

  1. detect the customer's language; keep the conversation's reply
     language inside the tenant's configured set
  2. store the customer message (with any attachment metadata)
  3. assemble AI context: similar past cases (precedent) + this
     customer's own history with the tenant
  4. let the AI answer while it owns the conversation
  5. run the escalation engine (which archives, routes and notifies)

Channel routers differ only in how messages arrive and how replies are
delivered back out (see lib.mailer); everything above is shared, so a
fix or improvement lands on every channel at once.
"""
import db as db_module
from lib import ai, conversations as conv_lib, customers, escalation
from lib import language as lang_lib
from lib import learning
from lib.util import now


async def attachment_metadata(database, tenant: dict, conversation: dict,
                              attachment_ids: list[str] | None) -> list[dict]:
    """Resolve + scope-check attachment ids into message metadata."""
    out = []
    for attachment_id in attachment_ids or []:
        doc = await database[db_module.ATTACHMENTS].find_one(
            {"_id": attachment_id}
        )
        if not doc:
            continue
        if doc["tenant_id"] != tenant["_id"] \
                or doc["conversation_id"] != conversation["_id"]:
            continue  # never attach another tenant's/conversation's file
        out.append({
            "id": doc["_id"],
            "filename": doc["filename"],
            "content_type": doc.get("content_type"),
            "size": doc.get("size"),
        })
    return out


async def build_ai_context(database, tenant: dict, conversation: dict,
                           text: str) -> dict:
    """Precedent + customer history + language for the next AI reply."""
    cases = await learning.similar_cases(
        database, tenant["_id"], text,
        exclude_conversation_id=conversation["_id"],
    )
    history = await customers.history(
        database, tenant["_id"], conversation.get("customer_key"),
        exclude_conversation_id=conversation["_id"],
    )
    return {
        "language": conversation.get("language"),
        "precedents": learning.precedent_lines(cases),
        "customer_history": customers.context_lines(history),
        "similar_cases": cases,
    }


async def handle_customer_message(database, tenant: dict, conversation: dict,
                                  text: str,
                                  attachment_ids: list[str] | None = None) -> dict:
    """Run one inbound customer message through the hub core.

    Returns {messages: [new message docs], ai_result, escalation,
    similar_cases}.
    """
    detected = lang_lib.detect_language(text)
    reply_lang = lang_lib.reply_language(tenant, conversation, detected)
    if reply_lang != conversation.get("language"):
        await database[db_module.CONVERSATIONS].update_one(
            {"_id": conversation["_id"]},
            {"$set": {"language": reply_lang, "updated_at": now()}},
        )
        conversation["language"] = reply_lang
    if detected:
        conversation["detected_language"] = detected

    attachments = await attachment_metadata(
        database, tenant, conversation, attachment_ids)
    customer_msg = await conv_lib.add_message(
        database, conversation, "customer", text,
        attachments=attachments, language=detected,
    )
    new_messages = [customer_msg]

    context = await build_ai_context(database, tenant, conversation, text)
    ai_result = None
    if conversation["status"] in ("open", "ai_handling"):
        ai_result = await ai.generate_reply(
            database, tenant, conversation["_id"], text, context=context)
        new_messages.append(await conv_lib.add_message(
            database, conversation, "ai", ai_result["text"],
            confidence=ai_result["confidence"], demo=ai_result["demo"],
            usage=ai_result["usage"],
        ))

    reason = await escalation.maybe_escalate(
        database, tenant, conversation, text, ai_result
    )
    return {
        "messages": new_messages,
        "ai_result": ai_result,
        "escalation": reason,
        "similar_cases": context["similar_cases"],
    }
