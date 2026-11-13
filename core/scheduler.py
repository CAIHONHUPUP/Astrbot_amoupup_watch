# -*- coding: utf-8 -*-
"""定时器：自己创建、自己取消、卸载时一个不留。

规则（docs/ISOLATION-DESIGN.md 1.4）：
- 只在 `initialize()` 里建，handler 里建一个热重载就翻倍；
- 循环条件永远是 `while not runtime.closed.is_set()`；
- 最小间隔 60 秒（内部 flush 用 allow_fast 显式开口子），±5% 抖动；
- 单个任务抛异常只记 `[WATCH] isolated`，不影响其它任务。
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

try:
    from ..guard import isolated
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated


class Scheduler:
    def __init__(self, runtime, min_interval: float = 60.0):
        self.runtime = runtime
        self.min_interval = float(min_interval)
        self._jobs: dict[str, asyncio.Task] = {}

    def every(self, name: str, seconds: float, fn, jitter: float = 0.05,
              immediate: bool = False, allow_fast: bool = False):
        if name in self._jobs and not self._jobs[name].done():
            return self._jobs[name]
        interval = float(seconds)
        if not allow_fast:
            interval = max(self.min_interval, interval)
        task = self.runtime.spawn("timer.%s" % name, self._loop(name, interval, fn, jitter, immediate))
        self._jobs[name] = task
        return task

    async def _loop(self, name, interval, fn, jitter, immediate):
        if not immediate:
            await asyncio.sleep(interval * (1 + random.uniform(-jitter, jitter)))
        while not self.runtime.closed.is_set():
            started = time.monotonic()
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                isolated("timer.%s" % name, exc)
            elapsed = time.monotonic() - started
            # 下一轮至少等 floor；floor 跟 interval 取小，长任务不会连环重跑，短间隔也能测
            floor = min(1.0, interval)
            wait = max(floor, interval - elapsed)
            await asyncio.sleep(wait * (1 + random.uniform(-jitter, jitter)))

    def cancel(self, name: str) -> bool:
        task = self._jobs.pop(name, None)
        if task is None:
            return False
        task.cancel()
        return True

    def names(self) -> list[str]:
        return sorted(self._jobs)

    async def stop(self, timeout: float = 3.0) -> dict:
        tasks = list(self._jobs.values())
        self._jobs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=timeout)
            except Exception as exc:
                isolated("scheduler.stop", exc)
        survivors = [t for t in tasks if not t.done()]
        if survivors:
            logger.warning("[WATCH] scheduler.stop: %d 个任务未退出", len(survivors))
        return {"stopped": len(tasks), "survivors": len(survivors)}
