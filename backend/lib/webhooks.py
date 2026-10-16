"""Tenant webhooks OUT (v2, section E).

Products subscribe by setting ``webhook_url`` (+ optional
``webhook_secret``) on their tenant. The hub emits:

  - ``escalation.created``   — a conversation handed to humans
  - ``conversation.resolved``— a conversation was resolved
  - ``sla.breached``         — an SLA deadline passed unmet
  - ``csat.received``        — a customer left a satisfaction rating

Deliveries are signed with HMAC-SHA256 over the raw body
(``X-Hub-Signature-256: sha256=<hex>``) when a secret is configured,
attempted immediately, and retried with simple exponential backoff (max 3
attempts) by the SLA scheduler sweep. Every attempt lands in the delivery
log so operators can audit what was sent where. No webhook configured =
silent no-op, like every other key-gated provider in the hub.
"""
import hashlib
import hmac
import json
from datetime import timedelta

import httpx

import db as db_module
from lib.util import new_id, now, serialize

MAX_ATTEMPTS = 3
EVENTS = ("escalation.created", "conversation.resolved", "sla.breached",
          "csat.received")


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _post(url: str, body: bytes, headers: dict) -> int:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code


async def _attempt(database, delivery: dict, tenant: dict) -> bool:
    body = json.dumps(delivery["payload"]).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Event": delivery["event"],
        "X-Hub-Delivery": delivery["_id"],
    }
    if tenant.get("webhook_secret"):
        headers["X-Hub-Signature-256"] = sign(tenant["webhook_secret"], body)
    status = None
    error = None
    try:
        status = await _post(tenant["webhook_url"], body, headers)
        ok = 200 <= status < 300
    except Exception as exc:  # network down, bad URL — retry later
        ok, error = False, str(exc)[:200]
    attempts = delivery.get("attempts", 0) + 1
    updates = {
        "attempts": attempts,
        "last_status": status,
        "last_error": error,
        "last_attempt_at": now(),
        "status": "delivered" if ok else ("failed" if attempts >= MAX_ATTEMPTS
                                           else "pending"),
    }
    if not ok and attempts < MAX_ATTEMPTS:
        updates["next_retry_at"] = now() + timedelta(seconds=30 * (2 ** attempts))
    await database[db_module.WEBHOOK_DELIVERIES].update_one(
        {"_id": delivery["_id"]}, {"$set": updates}
    )
    delivery.update(updates)
    return ok


async def emit(database, tenant: dict, event: str, data: dict) -> dict | None:
    """Record + attempt a webhook delivery. No-op without a configured URL."""
    if event not in EVENTS or not tenant.get("webhook_url"):
        return None
    delivery = {
        "_id": new_id("whd_"),
        "tenant_id": tenant["_id"],
        "event": event,
        "payload": {
            "event": event,
            "tenant_id": tenant["_id"],
            "occurred_at": serialize({"t": now()})["t"],
            "data": data,
        },
        "attempts": 0,
        "status": "pending",
        "next_retry_at": None,
        "created_at": now(),
    }
    await database[db_module.WEBHOOK_DELIVERIES].insert_one(delivery)
    await _attempt(database, delivery, tenant)
    return delivery


async def retry_pending(database) -> int:
    """Re-attempt due deliveries (called by the scheduler sweep)."""
    deliveries = await database[db_module.WEBHOOK_DELIVERIES].find(
        {"status": "pending"}
    ).to_list(length=500)
    retried = 0
    for delivery in deliveries:
        due = delivery.get("next_retry_at")
        if due is not None and due > now():
            continue
        tenant = await database[db_module.TENANTS].find_one(
            {"_id": delivery["tenant_id"]}
        )
        if not tenant or not tenant.get("webhook_url"):
            continue
        await _attempt(database, delivery, tenant)
        retried += 1
    return retried
