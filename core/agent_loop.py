# -*- coding: utf-8 -*-
"""自主交易决策：她自己的钱她做主。

- 用户推荐只作为上下文，**不执行**；决策只由 decide() 产生。
- 三个时点：9:30（早盘）、午间（11:35，只复盘不成交）、14:50（收盘前最终决策）。
- 决策理由写记忆 + 小日记；14:50 额外写长日记。
- 15:00 收盘后结算当日盈亏。
"""
from __future__ import annotations

import datetime as _dt
import json
import random

try:
    from ..guard import isolated
    from ..services.market_api import fetch_quotes
    from ..utils.helpers import is_trading_day
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated
    from services.market_api import fetch_quotes
    from utils.helpers import is_trading_day

SLOTS = ((9, 30, "morning"), (11, 35, "noon"), (14, 50, "close"))
CANDIDATES = ("600519", "000001", "300750", "600036")


class FinanceDecision:
    def __init__(self, plugin, runtime, repo, finance, trading, help_=None, pools=None):
        self.plugin = plugin
        self.runtime = runtime
        self.repo = repo
        self.finance = finance
        self.trading = trading
        self.help = help_
        self.pools = pools

    # ---------- 上下文 ----------
    def _positions(self):
        out = []
        for row in self.repo.positions():
            cost = int(row.get("cost_cents") or 0)
            last = int(row.get("last_cents") or cost)
            qty = float(row.get("qty") or 0)
            base = cost * qty
            out.append({
                "kind": row.get("kind"), "code": row.get("code"), "name": row.get("name") or row.get("code"),
                "qty": qty, "cost_cents": cost, "last_cents": last,
                "pnl_pct": ((last - cost) / cost * 100.0) if cost else 0.0,
            })
        return out

    async def _refresh(self, codes):
        quotes = await self.runtime.run_io(fetch_quotes, list(codes), timeout=12) or {}
        for row in self.repo.positions():
            quote = quotes.get(str(row.get("code")))
            if quote and quote.get("price"):
                self.repo.upsert_position(row.get("kind"), row.get("code"), row.get("name"),
                                          row.get("qty"), row.get("cost_cents"),
                                          int(round(float(quote["price"]) * 100)),
                                          row.get("available"))
        return quotes

    # ---------- 决策（不做固定阈值，允许各种风格） ----------
    def decide(self, cash_cents, positions, mood_score, quotes, rng):
        if positions:
            best = max(positions, key=lambda p: p["pnl_pct"])
            worst = min(positions, key=lambda p: p["pnl_pct"])
            if best["pnl_pct"] > 0:
                roll = rng.random()
                if best["pnl_pct"] < 5 and roll < 0.50:
                    return {"action": "sell", "code": best["code"], "qty": int(best["qty"]),
                            "reason": "小赚 %.1f%% 先落袋" % best["pnl_pct"]}
                if best["pnl_pct"] >= 5 and roll < 0.35:
                    return {"action": "sell", "code": best["code"], "qty": int(best["qty"] // 2 * 100),
                            "reason": "大赚 %.1f%%，先卖一半稳稳的" % best["pnl_pct"]}
                return {"action": "hold", "code": best["code"],
                        "reason": "还看好，想再拿拿看（现在 %+.1f%%）" % best["pnl_pct"]}
            if worst["pnl_pct"] <= -8 and (mood_score < 45 or rng.random() < 0.4):
                return {"action": "sell", "code": worst["code"], "qty": int(worst["qty"]),
                        "reason": "亏 %.1f%% 心里难受，割肉换点现金" % worst["pnl_pct"]}
            return {"action": "hold", "code": worst["code"],
                    "reason": "亏 %.1f%% 但看好基本面，继续扛" % worst["pnl_pct"]}
        # 空仓：看行情挑一个买得起的
        afford = []
        for code in CANDIDATES:
            quote = quotes.get(code)
            if not quote or not quote.get("price"):
                continue
            need = int(round(float(quote["price"]) * 100)) * 100
            if need + 600 <= cash_cents and need <= (cash_cents * 0.5):
                afford.append((code, need))
        if afford and rng.random() < 0.6:
            code, _ = rng.choice(afford)
            reason = rng.choice(["空仓观望后想试试水", "看它跌得差不多，抄底试试", "感觉要涨，追一点"])
            return {"action": "buy", "code": code, "qty": 100, "reason": reason}
        return {"action": "hold", "code": "", "reason": "空仓观望，等更好的机会"}

    # ---------- 执行 ----------
    async def tick(self, now=None):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        if not is_trading_day(day):
            return {"skipped": "holiday"}
        done = []
        for hour, minute, slot in SLOTS:
            if now.hour * 60 + now.minute < hour * 60 + minute:
                continue
            flag = "decision_done:%s:%s" % (day, slot)
            if self.repo.meta_get(flag):
                continue
            result = await self.run_slot(slot, now)
            self.repo.meta_set(flag, "1")
            done.append({"slot": slot, "result": result})
        if now.hour * 60 + now.minute >= 15 * 60 + 5:
            await self.close_day(day)
        return {"done": done}

    async def run_slot(self, slot, now=None):
        now = now or _dt.datetime.now()
        positions = self._positions()
        codes = [p["code"] for p in positions]
        if self.pools is not None:
            for code in self.pools.daily_anchors(now.date().isoformat()):
                if code not in codes:
                    codes.append(code)
            for row in self.pools.repo.candidates():
                code = str(row.get("code") or "")
                if code and code not in codes:
                    codes.append(code)
        else:
            codes += list(CANDIDATES)
        day = now.date().isoformat()
        # 数据失败兜底：当天不再自主交易（只记日志，不写记忆/日记）
        if self.repo.meta_get("news_fail:" + day):
            return {"skipped": "data_failed",
                    "decision": {"action": "hold", "reason": "今天行情/新闻拉取失败，先不交易"},
                    "executed": {"ok": False, "message": "数据失败，今天不交易"}}
        quotes = await self._refresh(codes)
        if not quotes:
            self.repo.meta_set("news_fail:" + day, "quotes")
            return {"skipped": "quotes_failed",
                    "decision": {"action": "hold", "reason": "行情拉不到，今天先不动"},
                    "executed": {"ok": False, "message": "行情失败，今天不交易"}}
        if self.pools is not None:
            self.pools.remember_names(quotes)
            observation = self.pools.observe(quotes, now.date().isoformat())
            for code in observation:
                if code not in codes:
                    codes.append(code)
        cash = self.finance.balance_cents() or 0
        mood = self.finance.mood()
        decision = None
        try:
            from .llm_decider import llm_decide
        except ImportError:  # 直接按文件加载时
            from core.llm_decider import llm_decide
        news_text = ""
        self._news_used = False
        try:
            fetched = int(self.repo.meta_get("news_fetch:" + day, 0) or 0)
        except Exception:
            fetched = 0
        if fetched >= 3:      # 每天最多查三次（9:30 / 11:35 / 14:50）
            return {"skipped": "news_quota",
                    "decision": {"action": "hold", "reason": "今天新闻额度用完了"},
                    "executed": {"ok": False, "message": "不重复查新闻"}}
        try:
            from ..services.news_api import fetch_cls, fetch_eastmoney, merge_stock_news
        except ImportError:  # 直接按文件加载时
            from services.news_api import fetch_cls, fetch_eastmoney, merge_stock_news
        try:
            market = await self.runtime.run_io(fetch_eastmoney, "财经", "global", 5, timeout=10) or []
            if not market:
                market = await self.runtime.run_io(fetch_cls, 6, timeout=10) or []
            parts = []
            if market:
                parts.append("市场：" + "；".join(str(x.get("title") or "")[:40] for x in market[:5]))
            for p in positions[:5]:
                items = await self.runtime.run_io(fetch_eastmoney, p["code"], "stock", 3, timeout=10) or []
                merged = merge_stock_news(p["code"], items, now.date().isoformat())
                if merged:
                    parts.append(merged)
                    tm = getattr(self.plugin, "finance_memory", None)
                    if tm is not None:
                        await tm.remember("news", merged,
                                          "news:%s:%s" % (now.date().isoformat(), p["code"]),
                                          related=p["code"])
            if not parts:
                raise RuntimeError("news empty")
            news_text = "\n".join(parts)
            self._news_used = bool(news_text)
            self.repo.meta_set("news_fetch:" + day, fetched + 1)
        except Exception as exc:
            isolated("finance.news.inject", exc)
        if self.plugin is not None:
            allowed = {p["code"] for p in positions}
            if self.pools is not None:
                allowed |= set(self.pools.affordable(quotes, cash))
            memory_text = ""
            tm = getattr(self.plugin, "finance_memory", None)
            if tm is not None:
                memory_text = tm.context_text([p["code"] for p in positions] + list(allowed), limit=18)
            decision = await llm_decide(self.plugin, cash, positions, quotes, mood.get("word"), allowed,
                                        news_text, memory_text)
        if not decision:
            decision = self.decide(cash, positions, float(mood.get("score") or 50), quotes,
                                   random.Random(now.strftime("%Y-%m-%d") + ":" + slot))
        executed = {"ok": False, "message": "只复盘，不成交"}
        if slot != "noon":
            executed = await self._execute(slot, decision, now)
        await self._record(slot, decision, executed, now)
        if self.help is not None:
            state = self.help.status()
            if state["low"]:
                await self._low_fund(state, now)
        return {"decision": decision, "executed": executed}

    async def _execute(self, slot, decision, now):
        idem = "decision:%s:%s" % (now.date().isoformat(), slot)
        if decision["action"] == "buy" and decision.get("code"):
            return await self.trading.buy(decision["code"], decision.get("qty", 100), None, idem + ":buy")
        if decision["action"] == "sell" and decision.get("code") and decision.get("qty"):
            return await self.trading.sell(decision["code"], decision["qty"], None, idem + ":sell")
        return {"ok": False, "message": "决定先不动"}

    async def _record(self, slot, decision, executed, now):
        mood = self.finance.mood()
        when = now.strftime("%Y-%m-%d %H:%M")
        line = "%s 决策：%s" % (when, decision["reason"])
        if executed.get("ok"):
            line += "；%s" % executed.get("message", "")
        try:
            self.repo.meta_set("last_decision", json.dumps({
                "slot": slot, "action": decision.get("action"), "code": decision.get("code"),
                "reason": decision.get("reason"), "at": now.strftime("%m-%d %H:%M"),
                "executed": bool(executed.get("ok")),
            }, ensure_ascii=False))
        except Exception as exc:
            isolated("finance.decision.meta", exc)
        await self.finance.memory.remember(line, kind="finance_decision",
                                           idem="decision:%s:%s:mem" % (now.date().isoformat(), slot))
        try:
            amo = self.finance._amo()
            if amo is not None:
                extra = ""
                if getattr(self, "_news_used", False):
                    extra = "看了新闻，" + ("参考着做了决定。" if executed.get("ok") else "没太看懂，没动。")
                amo.db.save_diary(now.date().isoformat(), "%02d" % now.hour,
                                  "今天%s，%s。%s" % (decision["reason"], mood["word"], extra))
        except Exception as exc:
            isolated("finance.decision.diary", exc)

    async def _low_fund(self, state, now):
        day = now.date().isoformat()
        if self.repo.meta_get("low_fund_alert") == day:
            return
        self.repo.meta_set("low_fund_alert", day)
        text = self.help.prompt_text()
        await self.finance.remember_event("资金紧张", state["cash_cents"], text, "lowfund:" + day)
        try:
            amo = self.finance._amo()
            if amo is not None:
                amo.state.add_schedule_item(day, {
                    "start": now.strftime("%H:%M"),
                    "end": now.strftime("%H:%M"),
                    "activity": "资金紧张", "place": "家", "category": "finance",
                    "detail": text, "event": "情绪有点焦虑", "subject": "bot",
                }, source="finance", reason="low_fund")
        except Exception as exc:
            isolated("finance.decision.lowfund", exc)

    # ---------- 收盘 ----------
    async def close_day(self, day):
        flag = "close_done:" + day
        if self.repo.meta_get(flag):
            return {"skipped": "done"}
        positions = self._positions()
        if positions:
            await self._refresh([p["code"] for p in positions])
        items = []
        total = 0
        for row in self.repo.positions():
            cost = int(row.get("cost_cents") or 0)
            last = int(row.get("last_cents") or cost)
            qty = float(row.get("qty") or 0)
            pnl = int(round((last - cost) * qty))
            total += pnl
            base = cost * qty
            items.append({"name": row.get("name") or row.get("code"), "code": row.get("code"),
                          "price": "%.2f" % (last / 100.0), "pnl_cents": pnl,
                          "pnl_pct": (pnl / base) if base else 0.0})
        await self.finance.record_daily_pnl(items, total, idem="close:" + day)
        self.repo.meta_set(flag, "1")
        return {"ok": True, "total_pnl": total / 100.0, "items": len(items)}
