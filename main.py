# -*- coding: utf-8 -*-
"""我能永远守护你（Astrbot_amoupup_watch）—— 独立社会模拟插件。

v0.1 边界层：guard / runtime / store / scheduler 四层就位，业务逻辑（world.py）还没写。
任何新功能都必须走这四层，见 docs/ISOLATION-DESIGN.md。

握手契约：docs/HANDSHAKE.md（amo 只调 describe()）
"""
from __future__ import annotations

import asyncio
import datetime as _dt
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

def _optional_hook(name):
    """离线测试/老版本 AstrBot 没有这个钩子时，装饰器退化成空操作。"""
    finder = getattr(filter, name, None) if filter is not None else None
    if callable(finder):
        return finder

    def _decorator(*args, **kwargs):
        def _apply(func):
            return func
        return _apply

    return _decorator


_agent_begin = _optional_hook("on_agent_begin")

try:
    from .api.handlers import ApiMixin
    from .ui.cards import CardMixin
except ImportError:  # 离线测试 / 顶层加载
    from api.handlers import ApiMixin
    from ui.cards import CardMixin

try:  # 包模式
    from .services.candidates import CandidatePools
    from .services.consume_engine import FinanceConsume
    from .services.daily_engine import FinanceDaily
    from .services.expense_engine import FinanceExpense
    from .core.agent_loop import FinanceDecision
    from .services.credit_engine import FinanceHelp
    from .services.life_engine import FinanceLife
    from .core.amo_bridge import FinanceLink
    from .services.property_engine import FinanceProperty
    from .ui.report import life_block, market_block
    from .services.work_engine import FinanceSalary
    from .services.work_events import WorkEventEngine
    from .core.memory_tier import TieredMemory
    from .models.ledger import FinanceRepo
    from .services.trade_engine import Trading
    from .guard import guard_sync, isolated
    from .core.runtime import Runtime
    from .core.scheduler import Scheduler
    from .models.db import Store
except ImportError:  # 直接按文件加载时
    from services.candidates import CandidatePools
    from services.consume_engine import FinanceConsume
    from services.daily_engine import FinanceDaily
    from services.expense_engine import FinanceExpense
    from core.agent_loop import FinanceDecision
    from services.credit_engine import FinanceHelp
    from services.life_engine import FinanceLife
    from core.amo_bridge import FinanceLink
    from services.property_engine import FinanceProperty
    from ui.report import life_block, market_block
    from services.work_engine import FinanceSalary
    from services.work_events import WorkEventEngine
    from core.memory_tier import TieredMemory
    from models.ledger import FinanceRepo
    from services.trade_engine import Trading
    from guard import guard_sync, isolated
    from core.runtime import Runtime
    from core.scheduler import Scheduler
    from models.db import Store


PLUGIN_NAME = "Astrbot_amoupup_watch"
DISPLAY_NAME = "我能永远守护你"
PLUGIN_VERSION = "0.2.2"
API_VERSION = "1.0"
FEATURES: list[str] = ["diag", "finance"]


