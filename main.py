# -*- coding: utf-8 -*-
"""我能永远守护你（Astrbot_amoupup_watch）—— 独立社会模拟插件。

v0.1 边界层：guard / runtime / store / scheduler 四层就位，业务逻辑（world.py）还没写。
任何新功能都必须走这四层，见 docs/ISOLATION-DESIGN.md。

握手契约：docs/HANDSHAKE.md（amo 只调 describe()）
"""
from __future__ import annotations

import asyncio
import datetime as _dt
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

try:  # 包模式
    from .core.compat import load as _load
except ImportError:  # 直接按文件加载
    from core.compat import load as _load

_PKG = __package__ or ""

ApiMixin = _load(_PKG, "api.handlers", "ApiMixin")
CardMixin = _load(_PKG, "ui.cards", "CardMixin")
CandidatePools = _load(_PKG, "services.candidates", "CandidatePools")
FinanceConsume = _load(_PKG, "services.consume_engine", "FinanceConsume")
FinanceDaily = _load(_PKG, "services.daily_engine", "FinanceDaily")
FinanceExpense = _load(_PKG, "services.expense_engine", "FinanceExpense")
FinanceDecision = _load(_PKG, "core.agent_loop", "FinanceDecision")
FinanceHelp = _load(_PKG, "services.credit_engine", "FinanceHelp")
FinanceLife = _load(_PKG, "services.life_engine", "FinanceLife")
FinanceLink = _load(_PKG, "core.amo_bridge", "FinanceLink")
FinanceProperty = _load(_PKG, "services.property_engine", "FinanceProperty")
life_block, market_block = _load(_PKG, "ui.report", "life_block", "market_block")
FinanceSalary = _load(_PKG, "services.work_engine", "FinanceSalary")
WorkEventEngine = _load(_PKG, "services.work_events", "WorkEventEngine")
build_medical = _load(_PKG, "services.medical_engine", "build_medical")
build_fortune = _load(_PKG, "services.fortune_engine", "build_fortune")
build_lending = _load(_PKG, "services.lending_engine", "build_lending")
build_gamble = _load(_PKG, "services.gamble_engine", "build_gamble")
TieredMemory = _load(_PKG, "core.memory_tier", "TieredMemory")
FinanceRepo = _load(_PKG, "models.ledger", "FinanceRepo")
Trading = _load(_PKG, "services.trade_engine", "Trading")
guard_sync, isolated = _load(_PKG, "guard", "guard_sync", "isolated")
Runtime = _load(_PKG, "core.runtime", "Runtime")
Scheduler = _load(_PKG, "core.scheduler", "Scheduler")
Store = _load(_PKG, "models.db", "Store")
ROUTES = _load(_PKG, "api.routes", "ROUTES")

PLUGIN_NAME = "Astrbot_amoupup_watch"
DISPLAY_NAME = "我能永远守护你"
PLUGIN_VERSION = "1.0.0"
API_VERSION = "1.0"
FEATURES: list[str] = ["diag", "finance"]

try:
    from .core.tool_wrappers import ToolWrappersMixin
except ImportError:
    from core.tool_wrappers import ToolWrappersMixin

