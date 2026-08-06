from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from time import perf_counter
from typing import Iterator

from common.io_utils import append_jsonl


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def measure_latency() -> Iterator[dict[str, float]]:
    result: dict[str, float] = {}
    start = perf_counter()
    try:
        yield result
    finally:
        result["latency_ms"] = round((perf_counter() - start) * 1000, 3)


def append_run_log(path: str, record: dict) -> None:
    record = {"timestamp": now_iso(), **record}
    append_jsonl(record, path)
