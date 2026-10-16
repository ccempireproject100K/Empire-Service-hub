"""Escalation archive API: browse the learning corpus + JSONL export."""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

import db as db_module
from lib import archive as archive_lib, auth
from lib.util import serialize

router = APIRouter(prefix="/api/archive", tags=["archive"])


@router.get("")
async def list_archive(tenant_id: str, status: str | None = None, limit: int = 100,
                       operator: dict = Depends(auth.get_current_operator),
                       database=Depends(db_module.get_db)):
    auth.assert_tenant_access(operator, tenant_id)
    query: dict = {"tenant_id": tenant_id}
    if status:
        query["status"] = status
    records = await database[db_module.ARCHIVE].find(query).sort(
        "escalated_at", -1
    ).to_list(length=min(limit, 500))
    return {"records": [serialize(r) for r in records]}


@router.get("/export")
async def export_archive(tenant_id: str,
                         operator: dict = Depends(auth.get_current_operator),
                         database=Depends(db_module.get_db)):
    """Download the tenant's archive as JSON Lines — one escalation per line,
    transcript (PII-masked) + human resolution: the training/eval corpus."""
    auth.assert_tenant_access(operator, tenant_id)
    records = await database[db_module.ARCHIVE].find(
        {"tenant_id": tenant_id}
    ).sort("escalated_at", 1).to_list(length=100000)
    lines = [json.dumps(archive_lib.export_line(r)) for r in records]
    return PlainTextResponse(
        "\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="archive-{tenant_id}.jsonl"'
        },
    )


@router.get("/{record_id}")
async def get_record(record_id: str,
                     operator: dict = Depends(auth.get_current_operator),
                     database=Depends(db_module.get_db)):
    record = await database[db_module.ARCHIVE].find_one({"_id": record_id})
    if not record:
        raise HTTPException(status_code=404, detail="Archive record not found")
    auth.assert_tenant_access(operator, record["tenant_id"])
    return {"record": serialize(record)}
