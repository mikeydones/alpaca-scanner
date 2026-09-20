import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("ALPACA_KEY_ID", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

import pytest
from fastapi.testclient import TestClient

import app as webapp

client = TestClient(webapp.app)


def test_health_never_touches_alpaca():
    """Render hits /health before env vars are necessarily right."""
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_index_renders():
    r = client.get("/")
    assert r.status_code == 200 and "Opening-range scanner" in r.text


def test_setups_endpoint_shape():
    d = client.get("/api/setups").json()
    for k in ("running", "setups", "near_misses", "reject_counts", "paper", "trading_enabled"):
        assert k in d


def test_order_blocked_when_trading_disabled(monkeypatch):
    monkeypatch.setattr(webapp, "TRADING_ENABLED", False)
    assert client.post("/api/order", params={"symbol": "AAPL"}).status_code == 403


def test_order_requires_token(monkeypatch):
    monkeypatch.setattr(webapp, "TRADING_ENABLED", True)
    monkeypatch.setattr(webapp, "TOKEN", "secret")
    r = client.post("/api/order", params={"symbol": "AAPL"}, headers={"X-Scan-Token": "wrong"})
    assert r.status_code == 401
    r = client.post("/api/order", params={"symbol": "AAPL"}, headers={"X-Scan-Token": "secret"})
    assert r.status_code == 404          # good token, symbol simply not in the scan


def test_order_refuses_when_token_unset(monkeypatch):
    monkeypatch.setattr(webapp, "TRADING_ENABLED", True)
    monkeypatch.setattr(webapp, "TOKEN", None)
    r = client.post("/api/order", params={"symbol": "AAPL"}, headers={"X-Scan-Token": ""})
    assert r.status_code == 401


def test_concurrent_scan_is_rejected(monkeypatch):
    with webapp.STATE.lock:
        webapp.STATE.running = True
    try:
        assert client.post("/api/scan").status_code == 409
    finally:
        with webapp.STATE.lock:
            webapp.STATE.running = False
