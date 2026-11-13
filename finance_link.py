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

import logging
import time

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

try:
    from .finance_memory import MemoryLink
    from .finance_mood import expense_level, income_level, low_balance_level, pnl_level, word_for_score, yuan
    from .guard import isolated
except ImportError:  # 直接按文件加载时
    from finance_memory import MemoryLink
    from finance_mood import expense_level, income_level, low_balance_level, pnl_level, word_for_score, yuan
    from guard import isolated

AMO_PLUGIN_NAME = "Astrbot_amoupup_forever"


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
        except Exception:
            pass

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

    def _schedule(self, amo, kind, amount_cents, before, after, note=""):
        try:
            now = time.localtime()
            start = "%02d:%02d" % (now.tm_hour, now.tm_min)
            end_min = (now.tm_hour * 60 + now.tm_min + 10) % (24 * 60)
            item = {
                "start": start,
                "end": "%02d:%02d" % (end_min // 60, end_min % 60),
                "activity": "财务·" + str(kind),
                "place": "家",
                "category": "finance",
                "detail": ("%s %s" % (kind, yuan(amount_cents))) + (("，" + note) if note else ""),
                "event": "情绪由" + str(before) + "变为" + str(after),
                "subject": "bot",
            }
            day = time.strftime("%Y-%m-%d")
            result = amo.state.add_schedule_item(day, item, source="finance", reason=kind)
            return bool(isinstance(result, dict) and result.get("changed"))
        except Exception as exc:
            isolated("finance.schedule", exc)
            return False

    def _small_diary(self, amo, text):
        try:
            amo.db.save_diary(time.strftime("%Y-%m-%d"), time.strftime("%H"), str(text))
            return True
        except Exception as exc:
            isolated("finance.small_diary", exc)
            return False

    def _long_diary(self, amo, text):
        try:
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
    async def income(self, amount_cents, reason, idem, salary=False):
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        before = self.mood()["word"]
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
        steps["schedule"] = self._schedule(amo, "工资到账" if salary else "入账", amount_cents, before, after, reason)
        steps["diary"] = self._small_diary(amo, "今天%s，%s。" % (
            ("%s到账" % reason) if reason else "有一笔入账", level["word"] or "挺开心"))
        when = time.strftime("%Y-%m-%d %H:%M")
        if salary:
            text = "%s 发工资 %s，发放后可用余额 %s，%s。" % (when, yuan(amount_cents), self._balance_text(amo), after)
        else:
            text = "%s 收到%s %s，入账后可用余额 %s，%s。" % (
                when, reason or "一笔钱", yuan(amount_cents), self._balance_text(amo), after)
        steps["memory"] = await self._tier("permanent" if salary else "long", text, idem + ":mem", tag="income")
        if int(amount_cents) >= 500000:
            steps["long_diary"] = self._long_diary(
                amo, "今天%s，一共 %s。我从%s变成%s，心里踏实了不少。" % (
                    reason or "收到一笔钱", yuan(amount_cents), before, after))
        return {"ok": bool(steps.get("wallet")), "mood": after, "steps": steps}

    async def expense(self, amount_cents, reason, idem, necessary=False):
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        before = self.mood()["word"]
        try:
            steps["wallet"] = bool(amo.wallet.debit(int(amount_cents), reason, idem))
        except Exception as exc:
            isolated("finance.expense.wallet", exc)
            steps["wallet"] = False
        if not steps["wallet"]:
            return {"ok": False, "reason": "余额不足", "mood": before, "steps": steps}
        self._count("half_expense_cents", int(amount_cents))
        level = expense_level(int(amount_cents), necessary=necessary)
        steps["mood"] = self._nudge(amo, idem, level["deltas"]) if level["deltas"] else True
        after = self.mood()["word"]
        if necessary:
            steps["schedule"] = self._schedule(amo, "支出", amount_cents, before, after, reason)
        steps["diary"] = self._small_diary(amo, "今天%s，花了 %s。%s" % (
            reason or "有笔开销", yuan(amount_cents), level["word"] or "还行"))
        when = time.strftime("%Y-%m-%d %H:%M")
        text = "%s %s 花了 %s，扣款后余额 %s，%s。" % (
            when, reason or "一笔开销", yuan(amount_cents), self._balance_text(amo), after)
        steps["memory"] = await self._tier("long", text, idem + ":mem", tag="expense")
        return {"ok": True, "mood": after, "steps": steps}

    async def trade_pnl(self, pnl_cents, ratio, symbol, idem):
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        before = self.mood()["word"]
        level = pnl_level(int(pnl_cents), float(ratio))
        steps["mood"] = self._nudge(amo, idem, level["deltas"]) if level["deltas"] else True
        after = self.mood()["word"]
        if abs(int(pnl_cents)) >= 100000:
            steps["schedule"] = self._schedule(amo, "大额交易", pnl_cents, before, after, symbol)
        steps["diary"] = self._small_diary(amo, "今天%s %s，%s。" % (
            symbol or "交易",
            ("赚了 " + yuan(pnl_cents)) if pnl_cents >= 0 else ("亏了 " + yuan(-pnl_cents)),
            level["word"] or "没什么波动"))
        when = time.strftime("%Y-%m-%d %H:%M")
        text = "%s %s %s %s（%+.2f%%），%s。" % (
            when, symbol or "交易", "实现盈利" if pnl_cents >= 0 else "实现亏损",
            yuan(abs(int(pnl_cents))), float(ratio) * 100, after)
        steps["memory"] = await self._tier("short", text, idem + ":mem", related=str(symbol or ""), tag="pnl")
        if abs(int(pnl_cents)) >= 100000:
            steps["long_diary"] = self._long_diary(
                amo, "今天 %s %s %s，从%s变成%s。" % (
                    symbol or "这只票", "赚了" if pnl_cents >= 0 else "亏了",
                    yuan(abs(int(pnl_cents))), before, after))
        return {"ok": True, "mood": after, "steps": steps}

    async def borrow(self, amount_cents, reason, idem, months=12):
        """银行借款入账：钱进钱包，情绪「有点不好意思」，留记忆/日程/日记。"""
        amo = self._amo()
        if amo is None:
            return {"ok": False, "reason": "amo 未就绪", "steps": {}}
        steps = {}
        before = self.mood()["word"]
        try:
            steps["wallet"] = bool(amo.wallet.credit(int(amount_cents), reason, idem))
        except Exception as exc:
            isolated("finance.borrow.wallet", exc)
            steps["wallet"] = False
        steps["mood"] = self._nudge(amo, idem, {"委屈": 2, "焦虑": 3})
        after = self.mood()["word"]
        steps["schedule"] = self._schedule(amo, "银行借款", amount_cents, before, after, "分 %d 个月还" % months)
        steps["diary"] = self._small_diary(amo, "向银行借了 %s 周转，%s。" % (yuan(amount_cents), after))
        text = "%s 向银行借了 %s 周转，分 %d 个月还，%s。" % (
            time.strftime("%Y-%m-%d %H:%M"), yuan(amount_cents), months, after)
        steps["memory"] = await self._tier("permanent", text, idem + ":mem")
        return {"ok": bool(steps.get("wallet")), "mood": after, "steps": steps}

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
        steps["schedule"] = self._schedule(amo, "余额不足", int(need_cents), before, after, "还差 " + yuan(need_cents))
        steps["diary"] = self._small_diary(amo, "余额不够了，还差 %s，%s。" % (yuan(need_cents), level["word"]))
        text = "%s 余额不足，还差 %s，%s。" % (time.strftime("%Y-%m-%d %H:%M"), yuan(need_cents), after)
        steps["memory"] = await self._tier("permanent", text, idem + ":mem")
        return {"ok": True, "mood": after, "steps": steps}

    # ---------- 交易 / 收盘：结构化记忆（供交易模块调用） ----------
    async def record_trade(self, side, kind, name, code, price, qty, amount_cents, fee_cents=0,
                           pnl_cents=0, pnl_pct=0.0, holding_qty=0, idem=""):
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
