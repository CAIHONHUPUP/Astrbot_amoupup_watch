# -*- coding: utf-8 -*-
"""房产决策工具：自主出租/出售闲置自有房，UI 不提供按钮。"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.llm_tools import rent_out_property_tool, sell_property_tool  # noqa: E402


class FakeHousing:
    def __init__(self):
        self.homes = [
            {"id": "h1", "name": "自住小家", "kind": "own", "status": "living", "rent_cents": 0},
            {"id": "h2", "name": "闲置公寓", "kind": "own", "status": "idle", "rent_cents": 0},
            {"id": "h3", "name": "出租屋", "kind": "rent", "status": "living", "rent_cents": 150000},
        ]
        self.rented = []
        self.sold = []

    def current(self):
        return self.homes[0]

    def rent_out(self, ref, cents=0):
        self.rented.append((ref, cents))
        return True, "已把「闲置公寓」出租，每月收租 1500 元"

    def sell_home(self, ref):
        self.sold.append(ref)
        return True, "已卖掉「闲置公寓」"


class FakeFinance:
    def __init__(self, housing):
        self.housing = housing

    def _amo(self):
        return type("Amo", (), {"housing": self.housing})()


class FakePlugin:
    def __init__(self, housing):
        self.finance = FakeFinance(housing)


class HousingToolTests(unittest.TestCase):
    def test_rent_out_idle_own_home(self):
        housing = FakeHousing()
        p = FakePlugin(housing)
        msg = asyncio.run(rent_out_property_tool(p))
        self.assertIn("出租", msg)
        self.assertEqual(housing.rented[0][0], "h2")
        self.assertEqual(housing.rented[0][1], 150000)

    def test_rent_out_named(self):
        housing = FakeHousing()
        p = FakePlugin(housing)
        asyncio.run(rent_out_property_tool(p, "闲置公寓", 1800))
        self.assertEqual(housing.rented[0][1], 180000)

    def test_sell_idle_own_home(self):
        housing = FakeHousing()
        p = FakePlugin(housing)
        msg = asyncio.run(sell_property_tool(p))
        self.assertIn("卖掉", msg)
        self.assertEqual(housing.sold, ["h2"])

    def test_no_idle_home(self):
        housing = FakeHousing()
        housing.homes = [housing.homes[2]]
        p = FakePlugin(housing)
        msg = asyncio.run(sell_property_tool(p))
        self.assertIn("没有可出售", msg)
        self.assertFalse(housing.sold)

    def test_ui_has_no_action_buttons(self):
        source = open(os.path.join(ROOT, "views", "守护", "index.html"), encoding="utf-8").read()
        self.assertNotIn("data-house-action", source)
        self.assertNotIn("housing/move", source)


if __name__ == "__main__":
    unittest.main()
