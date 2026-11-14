# -*- coding: utf-8 -*-
"""约会与失信：约定、取消情绪、定金、安慰外卖、冷淡与补偿。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import date_engine  # noqa: E402

NOW = dt.datetime(2026, 11, 14, 12, 0)


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeState:
    def __init__(self):
        self.items = []

    def add_schedule_item(self, day, item, source="", reason=""):
        self.items.append((day, item))
        return {"changed": True}


class FakeAmo:
    def __init__(self):
        self.state = FakeState()
        self.moods = []
        self.added = []

    def set_date_mood(self, line, until_ts=0):
        self.moods.append((line, until_ts))

    def add_hunger(self, target, amount):
        self.added.append(amount)


class FakeFinance:
    def __init__(self, cash=20000):
        self.cash = cash
        self.moods = []
        self.diaries = []
        self.memories = []
        self.spent = []
        self.amo = FakeAmo()

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.cash

    def nudge_mood(self, deltas, idem):
        self.moods.append((deltas, idem))

    async def expense(self, amount, reason, idem, necessary=False):
        self.spent.append((amount, reason))
        return {"ok": True}

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)


class FakePlugin:
    def __init__(self, cash=20000):
        self.finance_repo = FakeRepo()
        self.finance = FakeFinance(cash)


class DateEngineTests(unittest.TestCase):
    def test_make_date_expects(self):
        plugin = FakePlugin()
        out = asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 18:00", now=NOW))
        self.assertTrue(out["ok"])
        self.assertEqual(len(plugin.finance.amo.state.items), 1)
        self.assertEqual(plugin.finance.moods[0][0], {"开心": 10})
        self.assertIn("期待", plugin.finance.amo.moods[0][0])

    def test_cancel_early_is_mild(self):
        plugin = FakePlugin()
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-15 18:00", now=NOW))
        out = asyncio.run(date_engine.cancel_date(plugin, "火锅", now=NOW))
        self.assertFalse(out["late"])
        self.assertEqual(out["comfort"], 0)
        self.assertIn("下次再去", out["message"])
        self.assertTrue(plugin.finance.memories)

    def test_cancel_late_loses_deposit_and_comfort_eats(self):
        plugin = FakePlugin(cash=20000)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 16:00", 5000, now=NOW))
        out = asyncio.run(date_engine.cancel_date(plugin, "火锅", now=NOW))
        self.assertTrue(out["late"])
        self.assertEqual(out["deposit_lost"], 5000)
        self.assertGreaterEqual(out["comfort"], 5000)
        self.assertIn("放我鸽子", out["message"])
        self.assertTrue(any("定金" in d for d in plugin.finance.diaries))
        self.assertTrue(any("外卖" in d for d in plugin.finance.diaries))
        self.assertTrue(any("冷淡" in m[0] for m in plugin.finance.amo.moods))

    def test_cancel_broke(self):
        plugin = FakePlugin(cash=0)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 18:00", now=NOW))
        out = asyncio.run(date_engine.cancel_date(plugin, "火锅", now=NOW))
        self.assertTrue(out["broke"])
        self.assertIn("泡面", out["message"])
        self.assertTrue(any("泡面" in d for d in plugin.finance.diaries))

    def test_transfer_compensation(self):
        plugin = FakePlugin(cash=0)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 18:00", now=NOW))
        asyncio.run(date_engine.cancel_date(plugin, "火锅", now=NOW))
        out = asyncio.run(date_engine.maybe_compensate(plugin, "[转账]200 你自己去吃"))
        self.assertTrue(out["ok"])
        self.assertIn("原谅", out["message"])
        self.assertTrue(any("转账" in m for m in plugin.finance.memories))
        self.assertEqual(plugin.finance.amo.moods[-1][0], "")

    def test_attend_host_with_fund(self):
        plugin = FakePlugin(cash=50000)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 18:00", now=NOW))
        asyncio.run(date_engine.note_transfer(plugin, "[转账]300 火锅基金"))
        out = asyncio.run(date_engine.attend_date(plugin, "火锅", now=NOW))
        self.assertTrue(out["ok"])
        self.assertEqual(out["payer"], "host")
        self.assertEqual(plugin.finance.amo.added, [100])
        self.assertIn("约会", plugin.finance.amo.moods[-1][0])
        self.assertTrue(plugin.finance_repo.meta_get("date:active"))

    def test_attend_her_treat_missing_fund(self):
        plugin = FakePlugin(cash=50000)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 18:00", now=NOW))
        out = asyncio.run(date_engine.attend_date(plugin, "火锅", now=NOW))
        self.assertEqual(out["payer"], "her")
        self.assertTrue(any("肉疼" in d for d in plugin.finance.diaries))

    def test_attend_proud_host_mode(self):
        plugin = FakePlugin(cash=50000)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 18:00", now=NOW))
        out = asyncio.run(date_engine.attend_date(plugin, "火锅", "我请客", now=NOW))
        self.assertEqual(out["payer"], "her")
        self.assertTrue(any("骄傲" in d for d in plugin.finance.diaries))

    def test_attend_aa(self):
        plugin = FakePlugin(cash=50000)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 18:00", now=NOW))
        out = asyncio.run(date_engine.attend_date(plugin, "火锅", "AA", now=NOW))
        self.assertEqual(out["payer"], "aa")
        bills = [x for x in plugin.finance.spent if x[1].startswith("约会：")]
        self.assertEqual(bills[0][0], 17500)

    def test_finish_writes_long_diary_and_memory(self):
        plugin = FakePlugin(cash=50000)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 12:30", now=NOW))
        asyncio.run(date_engine.attend_date(plugin, "火锅", now=NOW))
        out = asyncio.run(date_engine.finish_date(plugin, NOW + dt.timedelta(hours=3)))
        self.assertTrue(out["ok"])
        self.assertFalse(date_engine._active(plugin))
        self.assertTrue(any("最开心" in m for m in plugin.finance.memories))

    def test_missed_date_triggers_sadness(self):
        plugin = FakePlugin(cash=50000)
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 08:00", now=NOW))
        asyncio.run(date_engine.tick(plugin, now=NOW))
        rows = date_engine._load(plugin)
        self.assertEqual(rows[-1]["status"], "missed")
        self.assertTrue(any("忘了" in m for m in plugin.finance.memories))
        self.assertIn("冷淡", plugin.finance.amo.moods[-1][0])

    def test_duplicate_same_activity_within_4h_ignored(self):
        plugin = FakePlugin()
        first = asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 12:00", now=NOW))
        second = asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 13:00", now=NOW))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertTrue(second.get("duplicate"))
        rows = [r for r in date_engine._load(plugin) if r.get("status") == "pending"]
        self.assertEqual(len(rows), 1)

    def test_propose_date_tool_twice_one_record(self):
        from core.llm_tools import propose_date_tool
        plugin = FakePlugin()
        asyncio.run(propose_date_tool(plugin, "吃火锅", "中午"))
        second = asyncio.run(propose_date_tool(plugin, "吃火锅", "中午"))
        rows = [r for r in date_engine._load(plugin) if r.get("status") == "pending"]
        self.assertEqual(len(rows), 1)
        self.assertIn("约好", second)

    def test_duplicate_other_activity_allowed(self):
        plugin = FakePlugin()
        asyncio.run(date_engine.make_date(plugin, "吃火锅", "2026-11-14 12:00", now=NOW))
        other = asyncio.run(date_engine.make_date(plugin, "看电影", "2026-11-14 12:00", now=NOW))
        self.assertTrue(other["ok"])


if __name__ == "__main__":
    unittest.main()
