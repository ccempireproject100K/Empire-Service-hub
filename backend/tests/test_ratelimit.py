"""Rate limiting: unit behavior + a 429 through the public API."""
from lib import ratelimit


def test_limiter_window():
    ratelimit.reset()
    assert ratelimit._allow("unit-key", 2, 60) is True
    assert ratelimit._allow("unit-key", 2, 60) is True
    assert ratelimit._allow("unit-key", 2, 60) is False
    assert ratelimit._allow("other-key", 2, 60) is True
    ratelimit.reset()
    assert ratelimit._allow("unit-key", 2, 60) is True


def test_widget_config_rate_limited(client, hub, tenant, monkeypatch):
    monkeypatch.setitem(ratelimit.RATE_LIMITS, "widget_config", (2, 60))
    headers = hub.widget_headers(tenant)
    assert client.get("/api/widget/config", headers=headers).status_code == 200
    assert client.get("/api/widget/config", headers=headers).status_code == 200
    resp = client.get("/api/widget/config", headers=headers)
    assert resp.status_code == 429
    assert resp.headers.get("retry-after") == "60"
