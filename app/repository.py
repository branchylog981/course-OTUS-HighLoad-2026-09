from uuid import UUID, uuid4

from psycopg_pool import ConnectionPool

from app.models import RegisterRequest


class UserRepository:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def create(self, user: RegisterRequest, password_hash: str) -> UUID:
        user_id = uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO users
                    (id, first_name, second_name, birthdate, gender, biography, city, password_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    user.first_name,
                    user.second_name,
                    user.birthdate,
                    user.gender.value,
                    user.biography,
                    user.city,
                    password_hash,
                ),
            )
        return user_id

    def get_profile(self, user_id: UUID) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT id, first_name, second_name, birthdate, gender, biography, city
                FROM users WHERE id = %s
                """,
                (user_id,),
            ).fetchone()

    def get_password_hash(self, user_id: UUID) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return row["password_hash"] if row else None
