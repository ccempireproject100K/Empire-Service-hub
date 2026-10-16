"""Tenant administration + knowledge-base management.

A tenant is one product/business context: its own branding, languages,
knowledge scope, escalation config, public widget key and machine secret key.
Operators administer tenants via JWT (admins all, agents their assigned
ones); products can also manage knowledge with the tenant secret key.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

import db as db_module
from lib import auth
from lib.util import new_id, now, serialize

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


class BrandingIn(BaseModel):
    name: str | None = None
    primary_color: str = "#2563eb"
    accent_color: str = "#f59e0b"
    greeting: str = "Hi there! How can we help?"
    logo_url: str | None = None


class TenantIn(BaseModel):
    slug: str
    name: str
    branding: BrandingIn = BrandingIn()
    languages: list[str] = ["en"]
    allowed_origins: list[str] = []
    escalation_keywords: list[str] = []
    confidence_threshold: float = 0.55
    sentiment_threshold: float = -0.34


class TenantPatchIn(BaseModel):
    name: str | None = None
    branding: BrandingIn | None = None
    languages: list[str] | None = None
    allowed_origins: list[str] | None = None
    escalation_keywords: list[str] | None = None
    confidence_threshold: float | None = None
    sentiment_threshold: float | None = None


class KnowledgeIn(BaseModel):
    title: str
    content: str
    tags: list[str] = []


def _public_tenant(tenant: dict, include_secret: bool = False) -> dict:
    out = serialize(tenant, drop=("secret_key_hash", "_secret_once"))
    if include_secret and tenant.get("_secret_once"):
        out["secret_key"] = tenant["_secret_once"]
    return out


async def _get_tenant(database, tenant_id: str) -> dict:
    tenant = await database[db_module.TENANTS].find_one({"_id": tenant_id})
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.post("", status_code=201)
async def create_tenant(body: TenantIn,
                        operator: dict = Depends(auth.get_current_operator),
                        database=Depends(db_module.get_db)):
    auth.require_admin(operator)
    existing = await database[db_module.TENANTS].find_one({"slug": body.slug})
    if existing:
        raise HTTPException(status_code=409, detail="Tenant slug already exists")
    secret = auth.generate_secret_key()
    doc = {
        "_id": "ten_" + body.slug,
        "slug": body.slug,
        "name": body.name,
        "branding": body.branding.model_dump(),
        "languages": body.languages,
        "allowed_origins": body.allowed_origins,
        "escalation_keywords": body.escalation_keywords,
        "confidence_threshold": body.confidence_threshold,
        "sentiment_threshold": body.sentiment_threshold,
        "public_key": auth.generate_public_key(),
        "secret_key_hash": auth.hash_secret_key(secret),
        "created_at": now(),
    }
    await database[db_module.TENANTS].insert_one(doc)
    doc["_secret_once"] = secret  # returned exactly once, at creation
    return {"tenant": _public_tenant(doc, include_secret=True)}


@router.get("")
async def list_tenants(operator: dict = Depends(auth.get_current_operator),
                       database=Depends(db_module.get_db)):
    tenants = await database[db_module.TENANTS].find({}).to_list(length=500)
    if operator.get("role") != "admin":
        allowed = set(operator.get("tenant_ids") or [])
        tenants = [t for t in tenants if t["_id"] in allowed]
    return {"tenants": [_public_tenant(t) for t in tenants]}


@router.get("/{tenant_id}")
async def get_tenant(tenant_id: str,
                     operator: dict = Depends(auth.get_current_operator),
                     database=Depends(db_module.get_db)):
    auth.assert_tenant_access(operator, tenant_id)
    return {"tenant": _public_tenant(await _get_tenant(database, tenant_id))}


@router.patch("/{tenant_id}")
async def patch_tenant(tenant_id: str, body: TenantPatchIn,
                       operator: dict = Depends(auth.get_current_operator),
                       database=Depends(db_module.get_db)):
    auth.assert_tenant_access(operator, tenant_id)
    tenant = await _get_tenant(database, tenant_id)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "branding" in updates and isinstance(updates["branding"], dict):
        merged = {**(tenant.get("branding") or {}), **updates["branding"]}
        updates["branding"] = merged
    if updates:
        updates["updated_at"] = now()
        await database[db_module.TENANTS].update_one(
            {"_id": tenant_id}, {"$set": updates}
        )
        tenant.update(updates)
    return {"tenant": _public_tenant(tenant)}


@router.post("/{tenant_id}/rotate-secret")
async def rotate_secret(tenant_id: str,
                        operator: dict = Depends(auth.get_current_operator),
                        database=Depends(db_module.get_db)):
    auth.require_admin(operator)
    await _get_tenant(database, tenant_id)
    secret = auth.generate_secret_key()
    await database[db_module.TENANTS].update_one(
        {"_id": tenant_id},
        {"$set": {"secret_key_hash": auth.hash_secret_key(secret), "updated_at": now()}},
    )
    return {"secret_key": secret}  # shown once


# ------------------------------------------------------------- knowledge base
async def _knowledge_access(database, tenant_id: str, operator: dict | None,
                            secret_key: str | None) -> dict:
    """Either a scoped operator JWT or the tenant's own secret key."""
    if secret_key:
        tenant = await auth.tenant_from_secret_key(database, secret_key)
        if tenant["_id"] != tenant_id:
            raise HTTPException(status_code=403, detail="Secret key is for another tenant")
        return tenant
    if operator is None:
        raise HTTPException(status_code=401, detail="Operator token or tenant secret key required")
    auth.assert_tenant_access(operator, tenant_id)
    return await _get_tenant(database, tenant_id)


