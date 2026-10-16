"""Attachment storage (v2, section C).

Files attached to messages live behind a tiny storage abstraction: bytes
on local disk under ``HUB_UPLOAD_DIR`` (default ``backend/uploads/``),
metadata in Mongo. Filenames are sanitized to a PII-safe alphabet (no
customer names with spaces/accents leaking into paths or URLs), sizes and
extensions are limited, and every attachment is tenant- + conversation-
scoped — downloads re-check that scope, so one tenant can never fetch
another's files.
"""
import re
from pathlib import Path

import db as db_module
from lib import config
from lib.util import new_id, now, serialize

ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".csv", ".json", ".log", ".doc", ".docx", ".xls", ".xlsx", ".zip",
}

_UNSAFE_RX = re.compile(r"[^A-Za-z0-9._-]+")


def upload_root() -> Path:
    root = config.upload_dir()
    base = Path(root) if root else Path(__file__).resolve().parent.parent / "uploads"
    base.mkdir(parents=True, exist_ok=True)
    return base


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "file").name
    name = _UNSAFE_RX.sub("_", name).strip("._") or "file"
    return name[:120]


def validate(filename: str, data: bytes) -> str | None:
    """Return an error string, or None when the upload is acceptable."""
    ext = Path(sanitize_filename(filename)).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"File type {ext or '(none)'} is not allowed"
    if len(data) > config.max_attachment_bytes():
        return "File exceeds the maximum attachment size"
    if not data:
        return "Empty file"
    return None


async def save_attachment(database, tenant_id: str, conversation_id: str,
                          filename: str, content_type: str | None,
                          data: bytes, uploaded_by: str) -> dict:
    safe_name = sanitize_filename(filename)
    doc = {
        "_id": new_id("att_"),
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "filename": safe_name,
        "content_type": content_type,
        "size": len(data),
        "uploaded_by": uploaded_by,  # 'customer' | operator id
        "created_at": now(),
    }
    path = upload_root() / f"{doc['_id']}_{safe_name}"
    path.write_bytes(data)
    doc["storage_path"] = path.name
    await database[db_module.ATTACHMENTS].insert_one(doc)
    return doc


def read_bytes(doc: dict) -> bytes:
    return (upload_root() / doc["storage_path"]).read_bytes()


def public_attachment(doc: dict) -> dict:
    return serialize(doc, drop=("storage_path",))
