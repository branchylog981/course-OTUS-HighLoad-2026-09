import json
from datetime import UTC
from hashlib import sha256
from heapq import merge
from itertools import islice
from operator import itemgetter
from uuid import UUID

from psycopg import Connection
from psycopg_pool import ConnectionPool
from redis import Redis

MAX_FEED_SIZE = 1000
CACHE_TTL = 900
AUTHOR_POSTS_SQL = """
    SELECT post.*
    FROM unnest(%s::uuid[]) AS author(id)
    CROSS JOIN LATERAL (
        SELECT id, author_user_id, text, updated_at
        FROM posts
        WHERE author_user_id = author.id
        ORDER BY updated_at DESC, id DESC
        LIMIT %s
    ) AS post
    ORDER BY author.id, post.updated_at DESC, post.id DESC
"""


def redis_client(url: str) -> Redis:
    return Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, retry_on_timeout=False)


class FeedCache:
    def __init__(self, pool: ConnectionPool, redis: Redis):
        self.pool = pool
        self.redis = redis

    def page(self, user_id: UUID, offset: int, limit: int) -> list[dict]:
        if offset >= MAX_FEED_SIZE:
            return []
        return self._page(user_id, offset, min(limit, MAX_FEED_SIZE - offset))

    def rebuild(self, user_id: UUID) -> int:
        return len(self._page(user_id, 0, MAX_FEED_SIZE, rebuild=True))

    def _page(self, user_id: UUID, offset: int, limit: int, *, rebuild: bool = False) -> list[dict]:
        # A cache miss must load posts from the same snapshot as their version keys.
        with self.pool.connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            authors = conn.execute(
                """
                SELECT friends.friend_id, versions.version
                FROM friends
                LEFT JOIN author_post_versions AS versions
                    ON versions.author_user_id = friends.friend_id
                WHERE friends.user_id = %s
                ORDER BY friends.friend_id
                """,
                (user_id,),
            ).fetchall()
            signature = sha256(
                ",".join(f"{row['friend_id']}:{row['version']}" for row in authors).encode()
            ).hexdigest()
            key = f"feed:v1:{user_id}:{signature}"
            if not rebuild:
                with self.redis.pipeline() as pipe:
                    # The first list entry marks a cached feed, including an empty one.
                    exists, entries = (
                        pipe.exists(key).lrange(key, offset + 1, offset + limit).execute()
                    )
                if exists:
                    return [json.loads(entry) for entry in entries]

            keys = {
                row["friend_id"]: f"feed:author:v1:{row['friend_id']}:{row['version']}"
                for row in authors
                if row["version"] is not None
            }
            streams = self._author_streams(conn, keys, rebuild=rebuild)
            ordered = merge(*streams, key=itemgetter("updated_at", "id"), reverse=True)
            posts = []
            for post in islice(ordered, MAX_FEED_SIZE):
                posts.append({field: post[field] for field in ("id", "author_user_id", "text")})

            with self.redis.pipeline() as pipe:
                pipe.delete(key)
                pipe.rpush(key, b"", *(json.dumps(post, ensure_ascii=False) for post in posts))
                pipe.expire(key, CACHE_TTL)
                pipe.execute()
            return posts[offset : offset + limit]

    def _author_streams(
        self, conn: Connection, keys: dict[UUID, str], *, rebuild: bool
    ) -> list[list[dict]]:
        if not keys:
            return []

        cached = [None] * len(keys) if rebuild else self.redis.mget(list(keys.values()))
        streams = [json.loads(value) for value in cached if value is not None]
        missing = {author: [] for author, value in zip(keys, cached, strict=True) if value is None}
        if missing:
            for row in conn.execute(AUTHOR_POSTS_SQL, (list(missing), MAX_FEED_SIZE)):
                missing[row["author_user_id"]].append(
                    {
                        "id": str(row["id"]),
                        "author_user_id": str(row["author_user_id"]),
                        "text": row["text"],
                        "updated_at": row["updated_at"]
                        .astimezone(UTC)
                        .isoformat(timespec="microseconds"),
                    }
                )
            with self.redis.pipeline(transaction=False) as pipe:
                for author, posts in missing.items():
                    pipe.set(keys[author], json.dumps(posts, ensure_ascii=False), ex=CACHE_TTL)
                pipe.execute()
            streams.extend(missing.values())
        return streams
