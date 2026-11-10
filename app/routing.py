"""Round-robin read-only transactions; writes and login always use the primary."""

import threading
import time
from contextlib import ExitStack, contextmanager

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.config import Settings


def configure_reader(conn):
    conn.read_only = True


class ReadRouter:
    def __init__(self, settings: Settings):
        self.pools = [
            ConnectionPool(
                make_conninfo(settings.database_url.get_secret_value(), host=host),
                min_size=1,
                max_size=5,
                timeout=1,
                kwargs={
                    "row_factory": dict_row,
                    "connect_timeout": 2,
                    "application_name": "social-reader",
                    "options": "-c statement_timeout=5000",
                },
                configure=configure_reader,
                check=ConnectionPool.check_connection,
                open=False,
            )
            for host in dict.fromkeys(settings.read_hosts)
        ]
        self.lock = threading.Lock()
        self.next = 0
        self.unavailable_until = [0.0] * len(self.pools)

    def __enter__(self):
        self.stack = ExitStack()
        try:
            for pool in self.pools:
                self.stack.enter_context(pool)
            # A surviving replica is enough to start the API.
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    @contextmanager
    def connection(self):
        with self.lock:
            start = self.next
            self.next = (self.next + 1) % len(self.pools)
            candidates = [(start + offset) % len(self.pools) for offset in range(len(self.pools))]
            ready = [i for i in candidates if self.unavailable_until[i] <= time.monotonic()]
        last_error = None
        for index in ready or candidates:
            pool = self.pools[index]
            try:
                conn = pool.getconn(timeout=1)
            except (psycopg.OperationalError, PoolTimeout) as exc:
                with self.lock:
                    self.unavailable_until[index] = time.monotonic() + 5
                last_error = exc
                continue
            try:
                # Never retry a transaction after yielding: its result may be ambiguous.
                with conn.transaction():
                    yield conn
                return
            finally:
                pool.putconn(conn)
        raise last_error or psycopg.OperationalError("No read replica available")