class WatchPlugin(ToolWrappersMixin, ApiMixin, CardMixin, Star):
    def __init__(self, context: Context | None = None, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.runtime: Runtime | None = None
        self.store: Store | None = None
        self.scheduler: Scheduler | None = None
        self.finance = FinanceLink(self)
        for _name in ("finance_repo", "trading", "finance_life", "finance_property",
                      "finance_salary", "finance_expense", "work_events", "medical",
                      "finance_help", "finance_consume", "finance_decision",
                      "finance_daily", "finance_memory", "finance_pools"):
            setattr(self, _name, None)
        self.city_override = ""
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
            self.medical = build_medical(self)
            self.fortune = build_fortune(self)
            self.lending = build_lending(self)
            self.gamble = build_gamble(self)
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
            try:
                from .services.date_engine import tick as date_tick
            except ImportError:
                from services.date_engine import tick as date_tick
            await date_tick(self)
            try:
                from .services.late_night_engine import tick as late_tick
            except ImportError:
                from services.late_night_engine import tick as late_tick
            await late_tick(self)
            try:
                from .services.shopping_engine import tick as shopping_tick
            except ImportError:
                from services.shopping_engine import tick as shopping_tick
            await shopping_tick(self)
            try:
                from .services.shopping_engine import flush as shop_flush
            except ImportError:
                from services.shopping_engine import flush as shop_flush
            await shop_flush(self)
            try:
                from .services.gift_engine import tick as gift_tick
            except ImportError:
                from services.gift_engine import tick as gift_tick
            await gift_tick(self)
            await self._process_pending()
            if self.finance_help is not None:
                self.finance_help.maybe_reset()
                await self.finance_help.repay_tick()
            if self.finance_decision is not None:
                await self.finance_decision.tick()
            if self.finance_daily is not None:
                await self.finance_daily.tick()
            if getattr(self, "bank", None) is None:
                from .services.bank_engine import build_bank
                self.bank = build_bank(self)
            await self.bank.daily_tick()
            if self.work_events is not None:
                await self.work_events.daily_random(_dt.datetime.now())
                await self.work_events.daily_recap(_dt.datetime.now())
                try:
                    if any("加班" in str(e) for e in (self.work_events.today_events() or [])):
                        amo = self.finance._amo() if hasattr(self.finance, "_amo") else None
                        if amo is not None and hasattr(amo, "on_forced_overtime"):
                            amo.on_forced_overtime()
                except Exception:
                    pass
                await self.fortune.monthly_roll(_dt.datetime.now())
                await self.lending.repay_tick(_dt.datetime.now())
                await self.medical.insurance_tick(_dt.datetime.now())
                try:
                    from .services.social_engine import build_social
                    from .services.bankruptcy_engine import build_bankruptcy
                except ImportError:
                    from services.social_engine import build_social
                    from services.bankruptcy_engine import build_bankruptcy
                self.social = getattr(self, 'social', None) or build_social(self)
                await self.social.scheduled(_dt.datetime.now())
                await build_bankruptcy(self).check(_dt.datetime.now())
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
            for path, attr, methods, desc in ROUTES:
                register("/Astrbot_amoupup_watch" + path, getattr(self, attr), methods, desc)
            logger.info("[WATCH] web api registered: %d routes", len(ROUTES))
        except Exception as exc:
            isolated("web.register", exc)
    @_optional_hook("command")("城市")
    async def city_cmd(self, event: AstrMessageEvent, name: str = ""):
        """设定她所在城市（影响房租、饭钱、房价）：/城市 杭州"""
        try:
            from .services.city import city_reply, city_status_text
        except ImportError:
            from services.city import city_reply, city_status_text
        name = str(name or "").strip()
        if not name:
            yield event.plain_result(city_status_text(self))
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
            from .services.city import relocate
        except ImportError:
            from services.city import relocate
        moved = await relocate(self, name)
        yield event.plain_result(city_reply(self, moved, saved))
    @_optional_hook("command")("搬家")
    async def move_cmd(self, event: AstrMessageEvent, name: str = ""):
        async for item in self.city_cmd(event, name):
            yield item
    @_optional_hook("command")("设定城市")
    async def set_city_cmd(self, event: AstrMessageEvent, name: str = ""):
        """「设定城市 杭州」的同义指令。"""
        async for item in self.city_cmd(event, name):
            yield item
    async def handle_sick_event(self, *args, **kwargs):
        return await self.medical.handle_sick_event(*args, **kwargs) if self.medical else {"ok": False}
    async def on_sick_event(self, event):
        try:
            from .services.medical_engine import sick_event_reply
        except ImportError:
            from services.medical_engine import sick_event_reply
        reply = await sick_event_reply(self, getattr(event, "message_str", ""))
        if reply:
            yield event.plain_result(reply)
    async def on_llm_request(self, event, request):
        try:
            from .services.social_cmd import inject_request
        except ImportError:
            from services.social_cmd import inject_request
        inject_request(self, request)
    async def money_cmd(self, event: AstrMessageEvent):
        try:
            from .services.menu import money_reply
        except ImportError:
            from services.menu import money_reply
        reply = await money_reply(self, getattr(event, "message_str", ""))
        if reply:
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
        try:
            from .services.finance_cmd import finance_reply
        except ImportError:
            from services.finance_cmd import finance_reply
        return await finance_reply(self, action, day, event_kind)
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
        try:
            from .services.transfer_service import handle_transfer
        except ImportError:
            from services.transfer_service import handle_transfer
        message = await handle_transfer(self, getattr(event, "message_str", ""))
        try:
            from .services.date_engine import maybe_compensate
        except ImportError:
            from services.date_engine import maybe_compensate
        try:
            from .services.date_engine import note_transfer
        except ImportError:
            from services.date_engine import note_transfer
        try:
            await note_transfer(self, getattr(event, "message_str", ""))
        except Exception:
            pass
        try:
            from .services.late_night_engine import maybe_soothe
        except ImportError:
            from services.late_night_engine import maybe_soothe
        try:
            from .services.gift_engine import maybe_transfer as gift_transfer
        except ImportError:
            from services.gift_engine import maybe_transfer as gift_transfer
        try:
            gift_msg = await gift_transfer(self, getattr(event, "message_str", ""))
        except Exception:
            gift_msg = None
        if gift_msg and gift_msg.get("message"):
            message = (message + "\n" if message else "") + gift_msg["message"]
        try:
            soothe = await maybe_soothe(self, getattr(event, "message_str", ""))
        except Exception:
            soothe = None
        if soothe and soothe.get("message"):
            message = (message + "\n" if message else "") + soothe["message"]
        try:
            comp = await maybe_compensate(self, getattr(event, "message_str", ""))
        except Exception:
            comp = None
        if comp and comp.get("message"):
            message = (message + "\n" if message else "") + comp["message"]
        if message:
            yield event.plain_result(message)
    async def on_shopping_reply(self, event):
        """挂起订单的二次确认：买/好看/转账结算，太贵/不好看放弃。"""
        try:
            from .services.shopping_engine import handle_reply
        except ImportError:
            from services.shopping_engine import handle_reply
        try:
            await handle_reply(self, str(getattr(event, "message_str", "") or ""))
        except Exception:
            pass

    async def bill_cmd(self, event, text=""):
        """账单：按大类分组 + 今日/本月合计。"""
        try:
            from .services.bill_engine import text as bill_text
        except ImportError:
            from services.bill_engine import text as bill_text
        yield event.plain_result(bill_text(self))

    async def social_cmd(self, event, text=""):
        try:
            from .services.social_cmd import social_reply
        except ImportError:
            from services.social_cmd import social_reply
        reply = await social_reply(self, str(getattr(event, "message_str", "") or text))
        if reply:
            yield event.plain_result(reply)



    async def attend_date_cmd(self, event, text: str = ""):
        try:
            admin = event.is_admin() if hasattr(event, "is_admin") else True
        except Exception:
            admin = True
        if not admin:
            yield event.plain_result("这条指令只给管理员用哦。")
            return
        raw = str(getattr(event, "message_str", "") or text).replace("/赴约", "").strip()
        parts = raw.split()
        title = parts[0] if parts else ""
        mode = parts[1] if len(parts) > 1 else ""
        try:
            from .services.date_engine import attend_date
        except ImportError:
            from services.date_engine import attend_date
        out = await attend_date(self, title, mode)
        yield event.plain_result(out.get("message") or "我出门啦")


    async def on_date_propose(self, event):
        """自然语言兜底：约定“去吃火锅吧” → 建约会；“我到了/开吃” → 赴约。"""
        text = str(getattr(event, "message_str", "") or "")
        try:
            from .services.date_engine import attend_date, find_pending, make_date
        except ImportError:
            from services.date_engine import attend_date, find_pending, make_date
        pending = find_pending(self, "")
        if pending and any(w in text for w in ("我到了", "你人呢", "去吃", "走吧", "开吃")):
            out = await attend_date(self, pending.get("title"))
            if out.get("ok"):
                yield event.plain_result(out.get("message") or "")
            return
        if pending:
            return
        foods = ("火锅", "烤肉", "电影", "逛街", "吃饭", "咖啡")
        if any(f in text for f in foods) and any(w in text for w in ("好呀", "好的", "去吧", "去吃", "可以", "行", "约")):
            activity = next((f for f in foods if f in text), "吃饭")
            when = "中午" if "中午" in text else ("晚上" if "晚上" in text else "")
            out = await make_date(self, activity, when)
            if out.get("ok"):
                yield event.plain_result(out.get("message") or "")

    async def cancel_date_cmd(self, event, title: str = ""):
        text = str(getattr(event, "message_str", "") or title)
        text = text.replace("/取消约会", "").strip()
        try:
            from .services.date_engine import cancel_date
        except ImportError:
            from services.date_engine import cancel_date
        out = await cancel_date(self, text or title, "")
        yield event.plain_result(out.get("message") or "最近没有约好的事呀。")

    async def on_date_cancel(self, event):
        """自然语言里说去不了/放鸽子：有 pending 约会才触发。"""
        text = str(getattr(event, "message_str", "") or "")
        if "/取消约会" in text:
            return
        try:
            from .services.date_engine import CANCEL_WORDS, cancel_date, find_pending
        except ImportError:
            from services.date_engine import CANCEL_WORDS, cancel_date, find_pending
        if not any(word in text for word in CANCEL_WORDS):
            return
        if find_pending(self, "") is None:
            return
        out = await cancel_date(self, "", text)
        if out.get("ok"):
            yield event.plain_result(out.get("message") or "")

    # 命令装饰器只做注册；离线单测（没有 astrbot.api.event）时方法照样能直接调
    if filter is not None:
        watch_cmd = filter.command("watch")(watch_cmd)
        money_cmd = filter.command("理财")(money_cmd)
        filter.command("搬家")(city_cmd)
        filter.command("设定城市")(city_cmd)
        on_llm_request = filter.on_llm_request()(on_llm_request)
        on_transfer = filter.event_message_type(filter.EventMessageType.ALL)(on_transfer)
        on_date_cancel = filter.event_message_type(filter.EventMessageType.ALL)(on_date_cancel)
        on_shopping_reply = filter.event_message_type(filter.EventMessageType.ALL)(on_shopping_reply)
        on_date_propose = filter.event_message_type(filter.EventMessageType.ALL)(on_date_propose)
        cancel_date_cmd = filter.command("取消约会")(cancel_date_cmd)
        bill_cmd = filter.command("账单")(bill_cmd)
        bill_cmd = filter.command("日常明细")(bill_cmd)
        attend_date_cmd = filter.command("赴约")(attend_date_cmd)
        on_recommend = filter.event_message_type(filter.EventMessageType.ALL)(on_recommend)
        on_sick_event = filter.event_message_type(filter.EventMessageType.ALL)(on_sick_event)
        finance_tool = filter.llm_tool(name="finance")(finance_tool)
        social_cmd = filter.command("查关系")(social_cmd)
        social_cmd = filter.command("劝她别借钱")(social_cmd)
        social_cmd = filter.command("社交")(social_cmd)
        social_cmd = filter.command("外出")(social_cmd)
        social_cmd = filter.command("赞助")(social_cmd)
        social_cmd = filter.command("让她去体检")(social_cmd)
        social_cmd = filter.command("督促她买保险")(social_cmd)

def _tid() -> str:
    try:
        from .guard import trace_id
    except ImportError:
        from guard import trace_id
    return trace_id()
