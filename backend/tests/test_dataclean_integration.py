"""DataClean integration tests (2026-11-05): client contract, mapping
honesty, and the offline suggestion job.

The live API is never called here: the client is exercised through an
injected fake transport, and the suggestion job runs the REAL books
engine in-process (skipped, with the reason, where the engine or its
deps are unavailable in the test environment).
"""
import copy
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import dataclean_client as dc  # noqa: E402
from lib import dataclean_suggest as suggest  # noqa: E402


# --------------------------------------------------------------- fake wire
class FakeTransport:
    """Records calls; answers like the DataClean API would."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "payload": payload})
        if url.endswith("/cleanup/clean"):
            rows = payload["rows"]
            return 200, {
                "records": rows,
                "changes": [{"field": "email", "rule": "email:x"}
                            for _ in rows],
                "changes_by_rule": {"email:x": len(rows)},
            }
        if url.endswith("/cleanup/preview"):
            return 200, {"mess": {}, "schema": {}}
        if url.endswith("/dedupe/scan"):
            return 200, {"pairs": [], "review_pairs": []}
        if url.endswith("/normalize"):
            return 200, {"results": [{"normalized": v}
                                     for v in payload["values"]]}
        return 404, {"detail": "unknown"}


def _client(transport=None, **kw):
    return dc.DataCleanClient(base="https://example.test/api",
                              key="mdck_test", transport=transport or FakeTransport(),
                              enabled_override=True, **kw)


# ------------------------------------------------------------------ client
def test_client_inert_while_disabled(monkeypatch):
    monkeypatch.delenv("DATACLEAN_ENABLED", raising=False)
    client = dc.DataCleanClient(transport=FakeTransport())
    with pytest.raises(dc.DataCleanDisabled):
        client.clean("customers", [{"name": "A"}])
    with pytest.raises(dc.DataCleanDisabled):
        client.dedupe([{"name": "A"}])
    with pytest.raises(dc.DataCleanDisabled):
        client.normalize("phone", ["2145550147"])


def test_client_enabled_but_no_key(monkeypatch):
    monkeypatch.setenv("DATACLEAN_ENABLED", "1")
    monkeypatch.delenv("DATACLEAN_API_KEY", raising=False)
    client = dc.DataCleanClient()  # no transport -> real wire needs a key
    with pytest.raises(dc.DataCleanNotConfigured):
        client.preview("customers", [])


def test_clean_chunks_at_5000_and_aggregates():
    transport = FakeTransport()
    rows = [{"name": f"N{i}", "email": f"n{i}@x.co"} for i in range(5001)]
    out = _client(transport).clean("customers", rows)
    assert len(transport.calls) == 2
    assert [len(c["payload"]["rows"]) for c in transport.calls] == [5000, 1]
    assert out["chunks"] == 2 and len(out["records"]) == 5001
    assert out["changes_by_rule"] == {"email:x": 5001}
    assert transport.calls[0]["headers"]["X-API-Key"] == "mdck_test"
    assert transport.calls[0]["payload"]["kind"] == "customers"


def test_dedupe_within_single_window_and_cross_mode():
    transport = FakeTransport()
    client = _client(transport)
    client.dedupe([{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
    assert "mode" not in transport.calls[-1]["payload"]  # within-list default
    client.dedupe([{"id": "a"}], records_b=[{"id": "z"}])
    payload = transport.calls[-1]["payload"]
    assert payload["mode"] == "cross" and payload["records_b"][0]["id"] == "z"


def test_dedupe_oversize_uses_blocked_windows():
    transport = FakeTransport()
    records = [{"name": f"Person {i}", "email": f"p{i}@x.co"}
               for i in range(501)]
    out = _client(transport).dedupe(records)
    # 2 windows: 2 within scans + 1 consecutive cross scan.
    assert len(transport.calls) == 3
    assert "500-record windows" in out["blocking"]
    assert all(len(c["payload"]["records"]) <= 500
               for c in transport.calls)


def test_normalize_chunks_at_1000():
    transport = FakeTransport()
    out = _client(transport).normalize("phone", ["x"] * 1001)
    assert len(transport.calls) == 2
    assert len(out["results"]) == 1001
    assert transport.calls[0]["payload"]["type"] == "phone"


# ----------------------------------------------------------------- mapping
def test_conversation_to_identity_channel_shapes():
    sms = suggest.conversation_to_identity({
        "_id": "c1", "tenant_id": "t1", "channel": "sms",
        "customer_phone": "+12145550147", "customer_key": "phone:2145550147"})
    assert sms["name"] is None and sms["email"] is None
    assert sms["phone"] == "+12145550147"
    widget = suggest.conversation_to_identity({
        "_id": "c2", "tenant_id": "t1", "channel": "chat",
        "customer_name": "Bob Smith", "customer_email": "Bob@X.co",
        "customer_ref": "r9", "customer_key": "email:bob@x.co"})
    assert widget["phone"] is None and widget["customer_ref"] == "r9"


def test_cleanup_projection_drops_and_counts_unmapped():
    identity = {"id": "c2", "tenant_id": "t1", "channel": "chat",
                "name": "Bob Smith", "email": "Bob@X.co", "phone": None,
                "customer_ref": "r9", "customer_key": "email:bob@x.co",
                "external_ref": "ext1"}
    row, dropped = suggest.identity_to_cleanup_row(identity)
    assert row == {"name": "Bob Smith", "email": "Bob@X.co"}
    assert "address" not in row  # hub stores no address — never invented
    assert set(dropped) == {"customer_ref", "customer_key", "channel",
                            "external_ref"}


def test_fragmentation_analysis_exact_contact():
    identities = [
        {"id": "a", "channel": "chat", "email": "Bob@X.co", "phone": None,
         "customer_key": "email:bob@x.co"},
        {"id": "b", "channel": "sms", "email": None,
         "phone": "(214) 555-0147", "customer_key": "phone:2145550147"},
        {"id": "c", "channel": "voice", "email": "bob@x.co",
         "phone": "+1 214-555-0147", "customer_key": "email:bob@x.co"},
    ]
    frag = suggest.fragmentation_analysis(identities)
    # The phone group spans the sms phone: key and the voice email: key.
    assert frag["fragmented_group_count"] == 1
    assert set(frag["fragmented_customer_keys"]) == {
        "email:bob@x.co", "phone:2145550147"}


# ------------------------------------------------------------- suggest job
def _engine_or_skip():
    engine = suggest.load_engine()
    if engine is None:
        pytest.skip("books DataClean engine unavailable in this env")
    return engine


def test_suggest_job_small_set_real_engine():
    engine = _engine_or_skip()
    identities = [
        {"id": "w", "tenant_id": "t1", "channel": "chat",
         "name": "Robert  Smith", "email": " ROBERT@X.CO ", "phone": None,
         "customer_ref": None, "customer_key": "email:robert@x.co",
         "external_ref": None},
        {"id": "v", "tenant_id": "t1", "channel": "voice",
         "name": "Bob Smith", "email": "robert@x.co",
         "phone": "(214) 555-0147", "customer_ref": None,
         "customer_key": "email:robert@x.co", "external_ref": None},
        {"id": "s", "tenant_id": "t1", "channel": "sms", "name": None,
         "email": None, "phone": "2145550147", "customer_ref": None,
         "customer_key": "phone:2145550147", "external_ref": None},
        {"id": "u", "tenant_id": "t1", "channel": "chat",
         "name": "Una Solo", "email": "una@x.co", "phone": None,
         "customer_ref": None, "customer_key": "email:una@x.co",
         "external_ref": None},
    ]
    snapshot = copy.deepcopy(identities)
    report = suggest.build_report(identities, "test fixtures",
                                  engine=engine)
    assert identities == snapshot  # pure: inputs untouched
    assert report["suggestions_only"] is True
    assert report["auto_merge"] is False
    totals = report["totals"]
    assert totals["identities_in"] == 4
    assert totals["suggested_pairs"] >= 1  # widget <-> voice (email+nickname)
    assert totals["changes_by_rule"].get("phone:libphonenumber-e164") == 2
    assert totals["changes_by_rule"].get("email:trim-and-lowercase") == 1
    # SMS fragment is invisible to name-based linkage but the exact
    # contact analysis must surface it.
    assert totals["fragmented_groups"] == 1
    assert report["mapping"]["dropped_field_values"]["channel"] == 4


def test_synthetic_sample_shape_and_planted_fragmentation():
    sample = suggest.synthetic_identities()
    assert len(sample) >= 120
    channels = {i["channel"] for i in sample}
    assert channels == {"chat", "email", "sms", "voice"}
    frag = suggest.fragmentation_analysis(sample)
    # Every planted multi-channel person spans an email: and a phone: key.
    assert frag["fragmented_group_count"] == 36
    assert len(frag["fragmented_customer_keys"]) == 72


def test_suggest_job_synthetic_full_report():
    engine = _engine_or_skip()
    sample = suggest.synthetic_identities()
    report = suggest.build_report(sample, "SYNTHETIC", engine=engine)
    totals = report["totals"]
    assert totals["identities_in"] == len(sample)
    assert totals["suggested_pairs"] >= 30
    assert totals["changes_total"] > 100
    assert totals["fragmented_groups"] == 36


# ------------------------------------------------------------------ script
def test_script_live_mode_inert_while_disabled(tmp_path):
    backend = os.path.join(os.path.dirname(__file__), "..")
    env = {k: v for k, v in os.environ.items() if k != "DATACLEAN_ENABLED"}
    proc = subprocess.run(
        [sys.executable, "scripts/dataclean_report.py", "--synthetic",
         "--live"],
        cwd=backend, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2
    assert "INERT" in proc.stdout


def test_script_synthetic_dry_run_writes_report(tmp_path):
    _engine_or_skip()
    backend = os.path.join(os.path.dirname(__file__), "..")
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, "scripts/dataclean_report.py", "--synthetic",
         "--out", str(out)],
        cwd=backend, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out.read_text())
    assert report["mode"] == "dry-run"
    assert report["totals"]["identities_in"] >= 120
    assert "SYNTHETIC" in report["source"]
