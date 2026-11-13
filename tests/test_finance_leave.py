# -*- coding: utf-8 -*-
"""请假全链路 + 账单收尾语气。"""
import asyncio
import datetime as dt

from utils.mood import reaction_line
from services.work_engine import WORK_DAYS, FinanceSalary


class FakeRepo:
    def __init__(self):
        self.meta = {}
        self.items = []

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def add_salary_item(self, period, kind, label, amount_cents):
        self.items.append({"period": period, "kind": kind, "label": label,
                           "amount_cents": int(amount_cents)})
        return True

    def salary_items(self, period):
        return [i for i in self.items if i["period"] == period]

    def settled(self, period, kind):
        return False

    def mark_settled(self, period, kind, payload="{}"):
        return True


class FakeFinance:
    def __init__(self):
        self.schedules = []
        self.memories = []
        self.diaries = []
        self.moods = []
        self.helps = []

    def add_schedule(self, day, activity, detail="", place="家", category="leave"):
        self.schedules.append({"day": day, "activity": activity, "detail": detail})
        return True

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def small_diary(self, text):
        self.diaries.append(text)
        return True

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True

    async def low_balance(self, need, idem):
        self.helps.append(int(need))
        return {"ok": True}

    async def income(self, cents, reason, idem, salary=False):
        return {"ok": True}


class FakeHelp:
    def __init__(self, low):
        self.low = low

    def status(self):
        return {"low": self.low, "ratio": 0.02 if self.low else 0.4}


class FakePlugin:
    def __init__(self, low=False):
        self.finance_help = FakeHelp(low)


def make(low=False):
    repo = FakeRepo()
    fin = FakeFinance()
    sal = FinanceSalary(FakePlugin(low), repo, fin)
    sal.set_base(680000)
    return repo, fin, sal


def test_leave_records_deduction_schedule_memory_diary_mood():
    repo, fin, sal = make()
    result = asyncio.run(sal.apply_leave(dt.date(2026, 11, 14), kind="事假", days=1))
    assert result["ok"] and result["label"] == "事假一天"
    assert result["amount_cents"] == -int(round(680000 / WORK_DAYS))
    item = repo.items[-1]
    assert item["label"] == "事假一天" and item["amount_cents"] < 0
    assert fin.schedules and fin.schedules[0]["day"] == "2026-11-14"
    assert "休息" in fin.schedules[0]["activity"]
    assert fin.memories and "2026年11月14日" in fin.memories[0] and "被扣款" in fin.memories[0]
    assert fin.diaries and "好心痛" in fin.diaries[0]
    assert fin.moods and "委屈" in fin.moods[0]
    assert "本月实发还剩" in result["card"]
    assert result["net_cents"] == 680000 - int(round(680000 / WORK_DAYS))


def test_leave_half_day_and_sick_label():
    repo, fin, sal = make()
    result = asyncio.run(sal.apply_leave(dt.date(2026, 11, 14), kind="病假", days=0.5))
    assert result["label"] == "病假半天"
    assert result["amount_cents"] == -int(round(680000 / WORK_DAYS * 0.5))


def test_leave_can_target_tomorrow():
    repo, fin, sal = make()
    asyncio.run(sal.apply_leave(dt.date(2026, 11, 13), day="明天"))
    assert fin.schedules and fin.schedules[0]["day"] == "2026-11-14"
    assert "2026年11月14日" in fin.memories[0]
    asyncio.run(sal.apply_leave(dt.date(2026, 11, 13), day="2026-11-20"))
    assert fin.schedules[-1]["day"] == "2026-11-20"


def test_leave_when_broke_hits_crisis_branch():
    repo, fin, sal = make(low=True)
    result = asyncio.run(sal.apply_leave(dt.date(2026, 11, 14)))
    assert result["low"] is True
    assert fin.helps == [0]                      # 进了 5% 求助流程
    assert any(d.get("焦虑") for d in fin.moods)


def test_reaction_line_is_varied_and_mood_aware():
    happy = {reaction_line("很开心、心情不错", "seed-%d" % i) for i in range(20)}
    sad = {reaction_line("委屈、难过", "seed-%d" % i) for i in range(20)}
    neutral = {reaction_line("平静", "seed-%d" % i) for i in range(20)}
    assert len(happy) > 1 and len(sad) > 1 and len(neutral) > 1
    assert happy.isdisjoint(sad)
    # 同一条账目必须稳定（刷新不会变）
    assert reaction_line("很开心", "x") == reaction_line("很开心", "x")
    assert "心情不错" not in reaction_line("很开心、心情不错", "any")
