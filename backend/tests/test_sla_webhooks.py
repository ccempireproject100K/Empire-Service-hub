"""v2 sections D+E: SLA sweep/alerts and signed tenant webhooks with retry."""
import asyncio
import hashlib
import hmac
import json
from datetime import timedelta

import db as db_module
from lib import webhooks
from lib.util import new_id, now

WH_SECRET = "wh" + "sec-test-value"


def _capture_posts(monkeypatch, fail_first=False):
    calls = []
    state = {"failed": False}

    async def fake_post(url, body, headers):
        calls.append({"url": url, "body": body, "headers": headers})
        if fail_first and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("receiver down")
        return 200

    monkeypatch.setattr(webhooks, "_post", fake_post)
    return calls


def _webhook_tenant(hub, token, slug="hooks"):
    return hub.create_tenant(
        token, slug=slug, name="Hook Co",
        webhook_url="https://product.example/hook", webhook_secret=WH_SECRET)


def _escalate(hub, tenant, text="I want to speak to a human please"):
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    result = hub.say(tenant, conv_id, text)
    assert result["escalated"] is True
    return conv_id


def test_webhooks_fire_for_escalation_resolution_and_csat(
        client, hub, admin, monkeypatch):
    _, token = admin
    calls = _capture_posts(monkeypatch)
    tenant = _webhook_tenant(hub, token)
    conv_id = _escalate(hub, tenant)

    events = [json.loads(c["body"])["event"] for c in calls]
    assert events == ["escalation.created"]
    # Signature matches HMAC-SHA256 of the exact body bytes.
    call = calls[0]
    expected = "sha256=" + hmac.new(
        WH_SECRET.encode(), call["body"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Hub-Signature-256"] == expected
    assert json.loads(call["body"])["data"]["conversation_id"] == conv_id

    resp = client.post(f"/api/operator/conversations/{conv_id}/resolve",
                       json={"resolution": "Sorted it out."},
                       headers=hub.bearer(token))
    assert resp.status_code == 200
    events = [json.loads(c["body"])["event"] for c in calls]
    assert events[-1] == "conversation.resolved"

    resp = client.post(f"/api/widget/conversations/{conv_id}/csat",
                       json={"rating": 5}, headers=hub.widget_headers(tenant))
    assert resp.status_code == 200
    events = [json.loads(c["body"])["event"] for c in calls]
    assert events[-1] == "csat.received"

    async def _deliveries():
        return await hub.db[db_module.WEBHOOK_DELIVERIES].find(
            {"tenant_id": tenant["_id"]}).to_list(length=10)
    deliveries = asyncio.run(_deliveries())
    assert len(deliveries) == 3
    assert all(d["status"] == "delivered" for d in deliveries)


def test_webhook_delivery_retries_with_backoff(client, hub, admin, monkeypatch):
    _, token = admin
    calls = _capture_posts(monkeypatch, fail_first=True)
    tenant = _webhook_tenant(hub, token, slug="hooks2")
    _escalate(hub, tenant)
    assert len(calls) == 1  # first attempt failed

    async def _pending():
        rows = await hub.db[db_module.WEBHOOK_DELIVERIES].find(
            {"tenant_id": tenant["_id"], "status": "pending"}).to_list(length=5)
        for row in rows:  # make the retry due immediately
            await hub.db[db_module.WEBHOOK_DELIVERIES].update_one(
                {"_id": row["_id"]}, {"$set": {"next_retry_at": now()}})
        return rows
    pending = asyncio.run(_pending())
    assert len(pending) == 1 and pending[0]["attempts"] == 1

    resp = client.post("/api/operator/sla-sweep", headers=hub.bearer(token))
    assert resp.status_code == 200
    assert resp.json()["webhooks_retried"] == 1
    assert len(calls) == 2

    async def _deliveries():
        return await hub.db[db_module.WEBHOOK_DELIVERIES].find(
            {"tenant_id": tenant["_id"]}).to_list(length=5)
    deliveries = asyncio.run(_deliveries())
    assert deliveries[0]["status"] == "delivered"
    assert deliveries[0]["attempts"] == 2


def test_no_webhook_url_means_no_deliveries(client, hub, admin, tenant, monkeypatch):
    _, token = admin
    calls = _capture_posts(monkeypatch)
    _escalate(hub, tenant)
    assert calls == []


def _insert_old_conversation(hub, tenant_id, minutes_ago, status="escalated"):
    async def _run():
        doc = {
            "_id": new_id("conv_"),
            "tenant_id": tenant_id,
            "channel": "chat",
            "status": status,
            "customer_name": "Old Case",
            "customer_ref": None,
            "language": "en",
            "created_at": now() - timedelta(minutes=minutes_ago),
            "updated_at": now() - timedelta(minutes=minutes_ago),
            "first_response_at": now() - timedelta(minutes=minutes_ago - 1),
            "resolved_at": None,
            "assigned_operator_id": None,
            "escalation": {"rule": "keyword", "detail": "test"},
            "last_sentiment": None,
        }
        await hub.db[db_module.CONVERSATIONS].insert_one(doc)
        return doc["_id"]
    return asyncio.run(_run())


def test_sla_sweep_stamps_breach_alerts_and_webhooks(client, hub, admin, monkeypatch):
    _, token = admin
    calls = _capture_posts(monkeypatch)
    tenant = _webhook_tenant(hub, token, slug="slaco")
    # Resolution SLA is 480 min by default; this case is 600 min old.
    conv_id = _insert_old_conversation(hub, tenant["_id"], minutes_ago=600)

    resp = client.post("/api/operator/sla-sweep", headers=hub.bearer(token))
    result = resp.json()
    assert result["breaches"] >= 1

    resp = client.get(f"/api/operator/conversations/{conv_id}",
                      headers=hub.bearer(token))
    sla = resp.json()["conversation"]["sla"]
    assert sla["resolution"]["state"] == "breached"

    resp = client.get(
        f"/api/operator/sla-alerts?tenant_id={tenant['_id']}&acknowledged=false",
        headers=hub.bearer(token))
    alerts = resp.json()["alerts"]
    kinds = {a["kind"] for a in alerts}
    assert "resolution_breached" in kinds

    events = [json.loads(c["body"])["event"] for c in calls]
    assert "sla.breached" in events

    # Acknowledge clears it from the unacknowledged list.
    alert_id = alerts[0]["_id"]
    resp = client.post(f"/api/operator/sla-alerts/{alert_id}/ack",
                       headers=hub.bearer(token))
    assert resp.json()["alert"]["acknowledged"] is True

    # A second sweep doesn't duplicate alerts for the same breach.
    before = len(alerts)
    client.post("/api/operator/sla-sweep", headers=hub.bearer(token))
    resp = client.get(f"/api/operator/sla-alerts?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    assert len(resp.json()["alerts"]) == before


def test_sla_approaching_state(client, hub, admin, tenant):
    _, token = admin
    # 400 of 480 resolution minutes elapsed ≈ 83% → approaching, not breached.
    conv_id = _insert_old_conversation(hub, tenant["_id"], minutes_ago=400)
    client.post("/api/operator/sla-sweep", headers=hub.bearer(token))
    resp = client.get(f"/api/operator/conversations/{conv_id}",
                      headers=hub.bearer(token))
    assert resp.json()["conversation"]["sla"]["resolution"]["state"] == "approaching"
    resp = client.get(f"/api/operator/sla-alerts?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    kinds = {a["kind"] for a in resp.json()["alerts"]}
    assert "resolution_approaching" in kinds
