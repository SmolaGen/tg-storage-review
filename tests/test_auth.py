"""Auth tests: AUTH-01 (HMAC + JWT), AUTH-02 (refresh), AUTH-03 (protected endpoint)."""

import time

import jwt
import pytest

from app.config import settings
from tests.conftest import valid_tg_payload

# ---------------------------------------------------------------------------
# AUTH-01: HMAC + JWT unit tests
# ---------------------------------------------------------------------------


def test_verify_telegram_auth_valid():
    """Valid HMAC payload returns True."""
    from app.services.auth import verify_telegram_auth

    bot_token = "test-bot-token-12345"
    payload = valid_tg_payload(bot_token)
    assert verify_telegram_auth(dict(payload), bot_token) is True


def test_verify_telegram_auth_invalid_hash():
    """Forged hash returns False."""
    from app.services.auth import verify_telegram_auth

    bot_token = "test-bot-token-12345"
    payload = {**valid_tg_payload(bot_token), "hash": "forgedhash123"}
    assert verify_telegram_auth(dict(payload), bot_token) is False


def test_verify_telegram_auth_expired():
    """auth_date older than 86400s returns False."""
    from app.services.auth import verify_telegram_auth

    bot_token = "test-bot-token-12345"
    payload = valid_tg_payload(bot_token)
    # Override auth_date to >24h ago
    expired_payload = {**payload, "auth_date": int(time.time()) - 90000}
    assert verify_telegram_auth(dict(expired_payload), bot_token) is False


def test_create_access_token():
    """Returns JWT string, decodes to correct sub/type/telegram_id."""
    from app.services.auth import create_access_token, decode_token

    token = create_access_token(user_id=999, telegram_id=999)
    assert isinstance(token, str)
    payload = decode_token(token)
    assert payload["sub"] == "999"
    assert payload["type"] == "access"
    assert payload["telegram_id"] == 999


def test_create_refresh_token():
    """Returns JWT string with jti claim."""
    from app.services.auth import create_refresh_token, decode_token

    token_str, jti = create_refresh_token(user_id=999)
    assert isinstance(token_str, str)
    assert isinstance(jti, str)
    payload = decode_token(token_str)
    assert payload["jti"] == jti
    assert payload["type"] == "refresh"


