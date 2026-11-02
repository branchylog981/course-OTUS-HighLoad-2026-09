"""Load real names from the OTUS CSV, deterministically filling exactly --rows profiles."""

import argparse
import csv
import hashlib
import json
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg

from app.config import Settings

SOURCE_URL = "https://raw.githubusercontent.com/OtusTeam/highload/master/homework/people.v2.csv"


def people(path: Path):
    with path.open(encoding="utf-8", errors="replace", newline="") as source:
        for row in csv.reader(source):
            if len(row) != 3 or "\ufffd" in "".join(row):
                yield None
                continue
            try:
                last, first = row[0].split(" ", 1)
                birthday = date.fromisoformat(row[1])
                if not date(1900, 1, 1) <= birthday <= datetime.now(UTC).date():
                    raise ValueError("Invalid birthdate")
                if any(not field.strip() or len(field) > 100 for field in (first, last, row[2])):
                    raise ValueError("Invalid name or city")
                yield first, last, birthday, row[2]
            except ValueError:
                yield None


def seed(source: Path, rows: int, output: Path, reuse: bool = False) -> dict:
    if rows < 1:
        raise ValueError("--rows must be positive")
    with source.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    settings = Settings()
    started = time.perf_counter()
    with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5) as conn:
        existing = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        if existing:
            metadata = json.loads(output.read_text()) if output.exists() else {}
            if reuse and existing == rows == metadata.get("rows"):
                if checksum == metadata.get("source_sha256"):
                    print(f"Reuse verified dataset: {rows} rows", flush=True)
                    return metadata
            raise RuntimeError("Seeder requires an empty users table; existing data was preserved")

        first_names, last_names = Counter(), Counter()
        loaded = 0
        valid_source_rows = 0
        skipped_source_rows = 0
        passes = 0
        with conn.cursor().copy(
            """
            COPY users (
                id, first_name, second_name, birthdate, gender, biography, city, password_hash
            )
            FROM STDIN
            """
        ) as copy:
            while loaded < rows:
                passes += 1
                seen = 0
                for person in people(source):
                    if person is None:
                        if passes == 1:
                            skipped_source_rows += 1
                        continue
                    if passes == 1:
                        valid_source_rows += 1
                    seen += 1
                    first, last, birthday, city = person
                    user_id = uuid5(NAMESPACE_URL, f"otus-hw2-people-v2:{loaded + 1}")
                    copy.write_row(
                        (
                            user_id,
                            first,
                            last,
                            birthday,
                            "unspecified",
                            "Синтетическая анкета для нагрузочного тестирования",
                            city,
                            "!synthetic-account-disabled",
                        )
                    )
                    first_names[first] += 1
                    last_names[last] += 1
                    loaded += 1
                    if loaded % 100_000 == 0:
                        print(f"Loaded {loaded:,} profiles", flush=True)
                    if loaded == rows:
                        break
                if not seen:
                    raise RuntimeError("The source has no valid records")
        conn.commit()
        conn.autocommit = True
        conn.execute("VACUUM (ANALYZE) users")
        actual = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        if actual != rows:
            raise RuntimeError(f"Expected {rows} profiles, got {actual}")

    metadata = {
        "rows": loaded,
        "source_url": SOURCE_URL,
        "source_sha256": checksum,
        "source_bytes": source.stat().st_size,
        "valid_source_rows_first_pass": valid_source_rows,
        "skipped_source_rows": skipped_source_rows,
        "repeated_rows": loaded - valid_source_rows,
        "unique_first_names": len(first_names),
        "unique_last_names": len(last_names),
        "top_first_names": first_names.most_common(10),
        "top_last_names": last_names.most_common(10),
        "id_algorithm": "UUIDv5(NAMESPACE_URL, 'otus-hw2-people-v2:<1-based-row>')",
        "synthetic_accounts": "Login disabled; no shared plaintext password",
        "loaded_at_utc": datetime.now(UTC).isoformat(),
        "load_seconds": round(time.perf_counter() - started, 3),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    print(f"Verified {actual:,} profiles; metadata: {output}", flush=True)
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("/data/people.v2.csv"))
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--output", type=Path, default=Path("/reports/seed.json"))
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    seed(args.input, args.rows, args.output, args.reuse)
