"""Operator console API: the human side of the hub.

Operators list conversations across their tenants, take over escalations,
reply, and resolve. Resolving an escalated conversation completes its
archive record with the human's resolution — closing the learning loop.
Basic SLA + CSAT aggregates are exposed per tenant for the console header.
"""
import base64

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db as db_module
from lib import analytics, archive, auth, conversations as conv_lib, customers
from lib import learning, mailer, quality, sla as sla_lib, storage, webhooks
from lib.util import iso, now, serialize

router = APIRouter(prefix="/api/operator", tags=["operator"])


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    attachment_ids: list[str] = []


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=1, max_length=4000)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AssignIn(BaseModel):
    operator_id: str


class AvailabilityIn(BaseModel):
    available: bool


class AttachmentIn(BaseModel):
    filename: str
    content_type: str | None = None
    data_base64: str


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
    tenant = await database[db_module.TENANTS].find_one(
        {"_id": conversation["tenant_id"]})
    # v2: "solved before like this" — similar archived cases for the latest
    # customer message, plus this customer's history with the tenant.
    last_customer = next(
        (m["text"] for m in reversed(messages) if m["role"] == "customer"), "")
    similar = await learning.similar_cases(
        database, conversation["tenant_id"], last_customer,
        exclude_conversation_id=conversation_id)
    history = await customers.history(
        database, conversation["tenant_id"], conversation.get("customer_key"),
        exclude_conversation_id=conversation_id)
    notes = await database[db_module.NOTES].find(
        {"conversation_id": conversation_id}
    ).sort("created_at", 1).to_list(length=500)
    out = conv_lib.public_conversation(conversation)
    if tenant:
        out["sla"] = serialize(sla_lib.evaluate_conversation(tenant, conversation))
    return {
        "conversation": out,
        "messages": [conv_lib.public_message(m) for m in messages],
        "similar_cases": similar,
        "customer_history": history,
        "notes": [serialize(n) for n in notes],
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
    from lib import pipeline

    attachments = await pipeline.attachment_metadata(
        database, {"_id": conversation["tenant_id"]}, conversation,
        body.attachment_ids)
    msg = await conv_lib.add_message(
        database, conversation, "operator", body.text, attachments=attachments)
    await _deliver_outbound(database, conversation, body.text)
    fresh = await database[db_module.CONVERSATIONS].find_one({"_id": conversation_id})
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "message": conv_lib.public_message(msg),
    }


async def _deliver_outbound(database, conversation: dict, text: str) -> None:
    """Push an operator reply back out on email/SMS conversations."""
    tenant = await database[db_module.TENANTS].find_one(
        {"_id": conversation["tenant_id"]})
    if not tenant:
        return
    if conversation.get("channel") == "email" and conversation.get("customer_email"):
        subject = conversation.get("subject") or "Your support conversation"
        await mailer.send_email(
            database, tenant["_id"], conversation["customer_email"],
            f"Re: {subject}", text, conversation_id=conversation["_id"])
    elif conversation.get("channel") == "sms" and conversation.get("customer_phone"):
        await mailer.send_sms(
            database, tenant["_id"], conversation["customer_phone"], text,
            conversation_id=conversation["_id"])


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
    draft = None
    if was_escalated:
        record = await archive.complete_resolution(
            database, conversation_id, body.resolution,
            operator.get("name") or operator["email"],
        )
        archive_record = serialize(record) if record else None
        # v2: close the learning loop — the resolution becomes a KB draft
        # in the tenant's review queue.
        tenant = await database[db_module.TENANTS].find_one(
            {"_id": conversation["tenant_id"]})
        if record and tenant:
            created = await learning.draft_from_resolution(
                database, tenant, conversation, record, body.resolution)
            draft = serialize(created)
    fresh = await database[db_module.CONVERSATIONS].find_one({"_id": conversation_id})
    # v2: quality self-review + resolution webhook for subscribed products.
    tenant = await database[db_module.TENANTS].find_one(
        {"_id": conversation["tenant_id"]})
    review = await quality.review_on_resolve(database, tenant or {"_id": conversation["tenant_id"]}, fresh)
    if tenant:
        await webhooks.emit(database, tenant, "conversation.resolved", {
            "conversation_id": conversation_id,
            "resolution": body.resolution,
            "external_ref": conversation.get("external_ref"),
            "channel": conversation.get("channel"),
        })
    return {
        "conversation": conv_lib.public_conversation(fresh),
        "csat_prompt": True,
        "archive": archive_record,
        "kb_draft": draft,
        "quality_review": serialize(review),
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
        # v2: learning-loop metrics + estimated AI cost.
        "learning": await learning.learning_metrics(database, tenant_id),
        "ai_cost": await analytics.cost_summary(database, tenant_id),
    }


