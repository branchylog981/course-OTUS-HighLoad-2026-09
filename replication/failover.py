"""Crash a dedicated primary, select the freshest standby, reparent and compare client ACKs."""

import json
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from replication.common import REPORTS, Lab, save


def lsn_number(value):
    high, low = value.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def state(lab, node):
    return lab.json_sql(
        node,
        """
        SELECT json_build_object('recovery', pg_is_in_recovery(),
            'receive_lsn', pg_last_wal_receive_lsn(),
            'replay_lsn', pg_last_wal_replay_lsn(),
            'read_only', current_setting('transaction_read_only'));
    """,
    )


def experiment(mode, reset=False):
    lab = Lab(mode)
    marker = lab.topology_path.with_suffix(".started")
    if reset:
        lab.reset()
        marker.unlink(missing_ok=True)
    if marker.exists():
        raise RuntimeError(f"{mode} experiment already started; use --reset to repeat safely")
    if lab.topology.get("PRIMARY_HOST", "master") != "master":
        raise RuntimeError(f"{mode} already promoted; use --reset to repeat this failure lab")
    lab.topology["READ_HOSTS"] = '["slave1","slave2"]'
    lab.command("up", "-d", "--build", "--wait", "app")
    lab.ready()
    lab.streaming()
    lab.caught_up()
    marker.touch()
    run_id = str(uuid4())
    output = REPORTS / f"writes-{mode}.json"
    ledger = output.with_suffix(".jsonl")
    output.unlink(missing_ok=True)
    ledger.unlink(missing_ok=True)
    evidence = {
        "mode": mode,
        "run_id": run_id,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "project": lab.project,
        "crash_signal": "SIGKILL",
    }
    network = f"{lab.project}_default"
    disconnected = []
    writer = None
    try:
        if mode == "quorum":
            lab.sql(
                "master",
                """
                ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (slave1, slave2)';
                ALTER SYSTEM SET synchronous_commit = 'on';
                SELECT pg_reload_conf();
            """,
            )
            lab.wait(
                lambda: (
                    lab.sql(
                        "master",
                        """
                SELECT count(*) FROM pg_stat_replication
                WHERE state='streaming' AND sync_state='quorum';
            """,
                    )
                    == "2"
                ),
                "both quorum candidates",
            )
        else:
            # A controlled replication-link outage creates a deterministic asynchronous lag.
            # Containers keep running, but cannot receive any more WAL before the crash.
            lab.sql(
                "master",
                "ALTER SYSTEM SET synchronous_standby_names = ''; SELECT pg_reload_conf();",
            )
            for node in ("slave1", "slave2"):
                container = lab.command("ps", "-q", node, capture=True).strip()
                subprocess.run(["docker", "network", "disconnect", network, container], check=True)
                disconnected.append((node, container))
            lab.sql("master", "SELECT pg_terminate_backend(pid) FROM pg_stat_replication;")
            lab.wait(
                lambda: lab.sql("master", "SELECT count(*) FROM pg_stat_replication;") == "0",
                "replication links disconnected",
            )
        evidence["replication_before_crash"] = lab.json_sql(
            "master",
            """
            SELECT coalesce(json_agg(row_to_json(r)), '[]') FROM (
                SELECT application_name, state, sync_state, sent_lsn, write_lsn, flush_lsn,
                    replay_lsn FROM pg_stat_replication ORDER BY application_name
            ) r;
        """,
        )
        evidence["synchronous_standby_names"] = lab.sql("master", "SHOW synchronous_standby_names;")
        evidence["synchronous_commit"] = lab.sql("master", "SHOW synchronous_commit;")
        writer = subprocess.Popen(
            lab.arguments(
                "exec",
                "-T",
                "app",
                "python",
                "-m",
                "replication.writer",
                "--run-id",
                run_id,
                "--duration",
                "8",
                "--output",
                f"/reports/{output.name}",
            ),
            cwd=lab.topology_path.parent.parent,
            env=lab.environment(),
        )
        lab.wait(
            lambda: ledger.exists() and ledger.stat().st_size > 1000,
            "client acknowledgement ledger",
        )
        time.sleep(3)
        evidence["acked_before_crash"] = len(ledger.read_text().splitlines())
        evidence["primary_rows_before_crash"] = int(
            lab.sql("master", f"SELECT count(*) FROM replication_probe WHERE run_id='{run_id}';")
        )
        evidence["crashed_at_utc"] = datetime.now(UTC).isoformat()
        lab.command("kill", "-s", "SIGKILL", "master")
        # Stop and account for the writer before any promotion; failed commits are never ACKs.
        writer.wait(timeout=30)
        if writer.returncode:
            raise RuntimeError(f"Writer failed: {writer.returncode}")
        for node, container in disconnected:
            subprocess.run(
                ["docker", "network", "connect", "--alias", node, network, container], check=True
            )
        disconnected.clear()
        # A read-only endpoint can still work before the write route is switched.
        with httpx.Client(base_url=lab.url, timeout=10, trust_env=False) as api:
            response = api.get("/user/search", params={"first_name": "Absent", "last_name": "User"})
            response.raise_for_status()
            assert response.json() == []
            evidence["read_during_primary_outage"] = response.status_code
        master_id = lab.command("ps", "-aq", "master", capture=True).strip()
        evidence["old_primary_running"] = json.loads(
            subprocess.check_output(
                ["docker", "inspect", "--format", "{{json .State.Running}}", master_id], text=True
            )
        )
        assert not evidence["old_primary_running"], "Old primary must remain fenced"
        for node in ("slave1", "slave2"):
            lab.wait(
                lambda n=node: lab.sql(n, "SELECT count(*) FROM pg_stat_wal_receiver;") == "0",
                f"{node} receiver disconnected from dead primary",
            )
        evidence["candidates"] = {node: state(lab, node) for node in ("slave1", "slave2")}
        primary = max(
            evidence["candidates"],
            key=lambda n: lsn_number(
                evidence["candidates"][n]["receive_lsn"] or evidence["candidates"][n]["replay_lsn"]
            ),
        )
        follower = next(n for n in ("slave1", "slave2") if n != primary)
        evidence["promoted"] = primary
        evidence["follower"] = follower
        assert lab.sql(primary, "SELECT pg_promote(true, 60);") == "t"
        assert lab.sql(primary, "SELECT pg_is_in_recovery();") == "f"
        # Reuse the existing password and application_name without exposing primary_conninfo.
        lab.sql(
            follower,
            rf"""
            SELECT format('ALTER SYSTEM SET primary_conninfo = %L',
                regexp_replace(current_setting('primary_conninfo'), 'host=[^ ]+', 'host={primary}'))
            \gexec
            SELECT pg_reload_conf();
        """,
        )
        lab.streaming(primary, count=1)
        if mode == "quorum":
            lab.sql(
                primary,
                f"""
                ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 ({follower})';
                ALTER SYSTEM SET synchronous_commit = 'on'; SELECT pg_reload_conf();
            """,
            )
            lab.wait(
                lambda: (
                    lab.sql(
                        primary,
                        "SELECT count(*) FROM pg_stat_replication WHERE sync_state='quorum';",
                    )
                    == "1"
                ),
                "quorum on new primary",
            )
        lab.caught_up(primary, (follower,))
        client = json.loads(output.read_text())
        acknowledged = set(client["acknowledged"])
        persisted = set(
            lab.json_sql(
                primary,
                f"""
            SELECT coalesce(json_agg(seq ORDER BY seq), '[]') FROM replication_probe
            WHERE run_id='{run_id}';
        """,
            )
        )
        ledger_ids = {json.loads(line)["seq"] for line in ledger.read_text().splitlines()}
        assert ledger_ids == acknowledged
        evidence.update(
            {
                "attempts": client["attempts"],
                "acknowledged_count": len(acknowledged),
                "persisted_count": len(persisted),
                "lost_acknowledged": sorted(acknowledged - persisted),
                "persisted_without_ack": sorted(persisted - acknowledged),
                "writer_errors": client["errors"],
                "writer_elapsed_s": client["elapsed_s"],
            }
        )
        lab.route(primary, (follower,))
        body = {
            "first_name": "Failover",
            "second_name": mode,
            "birthdate": "1990-01-01",
            "biography": "API check after primary failover",
            "city": "Test",
            "password": "Failover-test-password",
        }
        with httpx.Client(base_url=lab.url, timeout=10, trust_env=False) as api:
            registered = api.post("/user/register", json=body)
            registered.raise_for_status()
            user_id = registered.json()["user_id"]
            lab.caught_up(primary, (follower,))
            profile = api.get(f"/user/get/{user_id}")
            profile.raise_for_status()
            assert profile.json()["first_name"] == "Failover"
            search = api.get("/user/search", params={"first_name": "Fail", "last_name": mode})
            search.raise_for_status()
            assert any(p["id"] == user_id for p in search.json())
            login = api.post("/login", json={"id": user_id, "password": body["password"]})
            login.raise_for_status()
        evidence["api_after_failover"] = {
            "register": registered.status_code,
            "get": profile.status_code,
            "search": search.status_code,
            "login": login.status_code,
        }
        evidence["final_roles"] = {n: state(lab, n) for n in (primary, follower)}
        evidence["finished_at_utc"] = datetime.now(UTC).isoformat()
        save(REPORTS / f"failover-{mode}.json", evidence)
        assert acknowledged, "No acknowledged writes: experiment is invalid"
        if mode == "quorum":
            assert not evidence["lost_acknowledged"], "Quorum lost acknowledged writes"
        else:
            assert evidence["lost_acknowledged"], "Controlled async partition did not cause loss"
        print(
            f"{mode}: acknowledged={len(acknowledged)}, persisted={len(persisted)}, "
            f"lost={len(evidence['lost_acknowledged'])}, promoted={primary}",
            flush=True,
        )
        return evidence
    finally:
        if writer is not None and writer.poll() is None:
            writer.terminate()
            writer.wait(timeout=10)
        for node, container in disconnected:
            subprocess.run(
                ["docker", "network", "connect", "--alias", node, network, container], check=False
            )
