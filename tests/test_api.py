from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import psycopg
import pytest


def login(client, user_id, password="Demo-password-2026"):
    response = client.post("/login", json={"id": user_id, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_register_login_and_profiles(client, register):
    user_id, data = register()
    profile = client.get(f"/user/get/{user_id}")
    assert profile.status_code == 200
    assert profile.json() == {"id": user_id, **{k: v for k, v in data.items() if k != "password"}}
    token = login(client, user_id)
    mine = client.get("/user/me", headers={"Authorization": f"Bearer {token}"})
    assert mine.status_code == 200
    assert mine.json() == profile.json()
    assert "password" not in mine.text


def test_another_public_profile(client, register):
    first_id, _ = register()
    second_id, _ = register(first_name="Анна", gender="female")
    token = login(client, first_id)
    assert client.get(f"/user/get/{second_id}").json()["first_name"] == "Анна"
    assert (
        client.get("/user/me", headers={"Authorization": f"Bearer {token}"}).json()["id"]
        == first_id
    )


def test_argon2_hashes_have_unique_salts(client, register):
    first_id, data = register()
    second_id, _ = register()
    with client.app.state.pool.connection() as conn:
        rows = conn.execute(
            "SELECT password_hash FROM users WHERE id = ANY(%s)",
            ([UUID(first_id), UUID(second_id)],),
        ).fetchall()
    hashes = [row["password_hash"] for row in rows]
    assert len(hashes) == 2
    assert hashes[0] != hashes[1]
    for stored_hash in hashes:
        assert stored_hash.startswith("$argon2id$")
        assert data["password"] not in stored_hash
        assert client.app.state.security.verify_password(data["password"], stored_hash)


def test_sql_injection_is_stored_as_text(client, register):
    payload = "Robert'); DROP TABLE users; --"
    user_id, _ = register(
        first_name=payload, second_name="O'Reilly", biography=payload, city=payload
    )
    response = client.get(f"/user/get/{user_id}")
    assert response.status_code == 200
    for field in ("first_name", "biography", "city"):
        assert response.json()[field] == payload
    assert response.json()["second_name"] == "O'Reilly"
    login(client, user_id)
    assert (
        client.post(
            "/login", json={"id": "' OR 1=1 --", "password": "whatever-password"}
        ).status_code
        == 400
    )
    assert client.get("/user/get/%27%20OR%201=1%20--").status_code == 400
    assert client.get(f"/user/get/{user_id}").status_code == 200


def test_wrong_password_and_unknown_id_have_same_response(client, register):
    user_id, _ = register()
    wrong_password = client.post("/login", json={"id": user_id, "password": "wrong-password"})
    unknown_id = client.post("/login", json={"id": str(uuid4()), "password": "wrong-password"})
    assert wrong_password.status_code == unknown_id.status_code == 404
    assert wrong_password.json() == unknown_id.json()


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic abc", "Bearer"])
def test_auth_required(client, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    response = client.get("/user/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("mode", ["expired", "forged", "missing_exp", "wrong_algorithm"])
def test_invalid_tokens_rejected(client, register, mode):
    user_id, _ = register()
    secret = client.app.state.security.settings.jwt_secret.get_secret_value()
    claims = {
        "sub": user_id,
        "iat": datetime.now(UTC) - timedelta(hours=2),
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "iss": "social-network",
        "aud": "social-network-api",
    }
    algorithm = "HS256"
    if mode == "expired":
        claims["exp"] = datetime.now(UTC) - timedelta(hours=1)
    elif mode == "forged":
        secret = "x" * 64
    elif mode == "missing_exp":
        del claims["exp"]
    elif mode == "wrong_algorithm":
        algorithm = "HS512"
        secret = "x" * 64
    token = jwt.encode(claims, secret, algorithm=algorithm)
    assert client.get("/user/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_name", "   "),
        ("second_name", "x" * 101),
        ("city", "\x00"),
        ("birthdate", "2023-02-29"),
        ("birthdate", "2999-01-01"),
        ("birthdate", "1899-12-31"),
        ("birthdate", "2000-01-01T00:00:00"),
        ("birthdate", 946684800),
        ("gender", "invalid"),
        ("password", "short"),
        ("password", "x" * 129),
        ("biography", "x" * 5001),
        ("first_name", None),
    ],
)
def test_registration_validation(client, profile_data, field, value):
    data = profile_data | {field: value}
    response = client.post("/user/register", json=data)
    assert response.status_code == 400
    assert "Demo-password-2026" not in response.text
    assert "password_hash" not in response.text


def test_missing_fields_unknown_fields_and_bad_json(client, profile_data):
    assert client.post("/user/register", json={}).status_code == 400
    assert client.post("/user/register", json=profile_data | {"admin": True}).status_code == 400
    response = client.post(
        "/user/register",
        content='{"password":"sensitive-value",',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "sensitive-value" not in response.text


def test_compatibility_without_gender(client, register):
    user_id, _ = register(omit=("gender",))
    assert client.get(f"/user/get/{user_id}").json()["gender"] == "unspecified"


def test_unknown_and_malformed_profile_ids(client):
    assert client.get(f"/user/get/{uuid4()}").status_code == 404
    assert client.get("/user/get/not-a-uuid").status_code == 400


def test_concurrent_reads(client, register):
    user_id, _ = register()
    with ThreadPoolExecutor(max_workers=8) as workers:
        responses = list(workers.map(lambda _: client.get(f"/user/get/{user_id}"), range(24)))
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["id"] == user_id for response in responses)


def test_database_failure_returns_503_without_details(client, monkeypatch):
    def unavailable(_user_id):
        raise psycopg.OperationalError("secret-database-details")

    monkeypatch.setattr(client.app.state.users, "get_profile", unavailable)
    response = client.get(f"/user/get/{uuid4()}")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "secret-database-details" not in response.text


def test_health_checks_database(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_actual_validation_status(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    for path in response.json()["paths"].values():
        for operation in path.values():
            assert "400" in operation["responses"]
            assert "422" not in operation["responses"]
