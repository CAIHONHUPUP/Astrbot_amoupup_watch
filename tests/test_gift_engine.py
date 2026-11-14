# -*- coding: utf-8 -*-
"""人情礼节：父母生日礼物档位、没钱内疚、份子钱、转账尽孝。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import gift_engine as gift  # noqa: E402


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeAmo:
    def __init__(self):
        self.lines = []

    def note_gift_event(self, line, until_ts=0):
        self.lines.append(line)


class FakeFinance:
    def __init__(self, amo, cash=100000):
        self.amo = amo
        self.cash = cash
        self.spent = []
        self.diaries = []
        self.memories = []
        self.moods = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, reason, idem, necessary=False):
        self.spent.append((amount, reason))
        return {"ok": True}

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)


class FakePlugin:
    def __init__(self, cash=100000):
        self.finance_repo = FakeRepo()
        self.finance_repo.meta_set("npc_birthdays", __import__("json").dumps({
            "妈妈": "03-12", "爸爸": "09-08", "柳如烟": "06-18"}))
        self.amo = FakeAmo()
        self.finance = FakeFinance(self.amo, cash)


class GiftTests(unittest.TestCase):
    def test_rich_parent_gets_gold(self):
        plugin = FakePlugin(100000)
        asyncio.run(gift.tick(plugin, dt.datetime(2026, 3, 12, 9, 0)))
        self.assertTrue(any("金手镯" in r for _, r in plugin.finance.spent))
        self.assertTrue(plugin.finance.memories)

    def test_poor_parent_gets_cake(self):
        plugin = FakePlugin(20000)
        asyncio.run(gift.tick(plugin, dt.datetime(2026, 3, 12, 9, 0)))
        self.assertTrue(any("水果" in r for _, r in plugin.finance.spent))

    def test_broke_does_not_spend(self):
        plugin = FakePlugin(1000)
        out = asyncio.run(gift.tick(plugin, dt.datetime(2026, 3, 12, 9, 0)))
        self.assertEqual(plugin.finance.spent, [])
        self.assertIn("妈妈", out.get("broke"))

    def test_yearly_once(self):
        plugin = FakePlugin(100000)
        asyncio.run(gift.tick(plugin, dt.datetime(2026, 3, 12, 9, 0)))
        asyncio.run(gift.tick(plugin, dt.datetime(2026, 3, 12, 10, 0)))
        gifts = [x for x in plugin.finance.spent if "礼物" in x[1]]
        self.assertEqual(len(gifts), 1)

    def test_birthday_persisted_and_label(self):
        plugin = FakePlugin()
        info = gift.birthday_info(plugin, "妈妈", dt.datetime(2026, 3, 10, 9, 0))
        self.assertEqual(info["birthday"], "03-12")
        self.assertEqual(info["days"], 2)
        self.assertIn("3月12日", info["text"])
        first = plugin.finance_repo.meta_get("npc_birthdays")
        gift._birthdays(plugin)
        self.assertEqual(first, plugin.finance_repo.meta_get("npc_birthdays"))

    def test_transfer_for_mother(self):
        plugin = FakePlugin(0)
        out = asyncio.run(gift.maybe_transfer(plugin, "[转账]1000 给咱妈买点好的"))
        self.assertTrue(out["ok"])
        self.assertTrue(any("尽孝" in line for line in plugin.amo.lines))
        self.assertTrue(plugin.finance.memories)


if __name__ == "__main__":
    unittest.main()
