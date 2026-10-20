import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Gender(StrEnum):
    male = "male"
    female = "female"
    other = "other"
    unspecified = "unspecified"


class ProfileFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: ShortText
    second_name: ShortText
    birthdate: date
    gender: Gender = Gender.unspecified
    biography: str = Field(max_length=5000)
    city: ShortText

    @field_validator("first_name", "second_name", "biography", "city")
    @classmethod
    def no_null_bytes(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Null bytes are not allowed")
        return value

    @field_validator("birthdate", mode="before")
    @classmethod
    def date_format(cls, value: object) -> object:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Use YYYY-MM-DD")
        elif type(value) is not date:
            raise ValueError("Use YYYY-MM-DD")
        return value

    @field_validator("birthdate")
    @classmethod
    def valid_birthdate(cls, value: date) -> date:
        if not date(1900, 1, 1) <= value <= datetime.now(UTC).date():
            raise ValueError("Birthdate must be between 1900-01-01 and today")
        return value


class RegisterRequest(ProfileFields):
    password: SecretStr = Field(min_length=8, max_length=128)


class Profile(ProfileFields):
    id: UUID


class RegisterResponse(BaseModel):
    user_id: UUID


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    password: SecretStr = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    token: str


class ErrorResponse(BaseModel):
    message: str
