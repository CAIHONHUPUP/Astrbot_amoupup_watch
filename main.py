# -*- coding: utf-8 -*-
"""我能永远守护你（Astrbot_amoupup_watch）—— 独立社会模拟插件。

当前版本：0.1.0 骨架
  只有：装载、卸载、握手端口 describe()。
  业务逻辑必须等 guard / runtime / store / scheduler 四层就位后再写，
  否则就是绕过 docs/ISOLATION-DESIGN.md 定的边界。

设计文档：docs/ISOLATION-DESIGN.md
握手契约：docs/HANDSHAKE.md
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


PLUGIN_NAME = "Astrbot_amoupup_watch"
PLUGIN_VERSION = "0.1.0"
API_VERSION = "1.0"
FEATURES: list[str] = []  # 有真实能力并测过之后再加，amo 按它决定用不用


class WatchPlugin(Star):
    def __init__(self, context: Context | None = None, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._closed = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._started_at = 0.0

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        self._started_at = time.time()
        logger.info("[WATCH] loaded %s api=%s", PLUGIN_VERSION, API_VERSION)

    async def terminate(self):
        if self._closed.is_set():  # 幂等：重载时可能被调两次
            return
        self._closed.set()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=3.0)  # 硬截止，绝不无限等
            except Exception as exc:
                logger.warning("[WATCH] isolated scope=terminate.wait err=%r", exc)
        self._tasks.clear()
        logger.info("[WATCH] terminated tasks=%d", len(tasks))

    # ---------------- 握手端口（amo 唯一入口） ----------------
    def describe(self) -> dict:
        """同步、纯函数、不碰 IO。amo 每次握手都调它。"""
        return {
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "api_version": API_VERSION,
            "features": list(FEATURES),
            "status": "stopped" if self._closed.is_set() else "ready",
            "started_at": self._started_at,
        }
