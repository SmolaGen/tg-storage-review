from pydantic import BaseModel


class UserInfo(BaseModel):
    id: int
    username: str | None = None
