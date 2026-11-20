# -*- coding: utf-8 -*-
"""一个饭点只允许一场约会（2026-11-20）。

现场：19:09 已和柳如烟约火锅，20:39 又凭空多一场「和老公吃饭」。
"""
import asyncio
import datetime as dt
import unittest
from types import SimpleNamespace

import services.date_engine as date_engine


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeBridge:
    def __init__(self, rows):
        self.ready = True
        self.rows = rows
        self.calls = []

    def schedule(self):
        return {"2026-11-20": list(self.rows)}

    def call(self, name, *args, **kwargs):
        self.calls.append(name)
        return SimpleNamespace(ok=True, value=None)


class FakePlugin:
    def __init__(self):
        self.finance_repo = FakeRepo()


def row(start, end, activity, category="social"):
    return {"start": start, "end": end, "activity": activity, "category": category}


NOW = dt.datetime(2026, 11, 20, 19, 20)


class DateMealConflictTests(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()
        self._orig = date_engine._bridge

    def tearDown(self):
        date_engine._bridge = self._orig

    def _run(self, bridge, title, when):
        date_engine._bridge = lambda *a, **k: bridge
        return asyncio.run(date_engine.make_date(self.plugin, title, when, now=NOW))

    def test_second_dinner_is_refused(self):
        bridge = FakeBridge([row("19:09", "20:00", "柳如烟下班吃火锅")])
        out = self._run(bridge, "吃饭", "2026-11-20 20:39")
        self.assertFalse(out["ok"])
        self.assertEqual("meal_conflict", out["reason"])
        self.assertIn("火锅", out["message"])

    def test_non_meal_date_still_allowed(self):
        bridge = FakeBridge([row("19:09", "20:00", "柳如烟下班吃火锅")])
        out = self._run(bridge, "看电影", "2026-11-20 20:39")
        self.assertTrue(out["ok"])

    def test_other_slot_allowed(self):
        bridge = FakeBridge([row("11:30", "13:00", "同事聚餐")])
        out = self._run(bridge, "吃火锅", "2026-11-20 19:30")
        self.assertTrue(out["ok"])

    def test_shopping_block_does_not_block_dinner(self):
        bridge = FakeBridge([row("19:00", "20:00", "和柳如烟逛街")])
        out = self._run(bridge, "吃火锅", "2026-11-20 19:30")
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
