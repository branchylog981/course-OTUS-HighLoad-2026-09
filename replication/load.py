"""Equal HTTP mix of get/search, closed-loop concurrency, full-body validation."""

import asyncio
import csv
import gzip
import hashlib
import itertools
import json
import time
from collections import Counter
from pathlib import Path

import aiohttp

from benchmarks.load import percentile


async def references(url, cases):
    digests = []
    async with aiohttp.ClientSession() as session:
        for case in cases:
            async with session.get(url + case["path"], params=case.get("params")) as response:
                body = await response.read()
                if response.status != 200:
                    raise RuntimeError(f"Preflight: {response.status}: {body[:200]!r}")
                data = json.loads(body)
                if case["kind"] == "get":
                    assert data["id"] == case["path"].rsplit("/", 1)[1]
                    profiles = [data]
                else:
                    profiles = data
                    assert [p["id"] for p in profiles] == sorted(p["id"] for p in profiles)
                    for profile in profiles:
                        assert profile["first_name"].startswith(case["params"]["first_name"])
                        assert profile["second_name"].startswith(case["params"]["last_name"])
                assert all("password_hash" not in p and "password" not in p for p in profiles)
                digests.append(hashlib.sha256(body).hexdigest())
    return digests


async def measure(url, cases, digests, concurrency, duration, output: Path | None):
    sequence = itertools.count()
    samples = []
    started = time.perf_counter()
    cpu_start = time.process_time()
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=concurrency), timeout=timeout
    ) as session:

        async def user():
            while time.perf_counter() < started + duration:
                index = next(sequence) % len(cases)
                case = cases[index]
                begin = time.perf_counter()
                status = "network_error"
                try:
                    async with session.get(
                        url + case["path"], params=case.get("params")
                    ) as response:
                        body = await response.read()
                        status = str(response.status)
                        if status == "200" and hashlib.sha256(body).hexdigest() != digests[index]:
                            status = "invalid_body"
                except (aiohttp.ClientError, TimeoutError) as exc:
                    status = type(exc).__name__
                end = time.perf_counter()
                samples.append(
                    (
                        begin - started,
                        end - started,
                        case["kind"],
                        index,
                        status,
                        (end - begin) * 1000,
                    )
                )

        await asyncio.gather(*(user() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(output, "wt", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["start_s", "end_s", "endpoint", "case", "status", "latency_ms"])
            writer.writerows(samples)
    result = {
        "elapsed_s": elapsed,
        "window_s": duration,
        "concurrency": concurrency,
        "client_cpu_percent_one_core": 100 * (time.process_time() - cpu_start) / elapsed,
        "endpoints": {},
    }
    for kind in ("get", "search", "total"):
        subset = [s for s in samples if kind == "total" or s[2] == kind]
        latencies = [s[5] for s in subset]
        successes = sum(s[4] == "200" for s in subset)
        result["endpoints"][kind] = {
            "attempts": len(subset),
            "successes": successes,
            "errors": len(subset) - successes,
            "rps": successes / elapsed,
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
            "p99_ms": percentile(latencies, 0.99),
            "statuses": dict(Counter(s[4] for s in subset)),
        }
    return result
