"""Reproduce the full experiment against an isolated Docker Compose project."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.load import measure, preflight
from benchmarks.seed import SOURCE_URL

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "social-benchmark",
    "-f",
    "compose.yaml",
    "-f",
    "compose.benchmark.yaml",
]


def command(*args: str, capture: bool = False):
    return subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def ensure_dataset(path: Path):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".download")
    try:
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as response:
            with temporary.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
        temporary.rename(path)
    finally:
        temporary.unlink(missing_ok=True)


def wait_ready(base_url: str):
    for _ in range(60):
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                if response.status == 200:
                    return
        except urllib.error.URLError, TimeoutError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Benchmark application did not become ready")


async def experiment(args):
    os.chdir(ROOT)
    directory = ROOT / "reports" / "search"
    directory.mkdir(parents=True, exist_ok=True)
    ensure_dataset(ROOT / "data" / "people.v2.csv")
    command("up", "-d", "--build", "--wait", "app")
    base_url = "http://127.0.0.1:8081"
    wait_ready(base_url)
    seed_args = [
        "exec",
        "-T",
        "app",
        "python",
        "-m",
        "benchmarks.seed",
        "--rows",
        str(args.rows),
    ]
    if args.reuse_data:
        seed_args.append("--reuse")
    command(*seed_args)

    queries = json.loads((ROOT / "benchmarks" / "queries.json").read_text())
    docker = json.loads(
        subprocess.check_output(["docker", "info", "--format", "{{json .}}"], text=True)
    )
    experiment_data = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "client_packages": {
            name: importlib.metadata.version(name) for name in ("aiohttp", "matplotlib")
        },
        "host_cpus": os.cpu_count(),
        "docker": {
            key: docker.get(key)
            for key in ("ServerVersion", "NCPU", "MemTotal", "OperatingSystem", "KernelVersion")
        },
        "resources": {"db_cpus": 2, "db_memory_gib": 2, "app_cpus": 2, "app_memory_gib": 2},
        "app_image_id": command("images", "-q", "app", capture=True).strip(),
        "db_image_id": command("images", "-q", "db", capture=True).strip(),
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_files_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ("app", "benchmarks")
            for path in sorted((ROOT / folder).glob("*.py"))
        },
        "duration_seconds": args.duration,
        "warmup_seconds": args.warmup,
        "repetitions": args.repetitions,
        "concurrency_levels": args.concurrency,
        "queries": queries,
        "runs": [],
    }
    reference = None
    for stage in ("before", "after"):
        command("exec", "-T", "app", "python", "-m", "benchmarks.database", "--stage", stage)
        digests = await preflight(base_url, queries)
        if reference is not None and digests != reference:
            raise RuntimeError("Response content changed after index creation")
        reference = digests
        experiment_data["response_sha256"] = digests
        for repetition in range(1, args.repetitions + 1):
            for concurrency in args.concurrency:
                print(f"{stage}: concurrency={concurrency}, repetition={repetition}", flush=True)
                result = await measure(
                    base_url,
                    queries,
                    digests,
                    concurrency,
                    args.duration,
                    args.warmup,
                    stage,
                    repetition,
                    directory / "raw",
                )
                experiment_data["runs"].append(result)
                (directory / "results.json").write_text(
                    json.dumps(experiment_data, ensure_ascii=False, indent=2) + "\n"
                )
                print(
                    f"  {result['throughput_rps']:.2f} successful rps; "
                    f"p95={result['latency_p95_ms']:.2f}ms; "
                    f"errors={result['errors']}/{result['attempts']}; "
                    f"elapsed={result['elapsed_seconds']:.2f}s",
                    flush=True,
                )
                await asyncio.sleep(1)
    experiment_data["finished_at_utc"] = datetime.now(UTC).isoformat()
    (directory / "results.json").write_text(
        json.dumps(experiment_data, ensure_ascii=False, indent=2) + "\n"
    )
    from benchmarks.report import build_report

    build_report(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--warmup", type=float, default=3)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 10, 100, 1000])
    parser.add_argument("--reuse-data", action="store_true")
    args = parser.parse_args()
    if min(args.rows, args.duration, args.repetitions, *args.concurrency) <= 0 or args.warmup < 0:
        parser.error("Counts and duration must be positive; warmup cannot be negative")
    asyncio.run(experiment(args))


if __name__ == "__main__":
    main()
