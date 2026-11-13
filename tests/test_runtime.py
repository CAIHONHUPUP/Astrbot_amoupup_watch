import asyncio
import threading

from core.runtime import Runtime


def test_run_io_uses_own_pool():
    rt = Runtime(io_threads=1)

    async def go():
        name = await rt.run_io(lambda: threading.current_thread().name)
        await rt.shutdown()
        return name

    assert asyncio.run(go()).startswith("watch-io")


def test_run_db_is_single_thread():
    rt = Runtime(io_threads=2)

    async def go():
        names = [await rt.run_db(lambda: threading.current_thread().name) for _ in range(3)]
        await rt.shutdown()
        return names

    names = asyncio.run(go())
    assert len(set(names)) == 1 and names[0].startswith("watch-db")


def test_spawn_and_snapshot():
    rt = Runtime(io_threads=1)

    async def go():
        async def sleeper():
            await asyncio.sleep(0.2)
        rt.spawn("a", sleeper())
        rt.spawn("b", sleeper())
        snap = rt.tasks()
        await rt.shutdown()
        return snap

    snap = asyncio.run(go())
    assert snap["count"] == 2 and snap["names"] == ["a", "b"]


def test_lag_stats_default():
    assert Runtime().lag_stats()["samples"] == 0
