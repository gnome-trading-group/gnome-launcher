"""Registry lookups run side by side. One at a time, a trigger of many listings (an rt_maker session holds every open
film's strikes) spends most of its Lambda's time waiting on round trips."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

THREADS = 16


def each(fn: Callable[[T], R], items: list[T]) -> list[R]:
    """fn of every item, in order; an exception in any is raised here."""
    if len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(min(THREADS, len(items))) as pool:
        return list(pool.map(fn, items))
