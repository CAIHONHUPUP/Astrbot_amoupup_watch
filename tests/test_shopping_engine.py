# -*- coding: utf-8 -*-
"""逛街：余额档位、交通奶茶午饭、衣服同步衣橱、只逛不买。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import shopping_engine as shop  # noqa: E402


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeAmo:
    def __init__(self, reason="周末逛街"):
        self.reason = reason
        self.items = []
        self.pushed = []

    def shopping_trigger(self, now=None):
        return self.reason

    def add_closet_item(self, item):
        self.items.append(dict(item))
        return {"ok": True, "item": item}

    async def push_to_owner(self, text):
        self.pushed.append(text)
        return True


class FakeFinance:
    def __init__(self, amo, cash=500000):
        self.amo = amo
        self.cash = cash
        self.spent = []
        self.diaries = []
        self.memories = []

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


class FakePlugin:
    def __init__(self, cash=500000, reason="周末逛街"):
        self.finance_repo = FakeRepo()
        self.amo = FakeAmo(reason)
        self.finance = FakeFinance(self.amo, cash)


class ShoppingTests(unittest.TestCase):
    def run_shop(self, cash=500000, reason="周末逛街"):
        plugin = FakePlugin(cash, reason)
        out = asyncio.run(shop.tick(plugin, dt.datetime(2026, 11, 14, 14, 0)))
        return plugin, out

    def test_rich_buys_mall_item(self):
        plugin, out = self.run_shop()
        self.assertIsNotNone(out)
        self.assertEqual(plugin.amo.items, [])
        self.assertTrue(plugin.finance_repo.meta_get("shop:pending"))
        self.assertTrue(any("试了一件" in t and "能买吗" in t for t in plugin.amo.pushed))

    def test_middle_buys_cheap(self):
        plugin, out = self.run_shop(cash=100000)
        self.assertEqual(plugin.amo.items, [])
        self.assertTrue(plugin.finance_repo.meta_get("shop:pending"))

    def test_low_balance_window_shopping(self):
        plugin, out = self.run_shop(cash=15000)
        self.assertIsNotNone(out)
        self.assertIsNone(out["item"])
        self.assertFalse(plugin.amo.items)
        self.assertTrue(any("只逛不买" in d for d in plugin.finance.diaries))

    def test_tryon_and_home_proactive(self):
        plugin, _ = self.run_shop()
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 31)))
        self.assertTrue(any("试了这件" in t for t in plugin.amo.pushed))
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 17, 1)))
        self.assertTrue(any("我回来啦" in t for t in plugin.amo.pushed))

    def test_guilt_when_big_spend_and_low_cash(self):
        plugin = FakePlugin(cash=100000)
        plugin.finance_repo.meta_set("shop:events", __import__("json").dumps([
            {"due": 1, "kind": "home", "name": "毛呢大衣", "price": 600, "cash": 1500}]))
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 0)))
        self.assertTrue(any("你不会骂我吧" in t for t in plugin.amo.pushed))

    def test_accept_reply_settles(self):
        plugin, _ = self.run_shop()
        out = asyncio.run(shop.handle_reply(plugin, "买！好看"))
        self.assertTrue(out["bought"])
        self.assertEqual(len(plugin.amo.items), 1)
        self.assertTrue(any("买衣服" in r for _, r in plugin.finance.spent))

    def test_reject_reply_clears(self):
        plugin, _ = self.run_shop()
        out = asyncio.run(shop.handle_reply(plugin, "太贵了，不好看"))
        self.assertFalse(out["bought"])
        self.assertEqual(plugin.amo.items, [])
        self.assertFalse(plugin.finance_repo.meta_get("shop:pending"))

    def test_timeout_returns_to_rack(self):
        plugin, _ = self.run_shop()
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 16, 0)))
        self.assertTrue(any("放回衣架" in t for t in plugin.amo.pushed))

    def test_no_trigger_no_shopping(self):
        plugin = FakePlugin(reason="")
        out = asyncio.run(shop.tick(plugin, dt.datetime(2026, 11, 14, 14, 0)))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
