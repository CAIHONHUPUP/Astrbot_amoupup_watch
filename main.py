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
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star
except Exception:  # 离线单测 / 直接按文件导入
    logger = logging.getLogger("watch")
    AstrMessageEvent = None
    filter = None

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
DISPLAY_NAME = "我能永远守护你"
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
        self._register_web()
        logger.info("[WATCH] loaded %s (%s) api=%s generation=%s",
                    PLUGIN_VERSION, DISPLAY_NAME, API_VERSION, self.runtime.generation)

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
            "display_name": DISPLAY_NAME,
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
        out.update({"plugin": PLUGIN_NAME, "display_name": DISPLAY_NAME,
                    "version": PLUGIN_VERSION, "api": API_VERSION,
                    "status": "stopped" if self._closed.is_set() else ("degraded" if self._degraded else "ready")})
        if self.store is not None and self.runtime is not None:
            try:
                out["store"] = await asyncio.wait_for(self.runtime.run_db(self.store.stats), 2)
            except Exception as exc:
                isolated("port.diag.store", exc)
                out["store"] = {"error": type(exc).__name__}
        return out

    # ---------------- Web 面板 ----------------
    def _register_web(self):
        context = getattr(self, "context", None)
        register = getattr(context, "register_web_api", None) if context else None
        if not callable(register):
            return
        try:
            register("/Astrbot_amoupup_watch/diag", self._api_diag, ["GET"], "守护面板：运行状态")
            register("/Astrbot_amoupup_watch/finance", self._api_finance, ["GET"], "守护面板：金融")
            register("/Astrbot_amoupup_watch/finance_demo", self._api_finance_demo, ["GET"], "守护面板：金融演示数据")
            register("/Astrbot_amoupup_watch/trade", self._api_trade, ["POST"], "守护面板：下单")
            logger.info("[WATCH] web api /api/plug/Astrbot_amoupup_watch/diag 已注册")
        except Exception as exc:
            isolated("web.register", exc)

    def _finance_snapshot(self, demo: bool = False) -> dict:
        """金融概览：真实数据要等账本/持仓接入，这里先把契约定下来。"""
        if demo:
            return {
                "mode": "demo",
                "wallet": {"total": 128430.22, "available": 23180.22, "market_value": 105250.0,
                           "today_pnl": 3746.0, "today_pct": 3.69},
                "positions": [
                    {"kind": "stock", "code": "600519", "name": "贵州茅台", "qty": 100, "cost": 1600.0,
                     "price": 1650.0, "pnl": 5000.0, "pnl_pct": 3.13},
                    {"kind": "stock", "code": "300750", "name": "宁德时代", "qty": 500, "cost": 180.0,
                     "price": 176.0, "pnl": -2000.0, "pnl_pct": -2.22},
                    {"kind": "fund", "code": "005827", "name": "易方达蓝筹", "qty": 12345.67, "cost": 2.10,
                     "price": 2.18, "pnl": 987.65, "pnl_pct": 3.81},
                    {"kind": "fund", "code": "110020", "name": "沪深300ETF联接", "qty": 8000, "cost": 1.30,
                     "price": 1.27, "pnl": -240.0, "pnl_pct": -2.31},
                ],
                "trades": [
                    {"day": "11-13 10:32", "kind": "买入", "target": "600519 贵州茅台", "amount": -160000, "fee": 45.0, "note": "100 股"},
                    {"day": "11-12 15:00", "kind": "赎回", "target": "110020 沪深300ETF联接", "amount": 2540.0, "fee": 12.7, "note": "T+1 确认"},
                    {"day": "11-15 09:00", "kind": "工资", "target": "工资到账", "amount": 6800.0, "fee": 0, "note": "每月 15 日"},
                ],
                "life": {"salary": 6800, "salary_day": 15, "next_salary": "2026-12-15",
                         "rent": 0, "rent_day": 1, "utilities": 320, "month_expense": 2870.0},
                "mood": {"score": 68, "dominant": "开心", "last_event": "茅台止盈 +¥5,000"},
                "market": {"checks": 1, "limit": 2, "last": "15:00 收盘已更新", "source": "腾讯 qt.gtimg.cn"},
                "bank": {"debt": 452000.0, "monthly": 2380.0, "rate": 3.1, "limit": 120000.0,
                         "items": [{"kind": "房贷", "balance": 452000.0, "rate": 3.1, "years": 30}],
                         "note": "房贷剩余 452,000 · 3.1% · 月供 2,380"},
                "plan": {"deposit": 23180.22, "invest_cap": 50,
                         "house": {"price": 1200000, "down_pct": 30, "years": [10, 20, 30], "monthly": 3580},
                         "car": {"price": 120000, "years": 5},
                         "rule": "她不会买买不起的；投资不超过存款 50%；存款够全款才考虑全款"},
            }
        return {
            "mode": "real",
            "wallet": {"total": 0, "available": 0, "market_value": 0, "today_pnl": 0, "today_pct": 0},
            "positions": [], "trades": [],
            "life": {"salary": 6800, "salary_day": 15, "next_salary": "", "rent": 0,
                     "rent_day": 1, "utilities": 0, "month_expense": 0},
            "mood": {"score": None, "dominant": "", "last_event": ""},
            "market": {"checks": 0, "limit": 2, "last": "", "source": "腾讯 qt.gtimg.cn"},
            "bank": {"debt": 0, "monthly": 0, "rate": 0, "limit": 0, "items": [], "note": "没钱时可以先贷款，或找他转账"},
            "plan": {"deposit": 0, "invest_cap": 50,
                     "house": {"price": 0, "down_pct": 30, "years": [10, 20, 30], "monthly": 0},
                     "car": {"price": 0, "years": 5},
                     "rule": "她不会买买不起的；投资不超过存款 50%；存款够全款才考虑全款"},
        }

    async def _api_finance_demo(self, request=None):
        try:
            return self._finance_snapshot(demo=True)
        except Exception as exc:
            isolated("web.finance_demo", exc)
            return {"status": "error", "message": "demo failed"}

    async def _api_finance(self, request=None):
        try:
            demo = False
            try:
                raw = str(getattr(request, "url", "") or "")
                query = getattr(request, "query_params", None)
                if query:
                    raw += " " + str(dict(query))
                demo = "demo=1" in raw
            except Exception:
                pass
            return self._finance_snapshot(demo=demo)
        except Exception as exc:
            isolated("web.finance", exc)
            return {"status": "error", "message": "finance failed"}

    async def _api_trade(self, request=None):
        try:
            return {"ok": False, "message": "交易后端未接入（账本/持仓模块还在施工）"}
        except Exception as exc:
            isolated("web.trade", exc)
            return {"ok": False, "message": "trade failed"}

    async def _api_diag(self, request=None):
        try:
            return await self.diag()
        except Exception as exc:
            isolated("web.diag", exc)
            return {"status": "error", "message": "diag failed"}

    # ---------------- 命令（入口，必须被隔离） ----------------
    def _fmt_diag(self, data: dict) -> str:
        store = data.get("store") or {}
        tasks = data.get("tasks") or {}
        lag = data.get("lag") or {}
        lines = [
            "[%s] %s gen=%s status=%s" % (data.get("display_name", DISPLAY_NAME),
                                          data.get("version", "?"), data.get("generation", "?"),
                                          data.get("status", "?")),
            "任务 %s 个: %s" % (tasks.get("count", 0), ",".join(tasks.get("names", [])) or "-"),
            "loop lag p99=%ss max=%ss hits=%s" % (lag.get("p99", 0), lag.get("max", 0), lag.get("hits", 0)),
        ]
        if store:
            lines.append("库 events=%s people=%s edges=%s settle=%s %.2fMB free=%.0fMB readonly=%s" % (
                store.get("events", "-"), store.get("people", "-"), store.get("edges", "-"),
                store.get("settlements", "-"), store.get("db_mb", 0), store.get("free_mb", 0),
                store.get("readonly", "-")))
        return "\n".join(lines)

    async def _handle_watch(self, event) -> str:
        text = (getattr(event, "message_str", "") or "").strip()
        parts = [p for p in text.split() if p]
        if parts and parts[0].lstrip("/").lower() in ("watch", "守护"):
            parts = parts[1:]
        if parts and parts[0].lower() == "test":
            return await self._handle_test(parts[1:])
        return self._fmt_diag(await self.diag())

    async def _handle_test(self, args) -> str:
        if not bool(self.config.get("allow_test_commands")):
            return "测试命令没开（allow_test_commands=false）"
        kind = args[0].lower() if args else ""
        if kind == "raise":
            raise RuntimeError("watch test raise（故意抛，验证隔离）")
        if kind == "busy":
            seconds = float(args[1]) if len(args) > 1 else 10.0
            seconds = max(0.1, min(seconds, 60.0))
            mode = (args[2].lower() if len(args) > 2 else "good")
            if mode == "bad":
                time.sleep(seconds)          # 反例：故意卡住事件循环
            else:
                await self.runtime.run_io(time.sleep, seconds)
            return "busy %.1fs mode=%s lag=%s" % (seconds, mode, self.runtime.lag_stats())
        return "未知测试：raise | busy <秒> [good|bad]"

    async def watch_cmd(self, event: AstrMessageEvent):
        """看社会插件状态：/watch、/watch diag、/watch test raise|busy"""
        try:
            text = await self._handle_watch(event)
        except Exception as exc:
            isolated("handler.watch", exc)
            text = "[WATCH] 命令出错已被隔离，amo 不受影响（trace_id=%s）" % _tid()
        yield event.plain_result(text)

    # 命令装饰器只做注册；离线单测（没有 astrbot.api.event）时方法照样能直接调
    if filter is not None:
        watch_cmd = filter.command("watch")(watch_cmd)


def _tid() -> str:
    try:
        from .guard import trace_id
    except ImportError:
        from guard import trace_id
    return trace_id()
