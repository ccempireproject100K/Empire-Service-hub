"""The learning loop: escalation -> human resolution -> exportable corpus."""
import json


def _escalated_conversation(hub, admin):
    _, token = admin
    tenant = hub.create_tenant(token, escalation_keywords=["chargeback"])
    conv_id = hub.open_conversation(tenant)["conversation"]["_id"]
    hub.say(tenant, conv_id,
            "I will file a chargeback. Reach me at jane.doe@example.com "
            "or 512-555-0142 about order 998877665.")
    return tenant, token, conv_id


def test_resolution_completes_archive_record(client, hub, admin):
    tenant, token, conv_id = _escalated_conversation(hub, admin)
    resp = client.post(f"/api/operator/conversations/{conv_id}/resolve",
                       json={"resolution": "Refunded in full and confirmed by email."},
                       headers=hub.bearer(token))
    assert resp.status_code == 200
    record = resp.json()["archive"]
    assert record is not None
    assert record["status"] == "resolved"
    assert record["resolution"]["text"].startswith("Refunded in full")
    assert record["resolution"]["resolved_by"] == "Admin"


def test_archive_transcript_is_pii_masked(client, hub, admin):
    tenant, token, conv_id = _escalated_conversation(hub, admin)
    resp = client.get(f"/api/archive?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    record = resp.json()["records"][0]
    blob = json.dumps(record)
    assert "jane.doe@example.com" not in blob
    assert "512-555-0142" not in blob
    assert "[email]" in blob and "[phone]" in blob


def test_jsonl_export(client, hub, admin):
    tenant, token, conv_id = _escalated_conversation(hub, admin)
    client.post(f"/api/operator/conversations/{conv_id}/resolve",
                json={"resolution": "Sorted out the duplicate charge."},
                headers=hub.bearer(token))
    resp = client.get(f"/api/archive/export?tenant_id={tenant['_id']}",
                      headers=hub.bearer(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in resp.text.splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["conversation_id"] == conv_id
    assert record["tenant_id"] == tenant["_id"]
    assert record["reason"]["rule"] == "keyword"
    assert record["resolution"]["text"] == "Sorted out the duplicate charge."
    assert isinstance(record["transcript"], list) and record["transcript"]
    # The live conversation keeps the original text; only the archive masks.
    detail = client.get(f"/api/operator/conversations/{conv_id}",
                        headers=hub.bearer(token))
    texts = [m["text"] for m in detail.json()["messages"]]
    assert any("jane.doe@example.com" in t for t in texts)
