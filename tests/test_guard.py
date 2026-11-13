import asyncio

import pytest

import guard


def test_guard_swallows_and_counts():
    guard.reset_counters()

    @guard.guard("unit.boom", default="fallback")
    async def boom():
        raise ValueError("kaboom")

    assert asyncio.run(boom()) == "fallback"
    assert guard.counters()["unit.boom"] == 1


def test_cancel_propagates():
    @guard.guard("unit.cancel")
    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())


def test_isolated_rate_limited_but_counts(monkeypatch):
    class Recorder:
        def __init__(self):
            self.calls = []

        def warning(self, *args, **kwargs):
            self.calls.append(args)

    recorder = Recorder()
    monkeypatch.setattr(guard, "logger", recorder)
    guard.reset_counters()
    exc = RuntimeError("x")
    for _ in range(8):
        guard.isolated("unit.rate", exc)
    assert guard.counters()["unit.rate"] == 8
    assert len(recorder.calls) == 1


def test_watch_task_reports(caplog):
    guard.reset_counters()

    async def go():
        async def bad():
            raise KeyError("nope")
        task = asyncio.create_task(bad())
        guard.watch_task(task, "task.unit")
        await asyncio.sleep(0)

    with caplog.at_level("WARNING"):
        asyncio.run(go())
    assert guard.counters()["task.unit"] == 1
