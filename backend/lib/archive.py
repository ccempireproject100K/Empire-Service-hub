"""The escalation archive — the learning loop's corpus and the hub's core asset.

Every escalated conversation is snapshotted here (PII-masked at write time),
and the human's resolution is attached when the conversation resolves. The
result is a clean, exportable (JSONL) record of *what the AI couldn't
handle and how a human fixed it* — exactly the corpus that shrinks future
escalations.
"""
import db as db_module
from lib import pii
from lib.util import new_id, now, serialize


async def write_escalation(database, tenant: dict, conversation: dict, reason: dict) -> dict:
    messages = await database[db_module.MESSAGES].find(
        {"conversation_id": conversation["_id"]}
    ).sort("created_at", 1).to_list(length=500)
    transcript = [
        {
            "role": m["role"],
            "text": m["text"],
            "created_at": m.get("created_at"),
            **({"confidence": m["confidence"]} if m.get("confidence") is not None else {}),
        }
        for m in messages
    ]
    record = {
        "_id": new_id("arc_"),
        "tenant_id": tenant["_id"],
        "conversation_id": conversation["_id"],
        "escalated_at": now(),
        "reason": reason,
        "status": "open",
        "transcript": pii.mask_transcript(transcript),
        "resolution": None,
    }
    await database[db_module.ARCHIVE].insert_one(record)
    return record


async def complete_resolution(database, conversation_id: str, resolution_text: str,
                              resolved_by: str) -> dict | None:
    """Attach the human resolution to the conversation's archive record."""
    record = await database[db_module.ARCHIVE].find_one(
        {"conversation_id": conversation_id}
    )
    if not record:
        return None
    resolution = {
        "text": pii.mask_text(resolution_text),
        "resolved_by": resolved_by,
        "resolved_at": now(),
    }
    await database[db_module.ARCHIVE].update_one(
        {"_id": record["_id"]},
        {"$set": {"resolution": resolution, "status": "resolved"}},
    )
    record["resolution"] = resolution
    record["status"] = "resolved"
    return record


def export_line(record: dict) -> dict:
    """One JSONL-exportable dict (datetimes as ISO strings, PII already masked)."""
    return serialize(record)