# ------------------------------------------------------------ v2: notes
@router.post("/conversations/{conversation_id}/notes", status_code=201)
async def add_note(conversation_id: str, body: NoteIn,
                   operator: dict = Depends(auth.get_current_operator),
                   database=Depends(db_module.get_db)):
    """Internal note — stored apart from messages, never customer-visible."""
    conversation = await _scoped_conversation(database, operator, conversation_id)
    note = {
        "_id": _new_note_id(),
        "tenant_id": conversation["tenant_id"],
        "conversation_id": conversation_id,
        "operator_id": operator["_id"],
        "operator_name": operator.get("name") or operator["email"],
        "text": body.text,
        "created_at": now(),
    }
    await database[db_module.NOTES].insert_one(note)
    return {"note": serialize(note)}


def _new_note_id() -> str:
    from lib.util import new_id

    return new_id("note_")


@router.get("/conversations/{conversation_id}/notes")
async def list_notes(conversation_id: str,
                     operator: dict = Depends(auth.get_current_operator),
                     database=Depends(db_module.get_db)):
    await _scoped_conversation(database, operator, conversation_id)
    notes = await database[db_module.NOTES].find(
        {"conversation_id": conversation_id}
    ).sort("created_at", 1).to_list(length=500)
    return {"notes": [serialize(n) for n in notes]}


# ------------------------------------------------------- v2: assignment
@router.post("/conversations/{conversation_id}/assign")
async def assign(conversation_id: str, body: AssignIn,
                 operator: dict = Depends(auth.get_current_operator),
                 database=Depends(db_module.get_db)):
    conversation = await _scoped_conversation(database, operator, conversation_id)
    if conversation["status"] == "resolved":
        raise HTTPException(status_code=409, detail="Conversation is resolved")
    target = await database[db_module.OPERATORS].find_one({"_id": body.operator_id})
    if not target:
        raise HTTPException(status_code=404, detail="Operator not found")
    from lib import routing

    if not routing.can_serve(target, conversation["tenant_id"]):
        raise HTTPException(
            status_code=422,
            detail="Operator is unavailable or has no access to this tenant")
    await database[db_module.CONVERSATIONS].update_one(
        {"_id": conversation_id},
        {"$set": {"assigned_operator_id": target["_id"], "updated_at": now()}},
    )
    await database[db_module.OPERATORS].update_one(
        {"_id": target["_id"]}, {"$set": {"last_assigned_at": now()}}
    )
    await conv_lib.add_message(
        database, conversation, "system",
        f"Assigned to {target.get('name') or target['email']}.")
    fresh = await database[db_module.CONVERSATIONS].find_one({"_id": conversation_id})
    return {"conversation": conv_lib.public_conversation(fresh)}


@router.get("/operators")
async def list_operators(operator: dict = Depends(auth.get_current_operator),
                         database=Depends(db_module.get_db)):
    operators = await database[db_module.OPERATORS].find({}).to_list(length=500)
    return {"operators": [serialize(o, drop=("password_hash",)) for o in operators]}


@router.post("/me/availability")
async def set_availability(body: AvailabilityIn,
                           operator: dict = Depends(auth.get_current_operator),
                           database=Depends(db_module.get_db)):
    await database[db_module.OPERATORS].update_one(
        {"_id": operator["_id"]}, {"$set": {"available": body.available}}
    )
    operator["available"] = body.available
    return {"operator": serialize(operator, drop=("password_hash",))}


