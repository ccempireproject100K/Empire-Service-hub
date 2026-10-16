"""Operator console API: the human side of the hub.

Operators list conversations across their tenants, take over escalations,
reply, and resolve. Resolving an escalated conversation completes its
archive record with the human's resolution — closing the learning loop.
Basic SLA + CSAT aggregates are exposed per tenant for the console header.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db as db_module
from lib import archive, auth, conversations as conv_lib
from lib.util import iso, now, serialize

router = APIRouter(prefix="/api/operator", tags=["operator"])


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=1, max_length=4000)


async def _scoped_conversation(database, operator: dict, conversation_id: str) -> dict:
    conversation = await database[db_module.CONVERSATIONS].find_one(
        {"_id": conversation_id}
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    auth.assert_tenant_access(operator, conversation["tenant_id"])
    return conversation


@router.get("/conversations")
async def list_conversations(tenant_id: str | None = None, status: str | None = None,
                             limit: int = 100,
                             operator: dict = Depends(auth.get_current_operator),
                             database=Depends(db_module.get_db)):
    query: dict = {}
    if tenant_id:
        auth.assert_tenant_access(operator, tenant_id)
        query["tenant_id"] = tenant_id
    elif operator.get("role") != "admin":
        query["tenant_id"] = {"$in": operator.get("tenant_ids") or []}
    if status:
        query["status"] = status
    conversations = await database[db_module.CONVERSATIONS].find(query).sort(
        "updated_at", -1
    ).to_list(length=min(limit, 500))
    out = []
    for conversation in conversations:
        messages = await conv_lib.list_messages(database, conversation["_id"])
        last = messages[-1] if messages else None
        summary = conv_lib.public_conversation(conversation)
        summary["last_message"] = (
            {"role": last["role"], "text": last["text"][:160],
             "created_at": iso(last["created_at"])}
            if last else None
        )
        summary["message_count"] = len(messages)
        out.append(summary)
    return {"conversations": out}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str,
                           operator: dict = Depends(auth.get_current_operator),
                           database=Depends(db_module.get_db)):
    conversation = await _scoped_conversation(database, operator, conversation_id)
    messages = await conv_lib.list_messages(database, conversation_id)
    return {
        "conversation": conv_lib.public_conversation(conversation),
        "messages": [conv_lib.public_message(m) for m in messages],
    }


@router.post("/conversations/{conversation_id}/takeover")
async def takeover(conversation_id: str,
                   operator: dict = Depends(auth.get_current_operator),
                   database=Depends(db_module.get_db)):
    conversation = await _scoped_conversation(database, operator, conversation_id)
    if conversation["status"] == "resolved":
        raise HTTPException(status_code=409, detail="Conversation is resolved")
    await database[db_module.CONVERSATIONS].update_one(
        {"_id": conversation_id},
        {"$set": {"status": "operator_handling",
                  "assigned_operator_id": operator["_id"], "updated_at": now()}},
    )
    await conv_lib.add_message(
        database, conversation, "system",
        f"{operator.get('name') or operator['email']} joined the conversation.",
    )
    fresh = await database[db_module.CONVERSATIONS].find_one({"_id": conversation_id})
    return {"conversation": conv_lib.public_conversation(fresh)}


@router.post("/conversations/{conversation_id}/reply")
async def reply(conversation_id: str, body: ReplyIn,
                operator: dict = Depends(auth.get_current_operator),
                database=Depends(db_module.get_db)):
    conversation = await _scoped_conversation(database, operator, conversation_id)
    if conversation["status"] == "resolved":
        raise HTTPException(status_code=409, detail="Conversation is resolved")
    if conversation["status"] in ("open", "ai_handling"):
        # Replying without an explicit takeover implies taking over.
        await database[db_module.CONVERSATIONS].update_one(
            {"_id": conversation_id},
            {"$set": {"status": "operator_handling",
                      "assigned_operator_id": operator["_id"]}},
        )
        conversation["status"] = "operator_handling"
    msg = await conv_lib.add_message(database, conversation, "operator", body.text)
    fresh = await database[db_module.CONVERSATIONS].find_one({"_id": conversation_id})
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "message": conv_lib.public_message(msg),
    }


@router.post("/conversations/{conversation_id}/resolve")
async def resolve(conversation_id: str, body: ResolveIn,
                  operator: dict = Depends(auth.get_current_operator),
                  database=Depends(db_module.get_db)):
    conversation = await _scoped_conversation(database, operator, conversation_id)
    if conversation["status"] == "resolved":
        raise HTTPException(status_code=409, detail="Conversation already resolved")
    was_escalated = conversation["status"] == "escalated" or bool(conversation.get("escalation"))
    await database[db_module.CONVERSATIONS].update_one(
        {"_id": conversation_id},
        {"$set": {"status": "resolved", "resolved_at": now(),
                  "assigned_operator_id": operator["_id"], "updated_at": now()}},
    )
    await conv_lib.add_message(
        database, conversation, "system",
        "This conversation was marked resolved. How did we do?",
    )
    archive_record = None
    if was_escalated:
        record = await archive.complete_resolution(
            database, conversation_id, body.resolution,
            operator.get("name") or operator["email"],
        )
        archive_record = serialize(record) if record else None
    fresh = await database[db_module.CONVERSATIONS].find_one({"_id": conversation_id})
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "csat_prompt": True,
        "archive": archive_record,
    }


@router.get("/stats")
async def stats(tenant_id: str,
                operator: dict = Depends(auth.get_current_operator),
                database=Depends(db_module.get_db)):
    auth.assert_tenant_access(operator, tenant_id)
    conversations = await database[db_module.CONVERSATIONS].find(
        {"tenant_id": tenant_id}
    ).to_list(length=5000)
    by_status: dict[str, int] = {}
    first_response_times, resolution_times = [], []
    for c in conversations:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
        if c.get("first_response_at") and c.get("created_at"):
            first_response_times.append(
                (c["first_response_at"] - c["created_at"]).total_seconds())
        if c.get("resolved_at") and c.get("created_at"):
            resolution_times.append(
                (c["resolved_at"] - c["created_at"]).total_seconds())

    csat_rows = await database[db_module.CSAT].find(
        {"tenant_id": tenant_id}
    ).to_list(length=5000)
    distribution = {str(i): 0 for i in range(1, 6)}
    for row in csat_rows:
        distribution[str(row["rating"])] += 1
    csat_avg = (
        round(sum(r["rating"] for r in csat_rows) / len(csat_rows), 2)
        if csat_rows else None
    )

    def _avg(values):
        return round(sum(values) / len(values), 1) if values else None

    escalations = await database[db_module.ARCHIVE].count_documents(
        {"tenant_id": tenant_id}
    )
    return {
        "tenant_id": tenant_id,
        "conversations_total": len(conversations),
        "by_status": by_status,
        "escalations_total": escalations,
        "csat": {"average": csat_avg, "count": len(csat_rows),
                 "distribution": distribution},
        "sla": {
            "avg_first_response_seconds": _avg(first_response_times),
            "avg_resolution_seconds": _avg(resolution_times),
        },
    }
