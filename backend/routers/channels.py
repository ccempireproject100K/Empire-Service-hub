"""Inbound channels beyond chat (v2, section C).

Email, SMS and voice transcripts all land in the same conversation core
as the widget (lib.pipeline), so escalation, archiving, routing and the
learning loop work identically no matter how the customer reached us.

Auth model: machine-to-machine, like the tenant secret key. Email and
voice payloads carry the tenant secret key (``X-Hub-Secret-Key``) — or,
when an operator has configured ``HUB_CHANNEL_SECRET``, that shared
secret in ``X-Hub-Channel-Secret``. SMS webhooks come from Twilio: the
tenant is resolved by the destination number (``To`` must match a
tenant's ``sms_number``) and the request signature is verified whenever
``TWILIO_AUTH_TOKEN`` is configured.

Outbound replies on email/SMS conversations go through lib.mailer
(key-gated; queued in the outbox when unconfigured).
"""
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import db as db_module
from lib import auth, config, conversations as conv_lib, customers, mailer
from lib import pipeline, ratelimit
from lib.util import new_id, now

router = APIRouter(prefix="/api/channels", tags=["channels"])


class EmailIn(BaseModel):
    from_address: str = Field(alias="from")
    subject: str | None = None
    body: str = Field(min_length=1, max_length=20000)
    external_ref: str | None = None

    model_config = {"populate_by_name": True}


class VoiceIn(BaseModel):
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None
    customer_ref: str | None = None
    external_ref: str | None = None
    summary: str | None = None
    transcript: str = Field(min_length=1, max_length=40000)


async def _tenant_for_channel(database, secret_key: str | None,
                              channel_secret: str | None,
                              tenant_hint: str | None = None) -> dict:
    if secret_key:
        return await auth.tenant_from_secret_key(database, secret_key)
    expected = config.channel_secret()
    if expected and channel_secret and channel_secret == expected and tenant_hint:
        tenant = await database[db_module.TENANTS].find_one({"_id": tenant_hint})
        if tenant:
            return tenant
    raise HTTPException(
        status_code=401,
        detail="Tenant secret key or channel secret required",
    )


async def _find_open_conversation(database, tenant_id: str, channel: str,
                                  key: str | None) -> dict | None:
    if not key:
        return None
    conversations = await database[db_module.CONVERSATIONS].find(
        {"tenant_id": tenant_id, "channel": channel, "customer_key": key}
    ).sort("created_at", -1).to_list(length=20)
    for conversation in conversations:
        if conversation["status"] != "resolved":
            return conversation
    return None


async def _new_conversation(database, tenant: dict, channel: str, **fields) -> dict:
    conversation = {
        "_id": new_id("conv_"),
        "tenant_id": tenant["_id"],
        "channel": channel,
        "status": "open",
        "customer_name": fields.get("customer_name"),
        "customer_ref": fields.get("customer_ref"),
        "customer_email": fields.get("customer_email"),
        "customer_phone": fields.get("customer_phone"),
        "customer_key": fields.get("customer_key"),
        "external_ref": fields.get("external_ref"),
        "subject": fields.get("subject"),
        "language": (tenant.get("languages") or ["en"])[0],
        "created_at": now(),
        "updated_at": now(),
        "first_response_at": None,
        "resolved_at": None,
        "assigned_operator_id": None,
        "escalation": None,
        "last_sentiment": None,
    }
    await database[db_module.CONVERSATIONS].insert_one(conversation)
    return conversation


async def _deliver_ai_reply(database, tenant: dict, conversation: dict,
                            result: dict) -> None:
    """Send the AI's reply back out on email/SMS channels."""
    ai_messages = [m for m in result["messages"] if m["role"] == "ai"]
    if not ai_messages:
        return
    text = ai_messages[-1]["text"]
    if conversation["channel"] == "email" and conversation.get("customer_email"):
        subject = conversation.get("subject") or "Your support conversation"
        await mailer.send_email(
            database, tenant["_id"], conversation["customer_email"],
            f"Re: {subject}", text, conversation_id=conversation["_id"])
    elif conversation["channel"] == "sms" and conversation.get("customer_phone"):
        await mailer.send_sms(
            database, tenant["_id"], conversation["customer_phone"], text,
            conversation_id=conversation["_id"])


# --------------------------------------------------------------------- email
@router.post("/email/inbound", status_code=201)
async def email_inbound(body: EmailIn, request: Request,
                        tenant_id: str | None = None,
                        x_hub_secret_key: str | None = Header(default=None),
                        x_hub_channel_secret: str | None = Header(default=None),
                        database=Depends(db_module.get_db)):
    ratelimit.check(request, "channel_email")
    tenant = await _tenant_for_channel(
        database, x_hub_secret_key, x_hub_channel_secret, tenant_id)
    email = body.from_address.strip().lower()
    key = customers.customer_key(email=email)
    conversation = await _find_open_conversation(
        database, tenant["_id"], "email", key)
    created = False
    if not conversation:
        conversation = await _new_conversation(
            database, tenant, "email",
            customer_email=email, customer_key=key,
            subject=body.subject, external_ref=body.external_ref,
        )
        created = True
    text = body.body
    if body.subject and created:
        text = f"Subject: {body.subject}\n\n{body.body}"
    result = await pipeline.handle_customer_message(database, tenant, conversation, text)
    await _deliver_ai_reply(database, tenant, conversation, result)
    fresh = await database[db_module.CONVERSATIONS].find_one(
        {"_id": conversation["_id"]})
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "created": created,
        "escalated": result["escalation"] is not None,
    }


