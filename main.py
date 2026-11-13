# -*- coding: utf-8 -*-
"""我能永远守护你（Astrbot_amoupup_watch）—— 独立社会模拟插件。

v0.1 边界层：guard / runtime / store / scheduler 四层就位，业务逻辑（world.py）还没写。
任何新功能都必须走这四层，见 docs/ISOLATION-DESIGN.md。

握手契约：docs/HANDSHAKE.md（amo 只调 describe()）
"""
from __future__ import annotations

import asyncio
import logging
import time

try:  # 正常运行时
    from astrbot.api import logger
    from astrbot.api.star import Context, Star
except Exception:  # 离线单测 / 直接按文件导入
    logger = logging.getLogger("watch")

    class Context:  # type: ignore[no-redef]
        pass

    class Star:  # type: ignore[no-redef]
        def __init__(self, context=None):
            self.context = context

try:  # 包模式
    from .guard import guard_sync, isolated
    from .runtime import Runtime
    from .scheduler import Scheduler
    from .store import Store
except ImportError:  # 直接按文件加载时
    from guard import guard_sync, isolated
    from runtime import Runtime
    from scheduler import Scheduler
    from store import Store


PLUGIN_NAME = "Astrbot_amoupup_watch"
PLUGIN_VERSION = "0.1.0"
API_VERSION = "1.0"
FEATURES: list[str] = ["diag"]


class WatchPlugin(Star):
    def __init__(self, context: Context | None = None, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.runtime: Runtime | None = None
        self.store: Store | None = None
        self.scheduler: Scheduler | None = None
        self._closed = asyncio.Event()
        self._started_at = 0.0
        self._degraded = ""

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        self._started_at = time.time()
        cfg = self.config
        self.runtime = Runtime(
            io_threads=int(cfg.get("io_threads", 4) or 4),
            lag_threshold=float(cfg.get("loop_lag_threshold", 0.5) or 0.5),
        )
        self.store = Store(min_free_mb=int(cfg.get("storage_min_free_mb", 500) or 0))
        try:
            info = await self.runtime.run_db(self.store.init, timeout=10)
            logger.info("[WATCH] store ready root=%s schema=%s free=%.0fMB",
                        info["root"], info["schema"], info["free_mb"])
        except Exception as exc:
            isolated("boot.store", exc)
            self._degraded = "store"
            logger.error("[WATCH] degraded reason=store_init err=%r", exc)
        self.runtime.start_watchdog()
        self.scheduler = Scheduler(self.runtime)
        self.scheduler.every("heartbeat", 300, self._heartbeat)
        logger.info("[WATCH] loaded %s api=%s generation=%s",
                    PLUGIN_VERSION, API_VERSION, self.runtime.generation)

    async def _heartbeat(self):
        """心跳必须走 DB 线程：scheduler 回调在事件循环上，同步写库就是阻塞 amo。"""
        if self.store is None:
            return
        try:
            await self.runtime.run_db(self.store.touch, timeout=5)
        except Exception as exc:
            isolated("timer.heartbeat", exc)

    async def terminate(self):
        if self._closed.is_set():  # 幂等：重载时可能被调两次
            return
        self._closed.set()
        result = {"scheduler": None, "runtime": None}
        if self.scheduler is not None:
            try:
                result["scheduler"] = await self.scheduler.stop()
            except Exception as exc:
                isolated("terminate.scheduler", exc)
        if self.runtime is not None:
            self.runtime.closed.set()
            if self.store is not None:
                try:
                    await self.runtime.run_db(self.store.close, timeout=2)
                except Exception as exc:
                    isolated("terminate.store", exc)
            try:
                result["runtime"] = await self.runtime.shutdown()
            except Exception as exc:
                isolated("terminate.runtime", exc)
        logger.info("[WATCH] terminated generation=%s %s",
                    getattr(self.runtime, "generation", "?"), result)

    # ---------------- 握手端口（amo 唯一入口） ----------------
    @guard_sync("port.describe", default=None)
    def describe(self) -> dict:
        """同步、纯内存、不碰 IO。amo 每次握手都调它。"""
        status = "stopped" if self._closed.is_set() else ("degraded" if self._degraded else "ready")
        tasks = self.runtime.tasks() if self.runtime is not None else {"count": 0, "names": []}
        return {
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "api_version": API_VERSION,
            "features": list(FEATURES),
            "status": status,
            "started_at": self._started_at,
            "generation": getattr(self.runtime, "generation", ""),
            "tasks": tasks["count"],
        }

    @guard_sync("port.diag", default={"status": "error"})
    def diag_sync(self) -> dict:
        return {
            "generation": getattr(self.runtime, "generation", ""),
            "tasks": self.runtime.tasks() if self.runtime else {"count": 0, "names": []},
            "lag": self.runtime.lag_stats() if self.runtime else {},
            "degraded": self._degraded,
        }

    async def diag(self) -> dict:
        out = self.diag_sync()
        if self.store is not None and self.runtime is not None:
            try:
                out["store"] = await asyncio.wait_for(self.runtime.run_db(self.store.stats), 2)
            except Exception as exc:
                isolated("port.diag.store", exc)
                out["store"] = {"error": type(exc).__name__}
        return out
