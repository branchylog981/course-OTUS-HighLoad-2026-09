import argparse
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings
from app.feed import FeedCache, redis_client
from app.security import Security

DEMO_USERS = tuple(UUID(int=0x4000 + number) for number in range(1, 5))
DEMO_PASSWORD = "Feed-demo-2026"


def seed(pool: ConnectionPool, settings: Settings) -> None:
    viewer, first_friend, second_friend, outsider = DEMO_USERS
    names = ("Анна", "Борис", "Вера", "Глеб")
    password_hash = Security(settings).hash_password(DEMO_PASSWORD)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO users
                (id, first_name, second_name, birthdate, gender, biography, city, password_hash)
            VALUES (%s, %s, 'Демо', DATE '1995-01-01', 'unspecified', '', 'Москва', %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [(user, name, password_hash) for user, name in zip(DEMO_USERS, names, strict=True)],
        )
        cur.executemany(
            "INSERT INTO friends (user_id, friend_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [(viewer, first_friend), (viewer, second_friend)],
        )
        cur.execute(
            """
            INSERT INTO posts (id, author_user_id, text, created_at, updated_at)
            SELECT md5('otus-homework4-post:' || number)::uuid,
                   CASE WHEN number = 1051 THEN %s::uuid
                        WHEN number %% 2 = 0 THEN %s::uuid ELSE %s::uuid END,
                   'Демонстрационный пост №' || number,
                   TIMESTAMPTZ '2026-01-01 00:00:00+00' + number * INTERVAL '1 minute',
                   TIMESTAMPTZ '2026-01-01 00:00:00+00' + number * INTERVAL '1 minute'
            FROM generate_series(1, 1051) AS series(number)
            ON CONFLICT (id) DO NOTHING
            """,
            (outsider, first_friend, second_friend),
        )
    for role, user_id in zip(
        ("viewer", "friend 1", "friend 2", "outsider"), DEMO_USERS, strict=True
    ):
        print(f"{role}: {user_id}")
    print(f"Demo password: {DEMO_PASSWORD}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage demo feed data and rebuild the feed cache")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="Load demo users, friendships and posts into PostgreSQL")
    rebuild = commands.add_parser("rebuild", help="Rebuild one feed from PostgreSQL")
    rebuild.add_argument("user_id", type=UUID)
    args = parser.parse_args()

    settings = Settings()
    redis_url = settings.redis_url.get_secret_value()
    if args.command == "rebuild" and not redis_url:
        parser.error("REDIS_URL is required to rebuild a feed")
    with ConnectionPool(
        settings.database_url.get_secret_value(),
        kwargs={"row_factory": dict_row, "connect_timeout": 5},
        min_size=1,
        max_size=1,
        timeout=5,
        open=False,
    ) as pool:
        if args.command == "seed":
            seed(pool, settings)
        else:
            with redis_client(redis_url) as cache:
                count = FeedCache(pool, cache).rebuild(args.user_id)
            print(f"Cached {count} posts for {args.user_id}")


if __name__ == "__main__":
    main()
