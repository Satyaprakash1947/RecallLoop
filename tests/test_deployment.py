"""Deployment configuration and production API protection tests."""

from fastapi.testclient import TestClient
import pytest

from recallloop.api import create_app


def test_health_endpoint_checks_database_and_is_public(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALLLOOP_ENV", "production")
    monkeypatch.setenv("RECALLLOOP_API_KEY", "deployment-test-secret")
    client = TestClient(create_app(tmp_path / "health.sqlite3"))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_production_requires_api_key_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALLLOOP_ENV", "production")
    monkeypatch.delenv("RECALLLOOP_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="RECALLLOOP_API_KEY"):
        create_app(tmp_path / "missing-key.sqlite3")


def test_production_api_requires_key_and_documents_header(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALLLOOP_ENV", "production")
    monkeypatch.setenv("RECALLLOOP_API_KEY", "deployment-test-secret")
    client = TestClient(create_app(tmp_path / "secured.sqlite3"))

    assert client.get("/memories/search", params={"query": "x"}).status_code == 401
    assert client.get("/health").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["ApiKeyAuth"] == {
        "type": "apiKey", "in": "header", "name": "X-API-Key"
    }
    response = client.get("/memories/search", params={"query": "x"},
                          headers={"X-API-Key": "deployment-test-secret"})
    assert response.status_code == 200

