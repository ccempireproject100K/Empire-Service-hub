"""Customer memory (v2, section G).

The hub recognizes a returning customer within a tenant by an identity
key — email first, then the product's own customer reference, then phone
— and surfaces their earlier conversations and resolutions. The AI gets
that history as context ("this customer asked about billing in March; it
was resolved by…") and the operator sees it in the timeline, so nobody has
to re-explain their problem from scratch. Identity never crosses tenants:
the key is only ever looked up inside one tenant's conversations.
"""
import db as db_module
from lib.util import serialize

_MAX_TEXT = 200


def customer_key(email: str | None = None, ref: str | None = None,
                 phone: str | None = None) -> str | None:
    if email:
        return "email:" + email.strip().lower()
    if ref:
        return "ref:" + str(ref).strip()
    if phone:
        digits = "".join(ch for ch in str(phone) if ch.isdigit())
        if digits:
            return "phone:" + digits[-10:]
    return None


async def history(database, tenant_id: str, key: str | None,
                  exclude_conversation_id: str | None = None,
                  limit: int = 5) -> list[dict]:
    """Past conversations for this customer in this tenant, newest first."""
    if not key:
        return []
    conversations = await database[db_module.CONVERSATIONS].find(
        {"tenant_id": tenant_id, "customer_key": key}
    ).sort("created_at", -1).to_list(length=50)
    out = []
    for conversation in conversations:
        if conversation["_id"] == exclude_conversation_id:
            continue
        messages = await database[db_module.MESSAGES].find(
            {"conversation_id": conversation["_id"]}
        ).sort("created_at", 1).to_list(length=200)
        first_customer = next(
            (m["text"] for m in messages if m["role"] == "customer"), ""
        )
        record = await database[db_module.ARCHIVE].find_one(
            {"conversation_id": conversation["_id"]}
        )
        resolution = None
        if record and record.get("resolution"):
            resolution = record["resolution"].get("text")
        out.append({
            "conversation_id": conversation["_id"],
            "status": conversation["status"],
            "channel": conversation.get("channel"),
            "created_at": serialize({"t": conversation.get("created_at")})["t"],
            "question": first_customer[:_MAX_TEXT],
            "resolution": (resolution or "")[:_MAX_TEXT] or None,
        })
        if len(out) >= limit:
            break
    return out


def context_lines(history_entries: list[dict]) -> list[str]:
    """Compact prior-history lines for the AI prompt."""
    lines = []
    for entry in history_entries:
        line = f"- Earlier ({entry['status']}): asked \"{entry['question']}\""
        if entry.get("resolution"):
            line += f" — resolved: {entry['resolution']}"
        lines.append(line)
    return lines
