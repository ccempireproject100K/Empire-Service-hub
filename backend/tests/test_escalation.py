"""One test per escalation rule + the archive record each escalation writes."""


def _tenant_with_kb(hub, admin, **tenant_overrides):
    _, token = admin
    tenant = hub.create_tenant(token, **tenant_overrides)
    hub.add_kb(token, tenant["_id"], "Opening hours",
               "We are open Monday to Friday, 8am to 6pm Central.", ["hours"])
    return tenant, token


def _assert_escalated(client, hub, token, tenant, result, rule):
    assert result["escalated"] is True
    assert result["escalation_reason"] == rule
    assert result["conversation"]["status"] == "escalated"
    resp = client.get(f"/api/archive?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    records = resp.json()["records"]
    assert len(records) == 1
    assert records[0]["reason"]["rule"] == rule
    assert records[0]["status"] == "open"
    assert records[0]["resolution"] is None
    assert records[0]["transcript"], "archive must snapshot the transcript"


def test_escalation_on_negative_sentiment(client, hub, admin):
    tenant, token = _tenant_with_kb(hub, admin)
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    result = hub.say(tenant, conv_id,
                     "This is terrible, awful, the worst. I am furious and "
                     "frustrated, everything is broken and useless.")
    _assert_escalated(client, hub, token, tenant, result, "sentiment")


def test_escalation_on_low_confidence(client, hub, admin):
    tenant, token = _tenant_with_kb(hub, admin)
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    # Nothing in the KB covers this; demo AI confidence collapses to 0.
    result = hub.say(tenant, conv_id,
                     "How do I expense a helicopter for my zeppelin crew?")
    _assert_escalated(client, hub, token, tenant, result, "low_confidence")


def test_escalation_on_customer_request(client, hub, admin):
    tenant, token = _tenant_with_kb(hub, admin)
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    result = hub.say(tenant, conv_id,
                     "What are your opening hours? Also, I want to talk to a "
                     "representative about my account.")
    _assert_escalated(client, hub, token, tenant, result, "customer_request")


def test_escalation_on_tenant_keyword(client, hub, admin):
    tenant, token = _tenant_with_kb(hub, admin,
                                    escalation_keywords=["chargeback"])
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    result = hub.say(tenant, conv_id, "I will file a chargeback tomorrow.")
    _assert_escalated(client, hub, token, tenant, result, "keyword")


def test_happy_question_does_not_escalate(client, hub, admin):
    tenant, token = _tenant_with_kb(hub, admin)
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    result = hub.say(tenant, conv_id, "What are your opening hours?")
    assert result["escalated"] is False
    resp = client.get(f"/api/archive?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    assert resp.json()["records"] == []
