# -*- coding: utf-8 -*-
"""日常开销：逐笔记账、分类汇总、日终记忆、饿肚子、有无车的交通分流。"""
import asyncio
import datetime as dt

from services.daily_engine import FinanceDaily
from services.expense_engine import FinanceExpense


class FakeRepo:
    def __init__(self):
        self.rows = []
        self.meta = {}
        self.log = []

    def add_expense(self, idem, day, category, label, amount_cents):
        if any(r["idem"] == idem for r in self.rows):
            return False
        self.rows.append({"idem": idem, "day": day, "category": category,
                          "label": label, "amount_cents": int(amount_cents)})
        return True

    def expense_totals(self, period):
        agg = {}
        for row in self.rows:
            if not row["day"].startswith(str(period)):
                continue
            agg[row["category"]] = agg.get(row["category"], 0) + row["amount_cents"]
        return [{"category": k, "total": v, "n": 1} for k, v in agg.items()]

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)


class FakeFinance:
    def __init__(self, cash=1000000, ok=True):
        self.cash = cash
        self.ok = ok
        self.calls = []
        self.moods = []
        self.helps = []
        self.memories = []
        self.long_memories = []
        self.diaries = []

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        self.calls.append((reason, int(cents), quiet))
        if not self.ok or self.cash < int(cents):
            return {"ok": False, "reason": "余额不足"}
        self.cash -= int(cents)
        return {"ok": True}

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True

    async def low_balance(self, need, idem):
        self.helps.append(int(need))
        return {"ok": True}

    async def remember_event(self, kind, amount, text, idem):
        self.memories.append(text)
        return True

    async def long_memory(self, text, idem):
        self.long_memories.append(text)
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True


def make(cash=1000000, ok=True):
    repo = FakeRepo()
    fin = FakeFinance(cash, ok)
    return repo, fin, FinanceExpense(None, repo, fin)


def test_summary_groups_by_category():
    repo, fin, exp = make()
    exp.record("2026-11-13", "food", "面条 × 1", 1500, "a")
    exp.record("2026-11-13", "transport", "地铁 × 1", 500, "b")
    exp.record("2026-11-12", "food", "包子 × 1", 200, "c")
    summary = exp.month_summary(dt.date(2026, 11, 13))
    assert summary["total_cents"] == 2200
    by_key = {c["key"]: c["amount_cents"] for c in summary["categories"]}
    assert by_key["food"] == 1700
    assert by_key["transport"] == 500
    assert by_key["daily"] == 0


def test_spend_records_and_no_record_when_insufficient():
    repo, fin, exp = make(cash=1000)
    ok = asyncio.run(exp.spend("food", "面条", 1500, day=dt.date(2026, 11, 13), idem="x"))
    assert ok["ok"] is False
    assert repo.rows == []
    fin.cash = 100000
    ok = asyncio.run(exp.spend("food", "面条", 1500, day=dt.date(2026, 11, 13), idem="y"))
    assert ok["ok"] is True
    assert len(repo.rows) == 1 and repo.rows[0]["category"] == "food"
    assert fin.calls[-1][2] is True          # quiet：不写长期记忆


def test_impulse_and_big_social_have_mood():
    repo, fin, exp = make(cash=1000000)
    asyncio.run(exp.spend("impulse", "奶茶", 1500, day=dt.date(2026, 11, 13), idem="i"))
    asyncio.run(exp.spend("social", "随份子", 80000, day=dt.date(2026, 11, 13), idem="s"))
    asyncio.run(exp.spend("daily", "纸巾", 1200, day=dt.date(2026, 11, 13), idem="d"))
    assert any("开心" in d for d in fin.moods)
    assert any("委屈" in d for d in fin.moods)
    assert len(fin.moods) == 2               # 日用品不触发情绪


def test_rollup_writes_one_daily_memory():
    repo, fin, exp = make()
    exp.record("2026-11-13", "food", "面条 × 1", 1500, "a")
    exp.record("2026-11-13", "transport", "地铁 × 1", 500, "b")
    out = asyncio.run(exp.rollup(dt.datetime(2026, 11, 14, 9, 0)))
    assert out["rolled"] and "餐饮饮食" in fin.long_memories[0]
    assert "¥20.00" in fin.long_memories[0]
    again = asyncio.run(exp.rollup(dt.datetime(2026, 11, 14, 9, 5)))
    assert again["rolled"] == []


def test_hungry_drops_mood_and_raises_help():
    repo, fin, exp = make()
    text = asyncio.run(exp.hungry("2026-11-13", 1500))
    assert "饿" in text
    assert fin.helps == [1500]
    assert fin.memories and fin.diaries == []   # 只记事件，不写长日记
    assert any("委屈" in d for d in fin.moods)
    assert repo.meta_get("hungry_until") == "2026-11-13"


class FakeProp:
    def __init__(self, has_car):
        self.has_car = has_car

    def asset(self, kind):
        return {"kind": kind} if (kind == "car" and self.has_car) else None


class FakeConsume:
    def __init__(self):
        self.bought = []

    async def buy_item(self, name, qty=1, idem="", when=""):
        self.bought.append(name)
        return {"ok": True}


class FakePlugin:
    def __init__(self, repo, fin, exp, has_car):
        self.finance_expense = exp
        self.finance_property = FakeProp(has_car)
        self.finance = fin
        self.repo = repo


def test_daily_skips_traffic_when_car_owned():
    repo, fin, exp = make()
    consume = FakeConsume()
    morning = dt.datetime(2026, 11, 9, 8, 10)   # 周一早高峰
    plugin = FakePlugin(repo, fin, exp, has_car=True)
    daily = FinanceDaily(plugin, repo, fin, consume)
    asyncio.run(daily._extras(morning, "2026-11-09"))
    assert consume.bought == []

    plugin.finance_property = FakeProp(has_car=False)
    asyncio.run(daily._extras(morning, "2026-11-10"))
    assert consume.bought and consume.bought[0] in ("公交", "地铁", "共享单车")
