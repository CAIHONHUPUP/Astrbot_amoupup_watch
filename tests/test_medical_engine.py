# -*- coding: utf-8 -*-
"""医疗结算：70% 报销、幂等、低余额求助、病假日扣薪、生病状态。"""
import asyncio
import datetime as dt

from services.medical_engine import MedicalEngine, COST_RANGES


class Repo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value


class Finance:
    def __init__(self, balance=1000000):
        self.cash = balance
        self.expenses = []
        self.memories = []
        self.diaries = []
        self.schedules = []

    def balance_cents(self):
        return self.cash

    async def expense(self, cents, reason, idem, necessary=False):
        if cents > self.cash:
            return {"ok": False, "reason": "余额不足"}
        self.cash -= cents
        self.expenses.append((cents, reason))
        return {"ok": True}

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def small_diary(self, text):
        self.diaries.append(text)
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True

    def add_schedule(self, day, activity, detail="", place="家", category="leave"):
        self.schedules.append((day, activity))
        return True


class Work:
    def __init__(self):
        self.fired = []

    def daily_rate_cents(self):
        return 31264

    async def fire(self, kind, **kw):
        self.fired.append((kind, kw))
        return {"ok": True}


class Help:
    def __init__(self, total):
        self.total = total

    def total_assets_cents(self):
        return self.total


class Plugin:
    config = {}


def make(balance=1000000, total=None):
    repo = Repo()
    fin = Finance(balance)
    work = Work()
    eng = MedicalEngine(Plugin(), repo, fin, salary=None, work_events=work,
                        help_engine=Help(total if total is not None else balance))
    return repo, fin, work, eng


def test_light_sick_settles_with_70_percent_reimbursement():
    repo, fin, work, eng = make()
    out = asyncio.run(eng.handle_sick_event("light", "感冒", 2, "sick_test_1"))
    assert out["ok"] and out["charged"]
    total = out["total_cents"]
    assert COST_RANGES["light"][0] <= total <= COST_RANGES["light"][1]
    assert out["insurance_cents"] == int(round(total * 0.7))
    assert out["self_pay_cents"] == total - out["insurance_cents"]
    assert fin.expenses[0][0] == out["self_pay_cents"]
    assert "医疗电子病历单" in out["card"] and "医保报销" in out["card"]
    assert work.fired and work.fired[0][0] == "sick"
    assert work.fired[0][1]["amount_cents"] == -31264 * 2
    assert len(fin.schedules) >= 2 and eng.is_sick()
    assert any("自费" in m for m in fin.memories)


def test_same_event_id_is_ignored():
    repo, fin, work, eng = make()
    first = asyncio.run(eng.handle_sick_event("mid", "肺炎", 3, "sick_dup"))
    second = asyncio.run(eng.handle_sick_event("mid", "肺炎", 3, "sick_dup"))
    assert first["ok"] and second.get("duplicate") is True
    assert len(fin.expenses) <= 2 and second.get("duplicate") is True


def test_low_balance_aborts_and_asks_for_help():
    repo, fin, work, eng = make(balance=100000, total=100000)
    out = asyncio.run(eng.handle_sick_event("heavy", "骨折", 5, "sick_low"))
    assert out["ok"] is False and out.get("need_help") is True
    assert fin.expenses == []
    assert eng.is_sick() is False


def test_sick_state_expires():
    repo, fin, work, eng = make()
    asyncio.run(eng.handle_sick_event("light", "感冒", 1, "sick_exp"))
    assert eng.is_sick(dt.date.today()) is True
    assert eng.is_sick(dt.date.today() + dt.timedelta(days=2)) is False
