"""A tiny in-process job runner for the web app (ARCHITECTURE §8, §10).

One worker thread runs jobs one at a time, in the order they fall due. Jobs are de-duplicated by
key: while a job with a key is waiting, another submit with the same key is dropped (or, with
``replace=True``, replaces the waiting one). A job may return a number of seconds: it then runs
again after that delay under the same key, which is how the multi-step work after "Sync now" and
after linking is expressed without blocking the worker (``sync.RefreshThenSync``,
``sync.Backfill``). Each job opens its own database sessions.

Jobs live only as long as the web process: the worker is a daemon thread and nothing is persisted.
A job interrupted by a restart leaves a ``running`` sync run, which the next sync marks
``interrupted``; the launchd schedule then catches up.

Tests build ``BackgroundJobs(start=False)`` and run the queue in their own thread with
:meth:`BackgroundJobs.run_all` (delays are recorded, not waited for).
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: A job: called with no arguments; returning a number re-runs it after that many seconds.
JobFn = Callable[[], Any]


@dataclass(order=True)
class _Job:
    run_at: float
    seq: int
    key: str = field(compare=False)
    fn: JobFn = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class BackgroundJobs:
    """One worker thread, jobs de-duplicated by key (see the module docstring).

    ``start=False`` never starts a thread: the caller runs the queue with :meth:`run_due` or
    :meth:`run_all` (tests, and anything that wants the work done synchronously).
    """

    def __init__(
        self,
        *,
        start: bool = True,
        name: str = "budgetapp-jobs",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._autostart = start
        self._name = name
        self._monotonic = monotonic
        self._cond = threading.Condition()
        self._heap: list[_Job] = []
        self._waiting: dict[str, _Job] = {}
        self._running: str | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._seq = itertools.count()
        #: ``(key, delay)`` of every job queued, in order (handy in tests and logs).
        self.history: list[tuple[str, float]] = []

    # ------------------------------------------------------------------ queueing

    def submit(self, key: str, fn: JobFn, *, delay: float = 0.0, replace: bool = False) -> bool:
        """Queue ``fn`` to run once after ``delay`` seconds; ``False`` when it was not queued.

        A job with the same key that is still waiting wins, unless ``replace`` is true (then the
        waiting one is cancelled and this one is queued). A running job never blocks a submit.
        """
        with self._cond:
            if self._stopped:
                return False
            existing = self._waiting.get(key)
            if existing is not None:
                if not replace:
                    return False
                existing.cancelled = True
            self._push(key, fn, max(0.0, float(delay)))
            self._cond.notify_all()
        if self._autostart:
            self._ensure_thread()
        return True

    def _push(self, key: str, fn: JobFn, delay: float) -> None:
        job = _Job(self._monotonic() + delay, next(self._seq), key, fn)
        heapq.heappush(self._heap, job)
        self._waiting[key] = job
        self.history.append((key, delay))

    def is_waiting(self, key: str) -> bool:
        with self._cond:
            return key in self._waiting

    def is_busy(self, key: str) -> bool:
        """True while a job with ``key`` is waiting or running."""
        with self._cond:
            return key in self._waiting or self._running == key

    def waiting_keys(self) -> list[str]:
        with self._cond:
            return sorted(self._waiting)

    def cancel(self, key: str) -> bool:
        """Drop the waiting job with ``key`` (a running one finishes and is not re-run)."""
        with self._cond:
            job = self._waiting.pop(key, None)
            if job is None:
                return False
            job.cancelled = True
            return True

    # ------------------------------------------------------------------ running

    def _pop_due(self, now: float | None) -> _Job | None:
        """The next job (due at ``now``; any job when ``now`` is None), or ``None``."""
        while self._heap:
            job = self._heap[0]
            if job.cancelled:
                heapq.heappop(self._heap)
                continue
            if now is not None and job.run_at > now:
                return None
            heapq.heappop(self._heap)
            if self._waiting.get(job.key) is job:
                del self._waiting[job.key]
            return job
        return None

    def _execute(self, job: _Job) -> None:
        result: Any = None
        with self._cond:
            self._running = job.key
        try:
            result = job.fn()
        except Exception:
            log.exception("Background job %s failed", job.key)
            result = None
        finally:
            with self._cond:
                self._running = None
                again = (
                    isinstance(result, (int, float))
                    and not isinstance(result, bool)
                    and result >= 0
                    and not self._stopped
                    and job.key not in self._waiting
                )
                if again:
                    self._push(job.key, job.fn, float(result))
                self._cond.notify_all()

    def run_due(self) -> int:
        """Run every job that is due now, in this thread; returns how many ran."""
        count = 0
        while True:
            with self._cond:
                job = self._pop_due(self._monotonic())
            if job is None:
                return count
            self._execute(job)
            count += 1

    def run_all(self, *, max_jobs: int = 1000) -> int:
        """Run every queued job (and its re-runs) in this thread, ignoring delays.

        Returns how many jobs ran; stops after ``max_jobs`` (a job that never finishes).
        """
        count = 0
        while count < max_jobs:
            with self._cond:
                job = self._pop_due(None)
            if job is None:
                break
            self._execute(job)
            count += 1
        return count

    def _ensure_thread(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker, name=self._name, daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._stopped:
                    job = self._pop_due(self._monotonic())
                    if job is not None:
                        break
                    live = [j for j in self._heap if not j.cancelled]
                    timeout = (
                        max(0.0, min(j.run_at for j in live) - self._monotonic()) if live else None
                    )
                    self._cond.wait(timeout)
                else:
                    return
            self._execute(job)

    def shutdown(self, *, wait: bool = False, timeout: float | None = None) -> None:
        """Stop the worker after the current job; waiting jobs are dropped."""
        with self._cond:
            self._stopped = True
            for job in self._heap:
                job.cancelled = True
            self._heap.clear()
            self._waiting.clear()
            self._cond.notify_all()
            thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)


def run_steps(
    task: Callable[[], float | None], sleep: Callable[[float], None] = time.sleep
) -> None:
    """Run a re-scheduling job synchronously: call it, sleep what it asks, until it says done."""
    while (delay := task()) is not None:
        sleep(delay)
