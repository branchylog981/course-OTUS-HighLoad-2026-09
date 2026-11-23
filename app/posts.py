from uuid import UUID, uuid4

from psycopg_pool import ConnectionPool


class PostRepository:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def add_friend(self, user_id: UUID, friend_id: UUID) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO friends (user_id, friend_id) VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (user_id, friend_id),
            )

    def delete_friend(self, user_id: UUID, friend_id: UUID) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "DELETE FROM friends WHERE user_id = %s AND friend_id = %s",
                (user_id, friend_id),
            )

    def create(self, author_id: UUID, text: str) -> UUID:
        post_id = uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO posts (id, author_user_id, text) VALUES (%s, %s, %s)",
                (post_id, author_id, text),
            )
        return post_id

    def update(self, author_id: UUID, post_id: UUID, text: str) -> bool:
        with self.pool.connection() as conn:
            return bool(
                conn.execute(
                    """
                    UPDATE posts SET text = %s, updated_at = clock_timestamp()
                    WHERE id = %s AND author_user_id = %s
                    """,
                    (text, post_id, author_id),
                ).rowcount
            )

    def delete(self, author_id: UUID, post_id: UUID) -> bool:
        with self.pool.connection() as conn:
            return bool(
                conn.execute(
                    "DELETE FROM posts WHERE id = %s AND author_user_id = %s",
                    (post_id, author_id),
                ).rowcount
            )

    def get(self, post_id: UUID) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT id, author_user_id, text FROM posts WHERE id = %s", (post_id,)
            ).fetchone()
