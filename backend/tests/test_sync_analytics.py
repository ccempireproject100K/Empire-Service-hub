"""v2 sections E+F: sync-back status API, topic clusters, cost, quality."""
import asyncio

import db as db_module


def _secret_headers(tenant):
    return {"X-Hub-Secret-Key": tenant["secret_key"]}


def _escalate_resolve(hub, client, tenant, token, question, resolution, **open_kw):
    opened = hub.open_conversation(tenant, **open_kw)
    conv_id = opened["conversation"]["_id"]
    result = hub.say(tenant, conv_id, question + " — speak to a human")
    assert result["escalated"] is True
    resp = client.post(f"/api/operator/conversations/{conv_id}/resolve",
                       json={"resolution": resolution},
                       headers=hub.bearer(token))
    assert resp.status_code == 200
    return conv_id


def test_sync_status_by_external_ref(client, hub, admin, tenant):
    _, token = admin
    conv_id = _escalate_resolve(
        hub, client, tenant, token,
        "My card was charged twice",
        "Refunded the duplicate charge in full.",
        external_ref="momo-ticket-42")
    resp = client.get("/api/sync/status?external_ref=momo-ticket-42",
                      headers=_secret_headers(tenant))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["conversation_id"] == conv_id
    assert payload["status"] == "resolved"
    assert payload["resolution"]["text"] == "Refunded the duplicate charge in full."
    assert payload["escalation_rule"] == "customer_request"

    resp = client.get(f"/api/sync/conversations/{conv_id}",
                      headers=_secret_headers(tenant))
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


def test_sync_is_tenant_scoped(client, hub, admin, tenant):
    _, token = admin
    other = hub.create_tenant(token, slug="other", name="Other Co")
    conv_id = _escalate_resolve(
        hub, client, tenant, token, "Portal is down for me",
        "Restored portal access.", external_ref="shared-ref")
    # The other tenant's secret cannot see it — by ref or by id.
    resp = client.get("/api/sync/status?external_ref=shared-ref",
                      headers=_secret_headers(other))
    assert resp.status_code == 404
    resp = client.get(f"/api/sync/conversations/{conv_id}",
                      headers=_secret_headers(other))
    assert resp.status_code == 404
    resp = client.get("/api/sync/status?external_ref=shared-ref")
    assert resp.status_code == 401


def test_topic_clusters_show_escalation_drivers(client, hub, admin, tenant):
    _, token = admin
    for i in range(2):
        _escalate_resolve(
            hub, client, tenant, token,
            f"Please refund my billing charge number {i}",
            "Issued the refund.")
    _escalate_resolve(
        hub, client, tenant, token,
        "How do I export my data export file",
        "Sent the export link.")
    resp = client.get(f"/api/operator/analytics?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    topics = resp.json()["topics"]
    assert topics, "expected topic clusters"
    top = topics[0]
    assert top["count"] == 2
    assert "refund" in top["topic"]
    assert top["resolved"] == 2
    assert top["share"] > 0.5


def test_cost_summary_counts_ai_usage(client, hub, admin, tenant):
    _, token = admin
    hub.add_kb(token, tenant["_id"], "Opening hours",
               "We are open Monday to Friday, 8am to 6pm Central.",
               ["hours", "opening"])
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    hub.say(tenant, conv_id, "What are your opening hours?")
    client.post(f"/api/operator/conversations/{conv_id}/resolve",
                json={"resolution": "Confirmed."}, headers=hub.bearer(token))

    resp = client.get(f"/api/operator/stats?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    cost = resp.json()["ai_cost"]
    assert cost["ai_messages"] >= 2  # greeting + answer
    assert cost["est_tokens"] > 0
    assert cost["est_cost_usd"] == 0.0  # demo provider rate is zero
    assert cost["est_cost_per_resolution_usd"] == 0.0
    assert cost["by_provider"]["demo"]["messages"] >= 2


def test_quality_review_queued_for_weak_resolution(client, hub, admin, tenant):
    _, token = admin
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    hub.say(tenant, conv_id, "I want to speak to a human about my invoice")
    # Operator closes with a negative tone and a thin resolution.
    client.post(f"/api/operator/conversations/{conv_id}/takeover",
                headers=hub.bearer(token))
    client.post(f"/api/operator/conversations/{conv_id}/reply",
                json={"text": "This is terrible, the invoice is broken and wrong."},
                headers=hub.bearer(token))
    resp = client.post(f"/api/operator/conversations/{conv_id}/resolve",
                       json={"resolution": "ok"},
                       headers=hub.bearer(token))
    review = resp.json()["quality_review"]
    assert review["status"] == "queued"
    assert review["score"] < 70
    assert "negative_operator_tone" in review["flags"]

    resp = client.get(
        f"/api/operator/quality-reviews?tenant_id={tenant['_id']}&status=queued",
        headers=hub.bearer(token))
    reviews = resp.json()["reviews"]
    assert len(reviews) == 1
    resp = client.post(
        f"/api/operator/quality-reviews/{reviews[0]['_id']}/review",
        headers=hub.bearer(token))
    assert resp.json()["review"]["status"] == "reviewed"


def test_quality_review_passes_clean_conversation(client, hub, admin, tenant):
    _, token = admin
    conv_id = _escalate_resolve(
        hub, client, tenant, token, "The portal export never arrives",
        "Re-sent the export and confirmed the customer's inbox rules "
        "were quarantining our sender; whitelisting fixed it.")
    async def _reviews():
        return await hub.db[db_module.QUALITY_REVIEWS].find(
            {"tenant_id": tenant["_id"]}).to_list(length=5)
    reviews = asyncio.run(_reviews())
    assert len(reviews) == 1
    assert reviews[0]["status"] == "passed"
    assert reviews[0]["score"] >= 70
