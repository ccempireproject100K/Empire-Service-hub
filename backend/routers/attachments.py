"""Attachment downloads (v2, section C).

Uploads happen on the widget/operator conversation endpoints; downloads
live here and re-check scope every time: operators must have tenant
access, widget customers must present the tenant public key *and* the
attachment must belong to a conversation of that tenant. Bytes stream
from the storage layer; metadata was PII-sanitized at upload time.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

import db as db_module
from lib import auth, storage

router = APIRouter(prefix="/api/attachments", tags=["attachments"])


async def _get(database, attachment_id: str) -> dict:
    doc = await database[db_module.ATTACHMENTS].find_one({"_id": attachment_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return doc


@router.get("/{attachment_id}")
async def download_operator(attachment_id: str,
                            operator: dict = Depends(auth.get_current_operator),
                            database=Depends(db_module.get_db)):
    doc = await _get(database, attachment_id)
    auth.assert_tenant_access(operator, doc["tenant_id"])
    return Response(
        content=storage.read_bytes(doc),
        media_type=doc.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@router.get("/{attachment_id}/widget")
async def download_widget(attachment_id: str,
                          x_hub_public_key: str | None = Header(default=None),
                          database=Depends(db_module.get_db)):
    tenant = await auth.tenant_from_public_key(database, x_hub_public_key)
    doc = await _get(database, attachment_id)
    if doc["tenant_id"] != tenant["_id"]:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return Response(
        content=storage.read_bytes(doc),
        media_type=doc.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )
