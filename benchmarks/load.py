"""Closed-loop HTTP load: each virtual user waits for its complete response before retrying."""

import asyncio
import csv
import gzip
import hashlib
import itertools
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import aiohttp


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


async def preflight(base_url: str, queries: list[dict]) -> list[str]:
    """Check content and ordering once; load requests compare the entire response's digest."""
    digests = []
    async with aiohttp.ClientSession() as session:
        for query in queries:
            async with session.get(base_url + "/user/search", params=query) as response:
                body = await response.read()
                if response.status != 200:
                    raise RuntimeError(f"Preflight failed: {response.status} {body[:200]!r}")
                profiles = json.loads(body)
                ids = [profile["id"] for profile in profiles]
                if ids != sorted(ids) or len(ids) != len(set(ids)):
                    raise RuntimeError("Search response is not ordered by unique id")
                for profile in profiles:
                    if not profile["first_name"].startswith(query["first_name"]):
                        raise RuntimeError("Incorrect first-name prefix")
                    if not profile["second_name"].startswith(query["last_name"]):
                        raise RuntimeError("Incorrect last-name prefix")
                    if "password" in profile or "password_hash" in profile:
                        raise RuntimeError("Search response leaked password fields")
                digests.append(hashlib.sha256(body).hexdigest())
    return digests


async def measure(
    base_url: str,
    queries: list[dict],
    digests: list[str],
    concurrency: int,
    duration: float,
    warmup: float,
    stage: str,
    repetition: int,
    output: Path,
) -> dict:
    timeout = aiohttp.ClientTimeout(total=120, connect=30)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        warmup_end = time.perf_counter() + warmup
        sequence = itertools.count()
        # Warm every query, then continue a single-user warmup. Excluded from measurements.
        warmed = 0
        while time.perf_counter() < warmup_end or warmed < len(queries):
            index = warmed % len(queries)
            async with session.get(base_url + "/user/search", params=queries[index]) as response:
                body = await response.read()
                if response.status != 200 or hashlib.sha256(body).hexdigest() != digests[index]:
                    raise RuntimeError("Warmup response failed validation")
            warmed += 1

        samples = []
        active = 0
        peak_active = 0
        cpu_started = time.process_time()
        started = time.perf_counter()
        deadline = started + duration
        started_at = datetime.now(UTC).isoformat()

        async def user():
            nonlocal active, peak_active
            while time.perf_counter() < deadline:
                index = next(sequence) % len(queries)
                request_started = time.perf_counter()
                active += 1
                peak_active = max(peak_active, active)
                status, byte_count = "network_error", 0
                try:
                    async with session.get(
                        base_url + "/user/search", params=queries[index]
                    ) as response:
                        body = await response.read()
                        byte_count = len(body)
                        status = str(response.status)
                        if response.status == 200:
                            if hashlib.sha256(body).hexdigest() != digests[index]:
                                status = "invalid_body"
                except (aiohttp.ClientError, TimeoutError) as exc:
                    status = type(exc).__name__
                finally:
                    active -= 1
                ended = time.perf_counter()
                samples.append(
                    (
                        request_started - started,
                        ended - started,
                        (ended - request_started) * 1000,
                        status,
                        byte_count,
                        index,
                    )
                )

        await asyncio.gather(*(user() for _ in range(concurrency)))
        elapsed = time.perf_counter() - started
        cpu_seconds = time.process_time() - cpu_started

    output.mkdir(parents=True, exist_ok=True)
    raw_file = output / f"{stage}-c{concurrency}-r{repetition}.csv.gz"
    with gzip.open(raw_file, "wt", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["start_s", "end_s", "latency_ms", "status", "bytes", "query_index"])
        writer.writerows(samples)
    latency = [sample[2] for sample in samples]
    good_latency = [sample[2] for sample in samples if sample[3] == "200"]
    statuses = Counter(sample[3] for sample in samples)
    successes = statuses["200"]
    return {
        "stage": stage,
        "concurrency": concurrency,
        "repetition": repetition,
        "started_at_utc": started_at,
        "request_window_seconds": duration,
        "elapsed_seconds": elapsed,
        "drain_seconds": max(0, elapsed - duration),
        "warmup_seconds": warmup,
        "attempts": len(samples),
        "successes": successes,
        "errors": len(samples) - successes,
        "statuses": dict(statuses),
        "throughput_rps": successes / elapsed,
        "attempts_rps": len(samples) / elapsed,
        "error_percent": 100 * (len(samples) - successes) / len(samples),
        "latency_mean_ms": sum(latency) / len(latency),
        "latency_p50_ms": percentile(latency, 0.5),
        "latency_p95_ms": percentile(latency, 0.95),
        "latency_p99_ms": percentile(latency, 0.99),
        "success_latency_p95_ms": percentile(good_latency, 0.95),
        "peak_inflight": peak_active,
        "mean_inflight": sum(latency) / 1000 / elapsed,
        "response_bytes": sum(sample[4] for sample in samples),
        "client_cpu_seconds": cpu_seconds,
        "client_cpu_percent_one_core": 100 * cpu_seconds / elapsed,
        "queries_attempted": dict(Counter(str(sample[5]) for sample in samples)),
        "raw_file": str(Path("raw") / raw_file.name),
    }
