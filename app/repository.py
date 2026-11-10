from uuid import UUID, uuid4

from psycopg_pool import ConnectionPool

from app.models import RegisterRequest
from app.routing import ReadRouter

SEARCH_SQL = """
    SELECT id, first_name, second_name, birthdate, gender, biography, city
    FROM users
    WHERE first_name LIKE %s ESCAPE '!' AND second_name LIKE %s ESCAPE '!'
    ORDER BY id
"""


def prefix_pattern(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


class UserRepository:
    def __init__(self, pool: ConnectionPool, readers: ReadRouter | None = None):
        self.pool = pool
        self.readers = readers

    def read_connection(self):
        if self.readers is not None:
            return self.readers.connection()
        return self.pool.connection()

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
        with self.read_connection() as conn:
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

    def search(self, first_name: str, last_name: str) -> list[dict]:
        with self.read_connection() as conn:
            # A generic prepared plan cannot derive LIKE ranges from unknown prefixes.
            return conn.execute(
                SEARCH_SQL,
                (prefix_pattern(first_name), prefix_pattern(last_name)),
                prepare=False,
            ).fetchall()