# ----------------------------------------------------- v2: attachments
@router.post("/conversations/{conversation_id}/attachments", status_code=201)
async def upload_attachment(conversation_id: str, body: AttachmentIn,
                            operator: dict = Depends(auth.get_current_operator),
                            database=Depends(db_module.get_db)):
    conversation = await _scoped_conversation(database, operator, conversation_id)
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="data_base64 is not valid base64")
    error = storage.validate(body.filename, data)
    if error:
        raise HTTPException(status_code=422, detail=error)
    doc = await storage.save_attachment(
        database, conversation["tenant_id"], conversation_id, body.filename,
        body.content_type, data, uploaded_by=operator["_id"])
    return {"attachment": storage.public_attachment(doc)}


# ------------------------------------------------------ v2: SLA alerts
@router.get("/sla-alerts")
async def list_sla_alerts(tenant_id: str, acknowledged: bool | None = None,
                          operator: dict = Depends(auth.get_current_operator),
                          database=Depends(db_module.get_db)):
    auth.assert_tenant_access(operator, tenant_id)
    query: dict = {"tenant_id": tenant_id}
    if acknowledged is not None:
        query["acknowledged"] = acknowledged
    alerts = await database[db_module.SLA_ALERTS].find(query).sort(
        "created_at", -1).to_list(length=500)
    return {"alerts": [serialize(a) for a in alerts]}


@router.post("/sla-alerts/{alert_id}/ack")
async def ack_sla_alert(alert_id: str,
                        operator: dict = Depends(auth.get_current_operator),
                        database=Depends(db_module.get_db)):
    alert = await database[db_module.SLA_ALERTS].find_one({"_id": alert_id})
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    auth.assert_tenant_access(operator, alert["tenant_id"])
    await database[db_module.SLA_ALERTS].update_one(
        {"_id": alert_id},
        {"$set": {"acknowledged": True, "acknowledged_by": operator["_id"]}},
    )
    alert["acknowledged"] = True
    return {"alert": serialize(alert)}


@router.post("/sla-sweep")
async def run_sla_sweep(operator: dict = Depends(auth.get_current_operator),
                        database=Depends(db_module.get_db)):
    """Run the SLA sweep on demand (the scheduler runs it periodically)."""
    auth.require_admin(operator)
    return await sla_lib.run_sweep(database)


# ------------------------------------------------- v2: quality reviews
@router.get("/quality-reviews")
async def list_quality_reviews(tenant_id: str, status: str | None = "queued",
                               operator: dict = Depends(auth.get_current_operator),
                               database=Depends(db_module.get_db)):
    auth.assert_tenant_access(operator, tenant_id)
    query: dict = {"tenant_id": tenant_id}
    if status:
        query["status"] = status
    reviews = await database[db_module.QUALITY_REVIEWS].find(query).sort(
        "created_at", -1).to_list(length=500)
    return {"reviews": [serialize(r) for r in reviews]}


@router.post("/quality-reviews/{review_id}/review")
async def mark_quality_reviewed(review_id: str,
                                operator: dict = Depends(auth.get_current_operator),
                                database=Depends(db_module.get_db)):
    review = await database[db_module.QUALITY_REVIEWS].find_one({"_id": review_id})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    auth.assert_tenant_access(operator, review["tenant_id"])
    await database[db_module.QUALITY_REVIEWS].update_one(
        {"_id": review_id},
        {"$set": {"status": "reviewed", "reviewed_by": operator["_id"]}},
    )
    review["status"] = "reviewed"
    return {"review": serialize(review)}


# --------------------------------------------------------- v2: analytics
@router.get("/analytics")
async def tenant_analytics(tenant_id: str, days: int = 30,
                           operator: dict = Depends(auth.get_current_operator),
                           database=Depends(db_module.get_db)):
    auth.assert_tenant_access(operator, tenant_id)
    return {
        "tenant_id": tenant_id,
        "topics": await analytics.topic_clusters(database, tenant_id),
        "learning": await learning.learning_metrics(database, tenant_id, days=days),
        "ai_cost": await analytics.cost_summary(database, tenant_id),
    }
