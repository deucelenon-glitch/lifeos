from fastapi import Request
from fastapi.responses import HTMLResponse
from app.main import create_app
import pytest

app = create_app()

def test_full_system():
    from fastapi.testclient import TestClient
    client = TestClient(app)

    # 1. Root/status check
    r = client.get("/api/status")
    assert r.status_code == 200
    assert "habits" in r.json()["plugins_loaded"]
    assert "expenses" in r.json()["plugins_loaded"]
    assert "telegram" in r.json()["plugins_loaded"]

    # 2. Habits CRUD flow
    r = client.post("/api/habits/", data={"name": "Gym", "target_streak": 5})
    assert r.status_code == 200
    assert "Gym" in r.text

    # 3. Expenses CRUD flow
    r = client.post("/api/expenses/", data={"amount": 12.50, "category": "food", "note": "lunch"})
    assert r.status_code == 200
    assert "12.50" in r.text

    # 4. Expenses Total
    r = client.get("/api/expenses/total")
    assert r.status_code == 200
    assert "12.50" in r.text
