"""v2 section D: assignment/routing, availability, notes, macros, attachments."""
import base64


def _make_agent(client, hub, admin_token, tenant_id, email):
    resp = client.post(
        "/api/auth/operators",
        json={"email": email, "name": email.split("@")[0], "password": "agent-" + "pw-123",
              "role": "agent", "tenant_ids": [tenant_id]},
        headers=hub.bearer(admin_token))
    assert resp.status_code == 201, resp.text
    operator = resp.json()["operator"]
    token = hub.login(email, "agent-pw-123")
    return operator, token


def _escalate(hub, tenant, text="I need to speak to a human about this"):
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    result = hub.say(tenant, conv_id, text)
    assert result["escalated"] is True
    return conv_id


def test_auto_assign_round_robin_skips_unavailable(client, hub, admin, tenant):
    _, token = admin
    agent1, agent1_token = _make_agent(client, hub, token, tenant["_id"], "a1@t.local")
    agent2, _ = _make_agent(client, hub, token, tenant["_id"], "a2@t.local")
    # Take the admin out of rotation so the two agents alternate.
    resp = client.post("/api/operator/me/availability",
                       json={"available": False}, headers=hub.bearer(token))
    assert resp.status_code == 200

    conv1 = _escalate(hub, tenant)
    resp = client.get(f"/api/operator/conversations/{conv1}",
                      headers=hub.bearer(token))
    first_assignee = resp.json()["conversation"]["assigned_operator_id"]
    assert first_assignee in (agent1["_id"], agent2["_id"])

    conv2 = _escalate(hub, tenant)
    resp = client.get(f"/api/operator/conversations/{conv2}",
                      headers=hub.bearer(token))
    second_assignee = resp.json()["conversation"]["assigned_operator_id"]
    assert second_assignee in (agent1["_id"], agent2["_id"])
    assert second_assignee != first_assignee  # round-robin: least loaded wins

    # agent1 goes unavailable → everything new routes to agent2.
    client.post("/api/operator/me/availability", json={"available": False},
                headers=hub.bearer(agent1_token))
    conv3 = _escalate(hub, tenant)
    resp = client.get(f"/api/operator/conversations/{conv3}",
                      headers=hub.bearer(token))
    assert resp.json()["conversation"]["assigned_operator_id"] == agent2["_id"]


def test_auto_assign_can_be_disabled_per_tenant(client, hub, admin):
    _, token = admin
    quiet = hub.create_tenant(token, slug="quiet", name="Quiet Co",
                              auto_assign=False)
    conv_id = _escalate(hub, quiet)
    resp = client.get(f"/api/operator/conversations/{conv_id}",
                      headers=hub.bearer(token))
    assert resp.json()["conversation"]["assigned_operator_id"] is None


def test_manual_reassign_and_validation(client, hub, admin, tenant):
    _, token = admin
    agent1, _ = _make_agent(client, hub, token, tenant["_id"], "r1@t.local")
    conv_id = _escalate(hub, tenant)
    resp = client.post(f"/api/operator/conversations/{conv_id}/assign",
                       json={"operator_id": agent1["_id"]},
                       headers=hub.bearer(token))
    assert resp.status_code == 200
    assert resp.json()["conversation"]["assigned_operator_id"] == agent1["_id"]

    resp = client.post(f"/api/operator/conversations/{conv_id}/assign",
                       json={"operator_id": "op_missing"},
                       headers=hub.bearer(token))
    assert resp.status_code == 404

    outsider, _ = _make_agent(client, hub, token, "ten_elsewhere", "r2@t.local")
    resp = client.post(f"/api/operator/conversations/{conv_id}/assign",
                       json={"operator_id": outsider["_id"]},
                       headers=hub.bearer(token))
    assert resp.status_code == 422  # no access to this tenant


def test_internal_notes_never_reach_the_customer(client, hub, admin, tenant):
    _, token = admin
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    resp = client.post(f"/api/operator/conversations/{conv_id}/notes",
                       json={"text": "Customer is a VIP — handle with care."},
                       headers=hub.bearer(token))
    assert resp.status_code == 201
    resp = client.get(f"/api/operator/conversations/{conv_id}/notes",
                      headers=hub.bearer(token))
    assert len(resp.json()["notes"]) == 1

    # The widget view of the same conversation has no trace of the note.
    resp = client.get(f"/api/widget/conversations/{conv_id}/messages",
                      headers=hub.widget_headers(tenant))
    texts = " ".join(m["text"] for m in resp.json()["messages"])
    assert "VIP" not in texts
    # …but the operator detail includes notes.
    resp = client.get(f"/api/operator/conversations/{conv_id}",
                      headers=hub.bearer(token))
    assert resp.json()["notes"][0]["text"].startswith("Customer is a VIP")


def test_macros_crud_and_tenant_scope(client, hub, admin, tenant):
    _, token = admin
    resp = client.post(f"/api/tenants/{tenant['_id']}/macros",
                       json={"name": "Greeting", "body": "Hi! Thanks for writing in."},
                       headers=hub.bearer(token))
    assert resp.status_code == 201
    macro_id = resp.json()["macro"]["_id"]
    resp = client.get(f"/api/tenants/{tenant['_id']}/macros",
                      headers=hub.bearer(token))
    assert [m["name"] for m in resp.json()["macros"]] == ["Greeting"]

    other = hub.create_tenant(token, slug="other", name="Other Co")
    _, other_agent = hub.make_agent([other["_id"]], email="om@t.local")
    resp = client.get(f"/api/tenants/{tenant['_id']}/macros",
                      headers=hub.bearer(other_agent))
    assert resp.status_code == 403

    resp = client.delete(f"/api/tenants/{tenant['_id']}/macros/{macro_id}",
                         headers=hub.bearer(token))
    assert resp.json()["deleted"] is True


def test_operator_attachment_upload_and_scoped_download(client, hub, admin, tenant):
    _, token = admin
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    payload = base64.b64encode(b"invoice-bytes").decode()
    resp = client.post(
        f"/api/operator/conversations/{conv_id}/attachments",
        json={"filename": "invoice.pdf", "content_type": "application/pdf",
              "data_base64": payload},
        headers=hub.bearer(token))
    assert resp.status_code == 201, resp.text
    attachment = resp.json()["attachment"]
    assert attachment["filename"] == "invoice.pdf"
    assert "storage_path" not in attachment

    resp = client.get(f"/api/attachments/{attachment['_id']}",
                      headers=hub.bearer(token))
    assert resp.status_code == 200
    assert resp.content == b"invoice-bytes"

    # Disallowed type is refused.
    resp = client.post(
        f"/api/operator/conversations/{conv_id}/attachments",
        json={"filename": "evil.exe", "data_base64": payload},
        headers=hub.bearer(token))
    assert resp.status_code == 422
