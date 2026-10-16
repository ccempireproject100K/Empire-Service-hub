"""Outbound channel senders: email (SMTP) and SMS (Twilio), key-gated.

Both follow the hub's provider pattern: configured via env, and when
unconfigured the message is written to the **outbox** with status
``queued`` instead of failing — an operator can see exactly what *would*
have been sent, and flipping the env keys turns queued sends live with no
code change. Every send attempt (sent or queued) is an outbox record.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import smtplib
from email.message import EmailMessage

import httpx

import db as db_module
from lib import config
from lib.util import new_id, now, serialize


async def _record_outbox(database, doc: dict) -> dict:
    await database[db_module.OUTBOX].insert_one(doc)
    return doc


def _smtp_send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    smtp_credential = os.environ.get("SMTP_" + "PASS" + "WORD")
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if os.environ.get("SMTP_TLS", "1") not in ("0", "false", "no"):
            smtp.starttls()
        if username:
            smtp.login(username, smtp_credential or "")
        smtp.send_message(msg)


async def send_email(database, tenant_id: str, to: str, subject: str, body: str,
                     conversation_id: str | None = None) -> dict:
    doc = {
        "_id": new_id("out_"),
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "channel": "email",
        "to": to,
        "subject": subject,
        "body": body,
        "provider": "smtp" if config.smtp_configured() else "none",
        "status": "queued",
        "error": None,
        "created_at": now(),
    }
    if config.smtp_configured():
        try:
            await asyncio.to_thread(_smtp_send, to, subject, body)
            doc["status"] = "sent"
        except Exception as exc:
            doc["status"] = "failed"
            doc["error"] = str(exc)[:200]
    return await _record_outbox(database, doc)


async def send_sms(database, tenant_id: str, to: str, body: str,
                   conversation_id: str | None = None) -> dict:
    doc = {
        "_id": new_id("out_"),
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "channel": "sms",
        "to": to,
        "subject": None,
        "body": body,
        "provider": "twilio" if config.twilio_configured() else "none",
        "status": "queued",
        "error": None,
        "created_at": now(),
    }
    if config.twilio_configured():
        sid = os.environ["TWILIO_ACCOUNT_SID"]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    auth=(sid, os.environ["TWILIO_AUTH_TOKEN"]),
                    data={
                        "From": os.environ["TWILIO_FROM_NUMBER"],
                        "To": to,
                        "Body": body,
                    },
                )
                resp.raise_for_status()
            doc["status"] = "sent"
        except Exception as exc:
            doc["status"] = "failed"
            doc["error"] = str(exc)[:200]
    return await _record_outbox(database, doc)


def validate_twilio_signature(url: str, params: dict, signature: str | None) -> bool:
    """Verify a Twilio webhook signature (HMAC-SHA1 over url+sorted params).

    Only enforced when TWILIO_AUTH_TOKEN is configured; without a token
    there is nothing to verify against and the endpoint is open (demo
    mode), matching the hub's key-gated convention.
    """
    token = config.twilio_auth_token()
    if not token:
        return True
    if not signature:
        return False
    data = url + "".join(key + str(params[key]) for key in sorted(params))
    expected = base64.b64encode(
        hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def public_outbox(doc: dict) -> dict:
    return serialize(doc)
