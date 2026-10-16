"""Sync-back API (v2, section E): products read resolution status back.

v1's product adapters pushed escalations *into* the hub one-way. v2 lets
the product poll the outcome: given the ``external_ref`` it attached when
the conversation was created (or the hub conversation id), it gets the
status, the human resolution (from the archive), and any CSAT — so Momo
can close its own ticket when the hub resolves the case. Authenticated
with the tenant secret key; refs are only ever resolved inside the
caller's own tenant.
"""
from fastapi import APIRouter, Depends, Header, HTTPException

import db as db_module
from lib import auth
from lib.util import serialize

router = APIRouter(prefix="/api/sync", tags=["sync"])


async def _status_payload(database, conversation: dict) -> dict:
    record = await database[db_module.ARCHIVE].find_one(
        {"conversation_id": conversation["_id"]}
    )
    csat = await database[db_module.CSAT].find_one(
        {"conversation_id": conversation["_id"]}
    )
    resolution = None
    if record and record.get("resolution"):
        resolution = {
            "text": record["resolution"].get("text"),
            "resolved_by": record["resolution"].get("resolved_by"),
            "resolved_at": serialize({"t": record["resolution"].get("resolved_at")})["t"],
        }
    return {
        "conversation_id": conversation["_id"],
        "tenant_id": conversation["tenant_id"],
        "external_ref": conversation.get("external_ref"),
        "channel": conversation.get("channel"),
        "status": conversation["status"],
        "escalation_rule": (conversation.get("escalation") or {}).get("rule"),
        "assigned_operator_id": conversation.get("assigned_operator_id"),
        "resolution": resolution,
        "csat": ({"rating": csat["rating"], "comment": csat.get("comment")}
                 if csat else None),
        "created_at": serialize({"t": conversation.get("created_at")})["t"],
        "updated_at": serialize({"t": conversation.get("updated_at")})["t"],
        "resolved_at": serialize({"t": conversation.get("resolved_at")})["t"],
    }


@router.get("/status")
async def status_by_ref(external_ref: str,
                        x_hub_secret_key: str | None = Header(default=None),
                        database=Depends(db_module.get_db)):
    tenant = await auth.tenant_from_secret_key(database, x_hub_secret_key)
    conversation = await database[db_module.CONVERSATIONS].find_one(
        {"tenant_id": tenant["_id"], "external_ref": external_ref}
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="No conversation for that ref")
    return await _status_payload(database, conversation)


@router.get("/conversations/{conversation_id}")
async def status_by_id(conversation_id: str,
                       x_hub_secret_key: str | None = Header(default=None),
                       database=Depends(db_module.get_db)):
    tenant = await auth.tenant_from_secret_key(database, x_hub_secret_key)
    conversation = await database[db_module.CONVERSATIONS].find_one(
        {"_id": conversation_id}
    )
    if not conversation or conversation["tenant_id"] != tenant["_id"]:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await _status_payload(database, conversation)
