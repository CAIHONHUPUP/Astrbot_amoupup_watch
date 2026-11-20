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
    from ..services.fund_market import FundMarket
    from ..services.market_api import fetch_quotes
    from ..utils.helpers import is_trading_day
    from ..utils.clock import today_str as _today_str
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated
    from services.fund_market import FundMarket
    from services.market_api import fetch_quotes
    from utils.helpers import is_trading_day
    from utils.clock import today_str as _today_str

SLOTS = ((9, 30, "morning"), (11, 35, "noon"), (14, 50, "close"))
CANDIDATES = ("600519", "000001", "300750", "600036")
#: 午间只复盘：可执行的买卖挂到 14:50，只认当天
PENDING_KEY = "pending_order"
TRADE_ACTIONS = ("buy", "sell", "buy_fund", "sell_fund")


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
            out.append({
                "kind": row.get("kind"), "code": row.get("code"), "name": row.get("name") or row.get("code"),
                "qty": qty, "cost_cents": cost, "last_cents": last,
                "pnl_pct": ((last - cost) / cost * 100.0) if cost else 0.0,
            })
        return out

    async def _refresh(self, codes):
        quotes = await self.runtime.run_io(fetch_quotes, list(codes), timeout=12) or {}
        # 收盘价快照（每天一次）：赌徒补仓要看“连跌 3 天”
        try:
            today = _today_str()
            for code, quote in (quotes or {}).items():
                price = quote.get("price") if isinstance(quote, dict) else None
                if not price:
                    continue
                try:
                    self.repo.cache_quote(today, code, int(round(float(price) * 100)),
                                          int(round(float(quote.get("prev_close") or price) * 100)),
                                          str(quote.get("name") or ""))
                except Exception as exc:
                    isolated("finance.quote.cache", exc)
                key = "hist_day:%s:%s" % (code, today)
                if self.repo.meta_get(key):
                    continue
                self.repo.meta_set(key, "1")
                try:
                    hist = json.loads(self.repo.meta_get("hist:" + code) or "[]")
                except Exception:
                    hist = []
                hist.append(round(float(price), 3))
                self.repo.meta_set("hist:" + code, json.dumps(hist[-5:]))
        except Exception as exc:
            isolated("finance.hist", exc)
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
                    return {"action": "sell", "code": best["code"], "qty": max(100, (int(best["qty"]) // 200) * 100),
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
        positions, codes = self._slot_candidates(now)
        day = now.date().isoformat()
        cash = self.finance.balance_cents() or 0
        fund_text = ""
        self._fund_codes = set()
        self._fund_quotes = {}
        fund_quotes = {}
        if slot == "close":
            try:
                market = FundMarket(self.plugin, self.runtime, self.repo)
                await market.refresh(now)
                fund_quotes = market.latest(day)
                self._fund_quotes = fund_quotes
                self._fund_codes = {str(code) for code, quote in fund_quotes.items()
                                    if (quote or {}).get("tradeable")}
                fund_text = market.facts(day, cash, fund_quotes)
            except Exception as exc:
                isolated("finance.fund.facts", exc)
            if not self._fund_codes:
                return await self._slot_outage(now, day, cash, fund_quotes)
        quotes, failure = await self._slot_quotes(codes, now, day)
        if failure is not None:
            return failure
        mood = self.finance.mood()
        try:
            gamble = getattr(self.plugin, "gamble", None)
            if gamble is not None:
                await gamble.maybe_gamble(quotes, positions, mood.get("word"), now)
        except Exception as exc:
            isolated("finance.gamble", exc)
        try:
            from .llm_decider import llm_decide
        except ImportError:  # 直接按文件加载时
            from core.llm_decider import llm_decide
        news_text, failure = await self._slot_news(positions, now, day)
        if failure is not None:
            return failure
        decision = await self._slot_decision(now, cash, positions, quotes, mood, news_text, llm_decide, fund_text=fund_text)
        if not decision:
            decision = self.decide(cash, positions, float(mood.get("score") or 50), quotes,
                                   random.Random(now.strftime("%Y-%m-%d") + ":" + slot))
        decision = self._idle_nudge(decision, positions, quotes, cash, slot)
        decision = self._fund_nudge(decision, slot, cash)
        executed = await self._slot_settle(slot, decision, now)
        self._update_hold_streak(decision, positions, executed)
        if self.help is not None:
            state = self.help.status()
            if state["low"]:
                await self._low_fund(state, now)
        return {"decision": decision, "executed": executed}

    async def _slot_outage(self, now, day, cash, fund_quotes, demo=False):
        """估值全挂：只抛事实，让她自己决定抱怨/放弃/挪钱；不拼任何情绪模板。"""
        try:
            from .llm_decider import llm_decide_outage
        except ImportError:
            from core.llm_decider import llm_decide_outage
        try:
            from .fund_actions import (reallocate_funds_action, send_message_to_user_action,
                                       skip_fund_trade_action)
        except ImportError:
            from core.fund_actions import (reallocate_funds_action, send_message_to_user_action,
                                           skip_fund_trade_action)
        names = []
        for code, quote in sorted((fund_quotes or {}).items()):
            quote = quote or {}
            names.append("%s %s（最近净值 %.4f）" % (
                code, quote.get("name") or "", float(quote.get("nav") or 0)))
        facts = ("【突发状况】现在是14:50，你本想看看基金。但是基金软件崩溃了/断网了，"
                 "你刷新了好几次都看不到估值。你当前余额是 %.2f 元。"
                 % (int(cash or 0) / 100.0))
        if names:
            facts += "\n候选基金：" + "、".join(names[:10])
        if demo:
            facts += "\n（测试模式：不要真的挪钱；请用 send_message_to_user 把你的第一反应发给他。）"
        mood = self.finance.mood()
        memory_text = ""
        tm = getattr(self.plugin, "finance_memory", None)
        if tm is not None:
            try:
                memory_text = tm.context_text([], limit=8)
            except Exception as exc:
                isolated("finance.fund_outage.memory", exc)
        try:
            decision = await llm_decide_outage(self.plugin, facts, mood.get("word"), memory_text)
        except Exception as exc:
            isolated("finance.fund_outage.decide", exc)
            decision = None
        executed = {"ok": False, "action": "", "message": "no_action"}
        if decision:
            action = decision.get("action")
            try:
                if action == "send_message_to_user":
                    executed = await send_message_to_user_action(self.plugin, decision.get("text"))
                elif action == "reallocate_funds":
                    if demo:
                        executed = {"ok": False, "action": "reallocate_funds",
                                    "message": "demo_no_reallocate"}
                    else:
                        executed = await reallocate_funds_action(
                            self.plugin, decision.get("target"),
                            int(decision.get("amount_cents") or 0),
                            int(decision.get("term_months") or 0), decision.get("note"))
                elif action == "skip_fund_trade":
                    executed = await skip_fund_trade_action(self.plugin, decision.get("reason"))
                    # 放弃了也要说人话：像真人一样在聊天里抱怨一句，而不是在面板上冒代码名
                    remark = str(decision.get("text") or "").strip()
                    if remark:
                        try:
                            said = await send_message_to_user_action(self.plugin, remark)
                            if said.get("ok"):
                                executed = dict(executed, text=remark, notified=True)
                        except Exception as exc:
                            isolated("finance.fund_outage.skip_notify", exc)
            except Exception as exc:
                isolated("finance.fund_outage.action", exc)
                executed = {"ok": False, "action": action, "message": "action_failed"}
        fact = {
            "day": day, "time": now.strftime("%H:%M"), "reason": "fund_quote_unavailable",
            "balance_cents": int(cash or 0), "fund_count": len(fund_quotes or {}),
            "facts": facts,
            "action": (decision or {}).get("action", ""),
            "target": (decision or {}).get("target", ""),
            "amount_cents": int((decision or {}).get("amount_cents") or 0),
            "note": ((decision or {}).get("note") or (decision or {}).get("text")
                     or (decision or {}).get("reason") or ""),
            "executed": bool(executed.get("ok")),
        }
        try:
            outage_idem = "decision:%s:close:outage:%s" % (day, fact["action"] or "none")
            await self.finance.record_fund_outage(fact, idem=outage_idem)
        except Exception as exc:
            isolated("finance.fund_outage.record", exc)
        try:
            self.repo.meta_set("last_decision", json.dumps({
                "slot": "close", "action": "fund_outage", "code": "",
                "reason": fact["note"] or "fund_quote_unavailable",
                "at": now.strftime("%m-%d %H:%M"), "executed": bool(executed.get("ok")),
                "outage_action": fact["action"],
            }, ensure_ascii=False))
        except Exception as exc:
            isolated("finance.fund_outage.meta", exc)
        return {"skipped": "fund_outage", "facts": facts,
                "decision": decision, "executed": executed}

    async def simulate_fund_outage(self, now=None):
        """调试用：模拟估值全挂，demo 模式不真的挪钱，只让她发消息/放弃。"""
        now = now or _dt.datetime.now()
        return await self._slot_outage(
            now, now.date().isoformat(), self.finance.balance_cents() or 0, {}, demo=True)

    def _slot_candidates(self, now):
        """候选代码：现有持仓 + 每日锚点 + 待观察候选。"""
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
        return positions, codes

    async def _slot_quotes(self, codes, now, day):
        """数据兜底与行情：数据失败当天不出手，否则刷新行情并补齐候选。"""
        # 数据失败兜底：当天不再自主交易（只记日志，不写记忆/日记）
        if self.repo.meta_get("news_fail:" + day):
            return None, {"skipped": "data_failed",
                          "decision": {"action": "hold", "reason": "今天行情/新闻拉取失败，先不交易"},
                          "executed": {"ok": False, "message": "数据失败，今天不交易"}}
        quotes = await self._refresh(codes)
        if not quotes:
            self.repo.meta_set("news_fail:" + day, "quotes")
            return None, {"skipped": "quotes_failed",
                          "decision": {"action": "hold", "reason": "行情拉不到，今天先不动"},
                          "executed": {"ok": False, "message": "行情失败，今天不交易"}}
        if self.pools is not None:
            self.pools.remember_names(quotes)
            observation = self.pools.observe(quotes, now.date().isoformat())
            for code in observation:
                if code not in codes:
                    codes.append(code)
        return quotes, None

    async def _slot_news(self, positions, now, day):
        """新闻注入：每天最多查三次（9:30 / 11:35 / 14:50），拉市场与持仓新闻。"""
        news_text = ""
        self._news_used = False
        try:
            fetched = int(self.repo.meta_get("news_fetch:" + day, 0) or 0)
        except Exception:
            fetched = 0
        if fetched >= 3:      # 每天最多查三次（9:30 / 11:35 / 14:50）
            return None, {"skipped": "news_quota",
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
        return news_text, None

    async def _slot_decision(self, now, cash, positions, quotes, mood, news_text, llm_decide, fund_text=""):
        """决策输入：可用标的、相关记忆、银行/保险提示，交给 llm_decide。"""
        decision = None
        if self.plugin is not None:
            allowed = {p["code"] for p in positions}
            if self.pools is not None:
                allowed |= set(self.pools.affordable(quotes, cash))
            memory_text = ""
            tm = getattr(self.plugin, "finance_memory", None)
            if tm is not None:
                memory_text = tm.context_text([p["code"] for p in positions] + list(allowed), limit=18)
            bank_line = self._slot_bank_line(now)
            allowed |= set(getattr(self, "_fund_codes", set()) or set())
            decision = await llm_decide(self.plugin, cash, positions, quotes, mood.get("word"), allowed,
                                        news_text, (memory_text + "\n" + bank_line).strip(),
                                        fund_text=fund_text)
        return decision

    def _slot_bank_line(self, now):
        """决策输入：银行利率提示 + 保险建议行。"""
        bank_line = ""
        try:
            bank = getattr(self.plugin, "bank", None)
            if bank is None:
                from ..services.bank_engine import build_bank
                bank = build_bank(self.plugin)
                self.plugin.bank = bank
            if bank is not None:
                bank_line = bank.prompt_line()
                ins = getattr(self.plugin, "insurance", None)
                if ins is not None:
                    bank_line += chr(10) + ins.prompt_line()
                    hint = ""
                    try:
                        hint = self._slot_insurance_hint(ins, now)
                    except Exception as exc2:
                        isolated('finance.insurance.hint', exc2)
                    if hint:
                        bank_line += chr(10) + hint
        except Exception as exc:
            isolated("finance.bank.prompt", exc)
        return bank_line

    def _slot_insurance_hint(self, ins, now):
        """保险建议：结合健康值、当日新闻与近期资产事件给出风险提示。"""
        repo = self.repo
        idle = int(self.finance.balance_cents() or 0)
        has_car = bool(repo.assets_of('car')) if hasattr(repo, 'assets_of') else False
        has_house = bool(repo.assets_of('house')) if hasattr(repo, 'assets_of') else False
        health = 80
        try:
            med = getattr(self.plugin, "medical", None)
            if med is not None:
                snap = med.health_snapshot()
                health = (60 if snap["severity"] >= 2 else 85)
        except Exception as exc3:
            isolated("insurance.health", exc3)
        news = str(repo.meta_get("news_text_today") or "")
        asset = str(repo.meta_get("new_asset_event") or "")
        if asset:
            repo.meta_set("new_asset_event", "")
        hint = ins.decision_hint(health=health, news=news, idle_cents=idle, has_car=has_car, has_house=has_house)
        extra = []
        if health < 60:
            extra.append("当前健康值：%d（偏低）" % health)
        for w in ("猝死", "重疾", "流感", "交通事故", "火灾", "漏水"):
            if w in news:
                extra.append("近期新闻包含「%s」" % w)
                break
        if asset == "car":
            extra.append("你昨天刚买了新车，建议买交强险/商业车险")
        if asset == "house":
            extra.append("你昨天刚买了新房，建议买家财险")
        if extra:
            hint = "【今日风险提示】" + "；".join(extra) + ("。" + hint.replace("【保险购买建议】", "") if hint else "")
        if hint and str(repo.meta_get("last_insurance_check_date") or "") == now.date().isoformat():
            hint = ""
        repo.meta_set("last_insurance_check_date", now.date().isoformat())
        return hint

    def _fund_nudge(self, decision, slot, cash):
        """收盘时如果基金池有可交易标的、还没有基金持仓，先小额试一笔。"""
        if slot != "close" or decision.get("action") != "hold":
            return decision
        try:
            if self.repo.positions("fund"):
                return decision
        except Exception:
            return decision
        codes = [code for code in (getattr(self, "_fund_codes", set()) or set())
                 if ((getattr(self, "_fund_quotes", {}) or {}).get(code) or {}).get("tradeable")]
        if not codes or int(cash or 0) < 50000:
            return decision
        return {"action": "buy_fund", "code": sorted(codes)[0], "amount_cents": 50000,
                "reason": "基金池还没碰过，先小额试一笔"}

    def _fund_entry_plan(self, decision, slot):
        """收盘补一笔 500 元基金首次申购（只要还没有基金持仓；与股票是否成交无关）。"""
        if slot != "close":
            return None
        if decision.get("action") in ("buy_fund", "sell_fund", "fund_outage"):
            return None
        try:
            cash = int(self.finance.balance_cents() or 0)
        except Exception as exc:
            isolated("finance.fund.entry.cash", exc)
            return None
        plan = self._fund_nudge({"action": "hold"}, "close", cash)
        return plan if plan.get("action") == "buy_fund" else None

    async def _record_fund_entry(self, plan, executed, now):
        """基金 500 兜底是收盘的第二笔；单独记进 last_decision.fund_entry，不覆盖股票决策。"""
        try:
            payload = json.loads(self.repo.meta_get("last_decision") or "{}")
        except (TypeError, ValueError):
            payload = {}
        payload["fund_entry"] = {
            "code": plan.get("code"), "amount_cents": int(plan.get("amount_cents") or 0),
            "executed": bool(executed.get("ok")),
        }
        if executed.get("message"):
            payload["fund_entry"]["message"] = str(executed.get("message"))
        try:
            self.repo.meta_set("last_decision", json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            isolated("finance.fund.entry.meta", exc)

    def _idle_nudge(self, decision, positions, quotes, cash, slot):
        """空仓连续 2 次 hold 后，下一笔优先小仓位试买，避免永远空仓。"""
        if slot == "noon" or positions or decision.get("action") != "hold":
            return decision
        try:
            streak = int(self.repo.meta_get("hold_streak", 0) or 0)
        except Exception:
            return decision
        if streak < 2 or self.pools is None:
            return decision
        try:
            codes = sorted(self.pools.affordable(quotes, cash) or [])
        except Exception:
            return decision
        if not codes:
            return decision
        return {"action": "buy", "code": codes[0], "qty": 100,
                "reason": "空仓观望好几天了，先小仓位试一笔"}

    # ---------- 午间挂单：11:35 只复盘，买卖排到 14:50 ----------
    def _is_trade(self, decision):
        """带代码、数量合法的买卖才算可挂单；hold / 存款 / 发消息都不挂。"""
        if not isinstance(decision, dict):
            return False
        action = str(decision.get("action") or "")
        if action not in TRADE_ACTIONS or not str(decision.get("code") or "").strip():
            return False
        try:
            if action in ("buy", "sell"):
                return int(float(decision.get("qty") or 0)) > 0
            if action == "buy_fund":
                return int(decision.get("amount_cents") or 0) > 0
            return float(decision.get("shares") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _queue_pending(self, decision, now):
        """午间把可执行决定写成当天挂单；午间不是买卖就清掉旧挂单。"""
        try:
            if not self._is_trade(decision):
                self.repo.meta_set(PENDING_KEY, "")
                return None
            plan = {
                "day": now.date().isoformat(), "slot": "noon",
                "queued_at": now.strftime("%H:%M"),
                "action": str(decision.get("action")),
                "code": str(decision.get("code")),
                "qty": int(float(decision.get("qty") or 0)),
                "amount_cents": int(decision.get("amount_cents") or 0),
                "shares": float(decision.get("shares") or 0),
                "reason": str(decision.get("reason") or ""),
            }
            self.repo.meta_set(PENDING_KEY, json.dumps(plan, ensure_ascii=False))
            return plan
        except Exception as exc:
            isolated("finance.pending.queue", exc)
            return None

    def _take_pending(self, now):
        """取出当天挂单；隔天、损坏、非买卖一律清掉，绝不隔日补成交。"""
        try:
            raw = self.repo.meta_get(PENDING_KEY) or ""
        except Exception as exc:
            isolated("finance.pending.read", exc)
            return None
        if not raw:
            return None
        try:
            self.repo.meta_set(PENDING_KEY, "")
        except Exception as exc:
            isolated("finance.pending.clear", exc)
        try:
            plan = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(plan, dict):
            return None
        if str(plan.get("day") or "") != now.date().isoformat():
            return None
        return plan if self._is_trade(plan) else None

    def _update_hold_streak(self, decision, positions, executed):
        """空仓连续 hold 才累计；挂单成交也算「动过」，要清零。"""
        try:
            held = (decision.get("action") == "hold" and not positions
                    and not executed.get("ok"))
            streak = int(self.repo.meta_get("hold_streak", 0) or 0)
            self.repo.meta_set("hold_streak", str(streak + 1) if held else "0")
        except Exception as exc:
            isolated("finance.hold_streak", exc)

    async def _slot_settle(self, slot, decision, now):
        """执行记录：午间只复盘并挂单；收盘先做自己的决定，hold 时补挂单。"""
        if slot == "noon":
            executed = {"ok": False, "message": "只复盘，不成交"}
            queued = self._queue_pending(decision, now)
            await self._record(slot, decision, executed, now, pending=bool(queued))
            return executed
        executed = await self._execute(slot, decision, now)
        from_pending = False
        if slot == "close":
            plan = self._take_pending(now)
            if plan is not None and not self._is_trade(decision):
                decision = dict(plan)
                executed = await self._execute(slot, decision, now)
                from_pending = True
        await self._record(slot, decision, executed, now, from_pending=from_pending)
        if slot == "close":
            fund_plan = self._fund_entry_plan(decision, slot)
            if fund_plan is not None:
                fund_exec = await self._execute(slot, fund_plan, now)
                await self._record_fund_entry(fund_plan, fund_exec, now)
        return executed

    async def _execute(self, slot, decision, now):
        idem = "decision:%s:%s" % (now.date().isoformat(), slot)
        if decision.get("action") == "deposit":
            bank = getattr(self.plugin, "bank", None)
            if bank is None and callable(getattr(self.plugin, "_bank", None)):
                bank = await self.plugin._bank()
            if bank is None:
                try:
                    from ..services.bank_engine import build_bank
                except ImportError:
                    from services.bank_engine import build_bank
                bank = build_bank(self.plugin)
            self.plugin.bank = bank
            amount = int(decision.get("amount_cents") or 0)
            term = int(decision.get("term_months") or 0)
            out = await bank.deposit(amount, term)
            if out.get("ok"):
                label = "活期" if not term else "定期%d个月" % term
                return {"ok": True, "message": "存了 %.2f 元%s" % (amount / 100.0, label)}
            return {"ok": False, "message": "存款没成功（%s）" % (out.get("reason") or "unknown")}
        if decision["action"] == "buy_fund" and decision.get("code"):
            code = str(decision["code"])
            quote = (getattr(self, "_fund_quotes", {}) or {}).get(code) or {}
            if not quote.get("tradeable"):
                return {"ok": False, "message": "今天这只基金没有可用估值，跳过交易"}
            nav = float(quote.get("estimate") or 0)
            name = str(quote.get("name") or "")
            try:
                return await self.trading.subscribe(
                    code, int(decision.get("amount_cents") or 0), nav,
                    idem + ":fundbuy", name=name)
            except TypeError:  # 兼容旧测试里的假 Trading
                return await self.trading.subscribe(
                    code, int(decision.get("amount_cents") or 0), nav, idem + ":fundbuy")
        if decision["action"] == "sell_fund" and decision.get("code"):
            code = str(decision["code"])
            quote = (getattr(self, "_fund_quotes", {}) or {}).get(code) or {}
            if not quote.get("tradeable"):
                return {"ok": False, "message": "今天这只基金没有可用估值，跳过交易"}
            nav = float(quote.get("estimate") or 0)
            return await self.trading.redeem(
                code, float(decision.get("shares") or 0), nav, idem + ":fundsell")
        if decision["action"] == "buy" and decision.get("code"):
            return await self.trading.buy(decision["code"], decision.get("qty", 100), None, idem + ":buy")
        if decision["action"] == "sell" and decision.get("code") and decision.get("qty"):
            return await self.trading.sell(decision["code"], decision["qty"], None, idem + ":sell")
        return {"ok": False, "message": "决定先不动"}

    async def _record(self, slot, decision, executed, now, pending=False, from_pending=False):
        mood = self.finance.mood()
        when = now.strftime("%Y-%m-%d %H:%M")
        if decision.get("action") in ("buy_fund", "sell_fund"):
            line = json.dumps({"at": when, "action": decision.get("action"),
                               "code": decision.get("code"),
                               "reason": decision.get("reason"),
                               "executed": bool(executed.get("ok"))},
                              ensure_ascii=False)
        else:
            line = "%s 决策：%s" % (when, decision["reason"])
            if executed.get("ok"):
                line += "；%s" % executed.get("message", "")
        payload = {
            "slot": slot, "action": decision.get("action"), "code": decision.get("code"),
            "reason": decision.get("reason"), "at": now.strftime("%m-%d %H:%M"),
            "executed": bool(executed.get("ok")),
        }
        for key in ("qty", "amount_cents", "shares"):
            if decision.get(key) not in (None, "", 0):
                payload[key] = decision.get(key)
        if executed.get("message"):
            payload["message"] = str(executed.get("message"))
        if pending:
            payload["pending"] = True
        if from_pending:
            payload["from_pending"] = True
        try:
            self.repo.meta_set("last_decision", json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            isolated("finance.decision.meta", exc)
        await self.finance.memory.remember(line, kind="finance_decision",
                                           idem="decision:%s:%s:mem" % (now.date().isoformat(), slot))
        if decision.get("action") in ("buy_fund", "sell_fund"):
            # 基金日记由 FinanceLink.record_fund_fact 交给 LLM 生成；这里绝不拼模板
            return
        try:
            amo = self.finance._amo()
            if amo is not None:
                extra = ""
                if getattr(self, "_news_used", False):
                    extra = "看了新闻，" + ("参考着做了决定。" if executed.get("ok") else "没太看懂，没动。")
                text = "今天%s，%s。%s" % (decision["reason"], mood["word"], extra)
                try:
                    amo.db.save_diary(now.date().isoformat(), "%02d" % now.hour, text,
                                      source="finance")
                except TypeError:  # 老版本/测试替身不接受 source
                    amo.db.save_diary(now.date().isoformat(), "%02d" % now.hour, text)
        except Exception as exc:
            isolated("finance.decision.diary", exc)

    async def _low_fund(self, state, now):
        try:
            bank = getattr(self.plugin, "bank", None)
            if bank is None:
                from ..services.bank_engine import build_bank
                bank = build_bank(self.plugin)
                self.plugin.bank = bank
            if bank is not None:
                liquid = await bank.emergency_liquidate(state.get("need_cents") or 50000)
                if liquid.get("ok") and liquid.get("from") != "cash":
                    return
        except Exception as exc:
            isolated("finance.bank.emergency", exc)
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
        try:
            cash = int(self.finance.balance_cents() or 0)
            asset = cash + sum(int(r.get("cost_cents") or 0) * float(r.get("qty") or 0)
                               for r in self.repo.positions())
            if total < 0 and abs(total) > 0.10 * max(asset, 1):
                amo = self.finance._amo() if hasattr(self.finance, "_amo") else None
                if amo is not None and hasattr(amo, "on_big_stock_loss"):
                    amo.on_big_stock_loss()
        except Exception as exc:
            isolated("finance.big_loss_notify", exc)
        self.repo.meta_set(flag, "1")
        return {"ok": True, "total_pnl": total / 100.0, "items": len(items)}
