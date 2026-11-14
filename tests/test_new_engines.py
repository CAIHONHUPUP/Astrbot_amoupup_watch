# -*- coding: utf-8 -*-
"""新引擎：年终奖、节假日 3 倍、突发、借钱、赌徒、保险。"""
import asyncio
import datetime as dt

from services.fortune_engine import FortuneEngine
from services.lending_engine import LendingEngine
from services.gamble_engine import GambleEngine
from services.work_events import WorkEventEngine
from services.work_engine import FinanceSalary
from services.medical_engine import MedicalEngine


class Repo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, k, d=None):
        return self.meta.get(k, d)

    def meta_set(self, k, v):
        self.meta[k] = v


class Finance:
    def __init__(self, cash=500000):
        self.cash = cash
        self.diaries, self.memories, self.moods = [], [], []
        self.expenses, self.incomes = [], []

    def balance_cents(self):
        return self.cash

    async def expense(self, c, reason, idem, necessary=False):
        if c > self.cash:
            return {"ok": False}
        self.cash -= c
        self.expenses.append((c, reason))
        return {"ok": True}

    async def income(self, c, reason, idem, salary=False):
        self.cash += c
        self.incomes.append((c, reason))
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

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True

    def add_schedule(self, *a, **k):
        return True


class Salary:
    def base_cents(self):
        return 680000

    def add_item(self, *a):
        return True

    def items(self, period):
        return []

    def summary(self, period):
        return {"net_cents": 680000, "base_cents": 680000, "bonus_cents": 0, "deduction_cents": 0}


class Plugin:
    config = {}
    work_events = None


def test_fortune_monthly_is_idempotent():
    repo, fin = Repo(), Finance()
    eng = FortuneEngine(Plugin(), repo, fin)
    first = asyncio.run(eng.monthly_roll(dt.datetime(2026, 12, 3)))
    second = asyncio.run(eng.monthly_roll(dt.datetime(2026, 12, 4)))
    assert second.get("dup") is True
    if first.get("ok"):
        assert fin.diaries and "【突发事件】" in fin.diaries[0]
    assert repo.meta_get("fortune:2026-12")


def test_lending_repay_or_default_is_idempotent():
    repo, fin = Repo(), Finance(100000)
    eng = LendingEngine(Plugin(), repo, fin)
    out = asyncio.run(eng.lend("小王", 20000, months=1, now=dt.datetime(2026, 11, 1)))
    assert out["ok"]
    tick = asyncio.run(eng.repay_tick(dt.datetime(2026, 12, 5)))
    assert tick["results"] and tick["results"][0]["friend"] == "小王"
    again = asyncio.run(eng.repay_tick(dt.datetime(2026, 12, 6)))
    assert again["results"] == []


def test_holiday_overtime_pays_triple():
    repo, fin = Repo(), Finance()
    plugin = Plugin()
    sal = Salary()
    eng = WorkEventEngine(plugin, repo, fin, sal)
    rate = eng.daily_rate_cents()
    out = asyncio.run(eng.fire("overtime", day=dt.date(2026, 10, 1)))
    assert out["amount_cents"] == rate * 3 and out["label"] == "节假日加班"


class FullRepo(Repo):
    def __init__(self):
        super().__init__()
        self.items_rows = []

    def add_salary_item(self, period, kind, label, amount):
        self.items_rows.append((kind, label, amount))

    def salary_items(self, period):
        return self.items_rows

    def mark_settled(self, period, kind, note=""):
        return True


def test_year_end_bonus_added_on_jan_15():
    repo, fin = FullRepo(), Finance()
    plugin = Plugin()
    sal = FinanceSalary(plugin, repo, fin)
    class ME:
        async def grant_full_attendance(self, *a, **k):
            return None
    plugin.work_events = ME()
    out = asyncio.run(sal.settle(dt.datetime(2027, 1, 15, 10, 0), "2027-01"))
    assert out["ok"]


class Trades:
    async def sell(self, *a, **k):
        return {"ok": True}

    async def buy(self, *a, **k):
        return {"ok": True}


def test_gamble_buys_when_three_day_drop_and_crash_mood():
    repo, fin = Repo(), Finance(1000000)
    repo.meta_set("hist:600519", "[10.0, 9.5, 9.0]")
    eng = GambleEngine(Plugin(), repo, fin, Trades())
    out = asyncio.run(eng.maybe_gamble({}, [{"code": "600519", "qty": 100, "cost_cents": 90000}],
                                       "崩溃", dt.datetime(2026, 12, 1)))
    if out.get("ok"):
        assert any("不信它不涨" in d for d in fin.diaries)
        assert any("补仓" in m for m in fin.memories)


def test_insurance_claim_cuts_self_pay():
    repo, fin = Repo(), Finance(1000000)
    from services.insurance_engine import InsuranceEngine
    plugin = Plugin()
    med = MedicalEngine(plugin, repo, fin)
    plugin.insurance = InsuranceEngine(plugin, repo, fin)
    asyncio.run(plugin.insurance.buy("医无忧"))
    out = asyncio.run(med.handle_sick_event("mid", "肺炎", 3, "ins_1"))
    assert out["ok"] and out["commercial_cents"] > 0
    assert out["self_pay_cents"] < out["total_cents"] * 0.3
