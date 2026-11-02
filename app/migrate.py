import argparse
from pathlib import Path

import psycopg

from app.config import Settings


def migrate(through: str | None = None) -> None:
    settings = Settings()
    directory = Path(__file__).resolve().parent.parent / "migrations"
    migrations = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if through is not None and through not in {path.name[:3] for path in migrations}:
        raise ValueError(f"Unknown migration: {through}")
    with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5) as conn:
        for path in migrations:
            if through is not None and path.name[:3] > through:
                break
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"Applied {path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply idempotent SQL migrations")
    parser.add_argument(
        "--through", help="Last migration number, e.g. 001 for the baseline benchmark"
    )
    migrate(parser.parse_args().through)
