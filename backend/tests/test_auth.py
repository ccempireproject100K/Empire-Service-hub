"""Operator auth: login, JWT protection, admin-only operations."""


def test_login_success_and_me(client, hub, admin):
    _, token = admin
    resp = client.get("/api/auth/me", headers=hub.bearer(token))
    assert resp.status_code == 200
    operator = resp.json()["operator"]
    assert operator["email"] == "admin@test.local"
    assert operator["role"] == "admin"
    assert "password_hash" not in operator


def test_login_wrong_password(client, hub):
    hub.make_admin()
    resp = client.post("/api/auth/login",
                       json={"email": "admin@test.local", "password": "nope"})
    assert resp.status_code == 401


def test_login_unknown_email(client):
    resp = client.post("/api/auth/login",
                       json={"email": "ghost@test.local", "password": "x"})
    assert resp.status_code == 401


def test_protected_endpoint_requires_token(client):
    assert client.get("/api/tenants").status_code == 401
    assert client.get("/api/operator/conversations").status_code == 401
    resp = client.get("/api/tenants", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


def test_agent_cannot_create_tenant(client, hub, admin, tenant):
    _, agent_token = hub.make_agent([tenant["_id"]])
    resp = client.post("/api/tenants", json={"slug": "nope", "name": "Nope"},
                       headers=hub.bearer(agent_token))
    assert resp.status_code == 403


def test_admin_creates_operator_and_duplicate_rejected(client, hub, admin):
    _, token = admin
    body = {"email": "second@test.local", "name": "Second",
            "password": "pw-12345", "role": "agent", "tenant_ids": []}
    resp = client.post("/api/auth/operators", json=body, headers=hub.bearer(token))
    assert resp.status_code == 201
    assert "password_hash" not in resp.json()["operator"]
    resp = client.post("/api/auth/operators", json=body, headers=hub.bearer(token))
    assert resp.status_code == 409
    # The new operator can log in.
    assert hub.login("second@test.local", "pw-12345")
