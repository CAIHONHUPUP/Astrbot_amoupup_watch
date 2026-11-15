# -*- coding: utf-8 -*-
"""外出与突发事件引擎：探望父母/闺蜜逛街/旅游/意外，全链路联动。

2026-11-15 封板：这里不再写死任何「今天跟闺蜜逛街，花了…」的句子，
代码只给 JSON 事实，由 social_ai.narrate 用她的话现写；LLM 抽风就退回 [社交]/[财务] 结构化兜底。
"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
    from ..utils.helpers import is_holiday, stable_int, yuan
    from . import npc_roster
except ImportError:
    from guard import isolated
    from utils.helpers import is_holiday, stable_int, yuan
    from services import npc_roster


class EventEngine:
    def __init__(self, plugin, repo, finance, social=None, medical=None):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.social = social
        self.medical = medical

    # ---------- 人话：LLM 现写，失败退回结构化 ----------
    async def _say(self, facts, fallback):
        text = ""
        try:
            try:
                from .social_ai import narrate
            except ImportError:
                from services.social_ai import narrate
            text = await narrate(self.plugin, facts, fallback)
        except Exception as exc:
            isolated("event.narrate", exc)
        return npc_roster.sanitize(text, str((facts or {}).get("npc") or "")) or fallback

    def _low(self):
        cash = int(self.finance.balance_cents() or 0)
        return cash < 50000

    def allow_medical_outing(self):
        return self.medical.allow_medical_outing() if hasattr(self.medical or object(), "allow_medical_outing") else False

    def deny_entertainment_outing(self):
        return bool(self.medical and (self.medical.deny_entertainment_outing()
                                      if hasattr(self.medical, "deny_entertainment_outing") else self.medical.is_sick()))

    def _sick(self):
        return bool(self.medical.is_sick()) if self.medical is not None else False

    def _weekend(self, day):
        return day.weekday() >= 5 or is_holiday(day)

    async def visit_parents(self, day=None):
        day = day or _dt.date.today()
        key = "event:parents:" + day.isoformat()
        if self.repo.task_done(key):
            return {"ok": False, "reason": "done"}
        if self.deny_entertainment_outing() or not self._weekend(day):
            return {"ok": False, "reason": "sick_or_weekday"}
        gift = stable_int("parent-gift:" + day.isoformat(), 20000, 50000)
        fare = stable_int("parent-fare:" + day.isoformat(), 5000, 20000)
        res = await self.finance.expense(gift + fare, "回家看爸妈（礼物+路费）", key, necessary=True)
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        self.finance.nudge_mood({"开心": 12, "依恋": 10}, key + ":mood")
        fallback = "[日程] 回家看爸妈，支出 %s" % yuan(gift + fare)
        line = await self._say({"action": "visit_parents", "npc": "妈妈",
                                "cost_yuan": round((gift + fare) / 100.0, 2), "place": "爸妈家"}, fallback)
        self.finance.long_diary(line)
        try:
            await self.finance.permanent_memory(npc_roster.sanitize(line, "妈妈") or fallback, key)
        except Exception as exc:
            isolated("event.parents.memory", exc)
        self.finance.add_schedule(day.isoformat(), "回家看爸妈",
                                  detail=fallback, category="family")
        self.repo.meta_set("last_visit_parents", day.isoformat())
        self.repo.mark_task(key, "parents")
        return {"ok": True, "cost": gift + fare, "text": line}

    async def mall_with_bff(self, day=None):
        day = day or _dt.date.today()
        key = "event:mall:" + day.isoformat()
        if self.repo.task_done(key) or self.deny_entertainment_outing() or not self._weekend(day):
            return {"ok": False, "reason": "blocked"}
        if self._low():
            fallback = "[社交] 没钱，没去逛街"
            line = await self._say({"action": "decline_shopping", "npc": "柳如烟",
                                    "reason": "low_balance"}, fallback)
            self.finance.small_diary(line)
            self.finance.nudge_mood({"委屈": 8}, key + ":low")
            self.repo.mark_task(key, "skip")
            return {"ok": False, "reason": "low_balance", "text": line}
        food = stable_int("mall-food:" + day.isoformat(), 8000, 20000)
        impulse = stable_int("mall-impulse:" + day.isoformat(), 0, 100) < 60
        buy = stable_int("mall-buy:" + day.isoformat(), 20000, 80000) if impulse else 0
        res = await self.finance.expense(food + buy, "和柳如烟逛街", key, necessary=bool(buy))
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        self.finance.nudge_mood({"开心": 10, "兴奋": 8}, key + ":mood")
        fallback = "[社交] 和柳如烟逛街，支出 %s" % yuan(food + buy)
        line = await self._say({"action": "shopping", "npc": "柳如烟",
                                "cost_yuan": round((food + buy) / 100.0, 2),
                                "bought_clothes": bool(buy), "place": "商场"}, fallback)
        self.finance.small_diary(line)
        if buy:
            self.repo.meta_set("regret_pending", day.isoformat())
        self.repo.mark_task(key, "mall")
        return {"ok": True, "cost": food + buy, "impulse": bool(buy), "text": line}

    async def trip(self, place="三亚", day=None):
        day = day or _dt.date.today()
        key = "event:trip:" + day.isoformat()
        if self.repo.task_done(key) or self.deny_entertainment_outing() or not is_holiday(day):
            return {"ok": False, "reason": "blocked"}
        cost = stable_int("trip-cost:" + day.isoformat(), 200000, 500000)
        cash = int(self.finance.balance_cents() or 0)
        if cash < cost:
            fallback = "[日程] 去%s旅游还差 %s，想请他赞助" % (place, yuan(cost - cash))
            line = await self._say({"action": "need_sponsor", "npc": "柳如烟", "place": place,
                                    "missing_yuan": round((cost - cash) / 100.0, 2)}, fallback)
            self.finance.small_diary(line)
            return {"ok": False, "reason": "need_sponsor", "missing": cost - cash, "text": line}
        res = await self.finance.expense(cost, "去%s旅游" % place, key, necessary=True)
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        self.finance.nudge_mood({"开心": 15, "兴奋": 12}, key + ":mood")
        fallback = "[日程] 在%s旅游，支出 %s" % (place, yuan(cost))
        line = await self._say({"action": "trip", "npc": "柳如烟", "place": place,
                                "cost_yuan": round(cost / 100.0, 2)}, fallback)
        for offset in range(3):
            d = (day + _dt.timedelta(days=offset)).isoformat()
            self.finance.add_schedule(d, "在%s旅游" % place, detail="第 %d 天" % (offset + 1),
                                      category="travel")
        self.finance.small_diary(line)
        self.repo.mark_task(key, "trip")
        return {"ok": True, "cost": cost, "text": line}

    async def car_accident(self, loss_cents=200000, has_car=True):
        ins = getattr(self.plugin, "insurance", None)
        if ins is None:
            return {"ok": False, "reason": "no_insurance"}
        res = await ins.car_claim(loss_cents, has_car)
        if not res.get("covered"):
            await self.finance.expense(loss_cents, "车祸维修（无车险）",
                                       "event:car:" + _dt.date.today().isoformat(), necessary=True)
        fallback = "[事件] 车被刮了，%s" % ("保险理赔" if res.get("covered") else "自费维修")
        line = await self._say({"action": "car_accident", "covered": bool(res.get("covered")),
                                "loss_yuan": round(int(loss_cents) / 100.0, 2)}, fallback)
        self.finance.small_diary(line)
        res["text"] = line
        return res

    async def house_disaster(self, loss_cents=200000, has_house=True):
        ins = getattr(self.plugin, "insurance", None)
        if ins is None:
            return {"ok": False, "reason": "no_insurance"}
        res = await ins.house_claim(loss_cents, has_house)
        if not res.get("covered"):
            await self.finance.expense(loss_cents, "房屋维修（无家财险）",
                                       "event:house:" + _dt.date.today().isoformat(), necessary=True)
        fallback = "[事件] 家里出事，%s" % ("保险理赔" if res.get("covered") else "自费维修")
        line = await self._say({"action": "house_disaster", "covered": bool(res.get("covered")),
                                "loss_yuan": round(int(loss_cents) / 100.0, 2)}, fallback)
        self.finance.small_diary(line)
        res["text"] = line
        return res

    async def sponsor(self, amount_cents, target=""):
        amount = int(amount_cents or 0)
        if amount <= 0:
            return {"ok": False, "reason": "zero"}
        self.finance.nudge_mood({"开心": 15, "兴奋": 15, "依恋": 10}, "sponsor:" + str(amount))
        fallback = "[财务] 他赞助 %s 去%s" % (yuan(amount), target or "旅游")
        line = await self._say({"action": "sponsor", "from": "老公", "amount_yuan": round(amount / 100.0, 2),
                                "target": target or "旅游"}, fallback)
        self.finance.small_diary(line)
        try:
            await self.finance.permanent_memory(npc_roster.sanitize(line) or fallback,
                                                "sponsor:%s:%s" % (target or "trip", amount))
        except Exception as exc:
            isolated("event.sponsor.memory", exc)
        return {"ok": True, "text": line}


def build_event(plugin):
    return EventEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None),
                       getattr(plugin, "social", None), getattr(plugin, "medical", None))
