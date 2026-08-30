import queue
import time
import typing as t
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from django.db import connections, transaction
from django.utils.module_loading import import_string

PRODUCER_TASK_PATH = "benchmarks.tasks.noop_sync"
ATOMIC_CHUNK_SIZE = 500
PRODUCER_THREAD_COUNT = 8
OFFER_ACHIEVED_FLOOR = 0.98


@dataclass(frozen=True)
class RateOfferReport:
    offered: int
    achieved_rate_per_s: float
    missed_deadline_count: int
    enqueue_p50_s: float
    enqueue_p99_s: float
    offered_ok: bool


def preload_tasks(
    task_path: str,
    count: int,
    *,
    threads: int = 4,
    kwargs: dict[str, t.Any] | None = None,
) -> float:
    """Enqueue ``count`` tasks as fast as the producer can, returning elapsed seconds.

    Threaded because ``AbsurdBackend.enqueue`` is one ``spawn`` round trip with no bulk
    path, so a single connection caps the preload at that call's latency.
    """
    task_object = import_string(task_path)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(enqueue_chunk, task_object, size, kwargs or {})
            for size in split_evenly(count, threads)
        ]
        for future in futures:
            future.result()
    return time.monotonic() - started


def run_rate_producer(
    task_path: str,
    rate_per_s: float,
    duration_s: float,
    *,
    threads: int = 4,
    kwargs: dict[str, t.Any] | None = None,
) -> RateOfferReport:
    """Offer tasks at ``rate_per_s`` for ``duration_s`` against absolute deadlines."""
    task_object = import_string(task_path)
    offered = max(1, int(rate_per_s * duration_s))
    latencies: list[float] = []
    missed = 0
    with ThreadPoolExecutor(max_workers=threads) as pool:
        # Origin taken INSIDE the pool context: timed from before executor setup, the
        # opening offers are late by construction and every report reads as degraded.
        started = time.monotonic()
        deadlines: queue.Queue[tuple[int, float]] = queue.Queue()
        for index in range(offered):
            deadlines.put((index, started + index / rate_per_s))
        futures = [
            pool.submit(offer_until_drained, task_object, deadlines, kwargs or {})
            for _ in range(threads)
        ]
        for future in futures:
            thread_latencies, thread_missed = future.result()
            latencies.extend(thread_latencies)
            missed += thread_missed
    achieved = offered / (time.monotonic() - started)
    return RateOfferReport(
        offered=offered,
        achieved_rate_per_s=achieved,
        missed_deadline_count=missed,
        enqueue_p50_s=read_percentile(latencies, 0.50),
        enqueue_p99_s=read_percentile(latencies, 0.99),
        offered_ok=achieved >= OFFER_ACHIEVED_FLOOR * rate_per_s,
    )


def run_producer_benchmark(
    mode: t.Literal["single", "threaded", "atomic"], count: int
) -> dict[str, t.Any]:
    """Stage F: enqueue ``count`` tasks three ways, reporting the producer's cost."""
    task_object = import_string(PRODUCER_TASK_PATH)
    started = time.monotonic()
    if mode == "single":
        latencies = enqueue_chunk(task_object, count, {})
    elif mode == "atomic":
        latencies = enqueue_atomic_chunks(task_object, count, {})
    else:
        latencies = []
        with ThreadPoolExecutor(max_workers=PRODUCER_THREAD_COUNT) as pool:
            futures = [
                pool.submit(enqueue_chunk, task_object, size, {})
                for size in split_evenly(count, PRODUCER_THREAD_COUNT)
            ]
            for future in futures:
                latencies.extend(future.result())
    elapsed = time.monotonic() - started
    return {
        "mode": mode,
        "count": count,
        "enqueues_per_s": count / elapsed,
        "enqueue_p50_s": read_percentile(latencies, 0.50),
        "enqueue_p99_s": read_percentile(latencies, 0.99),
    }


def enqueue_chunk(
    task_object: t.Any, count: int, kwargs: dict[str, t.Any]
) -> list[float]:
    latencies: list[float] = []
    try:
        for _ in range(count):
            started = time.perf_counter()
            task_object.enqueue(**kwargs)
            latencies.append(time.perf_counter() - started)
    finally:
        # Each pool thread opened its own connection; a full benchmark run leaks past
        # max_connections without this.
        connections.close_all()
    return latencies


def enqueue_atomic_chunks(
    task_object: t.Any, count: int, kwargs: dict[str, t.Any]
) -> list[float]:
    latencies: list[float] = []
    try:
        for offset in range(0, count, ATOMIC_CHUNK_SIZE):
            with transaction.atomic():
                for _ in range(min(ATOMIC_CHUNK_SIZE, count - offset)):
                    started = time.perf_counter()
                    task_object.enqueue(**kwargs)
                    latencies.append(time.perf_counter() - started)
    finally:
        connections.close_all()
    return latencies


def offer_until_drained(
    task_object: t.Any,
    deadlines: "queue.Queue[tuple[int, float]]",
    kwargs: dict[str, t.Any],
) -> tuple[list[float], int]:
    latencies: list[float] = []
    missed = 0
    try:
        while True:
            try:
                index, due = deadlines.get_nowait()
            except queue.Empty:
                break
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif index:
                # Offer 0 defines the series origin, so it has no deadline to be late
                # for; counting it would put a permanent 1 in every report.
                missed += 1
            started = time.perf_counter()
            task_object.enqueue(**kwargs)
            latencies.append(time.perf_counter() - started)
    finally:
        connections.close_all()
    return latencies, missed


def split_evenly(total: int, parts: int) -> list[int]:
    base, remainder = divmod(total, parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def read_percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]
