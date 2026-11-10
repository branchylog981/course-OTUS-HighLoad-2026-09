"""Real primary/standby tests, enabled only on the dedicated replication cluster."""

import os
import time
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.repository import UserRepository
from app.routing import ReadRouter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REPLICATION_TESTS") != "1", reason="Requires master/slave1/slave2"
)


def replayed(user_id):
    deadline = time.monotonic() + 10
    for host in ("slave1", "slave2"):
        with psycopg.connect(host=host, autocommit=True) as conn:
            while not conn.execute("SELECT 1 FROM users WHERE id=%s", (user_id,)).fetchone():
                assert time.monotonic() < deadline, f"{host} did not replay the profile"
                time.sleep(0.05)


def test_round_robin_and_read_only_transactions(client, register):
    user_id, _ = register()
    replayed(UUID(user_id))
    with ReadRouter(Settings(read_hosts=["slave1", "slave2"])) as router:
        addresses = []
        for _ in range(4):
            with router.connection() as conn:
                row = conn.execute("""
                    SELECT inet_server_addr()::text AS address,
                        pg_is_in_recovery() AS recovery,
                        current_setting('transaction_read_only') AS read_only
                """).fetchone()
                assert row["recovery"] and row["read_only"] == "on"
                addresses.append(row["address"])
        assert addresses[0] == addresses[2] != addresses[1] == addresses[3]
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), router.connection() as conn:
            conn.execute("INSERT INTO replication_probe VALUES (%s, 1, 'forbidden')", (uuid4(),))
        # A failed read transaction must not poison the reused pool connection.
        repo = UserRepository(client.app.state.pool, router)
        assert str(repo.get_profile(UUID(user_id))["id"]) == user_id
        assert any(str(row["id"]) == user_id for row in repo.search("Ив", "Пет"))


def test_unavailable_replica_uses_survivor(client, register):
    user_id, _ = register()
    replayed(UUID(user_id))
    with ReadRouter(Settings(read_hosts=["missing-replica", "slave2"])) as router:
        repo = UserRepository(client.app.state.pool, router)
        assert str(repo.get_profile(UUID(user_id))["id"]) == user_id


def test_reader_is_read_only_even_when_address_points_to_primary():
    with ReadRouter(Settings(read_hosts=["master"])) as router:
        with router.connection() as conn:
            assert conn.read_only is True
            assert conn.execute("SELECT pg_is_in_recovery() AS recovery").fetchone() == {
                "recovery": False
            }
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), router.connection() as conn:
            conn.execute("INSERT INTO replication_probe VALUES (%s, 1, 'forbidden')", (uuid4(),))


def test_all_replicas_unavailable_returns_503():
    with TestClient(create_app(Settings(read_hosts=["missing-replica"]))) as api:
        response = api.get(f"/user/get/{uuid4()}")
        assert response.status_code == 503
        assert "missing-replica" not in response.text


def test_writes_and_login_stay_on_primary_during_replay_lag(client, profile_data):
    user_id = None
    connections = [psycopg.connect(host=h, autocommit=True) for h in ("slave1", "slave2")]
    try:
        for conn in connections:
            conn.execute("SELECT pg_wal_replay_pause()")
        with TestClient(create_app(Settings(read_hosts=["slave1", "slave2"]))) as api:
            response = api.post("/user/register", json=profile_data)
            assert response.status_code == 200
            user_id = UUID(response.json()["user_id"])
            # The replicas have not applied this INSERT; login must still see the master.
            assert api.get(f"/user/get/{user_id}").status_code == 404
            login = api.post(
                "/login", json={"id": str(user_id), "password": profile_data["password"]}
            )
            assert login.status_code == 200
            assert (
                api.get("/user/search", params={"first_name": "Ив", "last_name": "Пет"}).status_code
                == 200
            )
    finally:
        for conn in connections:
            conn.execute("SELECT pg_wal_replay_resume()")
            conn.close()
        if user_id:
            with client.app.state.pool.connection() as conn:
                conn.execute("DELETE FROM users WHERE id=%s", (user_id,))
