# -*- coding: utf-8 -*-
"""Phase 16：core/scheduler.py 的 JobManager 契约（含 every() 兼容入口）。

用假 Runtime（只提供 closed + spawn），不碰真库、不起线程池。
"""
import asyncio
import time
import unittest

from core.scheduler import Scheduler


class _Runtime:
    def __init__(self):
        self.closed = asyncio.Event()
        self.spawned = []

    def spawn(self, name, coro):
        task = asyncio.create_task(coro)
        self.spawned.append(task)
        return task


class SchedulerContractTests(unittest.TestCase):
    def test_register_is_idempotent(self):
        async def go():
            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            first = scheduler.register("a", lambda: None, 1)
            second = scheduler.register("a", lambda: None, 5)
            out = await scheduler.stop()
            return first is second, second.interval, out

        same, interval, out = asyncio.run(go())
        self.assertTrue(same)
        self.assertEqual(interval, 1.0)
        self.assertEqual(out["survivors"], 0)

    def test_report_has_contract_fields(self):
        async def go():
            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            scheduler.register("a", lambda: None, 2, timeout=1, max_backoff=8)
            rows = scheduler.report()
            await scheduler.stop()
            return rows

        rows = asyncio.run(go())
        self.assertEqual(len(rows), 1)
        expected = {
            "name", "interval", "running", "last_start", "last_end", "last_ok",
            "last_error", "consecutive_failures", "next_run",
        }
        self.assertTrue(expected.issubset(set(rows[0])), rows[0])
        self.assertFalse(rows[0]["running"])

    def test_next_run_immediate_and_interval(self):
        async def go():
            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            scheduler.register("now", lambda: None, 30, immediate=True)
            scheduler.register("later", lambda: None, 5)
            before = time.monotonic()
            scheduler.start()
            rows = {row["name"]: row for row in scheduler.report()}
            await scheduler.stop()
            return before, rows

        before, rows = asyncio.run(go())
        self.assertGreaterEqual(rows["now"]["next_run"], before)
        self.assertLess(rows["now"]["next_run"] - before, 0.2)
        self.assertGreaterEqual(rows["later"]["next_run"] - before, 4.7)
        self.assertLess(rows["later"]["next_run"] - before, 5.3)

    def test_run_now_single_flight_skips(self):
        async def go():
            gate = asyncio.Event()
            started = asyncio.Event()

            async def slow():
                started.set()
                await gate.wait()

            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            scheduler.register("slow", slow, 60)
            first_task = asyncio.create_task(scheduler.run_now("slow"))
            await asyncio.wait_for(started.wait(), 1)
            second = await scheduler.run_now("slow")
            gate.set()
            first = await first_task
            row = scheduler.report()[0]
            await scheduler.stop()
            return first, second, row

        first, second, row = asyncio.run(go())
        self.assertFalse(first["skipped"])
        self.assertTrue(first["ok"])
        self.assertTrue(second["skipped"])
        self.assertEqual(row["skipped"], 1)

    def test_timeout_cancels_and_records(self):
        async def go():
            async def slow():
                await asyncio.sleep(5)

            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            scheduler.register("slow", slow, 60, timeout=0.05)
            out = await scheduler.run_now("slow")
            row = scheduler.report()[0]
            await scheduler.stop()
            return out, row

        out, row = asyncio.run(go())
        self.assertFalse(out["ok"])
        self.assertIn("timeout", out["error"])
        self.assertEqual(row["timeouts"], 1)
        self.assertFalse(row["running"])

    def test_exception_isolated(self):
        async def go():
            async def boom():
                raise ValueError("boom")

            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            scheduler.register("boom", boom, 60)
            out = await scheduler.run_now("boom")
            row = scheduler.report()[0]
            await scheduler.stop()
            return out, row

        out, row = asyncio.run(go())
        self.assertFalse(out["ok"])
        self.assertIn("ValueError: boom", out["error"])
        self.assertEqual(row["consecutive_failures"], 1)

    def test_backoff_grows_after_failure(self):
        async def go():
            def fail():
                raise RuntimeError("x")

            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            scheduler.register("b", fail, 0.02, immediate=True, max_backoff=0.5)
            scheduler.start()
            deadline = time.time() + 1
            while scheduler.report()[0]["runs"] < 1 and time.time() < deadline:
                await asyncio.sleep(0.005)
            started = time.time()
            while scheduler.report()[0]["runs"] < 2 and time.time() < deadline:
                await asyncio.sleep(0.005)
            gap = time.time() - started
            row = scheduler.report()[0]
            await scheduler.stop()
            return gap, row

        gap, row = asyncio.run(go())
        self.assertGreaterEqual(gap, 0.03)
        self.assertLess(gap, 0.6)
        self.assertGreaterEqual(row["consecutive_failures"], 2)

    def test_stop_cancels_and_waits(self):
        async def go():
            started = asyncio.Event()

            async def forever():
                started.set()
                await asyncio.sleep(30)

            runtime = _Runtime()
            scheduler = Scheduler(runtime, min_interval=0.01)
            scheduler.register("f", forever, 60, immediate=True)
            scheduler.start()
            await asyncio.wait_for(started.wait(), 1)
            began = time.time()
            out = await scheduler.stop()
            took = time.time() - began
            task = scheduler._jobs["f"].task
            return out, took, scheduler.report()[0], task

        out, took, row, task = asyncio.run(go())
        self.assertEqual(out["survivors"], 0)
        self.assertGreaterEqual(out["stopped"], 1)
        self.assertLess(took, 1.0)
        self.assertFalse(row["running"])
        self.assertTrue(task.done())

    def test_every_compat_entry(self):
        async def go():
            runtime = _Runtime()
            scheduler = Scheduler(runtime, min_interval=0.01)
            calls = []
            scheduler.every("tick", 0.02, lambda: calls.append(1), allow_fast=True)
            await asyncio.sleep(0.2)
            names = scheduler.names()
            out = await scheduler.stop()
            return calls, names, out

        calls, names, out = asyncio.run(go())
        self.assertEqual(names, ["tick"])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(out["survivors"], 0)

    def test_every_clamps_to_min_interval(self):
        async def go():
            scheduler = Scheduler(_Runtime(), min_interval=50)
            scheduler.every("slow", 1, lambda: None)
            scheduler.every("fast", 1, lambda: None, allow_fast=True)
            rows = {row["name"]: row for row in scheduler.report()}
            await scheduler.stop()
            return rows

        rows = asyncio.run(go())
        self.assertEqual(rows["slow"]["interval"], 50.0)
        self.assertEqual(rows["fast"]["interval"], 1.0)

    def test_run_now_unknown_returns_none(self):
        async def go():
            scheduler = Scheduler(_Runtime(), min_interval=0.01)
            out = await scheduler.run_now("missing")
            await scheduler.stop()
            return out

        self.assertIsNone(asyncio.run(go()))


if __name__ == "__main__":
    unittest.main()
