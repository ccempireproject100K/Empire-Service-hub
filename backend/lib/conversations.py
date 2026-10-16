"""Conversation/message persistence helpers shared by widget + operator APIs."""
import db as db_module
from lib.util import new_id, now, serialize


async def add_message(database, conversation: dict, role: str, text: str,
                      confidence: float | None = None, demo: bool | None = None,
                      attachments: list[dict] | None = None,
                      usage: dict | None = None,
                      language: str | None = None) -> dict:
    msg = {
        "_id": new_id("msg_"),
        "conversation_id": conversation["_id"],
        "tenant_id": conversation["tenant_id"],
        "role": role,
        "text": text,
        "created_at": now(),
    }
    if confidence is not None:
        msg["confidence"] = confidence
    if demo is not None:
        msg["demo"] = demo
    if attachments:
        msg["attachments"] = attachments
    if usage:
        msg["usage"] = usage
    if language:
        msg["language"] = language
    await database[db_module.MESSAGES].insert_one(msg)

    updates = {"updated_at": now()}
    if role in ("ai", "operator") and not conversation.get("first_response_at"):
        updates["first_response_at"] = now()
    if role == "ai" and conversation.get("status") == "open":
        updates["status"] = "ai_handling"
    await database[db_module.CONVERSATIONS].update_one(
        {"_id": conversation["_id"]}, {"$set": updates}
    )
    conversation.update(updates)
    return msg


async def list_messages(database, conversation_id: str) -> list[dict]:
    return await database[db_module.MESSAGES].find(
        {"conversation_id": conversation_id}
    ).sort("created_at", 1).to_list(length=1000)


def public_conversation(conversation: dict) -> dict:
    """Shape a conversation for API responses (no internal noise)."""
    out = serialize(conversation)
    return out


def public_message(msg: dict) -> dict:
    return serialize(msg)
