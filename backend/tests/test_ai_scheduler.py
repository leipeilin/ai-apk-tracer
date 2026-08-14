from __future__ import annotations

import asyncio

from app.analysis.ai_scheduler import (
    BoundedJobScheduler,
    CircuitOpenError,
    IndexedJob,
    JobStatus,
    ProviderController,
    TaskCircuit,
    get_provider_controller,
    provider_controller_registry,
    provider_identity_key,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


def test_scheduler_caps_peak_active_jobs() -> None:
    async def scenario() -> None:
        active = 0
        peak = 0

        async def work(job: IndexedJob[int]) -> int:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return job.value * 2

        run = await BoundedJobScheduler(3).run(
            [IndexedJob(index, index) for index in range(12)],
            work,
        )

        assert peak == 3
        assert run.stats.peak_active == 3
        assert run.stats.started == 12
        assert run.stats.completed == 12

    asyncio.run(scenario())


def test_scheduler_orders_results_by_submitted_index_after_out_of_order_completion() -> None:
    async def scenario() -> None:
        async def work(job: IndexedJob[str]) -> str:
            await asyncio.sleep({4: 0.001, 1: 0.003, 3: 0.002}[job.index])
            return job.value

        run = await BoundedJobScheduler(3).run(
            [IndexedJob(4, "four"), IndexedJob(1, "one"), IndexedJob(3, "three")],
            work,
        )

        assert [result.index for result in run.results] == [1, 3, 4]
        assert [result.value for result in run.results] == ["one", "three", "four"]

    asyncio.run(scenario())


def test_first_circuit_breaking_result_skips_queued_jobs() -> None:
    async def scenario() -> None:
        circuit = TaskCircuit()
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        started: list[int] = []

        async def work(job: IndexedJob[int]) -> str:
            started.append(job.index)
            if job.index == 0:
                await second_started.wait()
                return "break"
            if job.index == 1:
                second_started.set()
                await release_second.wait()
            return "ok"

        run_task = asyncio.create_task(BoundedJobScheduler(2).run(
            [IndexedJob(index, index) for index in range(6)],
            work,
            circuit=circuit,
            opens_circuit=lambda result: result == "break",
        ))
        await circuit.wait()
        release_second.set()
        run = await run_task

        assert started == [0, 1]
        assert [result.status for result in run.results] == [
            JobStatus.SUCCEEDED,
            JobStatus.SUCCEEDED,
            JobStatus.SKIPPED,
            JobStatus.SKIPPED,
            JobStatus.SKIPPED,
            JobStatus.SKIPPED,
        ]
        assert {result.skip_reason for result in run.results[2:]} == {
            "circuit_breaking_result"
        }
        assert run.stats.skipped == 4
        assert run.stats.circuit_opened is True

    asyncio.run(scenario())


def test_ordinary_candidate_failure_does_not_open_circuit() -> None:
    async def scenario() -> None:
        async def work(job: IndexedJob[int]) -> int:
            if job.index == 0:
                raise ValueError("candidate-only failure")
            return job.value

        run = await BoundedJobScheduler(1).run(
            [IndexedJob(index, index) for index in range(3)],
            work,
            opens_circuit=lambda result: result < 0,
        )

        assert [result.status for result in run.results] == [
            JobStatus.FAILED,
            JobStatus.SUCCEEDED,
            JobStatus.SUCCEEDED,
        ]
        assert run.stats.failed == 1
        assert run.stats.started == 3
        assert run.stats.circuit_opened is False

    asyncio.run(scenario())


def test_provider_controller_caps_peak_in_flight_requests() -> None:
    async def scenario() -> None:
        controller = ProviderController(
            "provider-key",
            max_in_flight=2,
            max_cooldown=10,
        )
        active = 0
        peak = 0

        async def send() -> None:
            nonlocal active, peak
            async with controller.lease():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.001)
                active -= 1

        await asyncio.gather(*(send() for _ in range(8)))

        assert peak == 2
        assert controller.stats.peak_in_flight == 2
        assert controller.stats.in_flight == 0

    asyncio.run(scenario())


def test_shared_retry_after_cooldown_delays_waiting_peer_and_is_capped() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        controller = ProviderController(
            "provider-key",
            max_in_flight=1,
            max_cooldown=5,
            clock=clock,
        )
        holder = await controller.acquire()
        peer_sent = asyncio.Event()

        async def peer() -> None:
            async with controller.lease():
                peer_sent.set()

        peer_task = asyncio.create_task(peer())
        await asyncio.sleep(0)
        assert controller.note_rate_limit(30) == 5
        holder.release()
        await peer_task

        assert peer_sent.is_set()
        assert clock.sleeps == [5]
        assert controller.cooldown_remaining == 0

    asyncio.run(scenario())


def test_provider_rechecks_circuit_after_waiting_for_slot() -> None:
    async def scenario() -> None:
        controller = ProviderController(
            "provider-key",
            max_in_flight=1,
            max_cooldown=5,
        )
        circuit = TaskCircuit()
        holder = await controller.acquire()
        sent = False

        async def peer() -> None:
            nonlocal sent
            try:
                async with controller.lease(circuit):
                    sent = True
            except CircuitOpenError:
                pass

        peer_task = asyncio.create_task(peer())
        await asyncio.sleep(0)
        circuit.open("preflight_failed")
        holder.release()
        await peer_task

        assert sent is False
        assert controller.stats.in_flight == 0

    asyncio.run(scenario())


def test_provider_registry_reuses_hashed_non_secret_identity() -> None:
    provider_controller_registry.clear()
    secret = "do-not-store-this-api-key"
    first = get_provider_controller(
        base_url="https://provider.invalid/v1",
        model="model-a",
        api_key_env="PROVIDER_API_KEY",
        max_in_flight=2,
        max_cooldown=5,
    )
    second = get_provider_controller(
        base_url="https://provider.invalid/v1",
        model="model-a",
        api_key_env="PROVIDER_API_KEY",
        max_in_flight=99,
        max_cooldown=99,
    )

    expected_key = provider_identity_key(
        "https://provider.invalid/v1",
        "model-a",
        "PROVIDER_API_KEY",
    )
    assert first is second
    assert provider_controller_registry.keys() == (expected_key,)
    assert len(expected_key) == 64
    assert secret not in expected_key
    assert "PROVIDER_API_KEY" not in expected_key
    provider_controller_registry.clear()
