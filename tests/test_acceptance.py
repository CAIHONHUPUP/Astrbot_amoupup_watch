import asyncio
import time

import pytest

import main as watch_main


class FakeEvent:
    def __init__(self, text="watch"):
        self.message_str = text

    def plain_result(self, text):
        return text


async def _collect(plugin, text="watch"):
    out = []
    async for item in plugin.watch_cmd(FakeEvent(text)):
        out.append(item)
    return out


def _patch_store(monkeypatch, tmp_path):
    """把 Store 的根指到 tmp，别污染真实 plugin_data。"""
    real = watch_main.Store
    monkeypatch.setattr(watch_main, "Store", lambda **kw: real(root=tmp_path / "d", **kw))


def _plugin(tmp_path, **cfg):
    base = {"io_threads": 2, "storage_min_free_mb": 0, "allow_test_commands": True}
    base.update(cfg)
    return watch_main.WatchPlugin(context=None, config=base)


def test_A_handler_exception_is_isolated(tmp_path, monkeypatch):
    _patch_store(monkeypatch, tmp_path)
    plugin = _plugin(tmp_path)

    async def go():
        await plugin.initialize()
        replies = await _collect(plugin, "watch test raise")
        diag = await plugin.diag()
        await plugin.terminate()
        return replies, diag

    replies, diag = asyncio.run(go())
    assert len(replies) == 1 and "已被隔离" in replies[0]
    assert diag["status"] in ("ready", "degraded")


def test_A_port_exception_returns_default(monkeypatch):
    plugin = watch_main.WatchPlugin(context=None, config={})

    def boom():
        raise RuntimeError("port boom")

    monkeypatch.setattr(plugin, "_diag_impl", boom, raising=False)
    # describe 被 guard_sync 包着：内部异常时返回 None，不抛
    p2 = watch_main.WatchPlugin(context=None, config={})
    p2.diag_sync = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    import guard
    guard.reset_counters()
    wrapped = guard.guard_sync("port.unit", default={"status": "error"})(p2.diag_sync)
    assert wrapped() == {"status": "error"}


def test_B_heavy_task_does_not_block_loop(tmp_path, monkeypatch):
    _patch_store(monkeypatch, tmp_path)
    plugin = _plugin(tmp_path)

    async def go():
        await plugin.initialize()
        ticks = 0

        async def ticker():
            nonlocal ticks
            while not plugin._closed.is_set():
                ticks += 1
                await asyncio.sleep(0.05)

        task = asyncio.create_task(ticker())
        start = time.monotonic()
        result = await plugin._handle_test(["busy", "1.2", "good"])
        elapsed = time.monotonic() - start
        plugin._closed.set()
        await task
        lag = plugin.runtime.lag_stats()
        await plugin.terminate()
        return result, elapsed, ticks, lag

    result, elapsed, ticks, lag = asyncio.run(go())
    assert "lag=" in result and elapsed >= 1.1
    assert lag["max"] < 0.3, lag
    assert ticks >= 12, ticks


def test_C_missing_dir_bootstraps(tmp_path):
    from models.db import Store

    s = Store(root=tmp_path / "brand-new", min_free_mb=0)
    info = s.init()
    try:
        assert info["schema"] == 1
        assert (tmp_path / "brand-new" / "social.db").exists()
    finally:
        s.close()


def test_C_low_disk_degrades_readonly(tmp_path):
    from models.db import Store

    s = Store(root=tmp_path / "d", min_free_mb=10 ** 9)  # 红线高到必然触发
    s.init()
    try:
        assert s.check_space() is False
        assert s.add_event("2026-11-13", "k") is False
        assert s.readonly is True and s.degraded_reason == "disk_low"
    finally:
        s.close()


def test_D_three_reloads_no_task_growth(tmp_path, monkeypatch):
    _patch_store(monkeypatch, tmp_path)

    async def go():
        baseline = len(asyncio.all_tasks())
        gens = []
        settles = []
        for _ in range(3):
            plugin = _plugin(tmp_path)
            await plugin.initialize()
            gens.append(plugin.runtime.generation)
            settles.append(await plugin.runtime.run_db(plugin.store.settle, "2026-11-13", "daily"))
            await plugin.terminate()
            assert plugin.runtime.tasks()["count"] == 0
        await asyncio.sleep(0.05)
        return baseline, len(asyncio.all_tasks()), gens, settles

    baseline, after, gens, settles = asyncio.run(go())
    assert after <= baseline + 1
    assert len(set(gens)) == 3
    # 三次重载只有一个结算生效：幂等键 UNIQUE(day, kind) 说了算
    assert settles == [True, False, False]