class WatchPlugin(ApiMixin, CardMixin, Star):
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
        self.finance_property = None
        self.finance_salary = None
        self.finance_expense = None
        self.work_events = None
        self.city_override = ""
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
            self.finance_property = FinanceProperty(self, self.finance_repo, self.finance, self.finance_life)
            self.finance_salary = FinanceSalary(self, self.finance_repo, self.finance)
            self.work_events = WorkEventEngine(self, self.finance_repo, self.finance, self.finance_salary)
            self.finance_help = FinanceHelp(self, self.finance_repo, self.finance)
            self.finance_consume = FinanceConsume(self, self.finance_repo, self.finance)
            self.finance_expense = FinanceExpense(self, self.finance_repo, self.finance)
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
        if self.finance_repo is not None:
            try:
                self.city_override = str(self.finance_repo.meta_get("city", "") or "")
            except Exception:
                self.city_override = ""
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
            if self.work_events is not None:
                await self.work_events.daily_random(_dt.datetime.now())
                await self.work_events.daily_recap(_dt.datetime.now())
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
    @_agent_begin()
    async def inject_work_status(self, *args):
        """把「今日职场」写进 LLM 上下文，让她能主动吐槽/炫耀。"""
        try:
            if self.work_events is None:
                return
            text = self.work_events.prompt_line()
            if not text:
                return
            run_context = args[-1] if args else None
            messages = getattr(run_context, "messages", None)
            if messages is None:
                return
            from astrbot.core.agent.message import Message
            messages.insert(0, Message(role="system", content=text))
        except Exception as exc:
            isolated("work.inject", exc)

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
            register("/Astrbot_amoupup_watch/finance/property", self._api_property, ["GET", "POST"], "守护面板：房产与车辆")
            register("/Astrbot_amoupup_watch/finance/salary", self._api_salary, ["GET", "POST"], "守护面板：工资明细")
            register("/Astrbot_amoupup_watch/finance/leave", self._api_leave, ["POST"], "守护面板：请假")
            register("/Astrbot_amoupup_watch/finance/work", self._api_work, ["GET", "POST"], "守护面板：职场事件")
            register("/Astrbot_amoupup_watch/finance/city", self._api_city, ["GET", "POST"], "守护面板：城市/搬家")
            register("/Astrbot_amoupup_watch/finance/expenses", self._api_expenses, ["GET"], "守护面板：日常开销")
            register("/Astrbot_amoupup_watch/trade", self._api_trade, ["POST"], "守护面板：下单")
            logger.info("[WATCH] web api /api/plug/Astrbot_amoupup_watch/diag 已注册")
        except Exception as exc:
            isolated("web.register", exc)

    @_optional_hook("command")("城市")
    async def city_cmd(self, event: AstrMessageEvent, name: str = ""):
        """设定她所在城市（影响房租、饭钱、房价）：/城市 杭州"""
        try:
            from .services.city import city_view
        except ImportError:
            from services.city import city_view
        name = str(name or "").strip()
        if not name:
            view = city_view(self)
            yield event.plain_result("她现在在「%s」（%s，物价 ×%s）。想换就发：/城市 杭州"
                                     % (view["city"] or "未设置", view["tier"], view["factor"]))
            return
        self.city_override = name
        saved = False
        if self.finance_repo is not None:
            try:
                self.finance_repo.meta_set("city", name)
                saved = True
            except Exception as exc:
                isolated("handler.city", exc)
        try:
            from .services.city import move_to
        except ImportError:
            from services.city import move_to
        moved = await move_to(self, name)
        view = city_view(self)
        weather = (moved or {}).get("weather") or {}
        weather_text = ("天气已经切到%s" % weather.get("name")) if weather else "天气城市没找到，只改了账本口径"
        yield event.plain_result("好，搬去%s了（%s，物价 ×%s），%s；房租、饭钱、房价都按这里算%s。"
                                 % (view["city"], view["tier"], view["factor"], weather_text,
                                    "" if saved else "（这次没存上，重启会恢复）"))

    @_optional_hook("command")("设定城市")
    async def set_city_cmd(self, event: AstrMessageEvent, name: str = ""):
        """「设定城市 杭州」的同义指令。"""
        async for item in self.city_cmd(event, name):
            yield item

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

    async def finance_tool(self, event, action: str = "status", day: str = "", event_kind: str = ""):
        '''金融管家：查看她的资产、持仓、最近决策；也可以让她现在做一次决策。

        Args:
            action(string): status（资产+情绪）/ positions（持仓）/ decide（让她自己决定买或卖）/ leave（替她请假）
            day(string): 请假日期，可填「今天」「明天」或 2026-11-14；不填就是今天
            event_kind(string): 职场事件，late（迟到）/ sick（病假）/ project（项目奖金）/ overtime（加班）
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
            if act in ("work", "职场"):
                if self.work_events is None:
                    return "职场模块未就绪。"
                return self.work_events.prompt_line() or "今天职场没什么事。"
            if act in ("leave", "请假"):
                if self.finance_salary is None:
                    return "工资模块未就绪。"
                result = await self.finance_salary.apply_leave(day=day or "")
                return ("好呀，正好有点累……不过请%s要扣 ¥%.2f，有点肉疼 QAQ。\n%s"
                        % (result["label"], abs(result["amount_cents"]) / 100.0, result["card"]))
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
