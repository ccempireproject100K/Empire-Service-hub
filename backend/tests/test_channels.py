"""v2 section C: email, SMS and voice-transcript channels."""
import asyncio
import base64
import hashlib
import hmac

import db as db_module


def _secret_headers(tenant):
    return {"X-Hub-Secret-Key": tenant["secret_key"]}


def _outbox(hub, tenant_id, channel):
    async def _run():
        return await hub.db[db_module.OUTBOX].find(
            {"tenant_id": tenant_id, "channel": channel}).to_list(length=50)
    return asyncio.run(_run())


def test_email_inbound_creates_and_continues_conversation(client, hub, admin, tenant):
    _, token = admin
    hub.add_kb(token, tenant["_id"], "Opening hours",
               "We are open Monday to Friday, 8am to 6pm Central.",
               ["hours", "opening"])
    resp = client.post(
        "/api/channels/email/inbound",
        json={"from": "Sam@Example.com", "subject": "Hours",
              "body": "What are your opening hours?"},
        headers=_secret_headers(tenant))
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["created"] is True
    conversation = payload["conversation"]
    assert conversation["channel"] == "email"
    assert conversation["customer_email"] == "sam@example.com"

    # The AI reply was queued to the outbox (no SMTP configured).
    outbox = _outbox(hub, tenant["_id"], "email")
    assert len(outbox) == 1
    assert outbox[0]["status"] == "queued"
    assert outbox[0]["to"] == "sam@example.com"
    assert "8am to 6pm" in outbox[0]["body"]

    # A follow-up from the same address continues the same conversation.
    resp = client.post(
        "/api/channels/email/inbound",
        json={"from": "sam@example.com", "body": "And what about billing?"},
        headers=_secret_headers(tenant))
    assert resp.json()["created"] is False
    assert resp.json()["conversation"]["_id"] == conversation["_id"]


def test_email_inbound_requires_auth(client, hub, admin, tenant):
    resp = client.post(
        "/api/channels/email/inbound",
        json={"from": "a@b.com", "body": "hello"})
    assert resp.status_code == 401
    resp = client.post(
        "/api/channels/email/inbound",
        json={"from": "a@b.com", "body": "hello"},
        headers={"X-Hub-Secret-Key": "sk_wrong"})
    assert resp.status_code == 401


def test_email_inbound_wrong_tenant_secret_cannot_post(client, hub, admin, tenant):
    _, token = admin
    other = hub.create_tenant(token, slug="other", name="Other Co")
    resp = client.post(
        "/api/channels/email/inbound?tenant_id=" + tenant["_id"],
        json={"from": "a@b.com", "body": "hello"},
        headers=_secret_headers(other))
    # The secret resolves to *its own* tenant; the conversation lands there,
    # never in the hinted tenant.
    assert resp.status_code == 201
    assert resp.json()["conversation"]["tenant_id"] == other["_id"]


def test_sms_inbound_via_twilio_webhook(client, hub, admin):
    _, token = admin
    tenant = hub.create_tenant(token, slug="smsco", name="SMS Co",
                               sms_number="+15550001111")
    hub.add_kb(token, tenant["_id"], "Opening hours",
               "We are open Monday to Friday, 8am to 6pm Central.",
               ["hours", "opening"])
    resp = client.post(
        "/api/channels/sms/inbound",
        data={"From": "+15557654321", "To": "+15550001111",
              "Body": "What are your opening hours?"})
    assert resp.status_code == 201, resp.text
    conversation = resp.json()["conversation"]
    assert conversation["channel"] == "sms"
    assert conversation["customer_phone"] == "+15557654321"
    outbox = _outbox(hub, tenant["_id"], "sms")
    assert len(outbox) == 1 and outbox[0]["status"] == "queued"
    assert "8am to 6pm" in outbox[0]["body"]

    # Unknown destination number → 404 (no tenant owns it).
    resp = client.post(
        "/api/channels/sms/inbound",
        data={"From": "+15557654321", "To": "+19999999999", "Body": "hi"})
    assert resp.status_code == 404


def test_sms_signature_enforced_when_token_configured(client, hub, admin, monkeypatch):
    _, token = admin
    tenant = hub.create_tenant(token, slug="smsco2", name="SMS Co 2",
                               sms_number="+15550002222")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-auth-token")
    params = {"From": "+15557654321", "To": "+15550002222", "Body": "hello there"}
    url = "http://testserver/api/channels/sms/inbound"

    resp = client.post("/api/channels/sms/inbound", data=params)
    assert resp.status_code == 403  # no signature at all

    data = url + "".join(k + params[k] for k in sorted(params))
    signature = base64.b64encode(
        hmac.new(b"test-auth-token", data.encode(), hashlib.sha1).digest()
    ).decode()
    resp = client.post("/api/channels/sms/inbound", data=params,
                       headers={"X-Twilio-Signature": signature})
    assert resp.status_code == 201, resp.text


def test_voice_transcript_lands_escalated_with_archive(client, hub, admin, tenant):
    _, token = admin
    transcript = (
        "Customer: My water heater stopped working this morning.\n"
        "Agent: I'm sorry — let me check your account.\n"
        "Customer: It is still not working and I need help today."
    )
    resp = client.post(
        "/api/channels/voice/transcript",
        json={"customer_name": "Pat", "customer_phone": "+15551234567",
              "external_ref": "call-123", "summary": "Water heater outage",
              "transcript": transcript},
        headers=_secret_headers(tenant))
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["escalated"] is True
    assert payload["escalation_reason"] == "voice_handoff"
    conversation = payload["conversation"]
    assert conversation["channel"] == "voice"
    assert conversation["status"] == "escalated"
    assert conversation["external_ref"] == "call-123"

    resp = client.get(f"/api/archive?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    records = resp.json()["records"]
    assert len(records) == 1
    texts = " ".join(m["text"] for m in records[0]["transcript"])
    assert "water heater stopped working" in texts

    resp = client.post("/api/channels/voice/transcript",
                       json={"transcript": "hello"},
                       headers={"X-Hub-Secret-Key": "sk_wrong"})
    assert resp.status_code == 401
