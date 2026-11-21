# -*- coding: utf-8 -*-
"""金融 ↔ amo 联动层（运行在「我能永远守护你」插件里）。

调用点（全部走 amo 公开方法，不碰表名/字段名）：
- 余额/扣款/入账 → amo_wallet.py Wallet.balance / credit / debit
- 心情          → amo_human_state.py HumanState.nudge_once / mood_words / mood_score
- 日程          → amo_human_state.py HumanState.add_schedule_item(day, item, source="finance", reason=...)
- 小日记        → amo_db.py AmoDB.save_diary(day, hour, text, version=None, status="valid")
- 长日记        → amo_api_diary.py DiaryApiMixin.write_diary_text(day, text)
- 记忆          → amo_memory_data.py HumanCorePlugin.finance_remember(content, kind, tier, idem_key)
                  与 finance_recall(query, limit)（见 finance_memory.py）

任何一步失败只记 [FIN] 日志并继续，绝不让交易/扣款中断，也不假装成功。
对外文本只出现自然语言情绪词，不出现心情数值。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import time

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

try:
    from ..utils.mood import (expense_level, income_level, low_balance_level, pnl_level,
                               word_for_score, yuan)
    from ..guard import isolated
    from .memory_link import MemoryLink
except ImportError:  # 按顶层包/单文件加载时
    from utils.mood import (expense_level, income_level, low_balance_level, pnl_level,
                              word_for_score, yuan)
    from guard import isolated
    from core.memory_link import MemoryLink

try:
    from ..utils import facts
except ImportError:
    from utils import facts

try:
    from . import amo_contract
except ImportError:  # 按顶层包加载时
    import amo_contract


AMO_PLUGIN_NAME = "Astrbot_amoupup_forever"

#: 逛街买到这些东西才算“给家里添家具”，普通衣服不碰房屋系统
FURNITURE_WORDS = ("沙发", "床", "衣柜", "餐桌", "书桌", "书柜", "电视柜", "茶几",
                   "冰箱", "洗衣机", "空调", "梳妆台", "地毯", "窗帘", "吊灯", "置物架",
                   "鞋柜", "床头柜", "台灯", "桌布", "抱枕", "床单", "被套")



def _data_provider(plugin):
    """找「我能永远记住你」对外门面（star.mem 优先）；与 nightly_memory 同一约定。"""
    context = getattr(plugin, "context", None)
    getter = getattr(context, "get_registered_star", None) if context else None
    if not callable(getter):
        return None
    for name in ("Astrbot_amoupup_data", "Astr_Amo_data"):
        try:
            meta = getter(name)
        except Exception:
            meta = None
        if meta is None or getattr(meta, "activated", True) is False:
            continue
        star = getattr(meta, "star_cls", None) or getattr(meta, "star", None)
        if star is None:
            continue
        return getattr(star, "mem", None) or star
    return None


def _protocol_dict(value):
    """protocol_info() 只收 dict；别的形状一律按缺失处理。"""
    return dict(value) if isinstance(value, dict) else None


# ======================================================================
# 跨插件调用层（Phase 14）：业务代码只许通过 AmoBridge 访问 amo
# ======================================================================
class AmoCallResult:
    """一次跨插件调用的结果；reason ∈ ok/not_ready/missing/failed/timeout。"""

    __slots__ = ("ok", "value", "reason", "error")

    def __init__(self, ok, value=None, reason="ok", error=None):
        self.ok = bool(ok)
        self.value = value
        self.reason = str(reason or "")
        self.error = error

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return "AmoCallResult(ok=%r, reason=%r)" % (self.ok, self.reason)


#: 方法名 -> 真实调用。lambda 第一参数故意叫 amo：跨插件契约快照按 `amo.xxx`
#: 扫描（tests/test_cross_plugin_contract.py），这里就是守护侧唯一的调用点。
_CALLS = {
    # amo 根方法
    "add_closet_item": lambda amo, *a, **k: amo.add_closet_item(*a, **k),
    "add_hunger": lambda amo, *a, **k: amo.add_hunger(*a, **k),
    "cancel_date_session": lambda amo, *a, **k: amo.cancel_date_session(*a, **k),
    "clear_date_ending": lambda amo, *a, **k: amo.clear_date_ending(*a, **k),
    "clear_date_pause": lambda amo, *a, **k: amo.clear_date_pause(*a, **k),
    "date_ending": lambda amo, *a, **k: amo.date_ending(*a, **k),
    "date_session": lambda amo, *a, **k: amo.date_session(*a, **k),
    "finish_date_session": lambda amo, *a, **k: amo.finish_date_session(*a, **k),
    "is_dating": lambda amo, *a, **k: amo.is_dating(*a, **k),
    "_owner_umos": lambda amo, *a, **k: amo._owner_umos(*a, **k),
    "note_expense_summary": lambda amo, *a, **k: amo.note_expense_summary(*a, **k),
    "protocol_info": lambda amo, *a, **k: amo.protocol_info(*a, **k),
    "push_to_owner": lambda amo, *a, **k: amo.push_to_owner(*a, **k),
    "set_current_action": lambda amo, *a, **k: amo.set_current_action(*a, **k),
    "set_date_fact": lambda amo, *a, **k: amo.set_date_fact(*a, **k),
    "set_date_home": lambda amo, *a, **k: amo.set_date_home(*a, **k),
    "set_date_incident": lambda amo, *a, **k: amo.set_date_incident(*a, **k),
    "set_date_meet": lambda amo, *a, **k: amo.set_date_meet(*a, **k),
    "set_date_mood": lambda amo, *a, **k: amo.set_date_mood(*a, **k),
    "set_date_now": lambda amo, *a, **k: amo.set_date_now(*a, **k),
    "set_date_pause": lambda amo, *a, **k: amo.set_date_pause(*a, **k),
    "set_date_prep": lambda amo, *a, **k: amo.set_date_prep(*a, **k),
    "set_date_separation": lambda amo, *a, **k: amo.set_date_separation(*a, **k),
    "set_date_ending": lambda amo, *a, **k: amo.set_date_ending(*a, **k),
    "set_last_date_event": lambda amo, *a, **k: amo.set_last_date_event(*a, **k),
    # 日程 / 心情状态机
    "add_schedule_item": lambda amo, *a, **k: amo.state.add_schedule_item(*a, **k),
    "relation_of": lambda amo, *a, **k: amo.relation_of(*a, **k),
    "remove_schedule_block": lambda amo, *a, **k: amo.remove_schedule_block(*a, **k),
    "append_diary_note": lambda amo, *a, **k: amo.append_diary_note(*a, **k),
    "commit_schedule_change": lambda amo, *a, **k: amo.state.commit_schedule_change(*a, **k),
    "current_activity": lambda amo, *a, **k: amo.state.current_activity(*a, **k),
    "nudge": lambda amo, *a, **k: amo.state.nudge(*a, **k),
    "get_schedule": lambda amo, *a, **k: amo.state.db.get_schedule(*a, **k),
    # 数据库 / 钱包 / 大模型
    "kv_get": lambda amo, *a, **k: amo.db.kv_get(*a, **k),
    "kv_set": lambda amo, *a, **k: amo.db.kv_set(*a, **k),
    "save_diary": lambda amo, *a, **k: amo.db.save_diary(*a, **k),
    "balance": lambda amo, *a, **k: amo.wallet.balance(*a, **k),
    "credit": lambda amo, *a, **k: amo.wallet.credit(*a, **k),
    "debit": lambda amo, *a, **k: amo.wallet.debit(*a, **k),
    "late_snack_tick": lambda amo, *a, **k: amo.late_snack_tick(*a, **k),
    "late_snack_decisions": lambda amo, *a, **k: amo.late_snack_decisions(*a, **k),
    "ledger": lambda amo, *a, **k: amo.wallet.ledger(*a, **k),
    "chat_raw": lambda amo, *a, **k: amo.llm.chat_raw(*a, **k),
}

#: Phase 15：set_date_ending / set_last_date_event 已经登记进 _CALLS 并补进快照，
#: 已知漂移清零。保留这个空元组，是为了让「没登记的方法」兜底分支继续存在。
_HIDDEN = ()


class AmoBridge:
    """amo 调用门面：统一「未就绪 / 方法缺失 / 异常 / 超时」的降级与日志。

    业务代码（date_engine / social_engine）不再自己 `_amo()` 后随手调，
    一律 `AmoBridge(plugin).call(...)`；返回 AmoCallResult，失败不抛。
    """

    def __init__(self, plugin, finance=None, scope="amo", amo=None):
        self.plugin = plugin
        self._finance = finance
        self.scope = str(scope or "amo")
        self._given = amo
        self._amo = None
        self._resolved = False

    # ---------- 解析 ----------
    def resolve(self):
        """懒解析 amo：优先显式 finance，其次 plugin.finance；拿到就缓存。"""
        if self._resolved:
            return self._amo
        self._resolved = True
        if self._given is not None:      # 老代码/测试直接传 amo 对象
            self._amo = self._given
            return self._amo
        finance = self._finance if self._finance is not None else getattr(self.plugin, "finance", None)
        getter = getattr(finance, "_amo", None)
        if not callable(getter):
            self._amo = None
            return None
        try:
            self._amo = getter()
        except Exception as exc:
            isolated("amo_bridge.resolve", exc)
            self._amo = None
        if self._amo is None:
            try:
                logger.info("[WATCH][AMO] 能陪未就绪，%s 降级继续", self.scope)
            except Exception:  # cleanup: 日志失败不影响主链
                pass
        return self._amo

    @property
    def ready(self):
        return self.resolve() is not None

    # ---------- 协议自述（Phase 19） ----------
    def protocol_info(self, provider="forever"):
        """读对方 protocol_info()：forever 走 amo 本体，data 走能记门面。

        对方没装 / 旧版本 / 抛异常一律返回 None，交给 amo_contract.compat 判 legacy。
        """
        name = str(provider or "forever")
        if name == "data":
            data = _data_provider(self.plugin)
            if data is None:
                return None
            try:
                return _protocol_dict(data.protocol_info())
            except AttributeError:          # 老能记没有这个方法 -> legacy
                return None
            except Exception as exc:
                isolated("amo_bridge.protocol_info.data", exc)
                return None
        amo = self.resolve()
        if amo is None:
            return None
        try:
            return _protocol_dict(amo.protocol_info())
        except AttributeError:              # 老能陪没有这个方法 -> legacy
            return None
        except Exception as exc:
            isolated("amo_bridge.protocol_info", exc)
            return None

    # ---------- 调用 ----------
    def _note_contract(self, method):
        ok, reason = amo_contract.check(method)
        if ok:
            return
        try:
            logger.warning("[WATCH][CONTRACT] 快照未收录 %s（%s）；按已知漂移/新方法放行",
                           method, reason or "不在 surface 快照里")
        except Exception:  # cleanup: 日志失败不影响主链
            pass

    def call(self, method, *args, **kwargs):
        """同步调用：返回 AmoCallResult（awaitable 原样放在 value 里）。"""
        amo = self.resolve()
        if amo is None:
            return AmoCallResult(False, reason="not_ready")
        fn = _CALLS.get(str(method or ""))
        if fn is None:
            if method in _HIDDEN:
                return self._hidden_call(amo, method, args, kwargs)
            try:
                logger.warning("[WATCH][AMO] 调用层没有登记方法 %s，已跳过", method)
            except Exception:  # cleanup: 日志失败不影响主链
                pass
            return AmoCallResult(False, reason="missing")
        self._note_contract(method)
        try:
            return AmoCallResult(True, fn(amo, *args, **kwargs))
        except AttributeError as exc:      # 老 amo / 测试桩没有这个方法
            return AmoCallResult(False, reason="missing", error=exc)
        except Exception as exc:
            isolated("amo_bridge." + str(method), exc)
            return AmoCallResult(False, reason="failed", error=exc)

    def _hidden_call(self, amo, method, args, kwargs):
        self._note_contract(method)
        try:
            target = getattr(amo, method)
        except AttributeError:
            try:
                logger.warning("[WATCH][AMO] 能陪没有方法 %s（老版本？），已跳过", method)
            except Exception:  # cleanup: 日志失败不影响主链
                pass
            return AmoCallResult(False, reason="missing")
        try:
            return AmoCallResult(True, target(*args, **kwargs))
        except Exception as exc:
            isolated("amo_bridge." + str(method), exc)
            return AmoCallResult(False, reason="failed", error=exc)

    def call_compat(self, method, variants):
        """老 amo 签名兼容：按顺序试参数变体，只有 TypeError 才降级到下一组。

        variants: [((args...), {kwargs...}), ...] 或 [(args...), ...]
        """
        last = AmoCallResult(False, reason="not_ready")
        for item in variants or []:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], dict):
                args, kwargs = item
            else:
                args, kwargs = item, {}
            last = self.call(method, *tuple(args or ()), **dict(kwargs or {}))
            if last.ok or not isinstance(last.error, TypeError):
                return last
        return last

    async def call_async(self, method, *args, timeout=None, **kwargs):
        """异步调用（能陪的 push_to_owner 等）：可带超时，超时按 failed 降级。"""
        import asyncio
        import inspect
        amo = self.resolve()
        if amo is None:
            return AmoCallResult(False, reason="not_ready")
        fn = _CALLS.get(str(method or ""))
        if fn is None:
            if method in _HIDDEN:
                async def _run_hidden():
                    return self._hidden_call(amo, method, args, kwargs)
                result = await _run_hidden()
                return result
            try:
                logger.warning("[WATCH][AMO] 调用层没有登记方法 %s，已跳过", method)
            except Exception:  # cleanup: 日志失败不影响主链
                pass
            return AmoCallResult(False, reason="missing")
        self._note_contract(method)
        try:
            value = fn(amo, *args, **kwargs)
            if inspect.isawaitable(value):
                if timeout:
                    value = await asyncio.wait_for(value, timeout)
                else:
                    value = await value
            return AmoCallResult(True, value)
        except Exception as exc:
            reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else "failed"
            isolated("amo_bridge." + str(method), exc)
            return AmoCallResult(False, reason=reason, error=exc)

    # ---------- 只读属性 ----------
    def get(self, path, default=None):
        """读 amo 上的属性链（如 "state.schedule"）；缺失/异常返回 default。"""
        amo = self.resolve()
        if amo is None:
            return default
        target = amo
        for part in str(path or "").split("."):
            if not part:
                continue
            target = getattr(target, part, None)
            if target is None:
                return default
        return target

    def state(self):
        return self.get("state", None)

    def db(self):
        return self.get("db", None)

    def schedule(self):
        return self.get("state.schedule", {}) or {}

    def owner_umos(self):
        return list(self.call("_owner_umos").value or [])

class FinanceLink:
    def __init__(self, plugin):
        self.plugin = plugin
        self.memory = MemoryLink(plugin)

    # ---------- 找 amo（懒解析，不 import 它的模块） ----------
    def _amo(self):
        try:
            context = getattr(self.plugin, "context", None)
            getter = getattr(context, "get_registered_star", None) if context else None
            meta = getter(AMO_PLUGIN_NAME) if callable(getter) else None
            if meta is None or getattr(meta, "activated", True) is False:
                return None
            amo = getattr(meta, "star_cls", None)
            if amo is None or getattr(amo, "state", None) is None or getattr(amo, "wallet", None) is None:
                return None
            return amo
        except Exception as exc:
            isolated("finance.amo", exc)
            return None

    # ---------- 房屋联动 ----------
    def house_bought(self, label="房子", city="", style="简约风"):
        """买房成功：写进 amo 房屋系统并搬入，再异步生成客厅/卧室场景图。"""
        amo = self._amo()
        housing = getattr(amo, "housing", None) if amo is not None else None
        if housing is None:
            return False
        try:
            home = housing.add_home(str(label or "房子"), kind="own",
                                    city=str(city or ""), style=str(style or "简约风"))
            housing.move_in(home["id"])
            spawn = getattr(amo, "_spawn", None)
            ensure = getattr(amo, "_ensure_home_images", None)
            if callable(spawn) and callable(ensure):
                spawn(ensure(force=True))
            return True
        except Exception as exc:
            isolated("finance.house_bought", exc)
            return False

    def house_lost(self):
        """破产/法拍：让她搬回最便宜的出租屋。"""
        amo = self._amo()
        housing = getattr(amo, "housing", None) if amo is not None else None
        if housing is None:
            return False
        try:
            housing.on_bankruptcy()
            return True
        except Exception as exc:
            isolated("finance.house_lost", exc)
            return False

    def furniture_bought(self, name):
        """逛街买到大件家具：写进当前家的陈设；大件再触发一次场景图更新。"""
        body = str(name or "").strip()
        if not any(word in body for word in FURNITURE_WORDS):
            return False
        amo = self._amo()
        housing = getattr(amo, "housing", None) if amo is not None else None
        if housing is None:
            return False
        try:
            result = housing.add_furniture(body)
            if not result.get("ok"):
                return False
            if result.get("redraw"):
                spawn = getattr(amo, "_spawn", None)
                ensure = getattr(amo, "_ensure_home_images", None)
                if callable(spawn) and callable(ensure):
                    spawn(ensure())
            return True
        except Exception as exc:
            isolated("finance.furniture_bought", exc)
            return False

    # ---------- 只读 ----------
    def balance_cents(self):
        amo = self._amo()
        if amo is None:
            return None
        try:
            return int(amo.wallet.balance())
        except Exception as exc:
            isolated("finance.balance", exc)
            return None

    def withdrawable_cents(self):
        amo = self._amo()
        if amo is None:
            return None
        try:
            return int(amo.wallet.balance("withdrawable"))
        except Exception as exc:
            isolated("finance.withdrawable", exc)
            return None

    def held_cents(self):
        """被冻结的钱（答应饭局到点才扣的那笔）。旧钱包没有 hold 就当 0。"""
        amo = self._amo()
        if amo is None:
            return 0
        try:
            fn = getattr(amo.wallet, "held", None)
            return int(fn()) if callable(fn) else 0
        except Exception as exc:
            isolated("finance.held", exc)
            return 0

    def available_cents(self):
        """可花余额 = 余额 - 冻结；旧钱包退化成余额。"""
        amo = self._amo()
        if amo is None:
            return None
        try:
            fn = getattr(amo.wallet, "available", None)
            if callable(fn):
                return int(fn())
            return int(amo.wallet.balance())
        except Exception as exc:
            isolated("finance.available", exc)
            return None

    def hold(self, amount_cents, reason, idem, expires_at=0.0):
        """冻结一笔钱（余额不变，别的扣款动不了）；返回是否冻住。"""
        amo = self._amo()
        if amo is None:
            return False
        try:
            fn = getattr(amo.wallet, "hold", None)
            if not callable(fn):
                return False
            return bool(fn(int(amount_cents), str(reason or ""), str(idem),
                           expires_at=float(expires_at or 0)))
        except Exception as exc:
            isolated("finance.hold", exc)
            return False

    def release_hold(self, idem):
        amo = self._amo()
        if amo is None:
            return False
        try:
            fn = getattr(amo.wallet, "release", None)
            if not callable(fn):
                return False
            return bool(fn(str(idem or "")))
        except Exception as exc:
            isolated("finance.release_hold", exc)
            return False

    def ledger(self, limit=200):
        """只读流水：面板生活账单要按原因分类，读 amo 公开的 wallet.ledger()，不碰表。"""
        amo = self._amo()
        if amo is None:
            return []
        try:
            return list(amo.wallet.ledger(int(limit)) or [])
        except Exception as exc:
            isolated("finance.ledger", exc)
            return []

    def transfer_in(self, amount_cents, idem):
        """用户转账：可用和可取同时增加（银行入账，不是卖出）。"""
        amo = self._amo()
        if amo is None:
            return False
        try:
            if not amo.wallet.credit(int(amount_cents), "用户转账", idem):
                return False
            amo.wallet.credit(int(amount_cents), "用户转账-可取", idem + ":w", account="withdrawable")
            return True
        except Exception as exc:
            isolated("finance.transfer_in", exc)
            return False

    def mark_withdrawable(self, amount_cents, idem):
        amo = self._amo()
        if amo is None:
            return False
        try:
            return bool(amo.wallet.credit(int(amount_cents), "转账可取", idem, account="withdrawable"))
        except Exception as exc:
            isolated("finance.mark_withdrawable", exc)
            return False

    def mood(self):
        """每次现读，不缓存（amo 是情绪权威）。"""
        amo = self._amo()
        if amo is None:
            return {"score": None, "word": "平静", "dominant": ""}
        try:
            state = amo.state
            score = float(state.mood_score())
            word_fn = getattr(state, "mood_words", None)
            word = str(word_fn()) if callable(word_fn) else word_for_score(score)
            return {"score": score, "word": word, "dominant": str(state.dominant_mood())}
        except Exception as exc:
            isolated("finance.mood", exc)
            return {"score": None, "word": "平静", "dominant": ""}

    def prompt_line(self):
        """给 LLM 提示词用的一句话（只含情绪词，不含数值）。"""
        return "她现在" + self.mood()["word"] + "。"

    async def memory_context(self, limit=5):
        """对话前检索持仓/盈亏记忆，供注入 LLM 上下文。"""
        return await self.memory.context(limit=limit)

    async def _tier(self, tier, content, idem, related="", tag=""):
        tm = getattr(self.plugin, "finance_memory", None)
        if tm is None:
            return (await self.memory.remember(content, kind="finance", idem=idem)).get("ok")
        if tier == "short" and related:
            return await tm.overwrite_short(related, content, idem, tag=tag or "pnl")
        return await tm.remember(tier, content, idem, related=related, tag=tag)

    def credit_initial(self, cents):
        """首次启动写入初始存款（只调 amo 钱包，不碰余额字段）。"""
        amo = self._amo()
        if amo is None:
            return False
        try:
            ok = bool(amo.wallet.credit(int(cents), "初始存款", "seed:initial"))
            if ok:
                amo.wallet.credit(int(cents), "初始存款-可取", "seed:initial:w", account="withdrawable")
            return ok
        except Exception as exc:
            isolated("finance.seed", exc)
            return False

    def backfill_withdrawable(self):
        """老库一次性补齐：已有的现金默认可取（只补一次）。"""
        amo = self._amo()
        repo = getattr(self.plugin, "finance_repo", None)
        if amo is None or repo is None:
            return False
        if repo.meta_get("withdrawable_backfilled") == "1":
            return False
        cash = int(amo.wallet.balance("cash") or 0)
        wd = int(amo.wallet.balance("withdrawable") or 0)
        if cash > wd:
            try:
                amo.wallet.credit(cash - wd, "可取余额初始化", "seed:withdrawable", account="withdrawable")
            except Exception as exc:
                isolated("finance.withdrawable_backfill", exc)
        repo.meta_set("withdrawable_backfilled", "1")
        return True

    def _count(self, key, cents):
        try:
            repo = getattr(self.plugin, "finance_repo", None)
            if repo is None:
                return
            now = int(repo.meta_get(key, 0) or 0)
            repo.meta_set(key, now + int(cents))
        except Exception as exc:
            logger.debug("[WATCH] optional step failed: %r", exc)

    # ---------- 写：内部小工具（每步单独兜底） ----------
    def _nudge(self, amo, idem, deltas):
        if not deltas:
            return True
        try:
            once = getattr(amo.state, "nudge_once", None)
            if callable(once):
                return bool(once(idem + ":mood", **deltas))
            amo.state.nudge(**deltas)
            return True
        except Exception as exc:
            isolated("finance.nudge", exc)
            return False

    def _schedule(self, amo, kind, amount_cents, note="", when_ts=None):
        try:
            stamp = time.localtime(when_ts) if when_ts else time.localtime()
            if 0 <= int(stamp.tm_hour) < 6:
                # 兜底：00:00-06:00 绝不写财务日程，一律记到当天 10:00
                stamp = time.struct_time((
                    stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 10, 0, 0,
                    stamp.tm_wday, stamp.tm_yday, stamp.tm_isdst,
                ))
            start = "%02d:%02d" % (stamp.tm_hour, stamp.tm_min)
            end_min = stamp.tm_hour * 60 + stamp.tm_min + 10
            if end_min >= 24 * 60:
                # 23:5x 的财务条目跨零点：日程是当天表，end 回绕成 00:0x 比 start 还早，
                # 日程校验会把它当坏数据；这里钳到当天最后一分钟。
                end_min = 24 * 60 - 1
            # 地点必须跟当前活动一致：约会中买单不能写成「在家买单」
            place, zone = "家", ""
            try:
                act = amo.state.current_activity() or {}
                place = str(act.get("place") or "家")
                zone = str(act.get("zone") or "")
            except Exception:
                place, zone = "家", ""
            item = {
                "start": start,
                "end": "%02d:%02d" % (end_min // 60, end_min % 60),
                "activity": "财务·" + str(kind),
                "place": place,
                "category": "finance",
                "detail": ("%s %s 元" % (kind, facts.money(amount_cents))) + (("，" + note) if note else ""),
                "subject": "bot",
            }
            if zone:
                item["zone"] = zone
            day = time.strftime("%Y-%m-%d", stamp)
            result = amo.state.add_schedule_item(day, item, source="finance", reason=kind)
            return bool(isinstance(result, dict) and result.get("changed"))
        except Exception as exc:
            isolated("finance.schedule", exc)
            return False

    @staticmethod
    def _diary_fallback(text):
        """没有 LLM 时也别把账本数字抄进日记：工资只写一句人话。"""
        raw = str(text or "").strip()
        if "发工资" in raw or "工资结算" in raw:
            return "今天发工资了，看到余额多了一点，心里踏实了些。"
        return raw

    def _small_diary(self, amo, text):
        try:
            day, hour = time.strftime("%Y-%m-%d"), time.strftime("%H")
            text = self._diary_fallback(text)
            merged = text
            getter = getattr(getattr(amo, "db", None), "get_diary", None)
            old = ""
            if callable(getter):
                try:
                    old = str(getter(day, hour) or "").strip()
                except Exception as exc:
                    logger.debug("[WATCH] optional step failed: %r", exc)
            # 2026-11-20：这一小时已经有陪伴的块日记 → 不覆盖（块日记会带账本事实自己写）。
            try:
                recorder = getattr(getattr(amo, "db", None), "get_diary_record", None)
                record = recorder(day, hour) if callable(recorder) else None
                if isinstance(record, dict) and str(record.get("source") or "") == "block":
                    return True
            except Exception as exc:
                logger.debug("[WATCH] optional step failed: %r", exc)
            if old and old != text:
                # 同一小时多条流水：合成一句，别让最后一条把前面的人话顶掉
                if text in old:
                    merged = old
                elif len(old) + len(text) + 1 <= 240:
                    merged = old.rstrip("。；; ") + "；" + text
                else:
                    merged = old
            try:
                amo.db.save_diary(day, hour, merged, source="finance")
            except TypeError:
                # 测试替身/老版本 save_diary 不接受 source
                amo.db.save_diary(day, hour, merged)
            return True
        except Exception as exc:
            isolated("finance.small_diary", exc)
            return False

    def _long_diary(self, amo, text):
        try:
            appender = getattr(amo, "append_diary_note", None)
            if callable(appender):
                result = appender(time.strftime("%Y-%m-%d"), str(text))
                if isinstance(result, dict) and result.get("code") == 0:
                    return True
            writer = getattr(amo, "write_diary_text", None)
            if not callable(writer):
                return False
            result = writer(time.strftime("%Y-%m-%d"), str(text))
            return bool(isinstance(result, dict) and result.get("code") == 0)
        except Exception as exc:
            isolated("finance.long_diary", exc)
            return False

    def _balance_text(self, amo):
        try:
            return yuan(int(amo.wallet.balance()))
        except Exception:
            return "未知余额"

    # ---------- 对外：入账 / 支出 / 交易 / 预警 ----------
    async def income(self, amount_cents, reason, idem, salary=False, detail=None, when_ts=None):
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        try:
            steps["wallet"] = bool(amo.wallet.credit(int(amount_cents), reason, idem))
        except Exception as exc:
            isolated("finance.income.wallet", exc)
            steps["wallet"] = False
        if steps.get("wallet"):
            self._count("half_income_cents", int(amount_cents))
        level = income_level(int(amount_cents), salary=salary)
        steps["mood"] = self._nudge(amo, idem, level["deltas"])
        after = self.mood()["word"]
        if salary and when_ts is None:
            # 工资固定记在当天 10:00：结算就算提前跑了，也不能显示成 00:00 到账
            when_ts = _dt.datetime.now().replace(
                hour=10, minute=0, second=0, microsecond=0).timestamp()
        steps["schedule"] = self._schedule(
            amo, "工资到账" if salary else "入账", amount_cents,
            note=str(reason or ""), when_ts=when_ts)
        balance = int(self.balance_cents() or 0)
        if salary and isinstance(detail, dict):
            # 记忆/小日记只留一句人话；金额明细在账本流水里，日记再由 LLM 改写成她的语气
            text = "这个月发工资了，实发 %s 元，明细记在账本里。" % facts.money(int(amount_cents))
        else:
            text = facts.ledger_fact("收入", int(amount_cents), balance,
                                     item=str(reason or "入账"))
        steps["diary"] = self.small_diary(text)
        steps["memory"] = await self._tier("permanent" if salary else "long", text,
                                           idem + ":mem", tag="income")
        return {"ok": bool(steps.get("wallet")), "mood": after, "steps": steps}

    @staticmethod
    def _expense_meta(reason):
        text = str(reason or "")
        table = (("早餐", ("包子", "豆浆", "油条", "早餐"), "餐饮", "早餐"),
                 ("午餐", ("快餐", "面条", "米饭", "午饭", "午餐"), "餐饮", "午餐"),
                 ("晚餐", ("晚饭", "晚餐", "米饭套餐"), "餐饮", "晚餐"),
                 ("夜宵", ("烧烤", "泡面", "炸鸡", "夜宵"), "餐饮", "夜宵"),
                 ("奶茶", ("奶茶",), "餐饮", "奶茶"),
                 ("打车", ("打车", "出租", "滴滴"), "交通", "打车"),
                 ("地铁", ("地铁", "公交", "通勤"), "交通", "公交地铁"),
                 ("衣服", ("衣服", "裙", "裤", "鞋", "外套", "购物"), "购物", "衣物"),
                 ("礼物", ("礼物", "礼盒", "按摩仪", "丝巾"), "人情", "礼物"),
                 ("随礼", ("份子", "随礼", "红包"), "人情", "随礼"),
                 ("保险", ("保险",), "保险", "保费"),
                 ("房租", ("房租", "物业", "水电", "话费", "宽带"), "固定账单", "生活缴费"))
        for _, words, cat, sub in table:
            if any(word in text for word in words):
                item = ""
                m = re.search(r"[：:](.+)$", text)
                if m:
                    item = m.group(1).strip()
                return cat, sub, item or text, text
        return "其他", "日常", text, text

    async def expense(self, amount_cents, reason, idem, necessary=False, quiet=False,
                      category="", sub_category="", item_name="", trigger_reason="", diary=True,
                      release_hold=""):
        """quiet=True：日常逐笔消费——只写小日记，不进长期记忆（日终按分类汇总再记）。

        release_hold：这笔钱冻结过（答应饭局那笔），扣款时原子解冻再扣。
        """
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        before = self.mood()["word"]
        stored_reason = str(reason or "日常")
        try:
            cat, sub, item, trigger = self._expense_meta(reason)
            # 写库只存纯原因/物品名：严禁再拼「餐饮：/日常：/其他：」这类前缀（2026-11-15 用户要求）
            stored_reason = str(item or reason or "日常").strip()
            debit_kwargs = dict(category=category or cat, sub_category=sub_category or sub,
                                item_name=item_name or item, trigger_reason=trigger_reason or trigger)
            if release_hold:
                debit_kwargs["release_hold"] = str(release_hold)
            steps["wallet"] = bool(amo.wallet.debit(
                int(amount_cents), stored_reason, idem, **debit_kwargs))
        except Exception as exc:
            isolated("finance.expense.wallet", exc)
            steps["wallet"] = False
        if not steps["wallet"]:
            return {"ok": False, "reason": "余额不足", "mood": before, "steps": steps}
        try:
            import logging
            logging.getLogger("amo.watch").info(
                "[明细流水] 扣款: %.2f 元 | 物品: %s | 原因: %s | 余额: %.2f",
                int(amount_cents) / 100.0,
                item_name or reason, trigger_reason or reason,
                int(self.balance_cents() or 0) / 100.0)
        except Exception as exc:
            isolated("finance.expense.log", exc)
        self._count("half_expense_cents", int(amount_cents))
        level = expense_level(int(amount_cents), necessary=necessary)
        steps["mood"] = self._nudge(amo, idem, level["deltas"]) if level["deltas"] else True
        after = self.mood()["word"]
        # 小日记/长期记忆只留一句人话：不抄账本格式、不带余额（余额会过期，2026-11-20 用户要求）
        text = facts.spend_line(int(amount_cents), item=stored_reason or reason)
        if necessary:
            steps["schedule"] = self._schedule(amo, "支出", int(amount_cents),
                                               note=str(reason or ""))
        if diary:
            steps["diary"] = self.small_diary(text)
        if not quiet:
            steps["memory"] = await self._tier("long", text, idem + ":mem", tag="expense")
        try:
            from ..services.bill_engine import prompt_line
            if hasattr(amo, "note_expense_summary"):
                amo.note_expense_summary(prompt_line(self))
        except Exception as exc:
            isolated("finance.expense.summary", exc)
        return {"ok": True, "mood": after, "steps": steps}

    async def trade_pnl(self, pnl_cents, ratio, symbol, idem, diary=True, memory=True):
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        level = pnl_level(int(pnl_cents), float(ratio))
        steps["mood"] = self._nudge(amo, idem, level["deltas"]) if level["deltas"] else True
        after = self.mood()["word"]
        text = facts.ledger_fact(
            "交易盈利" if int(pnl_cents) >= 0 else "交易亏损", abs(int(pnl_cents)),
            int(self.balance_cents() or 0),
            item="%s 收益率 %+.2f%%" % (symbol or "交易", float(ratio) * 100))
        if diary:
            steps["diary"] = self.small_diary(text)
        if abs(int(pnl_cents)) >= 100000:
            steps["schedule"] = self._schedule(amo, "大额交易", pnl_cents,
                                               note=str(symbol or ""))
            if diary:
                steps["long_diary"] = self.small_diary(text)
        if memory:
            steps["memory"] = await self._tier("short", text, idem + ":mem",
                                               related=str(symbol or ""), tag="pnl")
        return {"ok": True, "mood": after, "steps": steps}

    async def borrow(self, amount_cents, reason, idem, months=12):
        """银行借款入账：钱进钱包，情绪「有点不好意思」，留记忆/日程/日记。"""
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        try:
            steps["wallet"] = bool(amo.wallet.credit(int(amount_cents), reason, idem))
        except Exception as exc:
            isolated("finance.borrow.wallet", exc)
            steps["wallet"] = False
        steps["mood"] = self._nudge(amo, idem, {"委屈": 2, "焦虑": 3})
        after = self.mood()["word"]
        steps["schedule"] = self._schedule(amo, "银行借款", amount_cents,
                                           note="分 %d 个月还" % months)
        text = facts.ledger_fact("借款", int(amount_cents),
                                 int(self.balance_cents() or 0),
                                 item="分 %d 个月还" % months)
        steps["diary"] = self.small_diary(text)
        steps["memory"] = await self._tier("permanent", text, idem + ":mem")
        return {"ok": bool(steps.get("wallet")), "mood": after, "steps": steps}

    def nudge_mood(self, deltas, idem):
        """按情绪维度加/减心情（涨薪、扣钱用）。"""
        amo = self._amo()
        if amo is None:
            return False
        try:
            return bool(self._nudge(amo, str(idem), deltas))
        except Exception as exc:
            isolated("finance.nudge", exc)
            return False

    async def long_memory(self, text, idem):
        """长期层记忆：日记账按天汇总用（不把每笔都塞进去）。"""
        return await self._tier("long", str(text), str(idem), tag="expense")

    async def permanent_memory(self, text, idem):
        """永久层记忆：买房、还贷这种一辈子的事。"""
        return await self._tier("permanent", str(text), str(idem))

    def small_diary(self, text):
        """小日记：有 LLM 时把真实事件改写成她的话；失败/无模型直接写事实。"""
        amo = self._amo()
        if amo is None:
            return False
        if getattr(amo, "llm", None) is not None:
            try:
                import asyncio
                from ..services.diary_engine import rewrite_and_save
                loop = asyncio.get_running_loop()
                loop.create_task(rewrite_and_save(self, amo, str(text)))
                return True
            except Exception as exc:
                logger.debug("[WATCH] optional step failed: %r", exc)
        return self._small_diary(amo, str(text))

    def add_schedule(self, day, activity, detail="", place="家", category="leave",
                     start="00:00", end="23:59", zone="", status="leave"):
        """往 amo 当天日程里追加一条。

        默认是整天事项（00:00–23:59）；社交邀约要占真实时段，所以支持 14:00–17:00 这种区间。
        zone 只用来给生图/此刻动作判定场景，不写死业务逻辑。
        """
        amo = self._amo()
        if amo is None:
            return False
        try:
            item: dict = {
                "start": str(start or "00:00"), "end": str(end or "23:59"),
                "activity": str(activity), "place": str(place),
                "category": str(category), "detail": str(detail or activity),
                "subject": "bot", "status": str(status or "leave"),
            }
            if zone:
                item["zone"] = str(zone)
            # 同一天同一时段、同一件事只留一条：重复接受同一条邀约不该堆出一排日程
            schedule = getattr(getattr(amo, "state", None), "schedule", {}) or {}
            for row in (schedule.get(str(day)) or []):
                if not isinstance(row, dict):
                    continue
                if (str(row.get("start") or "") == item["start"]
                        and str(row.get("end") or "") == item["end"]
                        and str(row.get("activity") or "") == item["activity"]):
                    return True
            # 只给社交/餐饮/约会这一条打锁，不动当天的其它块（2026-11-20）：
            # 陪伴的整点重算遇到 locked 块会让行。
            if str(category) in ("social", "date", "meal", "ceremony"):
                item["locked"] = True
            result = amo.state.add_schedule_item(str(day), item, source="watch",
                                                 reason=str(activity))
            return bool(isinstance(result, dict) and result.get("changed", True))
        except Exception as exc:
            isolated("finance.schedule", exc)
            return False


    def schedule_windows(self, day):
        """读 amo 当天日程里已有的时间段（社交冲突检测用）。"""
        amo = self._amo()
        if amo is None:
            return []
        try:
            rows = (getattr(amo.state, "schedule", {}) or {}).get(str(day)) or []
        except Exception:
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            start, end = str(row.get("start") or ""), str(row.get("end") or "")
            if not start or not end:
                continue
            out.append({"start": start, "end": end,
                        "activity": str(row.get("activity") or ""),
                        "category": str(row.get("category") or "")})
        return out

    def fatigue(self):
        """疲惫值 0-100：读 amo 心情里的「疲惫」维度，生病/疲惫状态再拉高。"""
        amo = self._amo()
        if amo is None:
            return 0
        value = 0.0
        try:
            value = float((getattr(getattr(amo, "state", None), "mood", {}) or {}).get("疲惫", 0) or 0)
        except Exception:
            value = 0.0
        try:
            health = getattr(getattr(amo, "state", None), "health", {}) or {}
            if str(health.get("status") or "") == "疲惫":
                value = max(value, 60.0)
        except Exception as exc:
            isolated("finance.health_read", exc)
        return max(0, min(100, int(round(value))))

    def set_current_action(self, action, until_ts=0, place="外面", zone="restaurant", category="social"):
        """立刻刷新「此刻在干嘛」（她答应邀约后当天就在约会时用）。"""
        amo = self._amo()
        if amo is None:
            return False
        setter = getattr(getattr(amo, "state", None), "set_current_action", None)
        if not callable(setter):
            return False
        try:
            return bool(setter(str(action), float(until_ts or 0), place=str(place),
                               zone=str(zone or "restaurant"), category=str(category or "social")))
        except Exception as exc:
            isolated("finance.current_action", exc)
            return False

    def long_diary(self, text):
        """长篇日记（amo 只写当天那一篇，一天只写一次）。"""
        amo = self._amo()
        if amo is None:
            return False
        return self._long_diary(amo, str(text))

    async def remember_event(self, kind, amount_cents, text, idem):
        """通用事件记忆（求助、资金紧张等）。"""
        return await self.memory.remember(str(text), kind="finance", idem=str(idem) + ":mem")

    async def low_balance(self, need_cents, idem):
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        before = self.mood()["word"]
        level = low_balance_level(int(need_cents))
        steps = {"mood": self._nudge(amo, idem, level["deltas"])}
        after = self.mood()["word"]
        steps["schedule"] = self._schedule(
            amo, "余额不足", int(need_cents),
            note="（%s→%s）还差 %s" % (before, after, yuan(need_cents)))
        steps["diary"] = self._small_diary(amo, "余额不够了，还差 %s，%s。" % (yuan(need_cents), level["word"]))
        text = "%s 余额不足，还差 %s，%s。" % (time.strftime("%Y-%m-%d %H:%M"), yuan(need_cents), after)
        steps["memory"] = await self._tier("permanent", text, idem + ":mem")
        return {"ok": True, "mood": after, "steps": steps}

    # ---------- 交易 / 收盘：结构化记忆（供交易模块调用） ----------
    async def record_fund_fact(self, action, name, code, amount_cents, shares=0.0, nav=0.0,
                               fee_cents=0, pnl_cents=0, idem="", when=""):
        """只存结构化事实；日记由 LLM 依据事实+情绪+余额自己写，绝不拼模板。"""
        now_ts = time.time()
        local = time.localtime(now_ts)
        day = time.strftime("%Y-%m-%d", local)
        clock = str(when or time.strftime("%H:%M", local))
        fund_name = str(name or code or "")
        idem = str(idem or ("fund_fact:%s:%s:%d" % (action, code, int(now_ts * 1000))))
        fact = {
            "action": str(action),
            "fund_name": fund_name,
            "code": str(code or ""),
            "amount": round(int(amount_cents or 0) / 100.0, 2),
            "amount_cents": int(amount_cents or 0),
            "shares": round(float(shares or 0), 2),
            "nav": round(float(nav or 0), 4),
            "fee": round(int(fee_cents or 0) / 100.0, 2),
            "pnl": round(int(pnl_cents or 0) / 100.0, 2),
            "time": clock,
            "day": day,
        }
        repo = getattr(self.plugin, "finance_repo", None)
        if repo is not None:
            try:
                repo.add_fund_trade_fact(
                    idem, fact["action"], fact["code"], fact["fund_name"],
                    fact["amount_cents"], fact["shares"], fact["nav"],
                    int(fee_cents or 0), int(pnl_cents or 0), day=day, ts=now_ts)
            except Exception as exc:
                isolated("finance.fund_fact.save", exc)
        payload = json.dumps(fact, ensure_ascii=False, sort_keys=True)
        ok = False
        try:
            ok = bool(await self._tier("long", payload, idem + ":mem", tag="fund_trade"))
        except Exception as exc:
            isolated("finance.fund_fact.memory", exc)
        amo = self._amo()
        if amo is not None and getattr(amo, "llm", None) is not None:
            try:
                import asyncio
                from ..services.diary_engine import rewrite_fund_fact
                asyncio.get_running_loop().create_task(rewrite_fund_fact(self, amo, fact))
            except Exception as exc:
                isolated("finance.fund_fact.diary", exc)
        return {"ok": bool(ok), "verified": bool(ok), "fact": fact}

    async def record_fund_outage(self, fact, idem=""):
        """基金估值全挂：只落结构化事实 + JSON 记忆；感受交给 LLM 日记。"""
        fact = dict(fact or {})
        day = str(fact.get("day") or time.strftime("%Y-%m-%d"))
        idem = str(idem or ("fund_outage:%s:%d" % (day, int(time.time() * 1000))))
        # 2026-11-16：基金决策绝不写进社交动态表（skip_fund_trade 曾漏到「最近关系动态」）。
        # 只留短记忆 + 日记，面板不展示任何基金流水。
        payload = json.dumps(fact, ensure_ascii=False, sort_keys=True)
        ok = False
        try:
            ok = bool(await self._tier("short", payload, idem + ":mem", tag="fund_outage"))
        except Exception as exc:
            isolated("finance.fund_outage.memory", exc)
        amo = self._amo()
        if amo is not None and getattr(amo, "llm", None) is not None:
            try:
                import asyncio
                from ..services.diary_engine import rewrite_fund_outage
                asyncio.get_running_loop().create_task(rewrite_fund_outage(self, amo, fact))
            except Exception as exc:
                isolated("finance.fund_outage.diary", exc)
        return {"ok": bool(ok), "fact": fact}

    async def record_trade(self, side, kind, name, code, price, qty, amount_cents, fee_cents=0,
                           pnl_cents=0, pnl_pct=0.0, holding_qty=0, idem=""):
        if str(kind) == "fund":
            return await self.record_fund_fact(
                "buy_fund" if str(side) == "buy" else "sell_fund",
                name, code, amount_cents, shares=qty, nav=price,
                fee_cents=fee_cents, pnl_cents=pnl_cents, idem=idem)
        word = self.mood()["word"]
        when = time.strftime("%Y-%m-%d %H:%M")
        label = "股票" if str(kind) == "stock" else "基金"
        unit = "股" if label == "股票" else "份"
        if str(side) == "buy":
            text = "%s 买入%s %s(%s) %s%s，成交价 %s，成交金额 %s，费用 %s，现在持有 %s%s，%s。" % (
                when, label, name, code, qty, unit, price, yuan(amount_cents), yuan(fee_cents),
                holding_qty, unit, word)
        else:
            text = "%s 卖出%s %s(%s) %s%s，成交价 %s，成交金额 %s，费用 %s，本次实现盈亏 %s（%+.2f%%），卖出后剩余 %s%s，%s。" % (
                when, label, name, code, qty, unit, price, yuan(amount_cents), yuan(fee_cents),
                ("+" if pnl_cents >= 0 else "-") + yuan(abs(int(pnl_cents))), float(pnl_pct) * 100,
                holding_qty, unit, word)
        ok = await self._tier("long", text, idem + ":mem", tag="trade")
        try:
            self.small_diary(text)
        except Exception as exc:
            isolated("finance.trade.diary", exc)
        return {"ok": bool(ok), "verified": bool(ok)}

    async def record_daily_pnl(self, items, total_pnl_cents, idem=""):
        word = self.mood()["word"]
        parts = []
        for item in items or []:
            parts.append("%s(%s) 现价 %s，浮动盈亏 %s（%+.2f%%）" % (
                item.get("name", ""), item.get("code", ""), item.get("price", ""),
                ("+" if item.get("pnl_cents", 0) >= 0 else "-") + yuan(abs(int(item.get("pnl_cents", 0)))),
                float(item.get("pnl_pct", 0.0)) * 100))
        text = "%s 15:00 收盘持仓盈亏：%s；总持仓盈亏 %s，%s。" % (
            time.strftime("%Y-%m-%d"), "；".join(parts) or "无持仓",
            ("+" if total_pnl_cents >= 0 else "-") + yuan(abs(int(total_pnl_cents))), word)
        return await self._tier("short", text, idem + ":mem", related="pnl")
