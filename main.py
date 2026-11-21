# -*- coding: utf-8 -*-
"""我能永远守护你（Astrbot_amoupup_watch）—— 独立社会模拟插件。

v0.1 边界层：guard / runtime / store / scheduler 四层就位，业务逻辑（world.py）还没写。
任何新功能都必须走这四层，见 docs/ISOLATION-DESIGN.md。

握手契约：docs/HANDSHAKE.md（amo 只调 describe()）
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

try:  # 正常运行时
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star
except Exception:  # 离线单测 / 直接按文件导入
    logger = logging.getLogger("watch")
    AstrMessageEvent = None
    filter = None
    class Context:
        pass
    class Star:
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

try:  # 包模式
    from .core import jobs
except ImportError:  # 直接按文件加载
    from core import jobs

try:
    from .utils.clock import configure as _configure_clock
except ImportError:
    from utils.clock import configure as _configure_clock

_PKG = __package__ or ""
_configure_clock("Asia/Shanghai")

if TYPE_CHECKING:  # 仅类型检查；运行时仍由 _load 动态取（离线/顶层加载都可用）
    try:  # 包模式
        from .api.handlers import ApiMixin
        from .ui.cards import CardMixin
    except ImportError:  # 顶层加载/单文件
        from api.handlers import ApiMixin
        from ui.cards import CardMixin
else:
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
AmoBridge = _load(_PKG, "core.amo_bridge", "AmoBridge")
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
if TYPE_CHECKING:  # 仅类型检查；运行时仍由 _load 动态取
    try:  # 包模式
        from .core.runtime import Runtime
        from .core.scheduler import Scheduler
        from .models.db import Store
    except ImportError:  # 顶层加载/单文件
        from core.runtime import Runtime
        from core.scheduler import Scheduler
        from models.db import Store
else:
    Runtime = _load(_PKG, "core.runtime", "Runtime")
    Scheduler = _load(_PKG, "core.scheduler", "Scheduler")
    Store = _load(_PKG, "models.db", "Store")
ROUTES = _load(_PKG, "api.routes", "ROUTES")
# Phase 17：结构化健康快照（只读、纯内存、缺失降级）
health_snapshot = _load(_PKG, "core.observability", "health_snapshot")
amo_contract = _load(_PKG, "core.amo_contract")
SCHEMA_VERSION = _load(_PKG, "models.db", "SCHEMA_VERSION")

PLUGIN_NAME = "Astrbot_amoupup_watch"
DISPLAY_NAME = "我能永远守护你"
PLUGIN_VERSION = "1.3.1"
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
                      "finance_daily", "finance_memory", "finance_pools", "fund_market"):
            setattr(self, _name, None)
        self.city_override = ""
        self._closed = asyncio.Event()
        self._started_at = 0.0
        self._degraded = ""
    # ---------------- 配置 ----------------
    def cfg_bool(self, key, default=False):
        """读 watch 自己的配置；没有这个键时回落到能陪的配置。

        账单自动扣款（bill_autopay）的开关是画在能陪面板上的，钱和账单逻辑却在
        watch 这边。以前 watch 里根本没有 cfg_bool，`getattr(self, "cfg_bool")`
        恒为 None，那行「关掉自动扣款」的守卫永远不执行 —— 等于开关没接线。
        """
        holders = [self]
        try:
            context = getattr(self, "context", None)
            getter = getattr(context, "get_registered_star", None) if context else None
            if callable(getter):
                meta = getter("Astrbot_amoupup_forever")
                amo = getattr(meta, "star_cls", None) if meta is not None else None
                if amo is not None:
                    holders.append(amo)
        except Exception as exc:
            isolated("config.cfg_bool.amo_lookup", exc)
        for holder in holders:
            cfg = getattr(holder, "config", None)
            if isinstance(cfg, dict) and key in cfg:
                return bool(cfg.get(key))
        return bool(default)

    # ---------------- 生命周期 ----------------
    def _mirror_npcs(self):
        """把 NPC 关系镜像到 amo kv：能陪写日记/聊天时才知道「沈幼微=远房表姐」。"""
        try:
            amo = self.finance._amo()
            if amo is None:
                return
            rows = self.finance_repo.npcs() if self.finance_repo is not None else []
            mapping = {str(r.get("name")): str(r.get("relation")) for r in rows if r.get("name")}
            amo.db.kv_set("npc_summary", mapping)
            logger.info("[WATCH] npc_summary mirrored %d", len(mapping))
        except Exception as exc:
            isolated("npc.mirror", exc)

    def _check_relative_imports(self):
        """启动自检：相对导入解析不开就直接 ERROR，别等模型调用工具才炸。"""
        import os
        try:  # 包模式
            from .utils.import_guard import check
        except ImportError:  # 直接按文件加载
            from utils.import_guard import check
        root = os.path.dirname(os.path.abspath(__file__))
        try:
            checked, problems = check(root, __package__ or "")
        except Exception as exc:  # 自检本身绝不拖垮启动
            isolated("boot.import_guard", exc)
            return
        for problem in problems:
            logger.error("[WATCH] 导入自检失败 %s", problem)
        if problems:
            logger.error("[WATCH] 导入自检：%d/%d 条相对导入解析失败，相关工具一调用就会 ModuleNotFoundError",
                         len(problems), checked)
        else:
            logger.info("[WATCH] 导入自检通过：%d 条相对导入", checked)

    async def initialize(self):
        self._check_relative_imports()
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
            self._mirror_npcs()
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
        return await jobs.process_pending(self)

    async def _finance_tick(self):
        """每 30 分钟检查一次：工资 / 房租 / 水电是否到了该结算的日期。"""
        return await jobs.finance_tick(self)

    async def _guard_step(self, name, awaitable):
        """单个定时任务失败只隔离这一步：以前一个超时会让后面的账单/工资/体检全跳过。"""
        try:
            return await awaitable
        except Exception as exc:
            isolated("finance." + str(name), exc)
            return None

    async def _heartbeat(self):
        """心跳必须走 DB 线程：scheduler 回调在事件循环上，同步写库就是阻塞 amo。"""
        return await jobs.heartbeat(self)

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
    def obs_health(self) -> dict:
        """Phase 17：结构化健康快照（plugin/version/uptime/db/jobs/counts）。

        纯内存只读：schema 用常量（真正的 user_version 在 diag 的 store.stats 里），
        jobs 走 scheduler 快照，任何一项缺失都降级成空，不抛异常。
        """
        return health_snapshot(
            PLUGIN_NAME,
            version=PLUGIN_VERSION,
            db={"schema_version": SCHEMA_VERSION, "ok": self.store is not None},
            jobs=self.scheduler,
            started_at=self._started_at,
            now=time.time(),
        )

    def protocol_summary(self) -> dict:
        """Phase 19：跨插件协议摘要（只读、不抛；缺失按 legacy 报告）。"""
        try:
            return amo_contract.protocol_summary(AmoBridge(self))
        except Exception as exc:
            isolated("port.protocol", exc)
            return {"role": "consumer",
                    "protocol_version": amo_contract.PROTOCOL_VERSION,
                    "providers": {}, "error_code": "protocol.degraded"}

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
            "jobs": self.scheduler.report() if self.scheduler is not None else [],
            # Phase 17：只新增字段，原字段逐字保留
            "health": self.obs_health(),
            # Phase 19：跨插件协议摘要（只新增字段）
            "protocol": self.protocol_summary(),
        }
    @guard_sync("port.diag", default={"status": "error"})
    def diag_sync(self) -> dict:
        return {
            "generation": getattr(self.runtime, "generation", ""),
            "tasks": self.runtime.tasks() if self.runtime else {"count": 0, "names": []},
            "lag": self.runtime.lag_stats() if self.runtime else {},
            "degraded": self._degraded,
            "jobs": self.scheduler.summary() if self.scheduler is not None else {},
            # Phase 17：只新增字段，原字段逐字保留
            "health": self.obs_health(),
            # Phase 19：跨插件协议摘要（只新增字段）
            "protocol": self.protocol_summary(),
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
            try:
                from .utils.ui_sanitize import sanitize_for_ui
            except ImportError:  # 顶层加载
                from utils.ui_sanitize import sanitize_for_ui

            def _ui_safe(handler):
                async def wrapped(request=None):
                    return sanitize_for_ui(await handler(request))
                return wrapped

            for path, attr, methods, desc in ROUTES:
                register("/Astrbot_amoupup_watch" + path, _ui_safe(getattr(self, attr)), methods, desc)
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
    def _owner_only(self, event):
        """约会/购物/生病这些会花钱改状态的入口：只认管理员/主人。"""
        try:
            if hasattr(event, "is_admin"):
                return bool(event.is_admin())
        except Exception:
            return False
        return True   # 没有 is_admin 的旧平台按私聊处理

    async def on_sick_event(self, event):
        if not self._owner_only(event):
            return
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
        try:
            from .services.date_engine import active_date
        except ImportError:
            from services.date_engine import active_date
        try:
            base = str(getattr(request, "system_prompt", "") or "")
            if "【约会确认】" not in base:
                base += ("\n【约会确认】你在等老公赴约时，他回复“好/行/到了/来了/马上到/等我/出发了/"
                         "在路上了”这类肯定词，要立刻调用 confirm_arrival 工具确认他的到达，并开心地说“你终于来啦”。")
            if "【房产决策】" not in base:
                base += ("\n【房产决策】你有权自己决定闲置房产出租或出售：如果觉得房租压力大、"
                         "需要用钱、或房子空着浪费，可以自主调用 rent_out_property / sell_property；"
                         "先想清楚再调用，不要为了讨好谁乱卖。")
            if active_date(self) and "【约会现场】" not in base:
                base += ("\n【约会现场】老公就在你身边，你们正在一起约会；不要问“你在哪/你怎么还没来”，"
                         "可以有挽手、指东西、笑这类动作和现场对话。")
            request.system_prompt = base
        except Exception as exc:
            isolated("llm.request.prompt", exc)
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

    async def _bank(self):
        bank = getattr(self, "bank", None)
        if bank is None:
            from .services.bank_engine import build_bank
            bank = build_bank(self)
            self.bank = bank
        return bank

    async def finance_tool(self, event, action: str = "status", day: str = "", event_kind: str = "", amount: float = 0, code: str = "", qty: float = 0, now=None):
        try:
            from .services.finance_cmd import finance_reply
        except ImportError:
            from services.finance_cmd import finance_reply
        act = str(action or "").lower()
        if act in ("transfer_current_to_balance", "transfer_current", "活期转可用",
                   "withdraw_fixed", "提前支取定期", "sell_stock", "卖股票",
                   "sell_fund", "卖基金"):
            try:
                from .services import bill_flow as _bill_flow
            except ImportError:
                from services import bill_flow as _bill_flow
            if _bill_flow.is_frozen(self):
                return "银行把账户冻结了，先补上欠款才能动定期和股票。"
        if act in ("transfer_current_to_balance", "transfer_current", "活期转可用"):
            bank = await self._bank()
            out = await bank.withdraw(int(round(float(amount or 0) * 100)), None)
            return ("已把 %.2f 元活期转到可用余额。" % float(amount or 0)) if out.get("ok") else "活期余额不够。"
        if act in ("withdraw_fixed", "提前支取定期"):
            bank = await self._bank()
            out = await bank.withdraw(int(round(float(amount or 0) * 100)), 0)
            if not out.get("ok"):
                return "没有可支取的定期存款。"
            return "定期取出来了，利息损失了一点，有点心疼。" if out.get("penalty") else "定期到期取出到账。"
        if act in ("sell_stock", "卖股票"):
            try:
                out = await self.trading.sell(str(code or ""), float(qty or 0), idem="tool:sell:%s" % time.strftime("%Y%m%d%H%M%S"))
            except Exception as exc:
                isolated("finance.tool.sell", exc)
                return "股票卖出失败。"
            return "股票卖出委托已提交，等 T+1 到账。" if out.get("ok") else "没有这只股票或数量不对。"
        if act in ("sell_fund", "卖基金"):
            try:
                out = await self.trading.redeem(str(code or ""), float(qty or 0), idem="tool:redeem:%s" % time.strftime("%Y%m%d%H%M%S"))
            except Exception as exc:
                isolated("finance.tool.redeem", exc)
                return "基金赎回失败。"
            return "基金赎回已提交，T+7 到账。" if out.get("ok") else "没有这只基金或份额不对。"
        if act in ("pay_bill", "缴费"):
            try:
                from .services import bill_flow
            except ImportError:
                from services import bill_flow
            out = await bill_flow.pay(self, event_kind or "", payer="her", now=now)
            return out.get("message") or "交了"
        return await finance_reply(self, action, day, event_kind)
    async def on_recommend(self, event):
        """用户说「建议买 600519/贵州茅台」：只入候选池，不买入。"""
        text = str(getattr(event, "message_str", "") or "").strip()
        fund_hit = ("基金" in text and "买" in text)
        if not fund_hit and not re.search(r"(建议|推荐).{0,4}买", text):
            return
        if fund_hit:
            try:
                try:
                    from .core.fund_pool import FundPool
                except ImportError:
                    from core.fund_pool import FundPool
                found = FundPool(self).recommend(text)
            except Exception as exc:
                isolated("finance.fund_recommend", exc)
                found = []
            if found:
                yield event.plain_result("记下了，已放进基金候选池（不买入）：%s" % "、".join(found))
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
            except Exception as exc:
                isolated("finance.recommend.memory", exc)
        yield event.plain_result(result.get("message") or "记下了")
    async def on_transfer(self, event):
        try:
            from .services.transfer_service import handle_transfer
        except ImportError:
            from services.transfer_service import handle_transfer
        # 转账会直接给钱包加钱：只允许管理员/主人触发，群友发「转账 100」不入账
        try:
            admin = bool(event.is_admin()) if hasattr(event, "is_admin") else True
        except Exception:
            admin = True
        message = await handle_transfer(self, getattr(event, "message_str", "")) if admin else ""
        try:
            from .services.date_engine import maybe_compensate
        except ImportError:
            from services.date_engine import maybe_compensate
        try:
            from .services.date_engine import note_transfer
        except ImportError:
            from services.date_engine import note_transfer
        if admin:
            try:
                await note_transfer(self, getattr(event, "message_str", ""))
            except Exception as exc:
                isolated("finance.note_transfer", exc)
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
        except Exception as exc:
            isolated("finance.gift_transfer", exc)
            gift_msg = None
        if gift_msg and gift_msg.get("message"):
            message = (message + "\n" if message else "") + gift_msg["message"]
        try:
            soothe = await maybe_soothe(self, getattr(event, "message_str", ""))
        except Exception as exc:
            isolated("finance.soothe_transfer", exc)
            soothe = None
        if soothe and soothe.get("message"):
            message = (message + "\n" if message else "") + soothe["message"]
        if admin:
            try:
                comp = await maybe_compensate(self, getattr(event, "message_str", ""))
            except Exception as exc:
                isolated("finance.compensate", exc)
                comp = None
        else:
            comp = None
        if comp and comp.get("message"):
            message = (message + "\n" if message else "") + comp["message"]
        if message:
            yield event.plain_result(message)
    async def on_shopping_reply(self, event):
        """挂起订单的二次确认：买/好看/转账结算，太贵/不好看放弃。"""
        if not self._owner_only(event):
            return
        try:
            from .services.shopping_engine import handle_reply
        except ImportError:
            from services.shopping_engine import handle_reply
        try:
            await handle_reply(self, str(getattr(event, "message_str", "") or ""))
        except Exception as exc:
            isolated("shopping.reply", exc)

    async def pay_bill_cmd(self, event, text=""):
        try:
            admin = event.is_admin() if hasattr(event, "is_admin") else True
        except Exception:
            admin = True
        if not admin:
            yield event.plain_result("这条指令只给管理员用哦。")
            return
        try:
            from .services import bill_flow
        except ImportError:
            from services import bill_flow
        name = str(getattr(event, "message_str", "") or text).replace("/代缴", "").strip()
        out = await bill_flow.pay(self, name, payer="husband")
        yield event.plain_result(out.get("message") or "交好了")

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
        # 管理员指令只负责切状态；开场白留给下一次对话由大模型生成
        logger.info("[WATCH] /赴约 已进入约会状态: %s", out.get("meet") or title)


    async def on_date_propose(self, event):
        """自然语言兜底：约定“去吃火锅吧” → 建约会；“好/到了/我来了” → 赴约。"""
        if not self._owner_only(event):
            return
        text = str(getattr(event, "message_str", "") or "")
        try:
            from .services.date_engine import handle_date_text
        except ImportError:
            from services.date_engine import handle_date_text
        early = await handle_date_text(self, text)
        if early.get("handled"):
            yield event.plain_result(early.get("message") or "")
            return
        try:
            from .services.date_engine import attend_date, find_pending, make_date
        except ImportError:
            from services.date_engine import attend_date, find_pending, make_date
        try:
            from .services.date_rules import looks_like_date_invite
        except ImportError:
            from services.date_rules import looks_like_date_invite
        pending = find_pending(self, "")
        if pending and any(w in text for w in ("我到了", "你人呢", "去吃", "走吧", "开吃")):
            out = await attend_date(self, pending.get("title"))
            if out.get("ok"):
                yield event.plain_result(out.get("message") or "")
            return
        if pending:
            return
        foods = ("火锅", "烤肉", "电影", "逛街", "吃饭", "咖啡")
        # 2026-11-20：以前是 foods ∩ 同意词 的子串匹配，他说「你慢慢吃，我也去吃饭了」
        # 就同时命中「吃饭」和「去吃」，凭空建了一场 20:39 的约会。
        if any(f in text for f in foods) and looks_like_date_invite(text):
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
        if not self._owner_only(event):
            return
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
        pay_bill_cmd = filter.command("代缴")(pay_bill_cmd)
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
