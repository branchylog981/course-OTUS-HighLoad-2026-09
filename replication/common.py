"""Docker control restricted to the three dedicated homework projects."""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports" / "replication"
NODES = ("master", "slave1", "slave2")


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Lab:
    def __init__(self, mode="read"):
        if mode not in {"read", "async", "quorum"}:
            raise ValueError(mode)
        self.mode = mode
        self.project = "social-replication" + (f"-{mode}" if mode != "read" else "")
        self.port = {"read": 8082, "async": 8083, "quorum": 8084}[mode]
        self.url = f"http://127.0.0.1:{self.port}"
        self.topology_path = ROOT / "replication" / f"topology-{mode}.env"
        self.base = ["docker", "compose", "-p", self.project, "--env-file", ".env"]
        self.topology = {}
        if self.topology_path.exists():
            for line in self.topology_path.read_text().splitlines():
                key, value = line.split("=", 1)
                self.topology[key] = value

    def arguments(self, *args):
        return [*self.base, "-f", "compose.replication.yaml", *args]

    def environment(self):
        return (
            os.environ
            | {
                "REPLICATION_APP_PORT": str(self.port),
                "LOCAL_UID": str(os.getuid()),
                "LOCAL_GID": str(os.getgid()),
            }
            | self.topology
        )

    def command(self, *args, capture=False, input=None):
        result = subprocess.run(
            self.arguments(*args),
            cwd=ROOT,
            env=self.environment(),
            input=input,
            text=True,
            check=True,
            stdout=subprocess.PIPE if capture else None,
        )
        return result.stdout

    def sql(self, node, query):
        return self.command(
            "exec",
            "-T",
            node,
            "sh",
            "-c",
            'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At',
            input=query,
            capture=True,
        ).strip()

    def json_sql(self, node, query):
        return json.loads(self.sql(node, query))

    def wait(self, predicate, description, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise RuntimeError(f"Timeout: {description}")

    def ready(self):
        def healthy():
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as response:
                    return response.status == 200
            except urllib.error.URLError, TimeoutError:
                return False

        self.wait(healthy, "API ready")

    def route(self, primary="master", readers=()):
        self.topology = {
            "PRIMARY_HOST": primary,
            "READ_HOSTS": json.dumps(list(readers), separators=(",", ":")),
            "REPLICATION_APP_PORT": str(self.port),
        }
        self.topology_path.write_text("".join(f"{k}={v}\n" for k, v in self.topology.items()))
        self.command("up", "-d", "--no-deps", "--force-recreate", "--wait", "app")
        self.ready()

    def caught_up(self, primary="master", replicas=("slave1", "slave2")):
        target = self.sql(primary, "SELECT pg_current_wal_flush_lsn();")
        for node in replicas:
            self.wait(
                lambda n=node: (
                    self.sql(n, f"SELECT pg_last_wal_replay_lsn() >= '{target}'::pg_lsn;") == "t"
                ),
                f"{node} replay {target}",
            )
        return target

    def streaming(self, primary="master", count=2):
        self.wait(
            lambda: (
                int(
                    self.sql(
                        primary, "SELECT count(*) FROM pg_stat_replication WHERE state='streaming';"
                    )
                )
                == count
            ),
            "streaming standbys",
        )

    def reset(self):
        # Only dedicated failure-lab volumes; never the normal app or HW2 database.
        if self.mode == "read":
            raise RuntimeError("Read dataset must not be removed by the failure experiment")
        self.command("down", "-v", "--remove-orphans")
        self.topology = {}
        self.topology_path.unlink(missing_ok=True)
