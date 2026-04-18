from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.user import User
from app.services.auth import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/telegram")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI dependency: validate Bearer token and return current user."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise credentials_exception
        user_id = int(payload["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        raise credentials_exception

    user = await session.get(User, user_id)
    if user is None:
        raise credentials_exception
    return user
