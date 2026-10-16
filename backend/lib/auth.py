"""Operator authentication + tenant API keys.

Passwords: PBKDF2-HMAC-SHA256 with per-password salt (stdlib, no extra deps).
Sessions: HS256 JWTs (PyJWT), secret from env only.
Tenant keys: a *public* key (safe to embed in the widget snippet) and a
*secret* key for machine calls — only the SHA-256 hash of the secret is stored.
"""
import base64
import hashlib
import hmac
import os
import secrets
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, Request

import db as db_module
from lib import config
from lib.util import now

_PBKDF2_ITERATIONS = 200_000


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_b64, digest_b64 = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
        )
        return hmac.compare_digest(base64.b64encode(digest).decode(), digest_b64)
    except Exception:
        return False


# --------------------------------------------------------------------- JWTs
def create_token(operator: dict) -> str:
    payload = {
        "sub": operator["_id"],
        "email": operator["email"],
        "role": operator.get("role", "agent"),
        "exp": now() + timedelta(hours=config.jwt_ttl_hours()),
    }
    return jwt.encode(payload, config.jwt_secret(), algorithm="HS256")


async def operator_from_token(database, token: str) -> dict:
    """Resolve a raw JWT string to an operator doc (401 on any problem)."""
    try:
        payload = jwt.decode(token, config.jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    operator = await database[db_module.OPERATORS].find_one({"_id": payload.get("sub")})
    if not operator:
        raise HTTPException(status_code=401, detail="Operator no longer exists")
    return operator


async def get_current_operator(request: Request, database=Depends(db_module.get_db)) -> dict:
    """FastAPI dependency: resolve the Bearer JWT to an operator doc."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return await operator_from_token(database, header[7:])


def require_admin(operator: dict) -> dict:
    if operator.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return operator


def assert_tenant_access(operator: dict, tenant_id: str) -> None:
    """Admins see every tenant; agents only their assigned tenants."""
    if operator.get("role") == "admin":
        return
    if tenant_id not in (operator.get("tenant_ids") or []):
        raise HTTPException(status_code=403, detail="No access to this tenant")


# --------------------------------------------------------------- tenant keys
def generate_public_key() -> str:
    return "pk_" + secrets.token_urlsafe(18)


def generate_secret_key() -> str:
    return "sk_" + secrets.token_urlsafe(32)


def hash_secret_key(secret_key: str) -> str:
    return hashlib.sha256(secret_key.encode()).hexdigest()


async def tenant_from_public_key(database, public_key: str | None) -> dict:
    if not public_key:
        raise HTTPException(status_code=401, detail="Missing tenant public key")
    tenant = await database[db_module.TENANTS].find_one({"public_key": public_key})
    if not tenant:
        raise HTTPException(status_code=404, detail="Unknown tenant key")
    return tenant


async def tenant_from_secret_key(database, secret_key: str | None) -> dict:
    if not secret_key:
        raise HTTPException(status_code=401, detail="Missing tenant secret key")
    tenant = await database[db_module.TENANTS].find_one(
        {"secret_key_hash": hash_secret_key(secret_key)}
    )
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid tenant secret key")
    return tenant
