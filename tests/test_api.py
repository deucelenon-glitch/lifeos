"""Test plugin discovery + basic endpoints."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

app = create_app()
client = TestClient(app)


def test_root():
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert "habits" in body["plugins_loaded"]


def test_habits_endpoint():
    r = client.get("/api/habits/")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_plugin_registered():
    assert "habits" in app.state.plugins