"""SLA tracking + alerting sweep (v2, section D).

Each tenant carries two SLA clocks: first response
(``sla_first_response_minutes``) and resolution
(``sla_resolution_minutes``), measured from conversation creation. The
sweep stamps every open conversation's SLA state — ``ok``,
``approaching`` (past 80% of the budget), ``breached``, ``met`` — creates
a console alert on each transition into approaching/breached, and emits
the ``sla.breached`` webhook on breaches. It also drives webhook retries,
so one scheduler covers both. No external paging dependency: alerts live
in the console until acknowledged.
"""
from datetime import timedelta

import db as db_module
from lib import webhooks
from lib.util import new_id, now

APPROACHING_FRACTION = 0.8


def _clock_state(created_at, deadline_minutes, met_at, at) -> dict:
    due_at = created_at + timedelta(minutes=deadline_minutes)
    if met_at is not None:
        return {"due_at": due_at, "state": "met", "met_at": met_at}
    elapsed = (at - created_at).total_seconds()
    budget = deadline_minutes * 60
    if at > due_at:
        state = "breached"
    elif budget and elapsed / budget >= APPROACHING_FRACTION:
        state = "approaching"
    else:
        state = "ok"
    return {"due_at": due_at, "state": state, "met_at": None}


def evaluate_conversation(tenant: dict, conversation: dict, at=None) -> dict:
    at = at or now()
    created_at = conversation.get("created_at") or at
    return {
        "first_response": _clock_state(
            created_at,
            tenant.get("sla_first_response_minutes", 60),
            conversation.get("first_response_at"), at),
        "resolution": _clock_state(
            created_at,
            tenant.get("sla_resolution_minutes", 480),
            conversation.get("resolved_at"), at),
    }


async def _alert(database, tenant: dict, conversation: dict, kind: str,
                 clock: str, at) -> dict:
    alert = {
        "_id": new_id("sla_"),
        "tenant_id": tenant["_id"],
        "conversation_id": conversation["_id"],
        "kind": kind,          # e.g. first_response_breached
        "clock": clock,        # first_response | resolution
        "created_at": at,
        "acknowledged": False,
        "acknowledged_by": None,
    }
    await database[db_module.SLA_ALERTS].insert_one(alert)
    return alert


async def run_sweep(database, at=None) -> dict:
    """Stamp SLA states, raise alerts, retry webhooks. Returns counters."""
    at = at or now()
    tenants = await database[db_module.TENANTS].find({}).to_list(length=500)
    tenants_by_id = {t["_id"]: t for t in tenants}
    conversations = await database[db_module.CONVERSATIONS].find({}).to_list(
        length=20000)
    checked = alerts_created = breaches = 0
    for conversation in conversations:
        tenant = tenants_by_id.get(conversation["tenant_id"])
        if not tenant or conversation.get("status") == "resolved":
            continue
        checked += 1
        previous = (conversation.get("sla") or {})
        current = evaluate_conversation(tenant, conversation, at)
        changed = False
        for clock_name, clock in current.items():
            prev_state = (previous.get(clock_name) or {}).get("state")
            if clock["state"] in ("approaching", "breached") \
                    and clock["state"] != prev_state:
                kind = f"{clock_name}_{clock['state']}"
                await _alert(database, tenant, conversation, kind, clock_name, at)
                alerts_created += 1
                changed = True
                if clock["state"] == "breached":
                    breaches += 1
                    await webhooks.emit(database, tenant, "sla.breached", {
                        "conversation_id": conversation["_id"],
                        "clock": clock_name,
                        "due_at": clock["due_at"].isoformat(),
                    })
            elif clock["state"] != prev_state:
                changed = True
        if changed or previous != current:
            await database[db_module.CONVERSATIONS].update_one(
                {"_id": conversation["_id"]}, {"$set": {"sla": current}}
            )
    retried = await webhooks.retry_pending(database)
    return {"checked": checked, "alerts_created": alerts_created,
            "breaches": breaches, "webhooks_retried": retried}
