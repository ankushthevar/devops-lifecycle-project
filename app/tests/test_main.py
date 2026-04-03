from fastapi.testclient import TestClient
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from main import app

client = TestClient(app)

def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert "DevOps Lifecycle Project" in response.json()["app"]

def test_health_live():
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"

def test_health_ready():
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"

def test_list_items_empty():
    response = client.get("/api/v1/items")
    assert response.status_code == 200
    assert response.json()["count"] == 0

def test_create_and_get_item():
    response = client.post("/api/v1/items", json={"name": "test", "value": 42})
    assert response.status_code == 201
    item_id = response.json()["id"]

    response = client.get(f"/api/v1/items/{item_id}")
    assert response.status_code == 200
    assert response.json()["name"] == "test"

def test_item_not_found():
    response = client.get("/api/v1/items/nonexistent-id")
    assert response.status_code == 404
