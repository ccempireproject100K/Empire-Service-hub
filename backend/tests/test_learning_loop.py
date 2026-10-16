"""v2 section A: the closed learning loop — drafts, similar cases, metrics."""
import asyncio

import db as db_module


def _escalate_and_resolve(hub, client, tenant, token, question, resolution):
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    result = hub.say(tenant, conv_id, question + " — I want to speak to a human")
    assert result["escalated"] is True
    resp = client.post(
        f"/api/operator/conversations/{conv_id}/resolve",
        json={"resolution": resolution}, headers=hub.bearer(token))
    assert resp.status_code == 200, resp.text
    return conv_id, resp.json()


def test_resolution_creates_kb_draft_and_approval_publishes(client, hub, admin, tenant):
    _, token = admin
    conv_id, payload = _escalate_and_resolve(
        hub, client, tenant, token,
        "My invoice total looks wrong",
        "Reissued the invoice with the correct tax rate applied.")
    assert payload["kb_draft"] is not None
    assert payload["kb_draft"]["status"] == "pending"
    assert payload["kb_draft"]["source"] == "template"  # demo mode

    resp = client.get(f"/api/tenants/{tenant['_id']}/kb-drafts",
                      headers=hub.bearer(token))
    drafts = resp.json()["drafts"]
    assert len(drafts) == 1
    draft = drafts[0]
    assert "invoice" in draft["question"].lower()
    assert "Reissued the invoice" in draft["content"]

    # Edit then approve → publishes a live KB entry.
    resp = client.patch(
        f"/api/tenants/{tenant['_id']}/kb-drafts/{draft['_id']}",
        json={"title": "Invoice total looks wrong"},
        headers=hub.bearer(token))
    assert resp.status_code == 200
    resp = client.post(
        f"/api/tenants/{tenant['_id']}/kb-drafts/{draft['_id']}/approve",
        headers=hub.bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["entry"]["title"] == "Invoice total looks wrong"

    resp = client.get(f"/api/tenants/{tenant['_id']}/knowledge",
                      headers=hub.bearer(token))
    titles = [e["title"] for e in resp.json()["entries"]]
    assert "Invoice total looks wrong" in titles

    # Decided drafts leave the pending queue and can't be re-decided.
    resp = client.get(f"/api/tenants/{tenant['_id']}/kb-drafts",
                      headers=hub.bearer(token))
    assert resp.json()["drafts"] == []
    resp = client.post(
        f"/api/tenants/{tenant['_id']}/kb-drafts/{draft['_id']}/approve",
        headers=hub.bearer(token))
    assert resp.status_code == 409


def test_draft_discard(client, hub, admin, tenant):
    _, token = admin
    _, payload = _escalate_and_resolve(
        hub, client, tenant, token, "The export button spins forever",
        "Cleared the stuck export job and re-ran it.")
    draft_id = payload["kb_draft"]["_id"]
    resp = client.post(
        f"/api/tenants/{tenant['_id']}/kb-drafts/{draft_id}/discard",
        headers=hub.bearer(token))
    assert resp.json()["status"] == "discarded"
    resp = client.get(f"/api/tenants/{tenant['_id']}/knowledge",
                      headers=hub.bearer(token))
    assert resp.json()["entries"] == []


def test_no_draft_when_never_escalated(client, hub, admin, tenant):
    _, token = admin
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    resp = client.post(
        f"/api/operator/conversations/{conv_id}/resolve",
        json={"resolution": "Nothing to fix."}, headers=hub.bearer(token))
    assert resp.json()["kb_draft"] is None
    resp = client.get(
        f"/api/tenants/{tenant['_id']}/kb-drafts?status=",
        headers=hub.bearer(token))
    assert resp.json()["drafts"] == []


def test_similar_cases_surface_for_operator(client, hub, admin, tenant):
    _, token = admin
    _escalate_and_resolve(
        hub, client, tenant, token,
        "I need a refund for my duplicate billing charge",
        "Refunded the duplicate charge to the original card.")
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    hub.say(tenant, conv_id, "Please refund the duplicate billing charge on my account")

    resp = client.get(f"/api/operator/conversations/{conv_id}",
                      headers=hub.bearer(token))
    cases = resp.json()["similar_cases"]
    assert cases, "expected a similar past case"
    assert "refund" in cases[0]["question"].lower()
    assert "Refunded the duplicate charge" in (cases[0]["resolution"] or "")


def test_learning_metrics_deflection_and_repeat(client, hub, admin, tenant):
    _, token = admin
    hub.add_kb(token, tenant["_id"], "Opening hours",
               "We are open Monday to Friday, 8am to 6pm Central.",
               ["hours", "opening"])

    # Conversation 1: answered by AI, resolved without escalation.
    opened = hub.open_conversation(tenant, customer_email="sam@example.com")
    conv1 = opened["conversation"]["_id"]
    hub.say(tenant, conv1, "What are your opening hours?")
    resp = client.post(f"/api/operator/conversations/{conv1}/resolve",
                       json={"resolution": "Confirmed hours."},
                       headers=hub.bearer(token))
    assert resp.status_code == 200

    # Conversation 2: same customer returns and escalates.
    opened = hub.open_conversation(tenant, customer_email="sam@example.com")
    conv2 = opened["conversation"]["_id"]
    result = hub.say(tenant, conv2, "This is broken, I want to speak to a human")
    assert result["escalated"] is True
    client.post(f"/api/operator/conversations/{conv2}/resolve",
                json={"resolution": "Fixed the broken sync."},
                headers=hub.bearer(token))

    resp = client.get(f"/api/operator/analytics?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    metrics = resp.json()["learning"]
    assert metrics["conversations"] == 2
    assert metrics["escalated"] == 1
    assert metrics["ai_resolved"] == 1
    assert metrics["deflection_rate"] == 0.5
    assert metrics["repeat_customers"] == 1
    assert metrics["repeat_issue_rate"] == 1.0
    assert metrics["trend"], "expected a daily trend bucket"

    # Stats endpoint carries the same learning block for the console header.
    resp = client.get(f"/api/operator/stats?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    assert resp.json()["learning"]["deflection_rate"] == 0.5


def test_drafts_are_tenant_scoped(client, hub, admin, tenant):
    _, token = admin
    _escalate_and_resolve(
        hub, client, tenant, token, "Portal login fails every time",
        "Reset the customer's portal credentials.")
    _, agent_token = hub.make_agent([tenant["_id"]], email="a1@test.local")
    other = hub.create_tenant(token, slug="other", name="Other Co")
    _, other_agent_token = hub.make_agent([other["_id"]], email="a2@test.local")
    resp = client.get(f"/api/tenants/{tenant['_id']}/kb-drafts",
                      headers=hub.bearer(other_agent_token))
    assert resp.status_code == 403
    resp = client.get(f"/api/tenants/{tenant['_id']}/kb-drafts",
                      headers=hub.bearer(agent_token))
    assert resp.status_code == 200
    assert len(resp.json()["drafts"]) == 1
    asyncio.run(_assert_no_cross_tenant_drafts(hub))


async def _assert_no_cross_tenant_drafts(hub):
    drafts = await hub.db[db_module.KB_DRAFTS].find({}).to_list(length=10)
    assert all(d["tenant_id"] == drafts[0]["tenant_id"] for d in drafts)
