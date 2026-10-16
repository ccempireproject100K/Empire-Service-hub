"""Tenants, branding config for the widget, knowledge management, key rotation."""
import server as server_module


def test_create_tenant_returns_keys_once(client, hub, admin):
    _, token = admin
    tenant = hub.create_tenant(token, slug="acme", name="Acme")
    assert tenant["public_key"].startswith("pk_")
    assert tenant["secret_key"].startswith("sk_")
    assert "secret_key_hash" not in tenant
    # The secret is never shown again on reads.
    resp = client.get(f"/api/tenants/{tenant['_id']}", headers=hub.bearer(token))
    assert "secret_key" not in resp.json()["tenant"]


def test_duplicate_slug_rejected(client, hub, admin, tenant):
    _, token = admin
    resp = client.post("/api/tenants", json={"slug": "books", "name": "Again"},
                       headers=hub.bearer(token))
    assert resp.status_code == 409


def test_widget_config_branding(client, hub, admin):
    _, token = admin
    tenant = hub.create_tenant(
        token, slug="branded", name="Branded Co",
        branding={"name": "Branded Support", "primary_color": "#123456",
                  "accent_color": "#abcdef", "greeting": "Hello there!"},
    )
    resp = client.get("/api/widget/config", headers=hub.widget_headers(tenant))
    assert resp.status_code == 200
    data = resp.json()
    assert data["demo"] is True  # no provider keys in tests
    assert data["tenant"]["branding"]["name"] == "Branded Support"
    assert data["tenant"]["branding"]["primary_color"] == "#123456"
    assert data["tenant"]["branding"]["greeting"] == "Hello there!"


def test_widget_config_unknown_and_missing_key(client):
    assert client.get("/api/widget/config").status_code == 401
    resp = client.get("/api/widget/config",
                      headers={"X-Hub-Public-Key": "pk_nope"})
    assert resp.status_code == 404


def test_widget_cors_reflects_tenant_origins(client, hub, admin, monkeypatch):
    _, token = admin
    tenant = hub.create_tenant(token, slug="corsy", name="Corsy",
                               allowed_origins=["https://ok.example"])

    async def fake_allowed(_request):
        return ["https://ok.example"]

    monkeypatch.setattr(server_module, "_widget_origin_allowed", fake_allowed)
    headers = {**hub.widget_headers(tenant), "Origin": "https://ok.example"}
    resp = client.get("/api/widget/config", headers=headers)
    assert resp.headers.get("access-control-allow-origin") == "https://ok.example"

    headers = {**hub.widget_headers(tenant), "Origin": "https://evil.example"}
    resp = client.get("/api/widget/config", headers=headers)
    assert "access-control-allow-origin" not in resp.headers


def test_knowledge_crud_with_operator_token(client, hub, admin, tenant):
    _, token = admin
    entry = hub.add_kb(token, tenant["_id"], "Hours", "Open 8-6.", ["hours"])
    resp = client.get(f"/api/tenants/{tenant['_id']}/knowledge",
                      headers=hub.bearer(token))
    assert [e["_id"] for e in resp.json()["entries"]] == [entry["_id"]]
    resp = client.delete(f"/api/tenants/{tenant['_id']}/knowledge/{entry['_id']}",
                         headers=hub.bearer(token))
    assert resp.status_code == 200
    resp = client.get(f"/api/tenants/{tenant['_id']}/knowledge",
                      headers=hub.bearer(token))
    assert resp.json()["entries"] == []


def test_knowledge_with_tenant_secret_key(client, hub, admin, tenant):
    secret = tenant["secret_key"]
    resp = client.post(
        f"/api/tenants/{tenant['_id']}/knowledge",
        json={"title": "Refunds", "content": "30 days.", "tags": ["refund"]},
        headers={"X-Hub-Secret-Key": secret},
    )
    assert resp.status_code == 201
    resp = client.post(
        f"/api/tenants/{tenant['_id']}/knowledge",
        json={"title": "X", "content": "Y", "tags": []},
        headers={"X-Hub-Secret-Key": "sk_wrong"},
    )
    assert resp.status_code == 401


def test_secret_rotation(client, hub, admin, tenant):
    _, token = admin
    old_secret = tenant["secret_key"]
    resp = client.post(f"/api/tenants/{tenant['_id']}/rotate-secret",
                       headers=hub.bearer(token))
    assert resp.status_code == 200
    new_secret = resp.json()["secret_key"]
    assert new_secret != old_secret
    resp = client.get(f"/api/tenants/{tenant['_id']}/knowledge",
                      headers={"X-Hub-Secret-Key": old_secret})
    assert resp.status_code == 401
    resp = client.get(f"/api/tenants/{tenant['_id']}/knowledge",
                      headers={"X-Hub-Secret-Key": new_secret})
    assert resp.status_code == 200
