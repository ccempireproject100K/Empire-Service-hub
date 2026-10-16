"""Public widget API — what the embeddable chat widget talks to.

Auth is the tenant's *public* key (safe to embed in a web page). Every
conversation lookup is checked against that tenant, so one product's key can
never read another product's conversations. All endpoints are rate-limited.
"""
import base64

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import db as db_module
from lib import auth, config, conversations as conv_lib, customers, pipeline
from lib import ratelimit, storage
from lib.util import new_id, now, serialize

router = APIRouter(prefix="/api/widget", tags=["widget"])


class ConversationIn(BaseModel):
    customer_name: str | None = None
    customer_ref: str | None = None  # the product's own customer id, if any
    customer_email: str | None = None  # identity for customer memory (v2)
    external_ref: str | None = None  # the product's own conversation id (v2)
    language: str | None = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    attachment_ids: list[str] = []


class AttachmentIn(BaseModel):
    filename: str
    content_type: str | None = None
    data_base64: str  # file bytes, base64 — keeps uploads dependency-free


class CsatIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


async def _tenant(request: Request, database, public_key: str | None) -> dict:
    return await auth.tenant_from_public_key(database, public_key)


async def _conversation(database, tenant: dict, conversation_id: str) -> dict:
    conversation = await database[db_module.CONVERSATIONS].find_one(
        {"_id": conversation_id}
    )
    if not conversation or conversation["tenant_id"] != tenant["_id"]:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("/config")
async def widget_config(request: Request,
                        key: str | None = None,
                        x_hub_public_key: str | None = Header(default=None),
                        database=Depends(db_module.get_db)):
    ratelimit.check(request, "widget_config")
    tenant = await _tenant(request, database, x_hub_public_key or key)
    branding = tenant.get("branding") or {}
    return {
        "tenant": {
            "id": tenant["_id"],
            "name": tenant["name"],
            "branding": {
                "name": branding.get("name") or tenant["name"],
                "primary_color": branding.get("primary_color", "#2563eb"),
                "accent_color": branding.get("accent_color", "#f59e0b"),
                "greeting": branding.get("greeting", "Hi there! How can we help?"),
                "logo_url": branding.get("logo_url"),
            },
            "languages": tenant.get("languages", ["en"]),
        },
        # v2: proactive help triggers + tenant status banner.
        "proactive": {
            "triggers": tenant.get("proactive_triggers") or [],
            "status_banner": tenant.get("status_banner"),
        },
        "demo": config.demo_mode(),
    }


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationIn, request: Request,
                              x_hub_public_key: str | None = Header(default=None),
                              database=Depends(db_module.get_db)):
    ratelimit.check(request, "widget_create")
    tenant = await _tenant(request, database, x_hub_public_key)
    conversation = {
        "_id": new_id("conv_"),
        "tenant_id": tenant["_id"],
        "channel": "chat",
        "status": "open",
        "customer_name": body.customer_name,
        "customer_ref": body.customer_ref,
        "customer_email": body.customer_email,
        "customer_key": customers.customer_key(
            email=body.customer_email, ref=body.customer_ref),
        "external_ref": body.external_ref,
        "language": body.language or (tenant.get("languages") or ["en"])[0],
        "created_at": now(),
        "updated_at": now(),
        "first_response_at": None,
        "resolved_at": None,
        "assigned_operator_id": None,
        "escalation": None,
        "last_sentiment": None,
    }
    await database[db_module.CONVERSATIONS].insert_one(conversation)
    greeting = (tenant.get("branding") or {}).get("greeting") or "Hi there! How can we help?"
    greeting_msg = await conv_lib.add_message(
        database, conversation, "ai", greeting, demo=config.demo_mode()
    )
    messages = [serialize(greeting_msg)]
    return {
        "conversation": conv_lib.public_conversation(conversation),
        "messages": messages,
        "demo": config.demo_mode(),
    }


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: str, request: Request,
                       x_hub_public_key: str | None = Header(default=None),
                       database=Depends(db_module.get_db)):
    ratelimit.check(request, "widget_read")
    tenant = await _tenant(request, database, x_hub_public_key)
    conversation = await _conversation(database, tenant, conversation_id)
    messages = await conv_lib.list_messages(database, conversation_id)
    return {
        "conversation": conv_lib.public_conversation(conversation),
        "messages": [conv_lib.public_message(m) for m in messages],
    }


@router.post("/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, body: MessageIn, request: Request,
                       x_hub_public_key: str | None = Header(default=None),
                       database=Depends(db_module.get_db)):
    ratelimit.check(request, "widget_message")
    tenant = await _tenant(request, database, x_hub_public_key)
    conversation = await _conversation(database, tenant, conversation_id)
    if conversation["status"] == "resolved":
        raise HTTPException(status_code=409, detail="Conversation is resolved")

    result = await pipeline.handle_customer_message(
        database, tenant, conversation, body.text,
        attachment_ids=body.attachment_ids,
    )
    reason = result["escalation"]
    fresh = await database[db_module.CONVERSATIONS].find_one({"_id": conversation_id})
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "messages": [conv_lib.public_message(m) for m in result["messages"]],
        "escalated": reason is not None,
        "escalation_reason": reason["rule"] if reason else None,
        "demo": config.demo_mode(),
    }


@router.post("/conversations/{conversation_id}/attachments", status_code=201)
async def upload_attachment(conversation_id: str, body: AttachmentIn,
                            request: Request,
                            x_hub_public_key: str | None = Header(default=None),
                            database=Depends(db_module.get_db)):
    ratelimit.check(request, "widget_message")
    tenant = await _tenant(request, database, x_hub_public_key)
    conversation = await _conversation(database, tenant, conversation_id)
    if conversation["status"] == "resolved":
        raise HTTPException(status_code=409, detail="Conversation is resolved")
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="data_base64 is not valid base64")
    error = storage.validate(body.filename, data)
    if error:
        raise HTTPException(status_code=422, detail=error)
    doc = await storage.save_attachment(
        database, tenant["_id"], conversation_id, body.filename,
        body.content_type, data, uploaded_by="customer",
    )
    return {"attachment": storage.public_attachment(doc)}


@router.post("/conversations/{conversation_id}/csat")
async def submit_csat(conversation_id: str, body: CsatIn, request: Request,
                      x_hub_public_key: str | None = Header(default=None),
                      database=Depends(db_module.get_db)):
    ratelimit.check(request, "widget_csat")
    tenant = await _tenant(request, database, x_hub_public_key)
    conversation = await _conversation(database, tenant, conversation_id)
    if conversation["status"] != "resolved":
        raise HTTPException(
            status_code=409,
            detail="Satisfaction survey opens once the conversation is resolved",
        )
    existing = await database[db_module.CSAT].find_one(
        {"conversation_id": conversation_id}
    )
    if existing:
        raise HTTPException(status_code=409, detail="Survey already submitted")
    doc = {
        "_id": new_id("csat_"),
        "tenant_id": tenant["_id"],
        "conversation_id": conversation_id,
        "rating": body.rating,
        "comment": body.comment,
        "created_at": now(),
    }
    await database[db_module.CSAT].insert_one(doc)
    from lib import webhooks

    await webhooks.emit(database, tenant, "csat.received", {
        "conversation_id": conversation_id,
        "rating": body.rating,
        "external_ref": conversation.get("external_ref"),
    })
    return {"csat": serialize(doc), "thanks": True}
