"""v2 section B: document ingestion + hybrid (keyword/semantic) retrieval."""
from lib import documents as docs_lib
from lib import embeddings


def _ingest(client, hub, token, tenant_id, **body):
    resp = client.post(f"/api/tenants/{tenant_id}/documents", json=body,
                       headers=hub.bearer(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["document"]


def test_ingested_document_answers_questions(client, hub, admin, tenant):
    _, token = admin
    doc = _ingest(
        client, hub, token, tenant["_id"],
        title="Refund policy details", source_type="txt",
        content="Refunds are issued within 30 days of the request. "
                "Refunds go back to the original payment method.")
    assert doc["chunk_count"] == 1
    assert doc["source_type"] == "upload"

    opened = hub.open_conversation(tenant)
    result = hub.say(tenant, opened["conversation"]["_id"],
                     "How long do refunds take?")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    assert ai_texts and "Refund policy details" in ai_texts[0]


def test_ingest_html_and_csv_are_normalized(client, hub, admin, tenant):
    _, token = admin
    doc = _ingest(
        client, hub, token, tenant["_id"], title="Holiday hours", source_type="html",
        content="<html><body><h1>Holiday hours</h1><script>evil()</script>"
                "<p>We close on December 25 for the holiday.</p></body></html>")
    assert doc["chunk_count"] == 1
    opened = hub.open_conversation(tenant)
    result = hub.say(tenant, opened["conversation"]["_id"],
                     "When do you close for the holiday in December?")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    assert ai_texts and "December 25" in ai_texts[0]
    assert "evil" not in ai_texts[0]

    doc = _ingest(
        client, hub, token, tenant["_id"], title="Plan prices", source_type="csv",
        content="plan,price\nLight,10\nMedium,20\n")
    assert doc["chunk_count"] == 1


def test_ingest_from_url_uses_fetcher(client, hub, admin, tenant, monkeypatch):
    _, token = admin

    async def fake_fetch(url):
        return "Our depot address is 42 Harbor Way. Bring your invoice."

    monkeypatch.setattr(docs_lib, "fetch_url", fake_fetch)
    doc = _ingest(client, hub, token, tenant["_id"],
                  title="Depot directions", url="https://example.com/depot")
    assert doc["source_type"] == "url"
    opened = hub.open_conversation(tenant)
    result = hub.say(tenant, opened["conversation"]["_id"],
                     "What is the depot address?")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    assert ai_texts and "Harbor Way" in ai_texts[0]


def test_semantic_retrieval_with_embeddings(client, hub, admin, tenant, monkeypatch):
    _, token = admin

    def vector_for(text):
        lowered = (text or "").lower()
        if "return" in lowered or "money back" in lowered:
            return [1.0, 0.0]
        return [0.0, 1.0]

    async def fake_embed_texts(texts):
        return [vector_for(t) for t in texts]

    async def fake_embed_one(text):
        return vector_for(text)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(embeddings, "embed_one", fake_embed_one)
    monkeypatch.setattr(embeddings, "available", lambda: True)

    _ingest(client, hub, token, tenant["_id"], title="Returns", source_type="txt",
            content="We return purchase amounts to the original payment "
                    "method within thirty days.")
    opened = hub.open_conversation(tenant)
    result = hub.say(tenant, opened["conversation"]["_id"],
                     "Is there a money back guarantee?")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    # Zero keyword overlap with the document — only the embedding can match.
    assert ai_texts and "thirty days" in ai_texts[0]


def test_documents_are_tenant_scoped_and_deletable(client, hub, admin, tenant):
    _, token = admin
    other = hub.create_tenant(token, slug="other", name="Other Co")
    _ingest(client, hub, token, tenant["_id"], title="Secret playbook",
            source_type="txt",
            content="The secret word is pineapple turnover.")
    # The other tenant's widget cannot retrieve it.
    opened = hub.open_conversation(other)
    result = hub.say(other, opened["conversation"]["_id"],
                     "What is the secret word pineapple turnover?")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    assert not any("Secret playbook" in t for t in ai_texts)

    # An agent of the other tenant cannot list/delete this tenant's docs.
    _, other_agent = hub.make_agent([other["_id"]], email="o@test.local")
    resp = client.get(f"/api/tenants/{tenant['_id']}/documents",
                      headers=hub.bearer(other_agent))
    assert resp.status_code == 403

    resp = client.get(f"/api/tenants/{tenant['_id']}/documents",
                      headers=hub.bearer(token))
    doc_id = resp.json()["documents"][0]["_id"]
    resp = client.delete(f"/api/tenants/{tenant['_id']}/documents/{doc_id}",
                         headers=hub.bearer(token))
    assert resp.json()["deleted"] is True
    opened = hub.open_conversation(tenant)
    result = hub.say(tenant, opened["conversation"]["_id"],
                     "What is the secret word pineapple turnover?")
    ai_texts = [m["text"] for m in result["messages"] if m["role"] == "ai"]
    assert not any("Secret playbook" in t for t in ai_texts)


def test_long_document_is_chunked(client, hub, admin, tenant):
    _, token = admin
    content = ("Paragraph about onboarding exports. " * 80) + \
              "\n\nFinal note: exports finish with a checksum file."
    doc = _ingest(client, hub, token, tenant["_id"], title="Export guide",
                  source_type="txt", content=content)
    assert doc["chunk_count"] > 1
