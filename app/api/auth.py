from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.database import get_session
from app.limiter import limiter
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import (
    AuthResponse,
    RefreshRequest,
    RefreshResponse,
    TelegramAuthRequest,
)
from app.schemas.user import UserInfo
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    upsert_user,
    verify_telegram_auth,
)

router = APIRouter(tags=["auth"])


@router.post("/telegram", response_model=AuthResponse)
@limiter.limit("10/minute")
async def telegram_login(
    request: Request,
    body: TelegramAuthRequest,
    session: AsyncSession = Depends(get_session),
) -> AuthResponse:
    """Authenticate via Telegram Login Widget. Returns JWT access + refresh tokens."""
    tg_data = {k: v for k, v in body.model_dump().items() if v is not None}

    if not verify_telegram_auth(tg_data, settings.tg_bot_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram authentication data",
        )

    user = await upsert_user(session, tg_data)

    access_token = create_access_token(user_id=user.id, telegram_id=user.id)
    refresh_token_str, jti = create_refresh_token(user_id=user.id)

    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    token_record = RefreshToken(
        user_id=user.id,
        token=refresh_token_str,
        jti=jti,
        expires_at=expires_at,
    )
    session.add(token_record)
    await session.commit()

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token_str,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserInfo(id=user.id, username=user.username),
    )


@router.post("/refresh", response_model=RefreshResponse)
@limiter.limit("30/minute")
async def refresh_token(
    request: Request,
    body: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> RefreshResponse:
    """Exchange refresh token for a new access token + rotated refresh token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
    )
    try:
        payload = decode_token(body.refresh_token)
    except InvalidTokenError:
        raise credentials_exception

    if payload.get("type") != "refresh":
        raise credentials_exception

    jti = payload.get("jti")
    result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    token_record = result.scalar_one_or_none()
    if token_record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token not found or revoked",
        )

    user_id = int(payload["sub"])

    # Delete old token (rotation: one-time use)
    await session.delete(token_record)

    # Issue new refresh token
    new_refresh_token_str, new_jti = create_refresh_token(user_id=user_id)
    new_expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    new_token_record = RefreshToken(
        user_id=user_id,
        token=new_refresh_token_str,
        jti=new_jti,
        expires_at=new_expires_at,
    )
    session.add(new_token_record)

    access_token = create_access_token(user_id=user_id, telegram_id=user_id)
    await session.commit()

    return RefreshResponse(
        access_token=access_token,
        refresh_token=new_refresh_token_str,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: User = Depends(get_current_user)) -> UserInfo:
    """Return current authenticated user info."""
    return UserInfo(id=current_user.id, username=current_user.username)
