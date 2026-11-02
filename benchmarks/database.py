"""Switch the search index on the isolated benchmark DB and record exact query plans."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from app.config import Settings
from app.repository import SEARCH_SQL, prefix_pattern

INDEX_NAME = "idx_users_name_prefix"


def capture(stage: str, queries_path: Path, directory: Path):
    settings = Settings()
    queries = json.loads(queries_path.read_text())
    directory.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(
        settings.database_url.get_secret_value(), autocommit=True, connect_timeout=5
    ) as conn:
        if stage == "before":
            conn.execute("DROP INDEX IF EXISTS idx_users_name_prefix")
        else:
            migration = Path("/app/migrations/002_users_name_prefix.sql")
            conn.execute(migration.read_text())
        conn.execute("ANALYZE users")
        indexes = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s ORDER BY indexname",
            ("users",),
        ).fetchall()
        if stage == "before" and any(name != "users_pkey" for name, _ in indexes):
            raise RuntimeError(f"Unexpected baseline indexes: {indexes}")
        metadata = {
            "stage": stage,
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "version": conn.execute("SELECT version()").fetchone()[0],
            "rows": conn.execute("SELECT count(*) FROM users").fetchone()[0],
            "settings": dict(
                conn.execute(
                    "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
                    (
                        [
                            "shared_buffers",
                            "max_connections",
                            "max_parallel_workers_per_gather",
                            "work_mem",
                            "lc_collate",
                            "lc_ctype",
                            "random_page_cost",
                        ],
                    ),
                ).fetchall()
            ),
            "table_bytes": conn.execute("SELECT pg_relation_size('users')").fetchone()[0],
            "indexes": [
                {
                    "name": name,
                    "definition": definition,
                    "bytes": conn.execute(
                        "SELECT pg_relation_size(%s::regclass)", (name,)
                    ).fetchone()[0],
                }
                for name, definition in indexes
            ],
            "queries": [],
        }
        # Database locale is stored in pg_database, not exposed in pg_settings on PG17.
        collation, ctype = conn.execute(
            "SELECT datcollate, datctype FROM pg_database WHERE datname = current_database()"
        ).fetchone()
        metadata["settings"].update(lc_collate=collation, lc_ctype=ctype)
        for i, query in enumerate(queries):
            parameters = (prefix_pattern(query["first_name"]), prefix_pattern(query["last_name"]))
            rows = conn.execute(SEARCH_SQL, parameters, prepare=False).fetchall()
            plan = conn.execute(
                "EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) " + SEARCH_SQL,
                parameters,
                prepare=False,
            ).fetchone()[0][0]
            text_plan = conn.execute(
                "EXPLAIN (ANALYZE, BUFFERS, SETTINGS) " + SEARCH_SQL,
                parameters,
                prepare=False,
            ).fetchall()
            (directory / f"{stage}-{i + 1:02d}.txt").write_text(
                json.dumps(query, ensure_ascii=False)
                + "\n\n"
                + "\n".join(line[0] for line in text_plan)
                + "\n"
            )
            metadata["queries"].append(
                {
                    **query,
                    "matches": len(rows),
                    "selectivity_percent": 100 * len(rows) / metadata["rows"],
                    "plan": plan,
                }
            )
        (directory / f"{stage}.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
        )
    print(f"Captured {stage} plans and database metadata", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=["before", "after"])
    parser.add_argument("--queries", type=Path, default=Path("/app/benchmarks/queries.json"))
    parser.add_argument("--output", type=Path, default=Path("/reports/explain"))
    args = parser.parse_args()
    capture(args.stage, args.queries, args.output)
