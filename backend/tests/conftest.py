"""Test fixtures: an in-memory Motor-style fake DB + API helpers.

The sandbox has no MongoDB, so tests run against a fake implementing the
exact Motor subset the app uses (find/find_one/insert_one/update_one/
delete_one/count_documents). Tenant isolation assertions therefore exercise
the real query filters, not mocks of the app itself.
"""
import asyncio
import copy
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("HUB_JWT_SECRET", "test-secret")
os.environ.pop("GEMINI_API_KEY", None)  # force demo AI mode in tests
os.environ.pop("OPENAI_API_KEY", None)
os.environ["HUB_SCHEDULER"] = "off"  # sweeps are invoked directly in tests
for _var in ("SMTP_HOST", "SMTP_FROM", "TWILIO_ACCOUNT_SID",
             "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "HUB_CHANNEL_SECRET"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db as db_module  # noqa: E402
from lib import auth, ratelimit  # noqa: E402
from lib.util import new_id  # noqa: E402
from server import app  # noqa: E402


def _matches(doc: dict, filter: dict | None) -> bool:
    for key, cond in (filter or {}).items():
        val = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$in":
                    if val not in operand:
                        return False
                elif op == "$ne":
                    if val == operand:
                        return False
                elif op == "$exists":
                    if (key in doc) != bool(operand):
                        return False
                else:
                    raise ValueError(f"unsupported operator {op}")
        elif val != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs = sorted(
            self._docs,
            key=lambda d: (d.get(key) is None, d.get(key)),
            reverse=direction < 0,
        )
        return self

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        return SimpleNamespace(inserted_id=doc.get("_id"))

    async def find_one(self, filter=None):
        for doc in self.docs:
            if _matches(doc, filter):
                return copy.deepcopy(doc)
        return None

    def find(self, filter=None):
        return FakeCursor([d for d in self.docs if _matches(d, filter)])

    async def update_one(self, filter, update, upsert=False):
        for doc in self.docs:
            if _matches(doc, filter):
                for key, value in (update.get("$set") or {}).items():
                    doc[key] = copy.deepcopy(value)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, filter):
        for i, doc in enumerate(self.docs):
            if _matches(doc, filter):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def count_documents(self, filter):
        return sum(1 for d in self.docs if _matches(d, filter))


class FakeDB:
    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._collections.setdefault(name, FakeCollection())

    async def command(self, _name):
        return {"ok": 1}


@pytest.fixture()
def fake_db():
    return FakeDB()


@pytest.fixture()
def client(fake_db):
    app.dependency_overrides[db_module.get_db] = lambda: fake_db
    ratelimit.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    ratelimit.reset()


# ------------------------------------------------------------------- helpers
async def _insert_operator(fake_db, email, password, role, tenant_ids=None, name="Op"):
    doc = {
        "_id": new_id("op_"),
        "email": email,
        "name": name,
        "password_hash": auth.hash_password(password),
        "role": role,
        "tenant_ids": tenant_ids or [],
    }
    await fake_db[db_module.OPERATORS].insert_one(doc)
    return doc


class Hub:
    """Small fluent helper over the API for readable tests."""

    def __init__(self, client, fake_db):
        self.client = client
        self.db = fake_db

    # -- operators ------------------------------------------------------
    def make_admin(self, email="admin@test.local", password="admin-pass"):
        doc = asyncio.run(
            _insert_operator(self.db, email, password, "admin", name="Admin")
        )
        token = self.login(email, password)
        return doc, token

    def make_agent(self, tenant_ids, email="agent@test.local", password="agent-pass"):
        doc = asyncio.run(
            _insert_operator(self.db, email, password, "agent",
                             tenant_ids=tenant_ids, name="Agent")
        )
        token = self.login(email, password)
        return doc, token

    def login(self, email, password):
        resp = self.client.post("/api/auth/login",
                                json={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    # -- tenants ----------------------------------------------------------
    def create_tenant(self, token, slug="books", name="Books Co", **overrides):
        body = {"slug": slug, "name": name}
        body.update(overrides)
        resp = self.client.post("/api/tenants", json=body,
                                headers=self.bearer(token))
        assert resp.status_code == 201, resp.text
        return resp.json()["tenant"]

    def add_kb(self, token, tenant_id, title, content, tags=None):
        resp = self.client.post(
            f"/api/tenants/{tenant_id}/knowledge",
            json={"title": title, "content": content, "tags": tags or []},
            headers=self.bearer(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["entry"]

    # -- widget -----------------------------------------------------------
    @staticmethod
    def widget_headers(tenant):
        return {"X-Hub-Public-Key": tenant["public_key"]}

    def open_conversation(self, tenant, **kwargs):
        resp = self.client.post("/api/widget/conversations", json=kwargs,
                                headers=self.widget_headers(tenant))
        assert resp.status_code == 201, resp.text
        return resp.json()

    def say(self, tenant, conversation_id, text):
        resp = self.client.post(
            f"/api/widget/conversations/{conversation_id}/messages",
            json={"text": text}, headers=self.widget_headers(tenant))
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.fixture()
def hub(client, fake_db):
    return Hub(client, fake_db)


@pytest.fixture()
def admin(hub):
    return hub.make_admin()


@pytest.fixture()
def tenant(hub, admin):
    _, token = admin
    return hub.create_tenant(token)
