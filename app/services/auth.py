import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User


def verify_telegram_auth(data: dict, bot_token: str) -> bool:
    """Verify Telegram Login Widget HMAC-SHA256 signature."""
    data_copy = dict(data)
    received_hash = data_copy.pop("hash", None)
    if received_hash is None:
        return False

    auth_date = int(data_copy.get("auth_date", 0))
    if time.time() - auth_date > 86400:
        return False

    check_items = {k: str(v) for k, v in data_copy.items() if v is not None}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(check_items.items()))

    # MUST use .digest() not .hexdigest() for the secret key
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    return hmac.compare_digest(computed, received_hash)


def create_access_token(user_id: int, telegram_id: int) -> str:
    """Create JWT access token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "telegram_id": telegram_id,
        "type": "access",
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "iat": now,
    }
    # algorithm= SINGULAR for encode
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> tuple[str, str]:
    """Create JWT refresh token. Returns (token_str, jti)."""
    now = datetime.now(timezone.utc)
    jti = str(uuid4())
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "type": "refresh",
        "exp": now + timedelta(days=settings.refresh_token_expire_days),
        "iat": now,
    }
    # algorithm= SINGULAR for encode
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti


def decode_token(token: str) -> dict:
    """Decode and verify JWT token. Raises jwt.InvalidTokenError on failure."""
    # algorithms= PLURAL LIST for decode
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


async def upsert_user(session: AsyncSession, tg_data: dict) -> User:
    """Create or update user from Telegram Login data (open registration)."""
    stmt = (
        pg_insert(User)
        .values(
            id=int(tg_data["id"]),
            username=tg_data.get("username"),
            first_name=tg_data["first_name"],
            last_name=tg_data.get("last_name"),
            photo_url=tg_data.get("photo_url"),
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "username": tg_data.get("username"),
                "first_name": tg_data["first_name"],
                "last_name": tg_data.get("last_name"),
                "photo_url": tg_data.get("photo_url"),
            },
        )
        .returning(User)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.scalars().one()
