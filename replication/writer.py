"""One INSERT/COMMIT per sequence number; fsynced client acknowledgements outside PostgreSQL."""

import argparse
import json
import os
import time
from pathlib import Path

import psycopg


def write(run_id, duration, output):
    deadline = time.monotonic() + duration
    acknowledged = []
    errors = []
    seq = 0
    conn = None
    started = time.monotonic()
    with output.with_suffix(".jsonl").open("w", buffering=1) as ledger:
        try:
            while time.monotonic() < deadline:
                seq += 1
                try:
                    if conn is None or conn.closed:
                        conn = psycopg.connect(
                            connect_timeout=2,
                            options="-c statement_timeout=2000",
                            application_name="lab-writer",
                        )
                    conn.execute(
                        "INSERT INTO replication_probe VALUES (%s, %s, %s)",
                        (run_id, seq, "x" * 1024),
                    )
                    conn.commit()
                    acknowledged.append(seq)
                    ledger.write(json.dumps({"run_id": run_id, "seq": seq}) + "\n")
                    ledger.flush()
                    os.fsync(ledger.fileno())
                except psycopg.Error as exc:
                    errors.append({"seq": seq, "type": type(exc).__name__})
                    if conn is not None:
                        conn.close()
                    conn = None
                    time.sleep(0.05)
        finally:
            if conn is not None:
                conn.close()
    output.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempts": seq,
                "acknowledged": acknowledged,
                "errors": errors,
                "elapsed_s": time.monotonic() - started,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--duration", type=float, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write(args.run_id, args.duration, args.output)
