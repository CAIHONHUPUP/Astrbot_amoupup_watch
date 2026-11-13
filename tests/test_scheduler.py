import asyncio

from runtime import Runtime
from scheduler import Scheduler


def test_interval_fires_and_stops():
    rt = Runtime(io_threads=1)
    calls = []

    async def go():
        sc = Scheduler(rt, min_interval=0.01)
        sc.every("tick", 0.05, lambda: calls.append(1), allow_fast=True)
        await asyncio.sleep(0.4)
        names = sc.names()
        stopped = await sc.stop()
        await rt.shutdown()
        return names, stopped

    names, stopped = asyncio.run(go())
    assert names == ["tick"]
    assert len(calls) >= 2
    assert stopped["survivors"] == 0


def test_three_reload_cycles_no_task_growth():
    async def go():
        await asyncio.sleep(0)
        baseline = len(asyncio.all_tasks())
        for _ in range(3):
            rt = Runtime(io_threads=1)
            sc = Scheduler(rt, min_interval=0.01)
            sc.every("x", 0.05, lambda: None, allow_fast=True)
            await asyncio.sleep(0.08)
            await sc.stop()
            await rt.shutdown()
        await asyncio.sleep(0.1)
        return baseline, len(asyncio.all_tasks())

    baseline, after = asyncio.run(go())
    assert after <= baseline + 1


def test_job_exception_does_not_kill_loop():
    rt = Runtime(io_threads=1)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("boom")

    async def go():
        sc = Scheduler(rt, min_interval=0.01)
        sc.every("bad", 0.05, boom, allow_fast=True)
        await asyncio.sleep(0.35)
        await sc.stop()
        await rt.shutdown()

    asyncio.run(go())
    assert len(calls) >= 2