# ----------------------------------------------------------------------- sms
@router.post("/sms/inbound", status_code=201)
async def sms_inbound(request: Request,
                      database=Depends(db_module.get_db)):
    ratelimit.check(request, "channel_sms")
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    from_number = params.get("From", "")
    to_number = params.get("To", "")
    body_text = params.get("Body", "")
    if not from_number or not body_text:
        raise HTTPException(status_code=422, detail="From and Body are required")
    signature = request.headers.get("x-twilio-signature")
    if not mailer.validate_twilio_signature(str(request.url), params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    tenant = await database[db_module.TENANTS].find_one({"sms_number": to_number})
    if not tenant:
        raise HTTPException(
            status_code=404, detail="No tenant owns the destination number")
    key = customers.customer_key(phone=from_number)
    conversation = await _find_open_conversation(database, tenant["_id"], "sms", key)
    created = False
    if not conversation:
        conversation = await _new_conversation(
            database, tenant, "sms",
            customer_phone=from_number, customer_key=key,
        )
        created = True
    result = await pipeline.handle_customer_message(
        database, tenant, conversation, body_text)
    await _deliver_ai_reply(database, tenant, conversation, result)
    fresh = await database[db_module.CONVERSATIONS].find_one(
        {"_id": conversation["_id"]})
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "created": created,
        "escalated": result["escalation"] is not None,
    }


# --------------------------------------------------------------------- voice
_ROLE_PREFIX_RX = re.compile(
    r"^(customer|caller|user|agent|assistant|ai|receptionist)\s*:\s*(.*)$",
    re.IGNORECASE,
)


def _transcript_messages(transcript: str) -> list[tuple[str, str]]:
    """Split a call transcript into (role, text) turns when prefixed.

    Lines like ``Customer: …`` / ``Agent: …`` map to customer/ai turns;
    anything else becomes one customer message with the full transcript.
    """
    turns: list[tuple[str, str]] = []
    for line in transcript.splitlines():
        match = _ROLE_PREFIX_RX.match(line.strip())
        if match and match.group(2):
            speaker = match.group(1).lower()
            role = "customer" if speaker in ("customer", "caller", "user") else "ai"
            turns.append((role, match.group(2)))
    return turns


@router.post("/voice/transcript", status_code=201)
async def voice_transcript(body: VoiceIn, request: Request,
                           tenant_id: str | None = None,
                           x_hub_secret_key: str | None = Header(default=None),
                           x_hub_channel_secret: str | None = Header(default=None),
                           database=Depends(db_module.get_db)):
    ratelimit.check(request, "channel_voice")
    tenant = await _tenant_for_channel(
        database, x_hub_secret_key, x_hub_channel_secret, tenant_id)
    key = customers.customer_key(
        email=body.customer_email, ref=body.customer_ref, phone=body.customer_phone)
    conversation = await _new_conversation(
        database, tenant, "voice",
        customer_name=body.customer_name, customer_email=body.customer_email,
        customer_phone=body.customer_phone, customer_ref=body.customer_ref,
        customer_key=key, external_ref=body.external_ref,
        subject=body.summary,
    )
    turns = _transcript_messages(body.transcript)
    if turns:
        for role, text in turns:
            await conv_lib.add_message(database, conversation, role, text)
    else:
        await conv_lib.add_message(database, conversation, "customer", body.transcript)
    if body.summary:
        await conv_lib.add_message(
            database, conversation, "system",
            f"Call summary from the AI receptionist: {body.summary}")
    # A voice handoff always needs a human: escalate with full context so it
    # lands in the queue + archive (the learning corpus) like any escalation.
    fresh = await database[db_module.CONVERSATIONS].find_one(
        {"_id": conversation["_id"]})
    reason = {"rule": "voice_handoff",
              "detail": "Escalated from a voice call with transcript."}
    await database[db_module.CONVERSATIONS].update_one(
        {"_id": conversation["_id"]},
        {"$set": {"status": "escalated",
                  "escalation": {**reason, "at": now()},
                  "updated_at": now()}},
    )
    fresh["status"] = "escalated"
    fresh["escalation"] = reason
    from lib import archive as archive_lib, routing, webhooks

    await archive_lib.write_escalation(database, tenant, fresh, reason)
    await routing.auto_assign(database, tenant, fresh)
    await webhooks.emit(database, tenant, "escalation.created", {
        "conversation_id": conversation["_id"],
        "rule": "voice_handoff",
        "channel": "voice",
        "external_ref": body.external_ref,
    })
    fresh = await database[db_module.CONVERSATIONS].find_one(
        {"_id": conversation["_id"]})
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "created": True,
        "escalated": True,
        "escalation_reason": "voice_handoff",
    }
