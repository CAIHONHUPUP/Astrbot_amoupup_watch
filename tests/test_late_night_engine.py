# -*- coding: utf-8 -*-
"""熬夜后果：夜宵冲动、迟到罚款、转账哄睡清空。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import late_night_engine as late  # noqa: E402
from utils.helpers import stable_int  # noqa: E402


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeAmo:
    def __init__(self, active=True, late_yesterday=True, hunger=20):
        self.hunger = hunger
        self.state = {"active": active, "late_yesterday": late_yesterday,
                      "cleared": False, "fined": False}
        self.snacks = []
        self.fines = []
        self.soothed = []
        self.added = []

    def late_night_state(self):
        return dict(self.state)

    def get_hunger(self):
        return self.hunger

    def add_hunger(self, target, amount):
        self.added.append(amount)

    def note_late_snack(self, amount, food=""):
        self.snacks.append((amount, food))

    def note_late_fine(self, amount):
        self.fines.append(amount)

    def soothe_late_night(self, reason=""):
        self.soothed.append(reason)


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo
        self.cash = 20000
        self.spent = []
        self.diaries = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, reason, idem, necessary=False):
        self.spent.append((amount, reason))
        return {"ok": True}

    def small_diary(self, text):
        self.diaries.append(text)

    def nudge_mood(self, deltas, idem):
        pass


class FakePlugin:
    def __init__(self, active=True, late_yesterday=True, hunger=20):
        self.finance_repo = FakeRepo()
        self.amo = FakeAmo(active, late_yesterday, hunger)
        self.finance = FakeFinance(self.amo)


class LateNightWatchTests(unittest.TestCase):
    def test_snack_at_1am(self):
        plugin = FakePlugin()
        asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 1, 10)))
        self.assertTrue(plugin.finance.spent)
        self.assertEqual(len(plugin.amo.snacks), 1)
        self.assertTrue(any("熬夜" in d for d in plugin.finance.diaries))

    def test_no_snack_when_full(self):
        plugin = FakePlugin(hunger=80)
        asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 1, 10)))
        self.assertEqual(plugin.finance.spent, [])

    def test_late_fine_on_workday(self):
        plugin = FakePlugin()
        day = None
        for i in range(14, 30):
            d = dt.date(2026, 11, i)
            if d.weekday() < 5 and stable_int(d.isoformat(), 0, 100) < 60:
                day = d
                break
        self.assertIsNotNone(day)
        asyncio.run(late.tick(plugin, dt.datetime.combine(day, dt.time(9, 0))))
        self.assertTrue(plugin.amo.fines)
        self.assertTrue(any("迟到" in d for d in plugin.finance.diaries))

    def test_transfer_soothes(self):
        plugin = FakePlugin()
        out = asyncio.run(late.maybe_soothe(plugin, "[转账]50 买个夜宵早点睡"))
        self.assertTrue(out["ok"])
        self.assertEqual(plugin.amo.soothed[-1], "夜宵转账")


if __name__ == "__main__":
    unittest.main()
