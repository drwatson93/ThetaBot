"""Shared test fixtures.

Web tests send Basic Auth automatically once a dashboard password is configured
(the dashboard is locked when the password is unset). Tests that need a truly
anonymous request pass ``auth=None``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TEST_DASH_USER = "admin"
TEST_DASH_PASS = "test-dashboard-password-ok"
TEST_CONTROL_TOKEN = "test-control-token-ok-long"


@pytest.fixture(autouse=True)
def _runtime_secrets(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", TEST_DASH_USER)
    monkeypatch.setenv("DASHBOARD_PASSWORD", TEST_DASH_PASS)
    monkeypatch.setenv("CONTROL_TOKEN", TEST_CONTROL_TOKEN)


@pytest.fixture(autouse=True)
def _order_window_open(monkeypatch):
    """Scanner/monitor tests assume the 10:00 ET gate is already open.

    The real helper is unit-tested separately; executor still enforces it on
    non-paper brokers.
    """
    monkeypatch.setattr(
        "agentic.services.scanner.is_order_window", lambda *a, **k: True, raising=False
    )
    monkeypatch.setattr(
        "agentic.services.monitor.is_order_window", lambda *a, **k: True, raising=False
    )


@pytest.fixture(autouse=True)
def _authed_test_client(monkeypatch):
    orig = TestClient.request

    def _request(self, method, url, **kwargs):
        if "auth" not in kwargs:
            kwargs["auth"] = (TEST_DASH_USER, TEST_DASH_PASS)
        return orig(self, method, url, **kwargs)

    monkeypatch.setattr(TestClient, "request", _request)
