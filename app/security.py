import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

from app.config import Settings


class Security:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID)
        # фиктивный хеш, чтобы время ответа для несуществующего id не отличалось
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def hash_password(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return self.hasher.verify(password_hash, password)
        except VerificationError, InvalidHashError:
            return False

    def issue_token(self, user_id: UUID) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": str(user_id),
                "iat": now,
                "exp": now + timedelta(seconds=self.settings.token_ttl_seconds),
                "iss": "social-network",
                "aud": "social-network-api",
                "jti": secrets.token_hex(16),
            },
            self.settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )

    def token_subject(self, token: str) -> UUID:
        payload = jwt.decode(
            token,
            self.settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer="social-network",
            audience="social-network-api",
            options={"require": ["sub", "iat", "exp", "iss", "aud"]},
        )
        return UUID(payload["sub"])
