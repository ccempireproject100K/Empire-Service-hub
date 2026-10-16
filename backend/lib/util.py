"""Small shared helpers: ids, timestamps, Mongo-doc serialization."""
import uuid
from datetime import datetime, timezone


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(dt)


def serialize(doc: dict | None, drop: tuple[str, ...] = ()) -> dict | None:
    """Copy a Mongo doc into a JSON-safe dict (datetimes -> ISO strings)."""
    if doc is None:
        return None
    out = {}
    for key, value in doc.items():
        if key in drop:
            continue
        if isinstance(value, datetime):
            out[key] = iso(value)
        elif isinstance(value, dict):
            out[key] = serialize(value)
        elif isinstance(value, list):
            out[key] = [serialize(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out
