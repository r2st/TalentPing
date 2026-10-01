"""Auth flow: registration, login, protected routes."""
from __future__ import annotations


def test_register_and_login(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "password": "password123", "full_name": "A"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "a@example.com"
    assert "hashed_password" not in body  # never leak the hash

    r = client.post(
        "/api/v1/auth/login", data={"username": "a@example.com", "password": "password123"}
    )
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"
    assert r.json()["access_token"]


def test_duplicate_email_rejected(client):
    payload = {"email": "dup@example.com", "password": "password123"}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201
    assert client.post("/api/v1/auth/register", json=payload).status_code == 409


def test_login_wrong_password(client):
    client.post(
        "/api/v1/auth/register",
        json={"email": "b@example.com", "password": "password123"},
    )
    r = client.post(
        "/api/v1/auth/login", data={"username": "b@example.com", "password": "wrong"}
    )
    assert r.status_code == 401


def test_me_requires_auth(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_returns_current_user(auth_client):
    r = auth_client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == "candidate@example.com"


def test_short_password_rejected(client):
    r = client.post(
        "/api/v1/auth/register", json={"email": "c@example.com", "password": "short"}
    )
    assert r.status_code == 422
