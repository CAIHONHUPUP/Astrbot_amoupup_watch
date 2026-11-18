# -*- coding: utf-8 -*-
"""定时器（Phase 16 JobManager 契约，守护版）。

三个插件各有一份实现，API 与语义保持一致：

    register(name, fn, interval, *, immediate=False, timeout=None, max_backoff=None)
    start()
    async stop(timeout=3.0)
    async run_now(name)
    report() -> list[dict]

语义（与 amo_jobs.py / services/jobs.py 同一份）：
- 单飞：同一 job 上一轮还没结束就跳过本轮，累计 skipped，不并发；
- 超时：单次运行超过 timeout 用 asyncio.wait_for 取消并记 timeout；
- 隔离：job 抛出的异常只记 last_error / consecutive_failures，绝不外溢；
- 退避：连续失败按 interval * 2**failures 指数退避，封顶 max_backoff（默认 8 倍）；
- stop：取消全部 job，等待收尾（默认 3 秒），返回 stopped / survivors。

守护的调度底座与另两个插件不同，这是刻意保留的：
- 任务仍走 core/runtime.spawn（runtime.tasks()/shutdown 能看见，热重载不翻倍）；
- 固定频率纠偏：下一轮 = 上一轮开始 + interval，单轮超时也不会把周期越拖越长；
  另两个插件是「上一轮结束 + interval」（陪伴 tick / 记忆维护原本就是这样）；
- 最小间隔 60 秒（内部 flush 用 allow_fast 显式开口子），±5% 抖动；
- 循环条件同时看 runtime.closed，卸载时不会漏跑一轮。
every() 是兼容入口：main.py 的两处调用不用改，注册后立即启动（老 Scheduler 也是
在 every() 里就 spawn 的）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")


class Job:
    """单个定时任务的运行状态；只由 Scheduler 读写。"""

    def __init__(self, name, fn, interval, *, immediate, timeout, max_backoff):
        self.name = name
        self.fn = fn
        self.interval = float(interval)
        self.immediate = bool(immediate)
        self.timeout = None if timeout is None else float(timeout)
        self.max_backoff = float(max_backoff)
        self.jitter = None
        self.task = None
        self.running = False
        self.runs = 0
        self.skipped = 0
        self.timeouts = 0
        self.last_start = None
        self.last_end = None
        self.last_ok = None
        self.last_error = ""
        self.consecutive_failures = 0
        self.next_run = None

    def snapshot(self):
        return {
            "name": self.name,
            "interval": self.interval,
            "running": bool(self.running),
            "last_start": self.last_start,
            "last_end": self.last_end,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "consecutive_failures": int(self.consecutive_failures),
            "next_run": self.next_run,
            "runs": int(self.runs),
            "skipped": int(self.skipped),
            "timeouts": int(self.timeouts),
        }


class Scheduler:
    """统一 JobManager 契约 + every() 兼容入口。"""

    def __init__(self, runtime, min_interval: float = 60.0, jitter: float = 0.05):
        self.runtime = runtime
        self.min_interval = float(min_interval)
        self.jitter = max(0.0, float(jitter))
        self._jobs: dict[str, Job] = {}
        self._closed = asyncio.Event()
        self._started = False

    # ---------- 注册与启动 ----------
    def register(self, name, fn, interval, *, immediate=False, timeout=None,
                 max_backoff=None):
        """注册（或取回）一个 job；重复注册同名 job 直接返回已有对象。"""
        job = self._jobs.get(name)
        if job is not None:
            return job
        interval = float(interval)
        if interval <= 0:
            raise ValueError("job %s 的 interval 必须大于 0" % name)
        if not callable(fn):
            raise TypeError("job %s 的 fn 必须可调用" % name)
        if max_backoff is None:
            max_backoff = interval * 8.0
        if max_backoff < interval:
            max_backoff = interval
        job = Job(name, fn, interval, immediate=immediate, timeout=timeout,
                  max_backoff=max_backoff)
        self._jobs[name] = job
        if self._started:
            self._ensure_task(job)
        return job

    def every(self, name: str, seconds: float, fn, jitter: float = 0.05,
              immediate: bool = False, allow_fast: bool = False):
        """兼容入口：注册并按需启动；返回该 job 的调度任务。"""
        job = self._jobs.get(name)
        if job is not None and job.task is not None and not job.task.done():
            self._ensure_task(job)
            return job.task
        interval = float(seconds)
        if not allow_fast:
            interval = max(self.min_interval, interval)
        job = self.register(name, fn, interval, immediate=immediate)
        job.jitter = max(0.0, float(jitter))
        if self._closed.is_set():
            self._closed = asyncio.Event()
        self._started = True
        self._ensure_task(job)
        return job.task

    def start(self):
        """启动所有已注册 job；幂等，可重复调用。"""
        if self._closed.is_set():
            self._closed = asyncio.Event()
        self._started = True
        for job in list(self._jobs.values()):
            self._ensure_task(job)
        return self

    async def wait(self):
        await self._closed.wait()

    # ---------- 停止与取消 ----------
    async def stop(self, timeout: float = 3.0) -> dict:
        """取消全部 job，等待收尾后返回统计。"""
        self._closed.set()
        self._started = False
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            try:
                task.cancel()
            except Exception as exc:
                logger.debug("[WATCH] scheduler.cancel: %r", exc)
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=float(timeout))
            except Exception as exc:
                logger.debug("[WATCH] scheduler.wait: %r", exc)
        survivors = [task for task in tasks if not task.done()]
        if survivors:
            logger.warning("[WATCH] scheduler.stop: %d 个任务未退出", len(survivors))
        for job in self._jobs.values():
            job.running = False
            job.next_run = None
        return {"stopped": len(tasks), "survivors": len(survivors)}

    def cancel(self, name: str) -> bool:
        job = self._jobs.get(name)
        if job is None:
            return False
        task = job.task
        if task is None:
            return False
        job.next_run = None
        job.task = None
        task.cancel()
        return True

    def names(self) -> list[str]:
        return sorted(self._jobs)

    # ---------- 手动触发与观测 ----------
    async def run_now(self, name):
        """立刻跑一次；正在跑则跳过并记 skipped。"""
        job = self._jobs.get(name)
        if job is None:
            logger.warning("[WATCH] run_now: 未知 job %s", name)
            return None
        return await self._execute(job)

    def report(self) -> list[dict]:
        """可观测快照：每个 job 一行，按名字排序。"""
        return [self._jobs[name].snapshot() for name in self.names()]

    def summary(self) -> dict:
        rows = self.report()
        return {
            "count": len(rows),
            "running": sum(1 for row in rows if row["running"]),
            "failing": sum(1 for row in rows if row["consecutive_failures"]),
            "jobs": rows,
        }

    # ---------- 调度核心 ----------
    def _stopping(self):
        if self._closed.is_set():
            return True
        try:
            return bool(self.runtime.closed.is_set())
        except Exception as exc:
            logger.debug("[WATCH] scheduler.closed: %r", exc)
            return False

    def _ensure_task(self, job):
        if job.task is not None and not job.task.done():
            return job.task
        job.next_run = time.monotonic() + self._first_delay(job)
        try:
            job.task = self.runtime.spawn("timer.%s" % job.name, self._loop(job))
        except RuntimeError:
            job.task = None
            logger.debug("[WATCH] scheduler.spawn: no running loop for %s", job.name)
        return job.task

    def _jittered(self, wait, job=None):
        wait = max(0.0, float(wait))
        jitter = self.jitter
        if job is not None and job.jitter is not None:
            jitter = job.jitter
        if jitter and wait > 0:
            wait *= 1.0 + random.uniform(-jitter, jitter)
        return max(0.0, wait)

    def _first_delay(self, job):
        if job.immediate:
            return self._jittered(0.0, job)
        return self._jittered(job.interval, job)

    def _next_delay(self, job, elapsed):
        interval = job.interval
        if job.consecutive_failures:
            backoff = interval * (2 ** min(job.consecutive_failures, 10))
            interval = min(backoff, job.max_backoff)
        floor = min(1.0, interval)
        wait = max(floor, interval - max(0.0, elapsed))
        return self._jittered(wait, job)

    async def _loop(self, job):
        delay = self._first_delay(job)
        while not self._stopping():
            job.next_run = time.monotonic() + delay
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                job.next_run = None
                return
            if self._stopping():
                job.next_run = None
                return
            job.next_run = None
            started = time.monotonic()
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                return
            delay = self._next_delay(job, time.monotonic() - started)

    async def _execute(self, job):
        if job.running:
            job.skipped += 1
            return {"name": job.name, "skipped": True, "ok": False,
                    "error": "skipped: still running"}
        job.running = True
        job.runs += 1
        job.last_start = time.monotonic()
        ok = False
        error = ""
        try:
            result = job.fn()
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                if job.timeout:
                    await asyncio.wait_for(result, job.timeout)
                else:
                    await result
            ok = True
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except asyncio.TimeoutError:
            job.timeouts += 1
            job.consecutive_failures += 1
            error = "timeout after %ss" % job.timeout
            logger.warning("[WATCH] job %s 超时 %ss，已取消", job.name, job.timeout)
        except Exception as exc:
            job.consecutive_failures += 1
            error = "%s: %s" % (type(exc).__name__, exc)
            logger.warning("[WATCH] job %s 异常（已隔离）: %s", job.name, exc)
        finally:
            job.running = False
            job.last_end = time.monotonic()
            job.last_error = error
            if ok:
                job.last_ok = True
                job.consecutive_failures = 0
            else:
                job.last_ok = False
        return {"name": job.name, "skipped": False, "ok": ok, "error": error}


__all__ = ["Job", "Scheduler"]
