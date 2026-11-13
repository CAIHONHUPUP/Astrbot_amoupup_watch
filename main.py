# -*- coding: utf-8 -*-
"""我能永远守护你（Astrbot_amoupup_watch）—— 独立社会模拟插件。

v0.1 边界层：guard / runtime / store / scheduler 四层就位，业务逻辑（world.py）还没写。
任何新功能都必须走这四层，见 docs/ISOLATION-DESIGN.md。

握手契约：docs/HANDSHAKE.md（amo 只调 describe()）
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
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
    from .finance_candidates import CandidatePools
    from .finance_consume import FinanceConsume
    from .finance_daily import FinanceDaily
    from .finance_decision import FinanceDecision
    from .finance_help import FinanceHelp
    from .finance_life import FinanceLife
    from .finance_link import FinanceLink
    from .finance_memory_tier import TieredMemory
    from .finance_store import FinanceRepo
    from .finance_trading import Trading
    from .guard import guard_sync, isolated
    from .runtime import Runtime
    from .scheduler import Scheduler
    from .store import Store
except ImportError:  # 直接按文件加载时
    from finance_candidates import CandidatePools
    from finance_consume import FinanceConsume
    from finance_daily import FinanceDaily
    from finance_decision import FinanceDecision
    from finance_help import FinanceHelp
    from finance_life import FinanceLife
    from finance_link import FinanceLink
    from finance_memory_tier import TieredMemory
    from finance_store import FinanceRepo
    from finance_trading import Trading
    from guard import guard_sync, isolated
    from runtime import Runtime
    from scheduler import Scheduler
    from store import Store


PLUGIN_NAME = "Astrbot_amoupup_watch"
DISPLAY_NAME = "我能永远守护你"
PLUGIN_VERSION = "0.1.0"
API_VERSION = "1.0"
FEATURES: list[str] = ["diag", "finance"]


class WatchPlugin(Star):
    def __init__(self, context: Context | None = None, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.runtime: Runtime | None = None
        self.store: Store | None = None
        self.scheduler: Scheduler | None = None
        self.finance = FinanceLink(self)
        self.finance_repo = None
        self.trading = None
        self.finance_life = None
        self.finance_help = None
        self.finance_consume = None
        self.finance_decision = None
        self.finance_daily = None
        self.finance_memory = None
        self.finance_pools = None
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
        if self.store is not None:
            self.finance_repo = FinanceRepo(self.store)
            self.trading = Trading(self, self.runtime, self.finance_repo, self.finance)
            self.finance_life = FinanceLife(self, self.finance_repo, self.finance)
            self.finance_help = FinanceHelp(self, self.finance_repo, self.finance)
            self.finance_consume = FinanceConsume(self, self.finance_repo, self.finance)
            self.finance_daily = FinanceDaily(self, self.finance_repo, self.finance, self.finance_consume)
            try:
                self.finance.backfill_withdrawable()
            except Exception as exc:
                isolated("finance.withdrawable_boot", exc)
            self.finance_memory = TieredMemory(self, self.finance_repo, self.finance.memory)
            self.finance_pools = CandidatePools(self, self.finance_repo)
            self.finance_decision = FinanceDecision(
                self, self.runtime, self.finance_repo, self.finance, self.trading,
                self.finance_help, self.finance_pools)
            try:
                await self.runtime.run_db(self.trading.refresh_available, timeout=5)
            except Exception as exc:
                isolated("finance.available", exc)
        self.runtime.start_watchdog()
        self.scheduler = Scheduler(self.runtime)
        self.scheduler.every("heartbeat", 300, self._heartbeat)
        if self.finance_life is not None:
            self.scheduler.every("finance_settle", 120, self._finance_tick, allow_fast=True)
        self._register_web()
        logger.info("[WATCH] loaded %s (%s) api=%s generation=%s",
                    PLUGIN_VERSION, DISPLAY_NAME, API_VERSION, self.runtime.generation)

    async def _process_pending(self):
        """到期的待入账：卖出资金 T+1 转可取，基金赎回 T+7 到账。"""
        repo = self.finance_repo
        amo = self.finance._amo()
        if repo is None or amo is None:
            return
        today = time.strftime("%Y-%m-%d")
        try:
            rows = await self.runtime.run_db(repo.due_pending, today, timeout=5)
        except Exception as exc:
            isolated("finance.pending.query", exc)
            return
        for row in rows or []:
            try:
                amount = int(row.get("amount_cents") or 0)
                kind = str(row.get("kind") or "")
                idem = str(row.get("idem") or "")
                if kind == "withdrawable":
                    amo.wallet.credit(amount, "卖出资金可取", idem + ":w", account="withdrawable")
                elif kind == "cash_withdrawable":
                    amo.wallet.credit(amount, "基金赎回到账", idem)
                    amo.wallet.credit(amount, "基金赎回可取", idem + ":w", account="withdrawable")
                repo.finish_pending(idem)
            except Exception as exc:
                isolated("finance.pending.apply", exc)

    async def _finance_tick(self):
        """每 30 分钟检查一次：工资 / 房租 / 水电是否到了该结算的日期。"""
        try:
            self.finance.backfill_withdrawable()
            await self.finance_life.tick()
            await self._process_pending()
            if self.finance_help is not None:
                self.finance_help.maybe_reset()
                await self.finance_help.repay_tick()
            if self.finance_decision is not None:
                await self.finance_decision.tick()
            if self.finance_daily is not None:
                await self.finance_daily.tick()
        except Exception as exc:
            isolated("finance.settle", exc)

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
            register("/Astrbot_amoupup_watch/finance/help", self._api_finance_help, ["GET"], "守护面板：资金紧张")
            register("/Astrbot_amoupup_watch/finance/loan", self._api_loan, ["POST"], "守护面板：借款")
            register("/Astrbot_amoupup_watch/finance/consume", self._api_consume, ["POST"], "守护面板：消费")
            register("/Astrbot_amoupup_watch/finance/decision", self._api_decision, ["POST"], "守护面板：决策")
            register("/Astrbot_amoupup_watch/finance/debug", self._api_debug, ["POST"], "守护面板：调试触发")
            register("/Astrbot_amoupup_watch/trade", self._api_trade, ["POST"], "守护面板：下单")
            logger.info("[WATCH] web api /api/plug/Astrbot_amoupup_watch/diag 已注册")
        except Exception as exc:
            isolated("web.register", exc)

    def _finance_provider(self):
        """金融插件（Astrbot_amoupup_finance）装了就把面板交给它，没装回退空态。"""
        try:
            context = getattr(self, "context", None)
            getter = getattr(context, "get_registered_star", None) if context else None
            meta = getter("Astrbot_amoupup_finance") if callable(getter) else None
            if meta is None or getattr(meta, "activated", True) is False:
                return None
            instance = getattr(meta, "star_cls", None)
            if instance is not None and callable(getattr(instance, "snapshot", None)):
                return instance
        except Exception as exc:
            isolated("finance.provider", exc)
        return None

    def _snapshot_real(self) -> dict:
        cash = self.finance.balance_cents() or 0
        rows = []
        if self.finance_repo is not None:
            try:
                rows = self.finance_repo.positions()
            except Exception as exc:
                isolated("finance.snapshot.positions", exc)
        positions = []
        market_value = 0
        total_pnl = 0
        for row in rows:
            qty = float(row.get("qty") or 0)
            cost = int(row.get("cost_cents") or 0)
            last = int(row.get("last_cents") or cost)
            pnl = int(round((last - cost) * qty))
            base = cost * qty
            total_pnl += pnl
            market_value += int(round(last * qty))
            positions.append({
                "kind": row.get("kind"), "code": row.get("code"),
                "name": row.get("name") or row.get("code"), "qty": qty,
                "cost": cost / 100.0, "price": last / 100.0,
                "pnl": pnl / 100.0, "pnl_pct": (pnl / base * 100.0) if base else 0.0,
            })
        trades = []
        if self.finance_repo is not None:
            try:
                for row in self.finance_repo.trades(20):
                    amount = int(row.get("amount_cents") or 0)
                    if str(row.get("side")) in ("buy", "subscribe"):
                        amount = -amount
                    trades.append({
                        "day": time.strftime("%m-%d %H:%M", time.localtime(float(row.get("ts") or 0))),
                        "kind": {"buy": "买入", "sell": "卖出", "subscribe": "申购", "redeem": "赎回"}.get(
                            str(row.get("side")), str(row.get("side"))),
                        "target": (row.get("name") or "") + " " + str(row.get("code") or ""),
                        "amount": amount / 100.0, "fee": int(row.get("fee_cents") or 0) / 100.0,
                        "note": row.get("note") or "",
                    })
            except Exception as exc:
                isolated("finance.snapshot.trades", exc)
        loans = []
        if self.finance_repo is not None:
            try:
                loans = self.finance_repo.loans()
            except Exception:
                loans = []
        debt = sum(int(x.get("balance_cents") or 0) for x in loans)
        monthly = sum(int(x.get("monthly_cents") or 0) for x in loans)
        rate = max([float(x.get("rate") or 0) for x in loans] or [0.0])
        mood = self.finance.mood()
        decision = {}
        try:
            if self.finance_repo is not None:
                raw = self.finance_repo.meta_get("last_decision")
                decision = json.loads(raw) if raw else {}
        except Exception:
            decision = {}
        help_state = {}
        try:
            if self.finance_help is not None:
                help_state = self.finance_help.status()
                help_state["prompt"] = self.finance_help.prompt_text()
        except Exception:
            help_state = {}
        return {
            "mode": "real",
            "wallet": {"total": (cash + market_value) / 100.0, "available": cash / 100.0,
                       "withdrawable": (self.finance.withdrawable_cents() or 0) / 100.0,
                       "market_value": market_value / 100.0,
                       "today_pnl": total_pnl / 100.0,
                       "today_pct": (total_pnl / max(1, market_value - total_pnl) * 100.0)},
            "positions": positions,
            "trades": trades,
            "life": {"salary": 6800, "salary_day": 15, "next_salary": "", "rent": 0,
                     "rent_day": 1, "utilities": 0, "month_expense": 0},
            "mood": {"score": mood.get("score"), "dominant": mood.get("dominant"),
                     "last_event": ""},
            "market": {"checks": 0, "limit": 3, "last": "每天三次：9:30 / 11:30 / 14:50", "source": "腾讯 qt.gtimg.cn"},
            "bank": {"debt": debt / 100.0, "monthly": monthly / 100.0, "rate": rate,
                     "limit": 0, "items": loans, "note": ""},
            "decision": decision,
            "help": help_state,
            "plan": {"deposit": cash / 100.0, "invest_cap": 50,
                     "house": {"price": 0, "down_pct": 30, "years": [10, 20, 30], "monthly": 0},
                     "car": {"price": 0, "years": 5},
                     "rule": "她不会买买不起的；投资不超过存款 50%"},
        }

    def _finance_snapshot(self, demo: bool = False) -> dict:
        """金融概览：优先问金融插件，没有就回退空态（契约见 docs/INTEGRATION-MAP.md）。"""
        if not demo:
            provider = self._finance_provider()
            if provider is not None:
                try:
                    view = provider.snapshot()
                    if isinstance(view, dict):
                        return view
                except Exception as exc:
                    isolated("finance.snapshot", exc)
        if not demo:
            try:
                return self._snapshot_real()
            except Exception as exc:
                isolated("finance.snapshot", exc)
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
                "market": {"checks": 3, "limit": 3, "last": "9:30 / 11:30 / 14:50 各查一次", "source": "腾讯 qt.gtimg.cn"},
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
            "market": {"checks": 0, "limit": 3, "last": "每天三次：9:30 / 11:30 / 14:50", "source": "腾讯 qt.gtimg.cn"},
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

    async def _read_json(self):
        try:
            from astrbot.api.web import request as web_request
            if web_request is not None:
                data = await web_request.json()
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            isolated("web.body", exc)
        return {}

    async def _api_trade(self, request=None):
        try:
            body = await self._read_json()
            action = str(body.get("action") or "").lower()
            code = str(body.get("code") or "").strip()
            qty = body.get("qty") or 0
            price = body.get("price") or None
            idem = str(body.get("idem") or "")
            trading = self.trading
            if trading is None:
                return {"ok": False, "message": "交易模块未就绪"}
            if action == "buy":
                return await trading.buy(code, qty, price, idem)
            if action == "sell":
                return await trading.sell(code, qty, price, idem)
            if action == "subscribe":
                amount_cents = int(round(float(body.get("qty") or body.get("amount") or 0) * 100))
                return await trading.subscribe(code, amount_cents, price, idem)
            if action == "redeem":
                return await trading.redeem(code, qty, price, idem)
            return {"ok": False, "message": "未知操作"}
        except Exception as exc:
            isolated("web.trade", exc)
            return {"ok": False, "message": "交易失败：%s" % exc}

    async def _api_finance_help(self, request=None):
        try:
            state = self.finance_help.status() if self.finance_help else {}
            state["prompt"] = self.finance_help.prompt_text() if self.finance_help else ""
            return state
        except Exception as exc:
            isolated("web.help", exc)
            return {"low": False, "prompt": ""}

    async def _api_loan(self, request=None):
        try:
            body = await self._read_json()
            amount = int(round(float(body.get("amount") or 0) * 100))
            periods = int(body.get("periods") or 12)
            rate = float(body.get("rate") or 0.06)
            return await self.finance_help.take_loan(amount, periods, rate)
        except Exception as exc:
            isolated("web.loan", exc)
            return {"ok": False, "message": "借款失败：%s" % exc}

    async def _api_consume(self, request=None):
        try:
            body = await self._read_json()
            items = body.get("items") or []
            return {"items": await self.finance_consume.buy_items(items)}
        except Exception as exc:
            isolated("web.consume", exc)
            return {"items": [], "message": "消费失败：%s" % exc}

    async def _api_decision(self, request=None):
        try:
            body = await self._read_json()
            slot = str(body.get("slot") or "close")
            return await self.finance_decision.run_slot(slot)
        except Exception as exc:
            isolated("web.decision", exc)
            return {"ok": False, "message": "决策失败：%s" % exc}

    async def _api_debug(self, request=None):
        try:
            body = await self._read_json()
            action = str(body.get("action") or "").lower()
            if action == "salary" and self.finance is not None:
                return await self.finance.income(680000, "工资(调试)", "debug:salary:%d" % int(time.time()), salary=True)
            if action == "rent" and self.finance is not None:
                return await self.finance.expense(120000, "房租(调试)", "debug:rent:%d" % int(time.time()), necessary=True)
            if action == "quotes" and self.finance_decision is not None:
                result = await self.finance_decision.run_slot("close")
                return {"ok": True, "result": result}
            return {"ok": False, "message": "未知调试动作"}
        except Exception as exc:
            isolated("web.debug", exc)
            return {"ok": False, "message": str(exc)}

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
        if parts and parts[0].lower() in ("finance", "金融", "钱"):
            return self._fmt_finance()
        return self._fmt_diag(await self.diag())

    def _fmt_finance(self) -> str:
        """只给自然语言情绪，不出现心情数值。"""
        mood = self.finance.mood()
        cents = self.finance.balance_cents()
        money = "余额读取失败" if cents is None else "可用余额 ¥%s" % format(cents / 100.0, ",.2f")
        holdings = []
        try:
            if self.finance_repo is not None:
                for row in self.finance_repo.positions()[:3]:
                    holdings.append("%s %s 股" % (row.get("name") or row.get("code"), row.get("qty")))
        except Exception as exc:
            isolated("finance.text.positions", exc)
        tail = ("，持有 " + "、".join(holdings)) if holdings else "，现在空仓"
        return "她现在%s。%s%s。" % (mood.get("word") or "平静", money, tail)

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

    async def money_cmd(self, event: AstrMessageEvent):
        """理财菜单：/理财、/理财 总资产|持仓|贷款|日程|关闭"""
        try:
            text = str(getattr(event, "message_str", "") or "")
            parts = [p for p in text.split() if p]
            if parts and parts[0].lstrip("/").lower() in ("理财", "money"):
                parts = parts[1:]
            reply = self._format_money(parts[0] if parts else "")
        except Exception as exc:
            isolated("handler.money", exc)
            reply = "理财信息暂时读不到。"
        yield event.plain_result(reply)

    async def watch_cmd(self, event: AstrMessageEvent):
        """看社会插件状态：/watch、/watch diag、/watch test raise|busy"""
        try:
            text = await self._handle_watch(event)
        except Exception as exc:
            isolated("handler.watch", exc)
            text = "[WATCH] 命令出错已被隔离，amo 不受影响（trace_id=%s）" % _tid()
        yield event.plain_result(text)

    async def finance_tool(self, event, action: str = "status"):
        '''金融管家：查看她的资产、持仓、最近决策；也可以让她现在做一次决策。

        Args:
            action(string): status（资产+情绪）/ positions（持仓）/ decide（让她自己决定买或卖）
        '''
        try:
            act = str(action or "status").lower()
            if act == "positions":
                rows = self.finance_repo.positions() if self.finance_repo else []
                if not rows:
                    return "她现在空仓。"
                return "\n".join("%s(%s) %s 股，现价 %.2f，浮动 %+.2f%%" % (
                    r.get("name") or r.get("code"), r.get("code"), r.get("qty"),
                    int(r.get("last_cents") or 0) / 100.0,
                    ((int(r.get("last_cents") or 0) - int(r.get("cost_cents") or 0))
                     / max(1, int(r.get("cost_cents") or 0)) * 100.0)) for r in rows)
            if act == "decide":
                if self.finance_decision is None:
                    return "决策模块未就绪。"
                result = await self.finance_decision.run_slot("close")
                d = (result or {}).get("decision") or {}
                return "她决定：%s（%s）" % (d.get("reason") or "先不动", d.get("action") or "hold")
            mood = self.finance.mood()
            cents = self.finance.balance_cents() or 0
            return "她现在%s，可用余额 ¥%.2f。" % (mood.get("word") or "平静", cents / 100.0)
        except Exception as exc:
            isolated("finance.tool", exc)
            return "金融信息暂时读不到。"

    def _format_money(self, sub=""):
        sub = str(sub or "").strip()
        repo = self.finance_repo
        cash = self.finance.balance_cents() or 0
        rows = repo.positions() if repo else []
        market = sum(int(r.get("last_cents") or 0) * float(r.get("qty") or 0) for r in rows)
        loans = repo.loans() if repo else []
        debt = sum(int(x.get("balance_cents") or 0) for x in loans)
        word = self.finance.mood().get("word") or "平静"
        if sub in ("", "菜单"):
            return ("理财菜单：1 总资产 2 持仓 3 贷款 4 日程 0 关闭\n"
                    "也可以直接发 /理财 总资产、/理财 持仓、/理财 贷款、/理财 日程、/理财 关闭")
        if sub in ("总资产", "1"):
            return "总资产 ¥%.2f（余额 ¥%.2f + 持仓 ¥%.2f − 负债 ¥%.2f），她现在%s。" % (
                (cash + market - debt) / 100.0, cash / 100.0, market / 100.0, debt / 100.0, word)
        if sub in ("持仓", "2"):
            if not rows:
                return "她现在空仓，%s。" % word
            return "\n".join("%s(%s) %s股 成本%.2f 现价%.2f %+.1f%%" % (
                r.get("name") or r.get("code"), r.get("code"), r.get("qty"),
                int(r.get("cost_cents") or 0) / 100.0, int(r.get("last_cents") or 0) / 100.0,
                ((int(r.get("last_cents") or 0) - int(r.get("cost_cents") or 0))
                 / max(1, int(r.get("cost_cents") or 0)) * 100.0)) for r in rows)
        if sub in ("贷款", "3"):
            if not loans:
                return "她现在没有贷款。"
            return "\n".join("%s 本金%.2f 年利率%.2f%% %d期 每期%.2f 剩余%.2f" % (
                x.get("kind"), int(x.get("principal_cents") or 0) / 100.0, float(x.get("rate") or 0) * 100,
                int(x.get("months") or 0), int(x.get("monthly_cents") or 0) / 100.0,
                int(x.get("balance_cents") or 0) / 100.0) for x in loans)
        if sub in ("日程", "4"):
            try:
                day = time.strftime("%Y-%m-%d")
                amo = self.finance._amo()
                items = (amo.state.schedule.get(day) or []) if amo is not None else []
            except Exception:
                items = []
            lines = [x for x in items if str(x.get("category")) == "finance"]
            if not lines:
                return "今天没有财务日程。"
            return "\n".join("%s-%s %s %s" % (x.get("start"), x.get("end"), x.get("activity"), x.get("detail") or "") for x in lines)
        if sub in ("关闭", "0"):
            if not rows:
                return "持仓简报：当前空仓。"
            return "持仓简报：\n" + "\n".join("%s %s股 成本%.2f 现价%.2f %+.1f%%" % (
                r.get("name") or r.get("code"), r.get("qty"),
                int(r.get("cost_cents") or 0) / 100.0, int(r.get("last_cents") or 0) / 100.0,
                ((int(r.get("last_cents") or 0) - int(r.get("cost_cents") or 0))
                 / max(1, int(r.get("cost_cents") or 0)) * 100.0)) for r in rows)
        return "没听懂，发 /理财 看菜单。"

    async def on_recommend(self, event):
        """用户说「建议买 600519/贵州茅台」：只入候选池，不买入。"""
        text = str(getattr(event, "message_str", "") or "").strip()
        if not re.search(r"(建议|推荐).{0,4}买", text):
            return
        try:
            result = self.finance_pools.recommend(text) if self.finance_pools else {"ok": False, "message": "候选池未就绪"}
        except Exception as exc:
            isolated("finance.recommend", exc)
            result = {"ok": False, "message": "记候选失败"}
        if result.get("ok"):
            try:
                await self.finance.memory.remember(
                    "%s 用户建议买 %s，已放进候选池（7 天有效，不买入）" % (
                        time.strftime("%Y-%m-%d %H:%M"), result.get("code")),
                    kind="finance", idem="rec:" + str(result.get("code")) + ":" + time.strftime("%Y%m%d"))
            except Exception:
                pass
        yield event.plain_result(result.get("message") or "记下了")

    async def on_transfer(self, event):
        """用户发 [转账]100元：只作为注资，不替她下单。"""
        text = str(getattr(event, "message_str", "") or "").strip()
        match = re.match(r"^\[?转账\]?\s*([0-9]+(?:\.[0-9]+)?)", text)
        if not match:
            return
        amount_cents = int(round(float(match.group(1)) * 100))
        try:
            result = await self.finance_help.user_transfer(amount_cents) if self.finance_help else {
                "ok": False, "message": "金融模块未就绪"}
        except Exception as exc:
            isolated("finance.transfer", exc)
            result = {"ok": False, "message": "转账入账失败"}
        yield event.plain_result(result.get("message") or "已收到")

    # 命令装饰器只做注册；离线单测（没有 astrbot.api.event）时方法照样能直接调
    if filter is not None:
        watch_cmd = filter.command("watch")(watch_cmd)
        money_cmd = filter.command("理财")(money_cmd)
        on_transfer = filter.event_message_type(filter.EventMessageType.ALL)(on_transfer)
        on_recommend = filter.event_message_type(filter.EventMessageType.ALL)(on_recommend)
        finance_tool = filter.llm_tool(name="finance")(finance_tool)


def _tid() -> str:
    try:
        from .guard import trace_id
    except ImportError:
        from guard import trace_id
    return trace_id()