@pytest.mark.asyncio
async def test_telegram_login_endpoint(client):
    """POST /auth/telegram with valid payload returns 200 + tokens + user."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = valid_tg_payload("test-bot-token-12345")
    response = await client.post("/auth/telegram", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert "user" in data
    assert data["user"]["id"] == payload["id"]


@pytest.mark.asyncio
async def test_telegram_login_invalid_hash(client):
    """POST /auth/telegram with bad hash returns 401."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = {**valid_tg_payload("test-bot-token-12345"), "hash": "badhash"}
    response = await client.post("/auth/telegram", json=payload)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_user_upsert(client):
    """Second login with same telegram_id updates username, returns same user id."""
    settings.tg_bot_token = "test-bot-token-12345"
    bot_token = "test-bot-token-12345"

    # First login
    payload1 = valid_tg_payload(bot_token)
    r1 = await client.post("/auth/telegram", json=payload1)
    assert r1.status_code == 200
    user_id_1 = r1.json()["user"]["id"]

    # Second login (same telegram id, different username)
    import hashlib
    import hmac

    auth_date = int(time.time())
    data2 = {
        "id": payload1["id"],
        "first_name": "Updated",
        "username": "newusername",
        "auth_date": auth_date,
    }
    check_items = {k: str(v) for k, v in data2.items() if v is not None}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(check_items.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    new_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    payload2 = {**data2, "hash": new_hash}

    r2 = await client.post("/auth/telegram", json=payload2)
    assert r2.status_code == 200
    user_id_2 = r2.json()["user"]["id"]
    assert user_id_1 == user_id_2


# ---------------------------------------------------------------------------
# AUTH-02: Refresh token tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_success(client):
    """POST /auth/refresh with valid refresh_token returns 200 + new access_token."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = valid_tg_payload("test-bot-token-12345")
    login_resp = await client.post("/auth/telegram", json=payload)
    assert login_resp.status_code == 200
    refresh_token = login_resp.json()["refresh_token"]

    refresh_resp = await client.post(
        "/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert refresh_resp.status_code == 200
    assert "access_token" in refresh_resp.json()


@pytest.mark.asyncio
async def test_refresh_invalid_token(client):
    """POST /auth/refresh with garbage token returns 401."""
    response = await client.post(
        "/auth/refresh", json={"refresh_token": "garbage.token.here"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_access_token(client):
    """POST /auth/refresh with access_token (type != refresh) returns 401."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = valid_tg_payload("test-bot-token-12345")
    login_resp = await client.post("/auth/telegram", json=payload)
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]

    refresh_resp = await client.post(
        "/auth/refresh", json={"refresh_token": access_token}
    )
    assert refresh_resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_returns_new_refresh_token(client):
    """POST /auth/refresh returns both access_token and a new refresh_token."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = valid_tg_payload("test-bot-token-12345")
    login_resp = await client.post("/auth/telegram", json=payload)
    assert login_resp.status_code == 200
    old_refresh_token = login_resp.json()["refresh_token"]

    refresh_resp = await client.post(
        "/auth/refresh", json={"refresh_token": old_refresh_token}
    )
    assert refresh_resp.status_code == 200
    data = refresh_resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["refresh_token"] != old_refresh_token


@pytest.mark.asyncio
async def test_refresh_old_token_rejected_after_rotation(client):
    """After using a refresh token, reusing the old one returns 401 (token rotation)."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = valid_tg_payload("test-bot-token-12345")
    login_resp = await client.post("/auth/telegram", json=payload)
    assert login_resp.status_code == 200
    old_refresh_token = login_resp.json()["refresh_token"]

    # First use — should succeed
    first_resp = await client.post(
        "/auth/refresh", json={"refresh_token": old_refresh_token}
    )
    assert first_resp.status_code == 200

    # Second use of the SAME old token — must be rejected (rotated out)
    second_resp = await client.post(
        "/auth/refresh", json={"refresh_token": old_refresh_token}
    )
    assert second_resp.status_code == 401


# ---------------------------------------------------------------------------
# AUTH-03: Protected endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_protected_endpoint_no_token(client):
    """GET /auth/me without Authorization header returns 401."""
    response = await client.get("/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_valid_token(client):
    """GET /auth/me with valid Bearer returns 200 + user info."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = valid_tg_payload("test-bot-token-12345")
    login_resp = await client.post("/auth/telegram", json=payload)
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]

    me_resp = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_resp.status_code == 200
    assert "id" in me_resp.json()


@pytest.mark.asyncio
async def test_protected_endpoint_expired_token(client):
    """GET /auth/me with expired access_token returns 401."""
    # Create token with past exp
    from datetime import datetime, timezone

    expired_payload = {
        "sub": "123456789",
        "telegram_id": 123456789,
        "type": "access",
        "exp": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "iat": datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    expired_token = jwt.encode(
        expired_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )

    me_resp = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {expired_token}"}
    )
    assert me_resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_refresh_as_access(client):
    """GET /auth/me with refresh_token in Bearer returns 401."""
    settings.tg_bot_token = "test-bot-token-12345"
    payload = valid_tg_payload("test-bot-token-12345")
    login_resp = await client.post("/auth/telegram", json=payload)
    assert login_resp.status_code == 200
    refresh_token = login_resp.json()["refresh_token"]

    me_resp = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {refresh_token}"}
    )
    assert me_resp.status_code == 401


# ---------------------------------------------------------------------------
# Rate limiting tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_login_rate_limit(client):
    """POST /auth/telegram returns 429 after exceeding rate limit (10/minute).

    Uses a structurally valid but HMAC-invalid body so Pydantic passes and
    the rate-limiter counter increments on every call (returning 401 until
    the limit is exceeded, then 429).
    """
    invalid_body = {
        "id": 1,
        "first_name": "Test",
        "auth_date": 1000000000,
        "hash": "deadbeef",
    }
    responses = []
    for _ in range(12):
        resp = await client.post("/auth/telegram", json=invalid_body)
        responses.append(resp.status_code)
    assert 429 in responses, f"Expected 429 in responses, got: {set(responses)}"
