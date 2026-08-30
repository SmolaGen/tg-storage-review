"""Admin endpoints — выдача JWT токенов без Telegram Login Widget.

Защищено заголовком X-Admin-Secret. Только для личного использования.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models.refresh_token import RefreshToken
from app.schemas.auth import AuthResponse
from app.schemas.user import UserInfo
from app.services.auth import create_access_token, create_refresh_token, upsert_user

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(x_admin_secret: str = Header(...)) -> None:
    if not settings.admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@router.post("/token", response_model=AuthResponse)
async def admin_get_token(
    telegram_id: int,
    username: str = "admin",
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_admin),
) -> AuthResponse:
    """Выдать JWT токены для указанного Telegram ID.

    Используется для первичной настройки без Telegram Login Widget.
    """
    tg_data = {
        "id": telegram_id,
        "first_name": username,
        "username": username,
        "auth_date": int(datetime.now(timezone.utc).timestamp()),
    }
    user = await upsert_user(session, tg_data)

    access_token = create_access_token(user_id=user.id, telegram_id=user.id)
    refresh_token_str, jti = create_refresh_token(user_id=user.id)
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )

    session.add(
        RefreshToken(
            user_id=user.id,
            token=refresh_token_str,
            jti=jti,
            expires_at=expires_at,
        )
    )
    await session.commit()

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token_str,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserInfo(id=user.id, username=user.username),
    )
