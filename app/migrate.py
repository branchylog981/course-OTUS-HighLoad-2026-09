from pathlib import Path

import psycopg

from app.config import Settings


def migrate() -> None:
    settings = Settings()
    migration = Path(__file__).resolve().parent.parent / "migrations" / "001_create_users.sql"
    with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5) as conn:
        conn.execute(migration.read_text(encoding="utf-8"))
    print("Migration 001 applied")


if __name__ == "__main__":
    migrate()
