from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = (
        "postgresql+asyncpg://tgstorage:tgstorage@localhost:5432/tgstorage"
    )
    tg_api_id: int = 0
    tg_api_hash: str = ""
    jwt_secret: str = "change-me-in-production-use-env-var!"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    tg_bot_token: str = ""
    tg_dc_port: int = 443
    janitor_interval_seconds: int = 300
    janitor_stale_minutes: int = 30
    admin_secret: str = ""


settings = Settings()
