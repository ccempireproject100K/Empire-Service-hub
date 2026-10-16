"""Satisfaction pulse: one-question CSAT on resolution + per-tenant stats."""


def _resolved_conversation(client, hub, admin_token, tenant):
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    hub.say(tenant, conv_id, "What are your opening hours?")
    resp = client.post(f"/api/operator/conversations/{conv_id}/resolve",
                       json={"resolution": "Answered the question."},
                       headers=hub.bearer(admin_token))
    assert resp.status_code == 200
    return conv_id


def _tenant(hub, admin):
    _, token = admin
    tenant = hub.create_tenant(token)
    hub.add_kb(token, tenant["_id"], "Opening hours",
               "We are open Monday to Friday, 8am to 6pm Central.", ["hours"])
    return tenant, token


def test_csat_rejected_before_resolution(client, hub, admin):
    tenant, _ = _tenant(hub, admin)
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    resp = client.post(f"/api/widget/conversations/{conv_id}/csat",
                       json={"rating": 5}, headers=hub.widget_headers(tenant))
    assert resp.status_code == 409


def test_csat_recorded_once(client, hub, admin):
    tenant, token = _tenant(hub, admin)
    conv_id = _resolved_conversation(client, hub, token, tenant)
    resp = client.post(f"/api/widget/conversations/{conv_id}/csat",
                       json={"rating": 5, "comment": "Fast and friendly"},
                       headers=hub.widget_headers(tenant))
    assert resp.status_code == 200
    assert resp.json()["csat"]["rating"] == 5
    resp = client.post(f"/api/widget/conversations/{conv_id}/csat",
                       json={"rating": 1}, headers=hub.widget_headers(tenant))
    assert resp.status_code == 409  # one response per conversation


def test_csat_rating_validation(client, hub, admin):
    tenant, token = _tenant(hub, admin)
    conv_id = _resolved_conversation(client, hub, token, tenant)
    resp = client.post(f"/api/widget/conversations/{conv_id}/csat",
                       json={"rating": 9}, headers=hub.widget_headers(tenant))
    assert resp.status_code == 422


def test_stats_aggregate_csat_and_sla(client, hub, admin):
    tenant, token = _tenant(hub, admin)
    for rating in (5, 3):
        conv_id = _resolved_conversation(client, hub, token, tenant)
        resp = client.post(f"/api/widget/conversations/{conv_id}/csat",
                           json={"rating": rating},
                           headers=hub.widget_headers(tenant))
        assert resp.status_code == 200
    resp = client.get(f"/api/operator/stats?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["conversations_total"] == 2
    assert stats["by_status"] == {"resolved": 2}
    assert stats["csat"]["count"] == 2
    assert stats["csat"]["average"] == 4.0
    assert stats["csat"]["distribution"]["5"] == 1
    assert stats["csat"]["distribution"]["3"] == 1
    assert stats["sla"]["avg_first_response_seconds"] is not None
    assert stats["sla"]["avg_resolution_seconds"] is not None
    assert stats["escalations_total"] == 0
