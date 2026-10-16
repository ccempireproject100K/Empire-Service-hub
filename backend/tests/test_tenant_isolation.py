"""Tenant isolation: the hub's hard boundary, exercised from every surface."""


def _two_tenants(hub, admin):
    _, token = admin
    tenant_a = hub.create_tenant(token, slug="alpha", name="Alpha")
    tenant_b = hub.create_tenant(token, slug="beta", name="Beta")
    hub.add_kb(token, tenant_a["_id"], "Zebra warranty",
               "Zebras are covered for nine years.", ["zebra", "warranty"])
    return tenant_a, tenant_b, token


def test_widget_key_cannot_read_other_tenants_conversation(client, hub, admin):
    tenant_a, tenant_b, _ = _two_tenants(hub, admin)
    conv_id = hub.open_conversation(tenant_a)["conversation"]["_id"]
    resp = client.get(f"/api/widget/conversations/{conv_id}/messages",
                      headers=hub.widget_headers(tenant_b))
    assert resp.status_code == 404
    resp = client.post(f"/api/widget/conversations/{conv_id}/messages",
                       json={"text": "sneaky"}, headers=hub.widget_headers(tenant_b))
    assert resp.status_code == 404
    resp = client.post(f"/api/widget/conversations/{conv_id}/csat",
                       json={"rating": 1}, headers=hub.widget_headers(tenant_b))
    assert resp.status_code == 404


def test_knowledge_is_scoped_per_tenant(client, hub, admin):
    tenant_a, tenant_b, _ = _two_tenants(hub, admin)
    conv_id = hub.open_conversation(tenant_b)["conversation"]["_id"]
    result = hub.say(tenant_b, conv_id, "What is the zebra warranty?")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    assert ai_texts, "expected an AI reply"
    assert not any("nine years" in t for t in ai_texts)
    assert not any("Zebra warranty" in t for t in ai_texts)


def test_agent_scoping(client, hub, admin):
    tenant_a, tenant_b, _ = _two_tenants(hub, admin)
    conv_a = hub.open_conversation(tenant_a)["conversation"]["_id"]
    hub.open_conversation(tenant_b)
    _, agent_token = hub.make_agent([tenant_a["_id"]])

    # List without a filter shows only the agent's tenant.
    resp = client.get("/api/operator/conversations", headers=hub.bearer(agent_token))
    ids = [c["_id"] for c in resp.json()["conversations"]]
    assert ids == [conv_a]

    # Explicit access to the other tenant is denied everywhere.
    for url in (
        f"/api/operator/conversations?tenant_id={tenant_b['_id']}",
        f"/api/operator/stats?tenant_id={tenant_b['_id']}",
        f"/api/archive?tenant_id={tenant_b['_id']}",
        f"/api/archive/export?tenant_id={tenant_b['_id']}",
        f"/api/tenants/{tenant_b['_id']}",
    ):
        resp = client.get(url, headers=hub.bearer(agent_token))
        assert resp.status_code == 403, url

    # And the agent's own tenant works.
    resp = client.get(f"/api/operator/stats?tenant_id={tenant_a['_id']}",
                      headers=hub.bearer(agent_token))
    assert resp.status_code == 200
    tenants = client.get("/api/tenants", headers=hub.bearer(agent_token))
    assert [t["_id"] for t in tenants.json()["tenants"]] == [tenant_a["_id"]]


def test_agent_cannot_take_over_other_tenants_conversation(client, hub, admin):
    tenant_a, tenant_b, _ = _two_tenants(hub, admin)
    conv_b = hub.open_conversation(tenant_b)["conversation"]["_id"]
    _, agent_token = hub.make_agent([tenant_a["_id"]])
    resp = client.post(f"/api/operator/conversations/{conv_b}/takeover",
                       headers=hub.bearer(agent_token))
    assert resp.status_code == 403
    resp = client.get(f"/api/operator/conversations/{conv_b}",
                      headers=hub.bearer(agent_token))
    assert resp.status_code == 403
