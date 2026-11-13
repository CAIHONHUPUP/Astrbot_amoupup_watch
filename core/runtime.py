# -*- coding: utf-8 -*-
"""运行时：watch 自己的线程池 + 事件循环看门狗。

两条不能踩的线（docs/ISOLATION-DESIGN.md 1.2）：
1. **不用** `asyncio.to_thread` —— 那是 loop 的默认线程池，amo 也在用；
   占满它等于让 amo 的 db.init / 生图下载排队。这里永远用自己的池。
2. 看门狗是「没阻塞」的证据：正常运行时零输出，卡住时能指到具体协程。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

try:
    from ..guard import isolated, watch_task
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated, watch_task


class Runtime:
    def __init__(self, io_threads: int = 4, lag_threshold: float = 0.5, lag_interval: float = 0.1):
        self.generation = uuid.uuid4().hex[:8]
        self.closed = asyncio.Event()
        self.lag_threshold = max(0.05, float(lag_threshold))
        self.lag_interval = max(0.02, float(lag_interval))
        self._io = ThreadPoolExecutor(max_workers=max(1, int(io_threads)), thread_name_prefix="watch-io")
        self._db = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-db")
        self._tasks: dict[asyncio.Task, str] = {}
        self._lags: deque[float] = deque(maxlen=600)
        self._watchdog: asyncio.Task | None = None
        self._last_lag_log = 0.0
        self._lag_hits = 0
        # 冷启动宽限：插件加载期整个进程都在忙，这时的 lag 不是 watch 造成的，
        # 报出来只会让 amo 的日志变噪音。超过 lag_warmup 之后才开始记账。
        self.lag_warmup = 10.0
        self._lag_since = 0.0

    # ---------- 三个入口：IO / DB / 后台任务 ----------
    async def run_io(self, fn, *args, timeout: float | None = None, **kwargs):
        future = asyncio.get_running_loop().run_in_executor(
            self._io, functools.partial(fn, *args, **kwargs))
        return await asyncio.wait_for(future, timeout) if timeout else await future

    async def run_db(self, fn, *args, timeout: float | None = None, **kwargs):
        future = asyncio.get_running_loop().run_in_executor(
            self._db, functools.partial(fn, *args, **kwargs))
        return await asyncio.wait_for(future, timeout) if timeout else await future

    def spawn(self, name: str, coro):
        task = asyncio.create_task(coro, name="watch:%s:%s" % (self.generation, name))
        self._tasks[task] = name
        task.add_done_callback(self._forget)
        watch_task(task, "task.%s" % name)
        return task

    def _forget(self, task):
        self._tasks.pop(task, None)

    # ---------- 看门狗 ----------
    def start_watchdog(self):
        if self._watchdog is None or self._watchdog.done():
            self._lag_since = time.monotonic()
            self._watchdog = self.spawn("watchdog", self._watch_loop())
        return self._watchdog

    async def _watch_loop(self):
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not self.closed.is_set():
            await asyncio.sleep(self.lag_interval)
            now = loop.time()
            lag = now - last - self.lag_interval
            last = now
            if lag <= 0:
                continue
            if self._lag_since and time.monotonic() - self._lag_since < self.lag_warmup:
                continue
            self._lags.append(lag)
            if lag >= self.lag_threshold:
                self._report_lag(lag)

    def _report_lag(self, lag: float):
        self._lag_hits += 1
        now = time.monotonic()
        if now - self._last_lag_log < 10.0:  # 限流：10 秒一条
            return
        self._last_lag_log = now
        names = []
        try:
            current = asyncio.current_task()
            for task in asyncio.all_tasks():
                if task is current or task.done():
                    continue
                names.append(task.get_name())
                if len(names) >= 5:
                    break
        except Exception as exc:  # pragma: no cover
            isolated("runtime.lag_scan", exc)
        logger.warning("[WATCH] loop_lag=%.2fs hits=%d tasks=%s", lag, self._lag_hits, names)

    def lag_stats(self) -> dict:
        if not self._lags:
            return {"samples": 0, "p99": 0.0, "max": 0.0, "hits": self._lag_hits}
        ordered = sorted(self._lags)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.99))
        return {
            "samples": len(ordered),
            "p99": round(ordered[idx], 4),
            "max": round(ordered[-1], 4),
            "hits": self._lag_hits,
        }

    # ---------- 状态 / 收尾 ----------
    def tasks(self) -> dict:
        return {"count": len(self._tasks), "names": sorted(self._tasks.values())}

    async def shutdown(self, timeout: float = 3.0):
        self.closed.set()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=timeout)
            except Exception as exc:
                isolated("runtime.shutdown.wait", exc)
        survivors = [t for t in tasks if not t.done()]
        if survivors:
            logger.warning("[WATCH] shutdown: %d 个任务未在 %.0fs 内退出: %s",
                           len(survivors), timeout, [t.get_name() for t in survivors[:5]])
        self._tasks.clear()
        self._io.shutdown(wait=False, cancel_futures=True)
        self._db.shutdown(wait=False, cancel_futures=True)
        return {"cancelled": len(tasks), "survivors": len(survivors)}
