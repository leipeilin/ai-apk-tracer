"""Bounded AI job scheduling and shared provider flow-control primitives."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

JobValue = TypeVar("JobValue")
ResultValue = TypeVar("ResultValue")


class TaskCircuit:
    """A task-local circuit that retains only its first non-sensitive reason."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def open(self, reason: str = "circuit_breaking_result") -> bool:
        """Open once, returning whether this call was the first opener."""

        if self._event.is_set():
            return False
        self._reason = reason
        self._event.set()
        return True

    async def wait(self) -> None:
        await self._event.wait()


class JobStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class IndexedJob(Generic[JobValue]):
    index: int
    value: JobValue


@dataclass(frozen=True, slots=True)
class IndexedJobResult(Generic[ResultValue]):
    index: int
    status: JobStatus
    value: ResultValue | None = None
    error: Exception | None = None
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerStats:
    submitted: int
    started: int
    succeeded: int
    failed: int
    skipped: int
    peak_active: int
    circuit_opened: bool

    @property
    def completed(self) -> int:
        return self.succeeded + self.failed


@dataclass(frozen=True, slots=True)
class SchedulerRun(Generic[ResultValue]):
    results: tuple[IndexedJobResult[ResultValue], ...]
    stats: SchedulerStats


class BoundedJobScheduler:
    """Run indexed jobs through a fixed number of workers."""

    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.max_concurrency = max_concurrency

    async def run(
        self,
        jobs: Iterable[IndexedJob[JobValue]],
        worker: Callable[[IndexedJob[JobValue]], Awaitable[ResultValue]],
        *,
        circuit: TaskCircuit | None = None,
        opens_circuit: Callable[[ResultValue], bool] | None = None,
        circuit_reason: str = "circuit_breaking_result",
    ) -> SchedulerRun[ResultValue]:
        """Run each unique indexed job at most once and return index-sorted results.

        The task circuit stops workers from dequeuing new work but does not cancel jobs already
        awaiting or running in ``worker``. Such in-flight jobs retain their real success/failure;
        only never-started jobs become skipped. Worker exceptions are captured per job rather than
        aborting the scheduler run.
        """

        submitted = tuple(jobs)
        indexes = [job.index for job in submitted]
        if len(set(indexes)) != len(indexes):
            raise ValueError("job indexes must be unique")

        shared_circuit = circuit or TaskCircuit()
        queue: asyncio.Queue[IndexedJob[JobValue]] = asyncio.Queue(
            maxsize=max(1, len(submitted))
        )
        for job in submitted:
            queue.put_nowait(job)

        results: dict[int, IndexedJobResult[ResultValue]] = {}
        started = 0
        succeeded = 0
        failed = 0
        active = 0
        peak_active = 0

        async def consume() -> None:
            nonlocal started, succeeded, failed, active, peak_active
            while not shared_circuit.is_open:
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                if shared_circuit.is_open:
                    queue.task_done()
                    return

                started += 1
                active += 1
                peak_active = max(peak_active, active)
                try:
                    value = await worker(job)
                except Exception as exc:
                    failed += 1
                    results[job.index] = IndexedJobResult(
                        index=job.index,
                        status=JobStatus.FAILED,
                        error=exc,
                    )
                else:
                    succeeded += 1
                    results[job.index] = IndexedJobResult(
                        index=job.index,
                        status=JobStatus.SUCCEEDED,
                        value=value,
                    )
                    if opens_circuit is not None and opens_circuit(value):
                        shared_circuit.open(circuit_reason)
                finally:
                    active -= 1
                    queue.task_done()

        worker_count = min(self.max_concurrency, len(submitted))
        async with asyncio.TaskGroup() as group:
            for _ in range(worker_count):
                group.create_task(consume())

        for job in submitted:
            if job.index not in results:
                results[job.index] = IndexedJobResult(
                    index=job.index,
                    status=JobStatus.SKIPPED,
                    skip_reason=shared_circuit.reason or "circuit_open",
                )

        ordered_results = tuple(results[index] for index in sorted(results))
        stats = SchedulerStats(
            submitted=len(submitted),
            started=started,
            succeeded=succeeded,
            failed=failed,
            skipped=len(submitted) - started,
            peak_active=peak_active,
            circuit_opened=shared_circuit.is_open,
        )
        return SchedulerRun(ordered_results, stats)


async def run_indexed_jobs(
    jobs: Iterable[IndexedJob[JobValue]],
    worker: Callable[[IndexedJob[JobValue]], Awaitable[ResultValue]],
    *,
    max_concurrency: int,
    circuit: TaskCircuit | None = None,
    opens_circuit: Callable[[ResultValue], bool] | None = None,
    circuit_reason: str = "circuit_breaking_result",
) -> SchedulerRun[ResultValue]:
    """Convenience entry point for a single bounded scheduler run."""

    return await BoundedJobScheduler(max_concurrency).run(
        jobs,
        worker,
        circuit=circuit,
        opens_circuit=opens_circuit,
        circuit_reason=circuit_reason,
    )


