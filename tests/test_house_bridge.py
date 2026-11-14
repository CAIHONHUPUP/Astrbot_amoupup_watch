# -*- coding: utf-8 -*-
"""watch ↔ amo 房屋联动：买房写入/搬入、破产搬回出租屋。"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.amo_bridge import FinanceLink  # noqa: E402


class FakeHousing:
    def __init__(self):
        self.homes = []
        self.moved = []
        self.bankrupt = 0

    def add_home(self, name, kind="own", city="", style="简约风"):
        home = {"id": "h%d" % (len(self.homes) + 1), "name": name, "kind": kind}
        self.homes.append(home)
        return home

    def move_in(self, ref):
        self.moved.append(ref)

    def on_bankruptcy(self):
        self.bankrupt += 1

    def add_furniture(self, name):
        self.furniture = getattr(self, "furniture", []) + [name]
        return {"ok": True, "big": True, "redraw": True}


class FakeAmo:
    def __init__(self):
        self.housing = FakeHousing()
        self.spawned = []
        self.state = object()
        self.wallet = object()

    async def _ensure_home_images(self, force=False):
        return {}

    def _spawn(self, coro):
        coro.close()
        self.spawned.append(True)


class FakeMeta:
    def __init__(self, amo):
        self.star_cls = amo
        self.activated = True


class FakeContext:
    def __init__(self, amo):
        self.amo = amo

    def get_registered_star(self, name):
        return FakeMeta(self.amo)


class FakePlugin:
    def __init__(self, amo):
        self.context = FakeContext(amo)


class HouseBridgeTests(unittest.TestCase):
    def test_buy_house_adds_and_moves_in(self):
        amo = FakeAmo()
        link = FinanceLink(FakePlugin(amo))
        self.assertTrue(link.house_bought("房子（90 平）"))
        self.assertEqual(amo.housing.homes[0]["name"], "房子（90 平）")
        self.assertEqual(amo.housing.homes[0]["kind"], "own")
        self.assertEqual(amo.housing.moved, ["h1"])
        self.assertTrue(amo.spawned)

    def test_house_lost_moves_back_to_rent(self):
        amo = FakeAmo()
        link = FinanceLink(FakePlugin(amo))
        self.assertTrue(link.house_lost())
        self.assertEqual(amo.housing.bankrupt, 1)

    def test_furniture_bought_writes_and_regenerates(self):
        amo = FakeAmo()
        link = FinanceLink(FakePlugin(amo))
        self.assertTrue(link.furniture_bought("双人床"))
        self.assertEqual(amo.housing.furniture, ["双人床"])
        self.assertTrue(amo.spawned)

    def test_non_furniture_does_not_touch_house(self):
        amo = FakeAmo()
        link = FinanceLink(FakePlugin(amo))
        self.assertFalse(link.furniture_bought("米色雪纺衬衫"))
        self.assertFalse(hasattr(amo.housing, "furniture"))

    def test_no_amo_is_safe(self):
        link = FinanceLink(FakePlugin(None))
        self.assertFalse(link.house_bought("房子"))
        self.assertFalse(link.house_lost())


if __name__ == "__main__":
    unittest.main()
