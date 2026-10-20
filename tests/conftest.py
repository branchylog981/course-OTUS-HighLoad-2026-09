from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api import create_app


@pytest.fixture(scope="session")
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def profile_data():
    return {
        "first_name": "Иван",
        "second_name": "Петров",
        "birthdate": "1994-05-21",
        "gender": "male",
        "biography": "Книги, музыка и программирование",
        "city": "Москва",
        "password": "Demo-password-2026",
    }


@pytest.fixture
def register(client, profile_data):
    created_ids = []

    def create(*, omit=(), **overrides):
        data = profile_data | overrides
        for field in omit:
            data.pop(field)
        response = client.post("/user/register", json=data)
        assert response.status_code == 200, response.text
        user_id = response.json()["user_id"]
        created_ids.append(UUID(user_id))
        return user_id, data

    yield create

    # подчищаем только то, что создали сами
    if created_ids:
        with client.app.state.pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (created_ids,))
