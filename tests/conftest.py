import hashlib
import hmac
import time

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import get_session
from app.main import app
from app.models.base import Base


# Use SQLite for test isolation
TEST_DATABASE_URL = "sqlite+aiosqlite://"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create all tables before each test, drop after."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with test_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    """AsyncClient with test DB override."""

    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session

    # Set test bot token
    settings.tg_bot_token = "test-bot-token-12345"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


def valid_tg_payload(bot_token: str) -> dict:
    """Generate a valid Telegram Login Widget payload with correct HMAC hash."""
    auth_date = int(time.time())
    data = {
        "id": 123456789,
        "first_name": "Test",
        "last_name": "User",
        "username": "testuser",
        "auth_date": auth_date,
    }

    # Build data_check_string same as verify_telegram_auth
    check_items = {k: str(v) for k, v in data.items() if v is not None}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(check_items.items()))

    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(
        secret_key, check_string.encode(), hashlib.sha256
    ).hexdigest()

    return {**data, "hash": computed_hash}