@router.post("/{tenant_id}/knowledge", status_code=201)
async def add_knowledge(tenant_id: str, body: KnowledgeIn,
                        x_hub_secret_key: str | None = Header(default=None),
                        authorization: str | None = Header(default=None),
                        database=Depends(db_module.get_db)):
    # Auth resolved manually so both the operator-JWT and secret-key paths work.
    operator = await _operator_from_header(database, authorization)
    await _knowledge_access(database, tenant_id, operator, x_hub_secret_key)
    doc = {
        "_id": new_id("kb_"),
        "tenant_id": tenant_id,
        "title": body.title,
        "content": body.content,
        "tags": body.tags,
        "created_at": now(),
    }
    await database[db_module.KNOWLEDGE].insert_one(doc)
    return {"entry": serialize(doc)}


@router.get("/{tenant_id}/knowledge")
async def list_knowledge(tenant_id: str,
                         x_hub_secret_key: str | None = Header(default=None),
                         authorization: str | None = Header(default=None),
                         database=Depends(db_module.get_db)):
    operator = await _operator_from_header(database, authorization)
    await _knowledge_access(database, tenant_id, operator, x_hub_secret_key)
    entries = await database[db_module.KNOWLEDGE].find(
        {"tenant_id": tenant_id}
    ).sort("created_at", 1).to_list(length=1000)
    return {"entries": [serialize(e) for e in entries]}


@router.delete("/{tenant_id}/knowledge/{entry_id}")
async def delete_knowledge(tenant_id: str, entry_id: str,
                           x_hub_secret_key: str | None = Header(default=None),
                           authorization: str | None = Header(default=None),
                           database=Depends(db_module.get_db)):
    operator = await _operator_from_header(database, authorization)
    await _knowledge_access(database, tenant_id, operator, x_hub_secret_key)
    result = await database[db_module.KNOWLEDGE].delete_one(
        {"_id": entry_id, "tenant_id": tenant_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    return {"deleted": True}


async def _operator_from_header(database, authorization: str | None) -> dict | None:
    """Resolve an optional Bearer JWT to an operator (None when absent)."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return await auth.operator_from_token(database, authorization[7:])
