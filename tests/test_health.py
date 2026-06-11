"""Tests for the /health endpoint."""

from app.web.routes import health


def test_health_returns_ok() -> None:
    response = health()
    assert response.status_code == 200
    assert response.body == b'{"status":"ok"}'
