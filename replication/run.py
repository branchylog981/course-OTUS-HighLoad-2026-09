"""Run read benchmarks and crash experiments on isolated PostgreSQL clusters."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from benchmarks.run import ensure_dataset
from replication.common import NODES, REPORTS, ROOT, Lab, save
from replication.load import measure, references


def counters(lab):
    return {
        node: lab.json_sql(
            node,
            """
        SELECT json_build_object(
            'get', coalesce(sum(calls) FILTER (WHERE query LIKE '%FROM users WHERE id =%'), 0),
            'search', coalesce(sum(calls) FILTER (WHERE query LIKE '%first_name LIKE%'), 0),
            'exec_ms', coalesce(sum(total_exec_time) FILTER (WHERE query LIKE '%FROM users%'), 0),
            'recovery', pg_is_in_recovery())
        FROM pg_stat_statements
        WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database());
    """,
        )
        for node in NODES
    }


def capture_cluster(lab):
    save(
        REPORTS / "cluster.json",
        {
            node: lab.json_sql(
                node,
                """
                SELECT json_build_object(
                    'version',version(), 'recovery',pg_is_in_recovery(),
                    'wal_level',current_setting('wal_level'),
                    'hot_standby',current_setting('hot_standby'),
                    'wal_keep_size',current_setting('wal_keep_size'),
                    'synchronous_standby_names',current_setting('synchronous_standby_names'),
                    'synchronous_commit',current_setting('synchronous_commit'),
                    'users',(SELECT count(*) FROM users),
                    'indexes',(SELECT json_agg(indexdef) FROM pg_indexes WHERE tablename='users'),
                    'replication',(SELECT json_agg(row_to_json(s)) FROM (
                        SELECT application_name,state,sync_state,flush_lsn,replay_lsn
                        FROM pg_stat_replication) s));
            """,
            )
            for node in NODES
        },
    )


async def read_experiment(args):
    lab = Lab()
    lab.command("up", "-d", "--build", "--wait", "app")
    lab.ready()
    lab.streaming()
    ensure_dataset(ROOT / "data" / "people.v2.csv")
    lab.command(
        "exec",
        "-T",
        "app",
        "python",
        "-m",
        "benchmarks.seed",
        "--rows",
        str(args.rows),
        "--output",
        "/reports/seed.json",
        "--reuse",
    )
    lab.caught_up()
    capture_cluster(lab)
    queries = json.loads((ROOT / "benchmarks" / "queries.json").read_text())
    cases = []
    for index, query in enumerate(queries):
        user_id = uuid5(NAMESPACE_URL, f"otus-hw2-people-v2:{index + 1}")
        pair = [
            {"kind": "get", "path": f"/user/get/{user_id}"},
            {"kind": "search", "path": "/user/search", "params": query},
        ]
        # Break parity correlation with the router's two-pool round-robin:
        # otherwise all expensive searches would reach the same replica at concurrency=1.
        cases.extend(pair if index % 2 == 0 else pair[::-1])
    result = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "rows": args.rows,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "repetitions": args.repetitions,
        "concurrency": args.concurrency,
        "cases": cases,
        "platform": platform.platform(),
        "host_cpus": os.cpu_count(),
        "docker": json.loads(
            subprocess.check_output(["docker", "info", "--format", "{{json .}}"], text=True)
        ),
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("app", "replication")
            for p in sorted((ROOT / folder).glob("*.py"))
        },
        "resources": {
            "each_db_cpus": 2,
            "each_db_memory_gib": 2,
            "app_cpus": 2,
            "app_memory_gib": 2,
            "total_read_pool_connections": 10,
            "write_pool_connections": 10,
        },
        "runs": [],
    }
    # Keep only useful environment metadata, never Docker credentials or proxy settings.
    result["docker"] = {
        k: result["docker"].get(k)
        for k in ("ServerVersion", "NCPU", "MemTotal", "OperatingSystem", "KernelVersion")
    }
    reference = None
    for stage, readers in (("master", ()), ("replicas", ("slave1", "slave2"))):
        lab.route(readers=readers)
        digests = await references(lab.url, cases)
        if reference is not None:
            assert reference == digests, "Master and replicas returned different response bodies"
        reference = digests
        result["body_sha256"] = digests
        for repetition in range(1, args.repetitions + 1):
            for concurrency in args.concurrency:
                await measure(lab.url, cases, digests, concurrency, args.warmup, None)
                before = counters(lab)
                label = f"{stage}-c{concurrency}-r{repetition}"
                print(label, flush=True)
                run = await measure(
                    lab.url,
                    cases,
                    digests,
                    concurrency,
                    args.duration,
                    REPORTS / "raw" / f"{label}.csv.gz",
                )
                after = counters(lab)
                run.update(
                    stage=stage,
                    repetition=repetition,
                    raw_file=f"raw/{label}.csv.gz",
                    counters_before=before,
                    counters_after=after,
                    sql_delta={
                        n: {k: after[n][k] - before[n][k] for k in ("get", "search", "exec_ms")}
                        for n in NODES
                    },
                )
                # Each successful HTTP call must execute exactly one SELECT on its designated role.
                for kind in ("get", "search"):
                    actual = sum(run["sql_delta"][n][kind] for n in NODES)
                    assert actual == run["endpoints"][kind]["successes"], (label, kind, actual)
                    if stage == "master":
                        assert all(run["sql_delta"][n][kind] == 0 for n in ("slave1", "slave2"))
                    else:
                        assert run["sql_delta"]["master"][kind] == 0
                        assert all(run["sql_delta"][n][kind] > 0 for n in ("slave1", "slave2"))
                result["runs"].append(run)
                save(REPORTS / "reads.json", result)
                print(
                    f"  {run['endpoints']['total']['rps']:.1f} rps, "
                    f"p95={run['endpoints']['total']['p95_ms']:.1f} ms, "
                    f"errors={run['endpoints']['total']['errors']}",
                    flush=True,
                )
    result["finished_at_utc"] = datetime.now(UTC).isoformat()
    save(REPORTS / "reads.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", choices=["all", "read", "failover"], default="all")
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--warmup", type=float, default=3)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 10, 100])
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete only the two failure-lab projects and their volumes first",
    )
    args = parser.parse_args()
    if min(args.rows, args.duration, args.warmup, args.repetitions, *args.concurrency) <= 0:
        parser.error("Counts and durations must be positive")
    REPORTS.mkdir(parents=True, exist_ok=True)
    if args.section in {"all", "read"}:
        asyncio.run(read_experiment(args))
    if args.section in {"all", "failover"}:
        from replication.failover import experiment

        for mode in ("async", "quorum"):
            experiment(mode, args.reset)
    from replication.report import build_report

    build_report()


if __name__ == "__main__":
    main()
