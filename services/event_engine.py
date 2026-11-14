# -*- coding: utf-8 -*-
"""外出与突发事件引擎：探望父母/闺蜜逛街/旅游/意外，全链路联动。"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
    from ..utils.helpers import is_holiday, stable_int, yuan
except ImportError:
    from guard import isolated
    from utils.helpers import is_holiday, stable_int, yuan


class EventEngine:
    def __init__(self, plugin, repo, finance, social=None, medical=None):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.social = social
        self.medical = medical

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
        self.finance.long_diary("今天回家看爸妈了，吃了妈妈做的红烧肉。虽然花了几百块买礼物，但好幸福。")
        try:
            await self.finance.permanent_memory("回家看爸妈，花了 %s，很幸福。" % yuan(gift + fare), key)
        except Exception as exc:
            isolated("event.parents.memory", exc)
        self.finance.add_schedule(day.isoformat(), "回家看爸妈", detail="礼物+路费 %s" % yuan(gift + fare), category="family")
        self.repo.meta_set("last_visit_parents", day.isoformat())
        self.repo.mark_task(key, "parents")
        return {"ok": True, "cost": gift + fare, "text": "回家看爸妈，花了 %s，好幸福。" % yuan(gift + fare)}

    async def mall_with_bff(self, day=None):
        day = day or _dt.date.today()
        key = "event:mall:" + day.isoformat()
        if self.repo.task_done(key) or self.deny_entertainment_outing() or not self._weekend(day):
            return {"ok": False, "reason": "blocked"}
        if self._low():
            self.finance.small_diary("闺蜜约我逛街，但我没钱了，只能借口加班拒绝，好难过。")
            self.finance.nudge_mood({"委屈": 8}, key + ":low")
            self.repo.mark_task(key, "skip")
            return {"ok": False, "reason": "low_balance"}
        food = stable_int("mall-food:" + day.isoformat(), 8000, 20000)
        impulse = stable_int("mall-impulse:" + day.isoformat(), 0, 100) < 60
        buy = stable_int("mall-buy:" + day.isoformat(), 20000, 80000) if impulse else 0
        res = await self.finance.expense(food + buy, "闺蜜逛街" + ("（冲动买了衣服）" if buy else ""), key, necessary=bool(buy))
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        self.finance.nudge_mood({"开心": 10, "兴奋": 8}, key + ":mood")
        self.finance.small_diary("今天跟闺蜜逛街，花了 %s%s。" % (yuan(food + buy), "，还买了件衣服" if buy else ""))
        if buy:
            self.repo.meta_set("regret_pending", day.isoformat())
        self.repo.mark_task(key, "mall")
        return {"ok": True, "cost": food + buy, "impulse": bool(buy),
                "text": "逛街花了 %s%s。" % (yuan(food + buy), "，有点上头" if buy else "")}

    async def trip(self, place="三亚", day=None):
        day = day or _dt.date.today()
        key = "event:trip:" + day.isoformat()
        if self.repo.task_done(key) or self.deny_entertainment_outing() or not is_holiday(day):
            return {"ok": False, "reason": "blocked"}
        cost = stable_int("trip-cost:" + day.isoformat(), 200000, 500000)
        cash = int(self.finance.balance_cents() or 0)
        if cash < cost:
            self.finance.small_diary("老公，%s约我去%s玩，可是我还差 %s，你能赞助我一下吗？"
                                     % ("闺蜜", place, yuan(cost - cash)))
            return {"ok": False, "reason": "need_sponsor", "missing": cost - cash,
                    "text": "老公，闺蜜约我去%s玩，我还差 %s，你能赞助我一下吗？" % (place, yuan(cost - cash))}
        res = await self.finance.expense(cost, "去%s旅游" % place, key, necessary=True)
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        self.finance.nudge_mood({"开心": 15, "兴奋": 12}, key + ":mood")
        for offset in range(3):
            d = (day + _dt.timedelta(days=offset)).isoformat()
            self.finance.add_schedule(d, "在%s旅游" % place, detail="第 %d 天" % (offset + 1), category="travel")
            self.finance.small_diary("%s的海好美，如果老公也能一起来就好了。" % place)
        self.repo.mark_task(key, "trip")
        return {"ok": True, "cost": cost, "text": "去%s玩啦，花了 %s！" % (place, yuan(cost))}

    async def car_accident(self, loss_cents=200000, has_car=True):
        ins = getattr(self.plugin, "insurance", None)
        if ins is None:
            return {"ok": False, "reason": "no_insurance"}
        res = await ins.car_claim(loss_cents, has_car)
        if not res.get("covered"):
            await self.finance.expense(loss_cents, "车祸维修（无车险）", "event:car:" + _dt.date.today().isoformat(), necessary=True)
        self.finance.small_diary("车子被刮了。" + ("平安全赔了，买保险真香！" if res.get("covered") else "没保险，自掏腰包，心痛。"))
        return res

    async def house_disaster(self, loss_cents=200000, has_house=True):
        ins = getattr(self.plugin, "insurance", None)
        if ins is None:
            return {"ok": False, "reason": "no_insurance"}
        res = await ins.house_claim(loss_cents, has_house)
        if not res.get("covered"):
            await self.finance.expense(loss_cents, "房屋维修（无家财险）", "event:house:" + _dt.date.today().isoformat(), necessary=True)
        self.finance.small_diary("家里出事了。" + ("家财险赔了，还好有保险。" if res.get("covered") else "没保险，只能自己扛。"))
        return res

    async def sponsor(self, amount_cents, target=""):
        amount = int(amount_cents or 0)
        if amount <= 0:
            return {"ok": False, "reason": "zero"}
        self.finance.nudge_mood({"开心": 15, "兴奋": 15, "依恋": 10}, "sponsor:" + str(amount))
        self.finance.small_diary("这次去旅游全靠老公赞助，他真的对我太好了，好爱他。")
        try:
            await self.finance.permanent_memory("老公赞助了 %s 去%s，好感动。" % (yuan(amount), target or "旅游"),
                                                "sponsor:%s:%s" % (target or "trip", amount))
        except Exception as exc:
            isolated("event.sponsor.memory", exc)
        return {"ok": True, "text": "谢谢老公！这次旅游全靠你啦，好爱你。"}


def build_event(plugin):
    return EventEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None),
                       getattr(plugin, "social", None), getattr(plugin, "medical", None))
