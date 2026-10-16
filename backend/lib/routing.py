"""Operator assignment & routing (v2, section D).

When a conversation escalates and the tenant has auto-assign on (the
default), the hub picks the next operator round-robin: among operators
who can see the tenant and are marked *available*, the one carrying the
fewest open conversations wins; ties break by who was assigned longest
ago. Operators toggle availability from the console (breaks, end of
shift); admins can reassign manually at any time.
"""
from datetime import datetime, timezone

import db as db_module
from lib.util import now

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_OPEN_STATUSES = ("escalated", "operator_handling")


def can_serve(operator: dict, tenant_id: str) -> bool:
    if operator.get("available") is False:
        return False
    if operator.get("role") == "admin":
        return True
    return tenant_id in (operator.get("tenant_ids") or [])


async def pick_operator(database, tenant_id: str) -> dict | None:
    """Choose the next operator for a tenant, or None when nobody is free."""
    operators = await database[db_module.OPERATORS].find({}).to_list(length=500)
    candidates = [op for op in operators if can_serve(op, tenant_id)]
    if not candidates:
        return None
    open_convs = await database[db_module.CONVERSATIONS].find(
        {"tenant_id": tenant_id}
    ).to_list(length=5000)
    load: dict[str, int] = {}
    for conversation in open_convs:
        assignee = conversation.get("assigned_operator_id")
        if assignee and conversation.get("status") in _OPEN_STATUSES:
            load[assignee] = load.get(assignee, 0) + 1

    def rank(operator):
        last = operator.get("last_assigned_at") or _EPOCH
        return (load.get(operator["_id"], 0), last)

    candidates.sort(key=rank)
    return candidates[0]


async def auto_assign(database, tenant: dict, conversation: dict) -> dict | None:
    """Assign an escalated conversation per routing rules (tenant-gated)."""
    if tenant.get("auto_assign") is False:
        return None
    if conversation.get("assigned_operator_id"):
        return None
    operator = await pick_operator(database, tenant["_id"])
    if not operator:
        return None
    await database[db_module.CONVERSATIONS].update_one(
        {"_id": conversation["_id"]},
        {"$set": {"assigned_operator_id": operator["_id"], "updated_at": now()}},
    )
    await database[db_module.OPERATORS].update_one(
        {"_id": operator["_id"]}, {"$set": {"last_assigned_at": now()}}
    )
    conversation["assigned_operator_id"] = operator["_id"]
    return operator