class AsyncClock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, delay: float) -> None: ...


class SystemAsyncClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


class CircuitOpenError(RuntimeError):
    """Raised after provider admission when the task circuit is open."""


@dataclass(frozen=True, slots=True)
class ProviderStats:
    in_flight: int
    peak_in_flight: int
    cooldown_updates: int


class ProviderLease(AbstractAsyncContextManager["ProviderLease"]):
    def __init__(self, controller: "ProviderController", circuit: TaskCircuit | None) -> None:
        self._controller = controller
        self._circuit = circuit
        self._acquired = False

    async def __aenter__(self) -> "ProviderLease":
        if not self._acquired:
            await self._controller._acquire(self._circuit)
            self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def release(self) -> None:
        if self._acquired:
            self._acquired = False
            self._controller._release()


class ProviderController:
    """Shared concurrency and Retry-After control for one provider identity."""

    def __init__(
        self,
        identity_key: str,
        *,
        max_in_flight: int,
        max_cooldown: float,
        clock: AsyncClock | None = None,
    ) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be at least 1")
        if max_cooldown < 0:
            raise ValueError("max_cooldown cannot be negative")
        self.identity_key = identity_key
        self.max_in_flight = max_in_flight
        self.max_cooldown = float(max_cooldown)
        self._clock = clock or SystemAsyncClock()
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._cooldown_until = 0.0
        self._in_flight = 0
        self._peak_in_flight = 0
        self._cooldown_updates = 0

    @property
    def stats(self) -> ProviderStats:
        return ProviderStats(
            in_flight=self._in_flight,
            peak_in_flight=self._peak_in_flight,
            cooldown_updates=self._cooldown_updates,
        )

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self._clock.monotonic())

    def note_rate_limit(self, retry_after_seconds: float | int | str) -> float:
        """Publish a shared, capped cooldown and return the applied duration."""

        try:
            requested = float(retry_after_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("Retry-After must be a number of seconds") from exc
        applied = min(self.max_cooldown, max(0.0, requested))
        self._cooldown_until = max(
            self._cooldown_until,
            self._clock.monotonic() + applied,
        )
        self._cooldown_updates += 1
        return applied

    record_rate_limit = note_rate_limit

    def lease(self, circuit: TaskCircuit | None = None) -> ProviderLease:
        return ProviderLease(self, circuit)

    async def acquire(self, circuit: TaskCircuit | None = None) -> ProviderLease:
        lease = self.lease(circuit)
        await lease.__aenter__()
        return lease

    async def _acquire(self, circuit: TaskCircuit | None) -> None:
        """Admit one request after shared cooldown and provider concurrency checks.

        Cooldown is rechecked after semaphore acquisition because another request may publish a
        later Retry-After while this caller waits. The task-local circuit is checked only after
        provider admission, then the semaphore is released before raising.
        """

        while True:
            await self._wait_for_cooldown()
            await self._semaphore.acquire()
            if self.cooldown_remaining <= 0:
                break
            self._semaphore.release()

        if circuit is not None and circuit.is_open:
            self._semaphore.release()
            raise CircuitOpenError(circuit.reason or "task circuit is open")

        self._in_flight += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

    async def _wait_for_cooldown(self) -> None:
        while (remaining := self.cooldown_remaining) > 0:
            await self._clock.sleep(remaining)

    def _release(self) -> None:
        self._in_flight -= 1
        self._semaphore.release()


class ProviderControllerRegistry:
    """Process-wide provider controllers indexed only by hashed identity.

    Controllers intentionally survive individual scheduler runs so concurrent runs targeting the
    same endpoint/model/key-environment share in-flight and Retry-After pressure. The key contains
    no secret value; the first controller's limits remain authoritative for that process identity.
    """

    def __init__(self) -> None:
        self._controllers: dict[str, ProviderController] = {}
        self._lock = threading.Lock()

    def get(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str,
        max_in_flight: int,
        max_cooldown: float,
        clock: AsyncClock | None = None,
    ) -> ProviderController:
        identity_key = provider_identity_key(base_url, model, api_key_env)
        with self._lock:
            controller = self._controllers.get(identity_key)
            if controller is None:
                controller = ProviderController(
                    identity_key,
                    max_in_flight=max_in_flight,
                    max_cooldown=max_cooldown,
                    clock=clock,
                )
                self._controllers[identity_key] = controller
            return controller

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._controllers))

    def clear(self) -> None:
        with self._lock:
            self._controllers.clear()


def provider_identity_key(base_url: str, model: str, api_key_env: str) -> str:
    """Hash non-secret provider identity fields into the registry key."""

    encoded = json.dumps(
        [base_url, model, api_key_env],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


provider_controller_registry = ProviderControllerRegistry()


def get_provider_controller(
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    max_in_flight: int,
    max_cooldown: float,
    clock: AsyncClock | None = None,
) -> ProviderController:
    return provider_controller_registry.get(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        max_in_flight=max_in_flight,
        max_cooldown=max_cooldown,
        clock=clock,
    )
