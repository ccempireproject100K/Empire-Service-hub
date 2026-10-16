"""Conversation lifecycle: widget open -> AI reply -> operator resolve."""


def _kb_tenant(hub, admin):
    _, token = admin
    tenant = hub.create_tenant(token)
    hub.add_kb(token, tenant["_id"], "Opening hours",
               "We are open Monday to Friday, 8am to 6pm Central.",
               ["hours", "opening"])
    return tenant, token


def test_open_conversation_greets_with_tenant_branding(client, hub, admin):
    tenant, _ = _kb_tenant(hub, admin)
    opened = hub.open_conversation(tenant, customer_name="Sam")
    conversation = opened["conversation"]
    assert conversation["tenant_id"] == tenant["_id"]
    assert conversation["status"] in ("open", "ai_handling")
    assert opened["demo"] is True
    assert opened["messages"][0]["role"] == "ai"
    assert "How can we help" in opened["messages"][0]["text"]


def test_ai_answers_from_knowledge_base(client, hub, admin):
    tenant, _ = _kb_tenant(hub, admin)
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    result = hub.say(tenant, conv_id, "What are your opening hours?")
    assert result["escalated"] is False
    ai_messages = [m for m in result["messages"] if m["role"] == "ai"]
    assert len(ai_messages) == 1
    assert "Opening hours" in ai_messages[0]["text"]
    assert "8am to 6pm" in ai_messages[0]["text"]
    assert ai_messages[0]["demo"] is True
    assert ai_messages[0]["confidence"] >= 0.55
    assert result["conversation"]["status"] == "ai_handling"


def test_demo_ai_is_deterministic(client, hub, admin):
    tenant, _ = _kb_tenant(hub, admin)
    replies = []
    for _ in range(2):
        opened = hub.open_conversation(tenant)
        result = hub.say(tenant, opened["conversation"]["_id"],
                         "What are your opening hours?")
        replies.append([m["text"] for m in result["messages"] if m["role"] == "ai"][0])
    assert replies[0] == replies[1]


def test_operator_takeover_reply_resolve(client, hub, admin):
    tenant, token = _kb_tenant(hub, admin)
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    hub.say(tenant, conv_id, "What are your opening hours?")

    # Operator sees it in the list with a summary.
    resp = client.get(f"/api/operator/conversations?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    assert resp.status_code == 200
    listed = resp.json()["conversations"]
    assert len(listed) == 1 and listed[0]["_id"] == conv_id
    assert listed[0]["message_count"] >= 3

    resp = client.post(f"/api/operator/conversations/{conv_id}/takeover",
                       headers=hub.bearer(token))
    assert resp.json()["conversation"]["status"] == "operator_handling"

    resp = client.post(f"/api/operator/conversations/{conv_id}/reply",
                       json={"text": "Hi Sam, anything else I can check?"},
                       headers=hub.bearer(token))
    assert resp.status_code == 200
    assert resp.json()["message"]["role"] == "operator"

    # While a human owns it, the widget gets no AI reply.
    result = hub.say(tenant, conv_id, "What are your opening hours?")
    assert [m["role"] for m in result["messages"]] == ["customer"]

    resp = client.post(f"/api/operator/conversations/{conv_id}/resolve",
                       json={"resolution": "Confirmed hours for the customer."},
                       headers=hub.bearer(token))
    assert resp.status_code == 200
    assert resp.json()["conversation"]["status"] == "resolved"
    assert resp.json()["csat_prompt"] is True
    assert resp.json()["archive"] is None  # never escalated

    # Resolved conversations reject new messages and double-resolution.
    resp = client.post(f"/api/widget/conversations/{conv_id}/messages",
                       json={"text": "hello again"},
                       headers=hub.widget_headers(tenant))
    assert resp.status_code == 409
    resp = client.post(f"/api/operator/conversations/{conv_id}/resolve",
                       json={"resolution": "again"}, headers=hub.bearer(token))
    assert resp.status_code == 409
