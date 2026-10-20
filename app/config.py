from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # пустой DSN — тогда libpq возьмёт PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD
    database_url: SecretStr = SecretStr("")
    jwt_secret: SecretStr
    token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)

    @field_validator("jwt_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET должен быть не короче 32 байт")
        return value
