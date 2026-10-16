"""Operator authentication: login + profile + admin operator management."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import db as db_module
from lib import auth, ratelimit
from lib.util import new_id, serialize

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class OperatorIn(BaseModel):
    email: str
    name: str
    password: str
    role: str = "agent"
    tenant_ids: list[str] = []


def _public_operator(operator: dict) -> dict:
    return serialize(operator, drop=("password_hash",))


@router.post("/login")
async def login(body: LoginIn, request: Request, database=Depends(db_module.get_db)):
    ratelimit.check(request, "auth_login", scope=body.email.lower())
    operator = await database[db_module.OPERATORS].find_one(
        {"email": body.email.lower()}
    )
    if not operator or not auth.verify_password(body.password, operator["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"token": auth.create_token(operator), "operator": _public_operator(operator)}


@router.get("/me")
async def me(operator: dict = Depends(auth.get_current_operator)):
    return {"operator": _public_operator(operator)}


@router.post("/operators", status_code=201)
async def create_operator(body: OperatorIn,
                          operator: dict = Depends(auth.get_current_operator),
                          database=Depends(db_module.get_db)):
    auth.require_admin(operator)
    existing = await database[db_module.OPERATORS].find_one({"email": body.email.lower()})
    if existing:
        raise HTTPException(status_code=409, detail="Operator email already exists")
    if body.role not in ("admin", "agent"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'agent'")
    doc = {
        "_id": new_id("op_"),
        "email": body.email.lower(),
        "name": body.name,
        "password_hash": auth.hash_password(body.password),
        "role": body.role,
        "tenant_ids": body.tenant_ids,
    }
    await database[db_module.OPERATORS].insert_one(doc)
    return {"operator": _public_operator(doc)}
