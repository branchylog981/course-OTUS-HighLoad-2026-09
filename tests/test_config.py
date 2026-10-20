import pytest
from pydantic import ValidationError

from app.config import Settings


def test_short_jwt_secret_is_rejected():
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(jwt_secret="short")


def test_secrets_are_redacted():
    settings = Settings(jwt_secret="a" * 32, database_url="postgresql://user:secret@db/social")
    assert "a" * 32 not in repr(settings)
    assert "user:secret" not in repr(settings)
