"""v2 sections C+G: proactive widget config, sensitive topics, language
detection, customer memory, widget attachments."""
import base64


def test_widget_config_exposes_proactive_triggers_and_banner(client, hub, admin):
    _, token = admin
    tenant = hub.create_tenant(
        token, slug="pro", name="Pro Co",
        proactive_triggers=[
            {"type": "time_on_page", "seconds": 45,
             "message": "Stuck? We can help."},
            {"type": "page_path", "path": "/pricing",
             "message": "Questions about pricing?"},
        ],
        status_banner={"active": True, "text": "Scheduled maintenance Sunday 2am."})
    resp = client.get("/api/widget/config", headers=hub.widget_headers(tenant))
    proactive = resp.json()["proactive"]
    assert proactive["triggers"][0]["type"] == "time_on_page"
    assert proactive["triggers"][1]["path"] == "/pricing"
    assert proactive["status_banner"]["text"].startswith("Scheduled maintenance")


def test_sensitive_topic_escalates_regardless_of_confidence(client, hub, admin):
    _, token = admin
    tenant = hub.create_tenant(
        token, slug="care", name="Care Co",
        sensitive_topics=[{"name": "medical", "keywords": ["diagnosis"]}])
    hub.add_kb(token, tenant["_id"], "Diagnosis information",
               "A diagnosis is provided by your clinician after an exam.",
               ["diagnosis"])
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    result = hub.say(tenant, conv_id, "Can you give me a diagnosis for this rash?")
    # The KB match is strong (high confidence) — the topic rule still wins.
    assert result["escalated"] is True
    assert result["escalation_reason"] == "sensitive_topic"


def test_language_detection_switches_reply_language(client, hub, admin):
    _, token = admin
    tenant = hub.create_tenant(token, slug="poly", name="Poly Co",
                               languages=["en", "es"])
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    hub.say(tenant, conv_id, "Hola, quiero saber el precio de la factura por favor")
    resp = client.get(f"/api/operator/conversations/{conv_id}",
                      headers=hub.bearer(token))
    assert resp.json()["conversation"]["language"] == "es"


def test_language_detection_bounded_by_tenant_languages(client, hub, admin, tenant):
    _, token = admin
    opened = hub.open_conversation(tenant)  # default tenant speaks only en
    conv_id = opened["conversation"]["_id"]
    hub.say(tenant, conv_id, "Hola, quiero saber el precio de la factura por favor")
    resp = client.get(f"/api/operator/conversations/{conv_id}",
                      headers=hub.bearer(token))
    assert resp.json()["conversation"]["language"] == "en"


def test_spanish_negative_sentiment_escalates(client, hub, admin):
    _, token = admin
    tenant = hub.create_tenant(token, slug="es", name="ES Co", languages=["en", "es"])
    opened = hub.open_conversation(tenant)
    result = hub.say(tenant, opened["conversation"]["_id"],
                     "Esto es terrible, estoy muy enojado, quiero un reembolso")
    assert result["escalated"] is True
    assert result["escalation_reason"] in ("sentiment", "customer_request",
                                           "low_confidence")


def test_customer_memory_surfaces_prior_resolution(client, hub, admin, tenant):
    _, token = admin
    opened = hub.open_conversation(tenant, customer_email="sam@example.com",
                                   customer_name="Sam")
    first_id = opened["conversation"]["_id"]
    result = hub.say(tenant, first_id, "My export is broken, speak to a human")
    assert result["escalated"] is True
    client.post(f"/api/operator/conversations/{first_id}/resolve",
                json={"resolution": "Rebuilt the export with the new template."},
                headers=hub.bearer(token))

    opened = hub.open_conversation(tenant, customer_email="sam@example.com")
    second_id = opened["conversation"]["_id"]
    hub.say(tenant, second_id, "The export is broken again")
    resp = client.get(f"/api/operator/conversations/{second_id}",
                      headers=hub.bearer(token))
    history = resp.json()["customer_history"]
    assert len(history) == 1
    assert history[0]["conversation_id"] == first_id
    assert "Rebuilt the export" in (history[0]["resolution"] or "")

    # Same email, different tenant → no shared memory.
    other = hub.create_tenant(token, slug="other", name="Other Co")
    opened = hub.open_conversation(other, customer_email="sam@example.com")
    resp = client.get(f"/api/operator/conversations/{opened['conversation']['_id']}",
                      headers=hub.bearer(token))
    assert resp.json()["customer_history"] == []


def test_widget_attachment_flow_and_limits(client, hub, admin, tenant, monkeypatch):
    opened = hub.open_conversation(tenant)
    conv_id = opened["conversation"]["_id"]
    data = base64.b64encode(b"screenshot-bytes").decode()
    resp = client.post(
        f"/api/widget/conversations/{conv_id}/attachments",
        json={"filename": "error shot.png", "content_type": "image/png",
              "data_base64": data},
        headers=hub.widget_headers(tenant))
    assert resp.status_code == 201, resp.text
    attachment = resp.json()["attachment"]
    assert attachment["filename"] == "error_shot.png"  # sanitized

    result = hub.say(tenant, conv_id, "Here is the error I see")
    # Attach properly via a second message carrying the attachment id.
    resp = client.post(
        f"/api/widget/conversations/{conv_id}/messages",
        json={"text": "And the screenshot", "attachment_ids": [attachment["_id"]]},
        headers=hub.widget_headers(tenant))
    assert resp.status_code == 200
    customer_msgs = [m for m in resp.json()["messages"] if m["role"] == "customer"]
    assert customer_msgs[0]["attachments"][0]["filename"] == "error_shot.png"

    # Download works with the owning tenant's key, not another tenant's.
    resp = client.get(f"/api/attachments/{attachment['_id']}/widget",
                      headers=hub.widget_headers(tenant))
    assert resp.status_code == 200 and resp.content == b"screenshot-bytes"
    _, token = admin
    other = hub.create_tenant(token, slug="other2", name="Other 2")
    resp = client.get(f"/api/attachments/{attachment['_id']}/widget",
                      headers=hub.widget_headers(other))
    assert resp.status_code == 404

    # Bad base64 and oversize uploads are refused.
    resp = client.post(
        f"/api/widget/conversations/{conv_id}/attachments",
        json={"filename": "x.txt", "data_base64": "!!!not-base64!!!"},
        headers=hub.widget_headers(tenant))
    assert resp.status_code == 422
    monkeypatch.setenv("HUB_MAX_ATTACHMENT_MB", "0.000001")
    resp = client.post(
        f"/api/widget/conversations/{conv_id}/attachments",
        json={"filename": "big.txt",
              "data_base64": base64.b64encode(b"x" * 500).decode()},
        headers=hub.widget_headers(tenant))
    assert resp.status_code == 422


def test_ai_reply_pii_is_masked_by_guardrail(client, hub, admin, tenant, monkeypatch):
    _, token = admin
    from lib import ai as ai_lib

    hub.add_kb(token, tenant["_id"], "Contacting support",
               "Reach us any time and we will help with your account.",
               ["support", "contact", "help"])

    def leaky_reply(tenant_, entries, confidence):
        return "Sure — I see your email is sam@example.com and we can help."

    monkeypatch.setattr(ai_lib, "_demo_reply", leaky_reply)
    opened = hub.open_conversation(tenant)
    result = hub.say(tenant, opened["conversation"]["_id"],
                     "I need support contact help")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    assert ai_texts and "sam@example.com" not in ai_texts[0]
    assert "[email]" in ai_texts[0]
