# -*- coding: utf-8 -*-
import datetime as dt
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from services import bill_flow as bf

class FakeRepo:
    def __init__(self): self.data = {}
    def meta_get(self, k, d=None): return self.data.get(k, d)
    def meta_set(self, k, v): self.data[k] = v
    def assets(self): return []

class FakeFinance:
    def __init__(self, cash=1000000):
        self.cash = cash; self.mem = []; self.diaries = []; self.moods = []
    def balance_cents(self): return self.cash
    async def expense(self, amount, reason, idem, necessary=False):
        if self.cash < amount: return {"ok": False}
        self.cash -= amount; return {"ok": True}
    async def permanent_memory(self, t, i): self.mem.append(t)
    def small_diary(self, t): self.diaries.append(t)
    def nudge_mood(self, d, i): self.moods.append(d)

class FakeLife:
    def _has(self, kind): return False
    def rent_cents(self, period): return 300000
    def utility_cents(self, period): return 20000

class FakePlugin:
    def __init__(self):
        self.finance_repo = FakeRepo(); self.finance = FakeFinance()
        self.finance_life = FakeLife(); self.finance_property = None
    def cfg_str(self, key, default=""): return default

class BillFlowTests(unittest.TestCase):
    def test_weekend_moves_to_monday(self):
        self.assertEqual(bf.due_day(dt.datetime(2026, 11, 1, 9)), dt.date(2026, 11, 2))

    def test_holiday_moves_forward(self):
        self.assertEqual(bf.due_day(dt.datetime(2026, 10, 1, 9), {"10-01", "10-02"}),
                         dt.date(2026, 10, 5))

    def test_create_due_idempotent(self):
        plugin = FakePlugin(); now = dt.datetime(2026, 11, 2, 9)
        first = bf.create_due(plugin, now)
        second = bf.create_due(plugin, now)
        self.assertEqual(len(first), len(second))
        self.assertEqual({r["kind"] for r in first}, {"rent", "utilities"})

    def test_pay_and_overdue(self):
        import asyncio
        plugin = FakePlugin(); now = dt.datetime(2026, 11, 2, 9)
        bf.create_due(plugin, now)
        out = asyncio.run(bf.pay(plugin, "rent", now))
        self.assertTrue(out["ok"])
        self.assertTrue(any("房租" in m for m in plugin.finance.mem))
        later = now + dt.timedelta(hours=25)
        res = asyncio.run(bf.overdue_tick(plugin, later))
        self.assertEqual(res["stage"], 1)
        self.assertTrue(any("滞纳金" in m for m in plugin.finance.mem))

if __name__ == "__main__":
    unittest.main()
