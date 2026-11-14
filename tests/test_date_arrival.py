# -*- coding: utf-8 -*-
"""约会等待/到达/中断/结束：短肯定词前置拦截与状态流转。"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import date_engine as de  # noqa: E402
from core.llm_tools import confirm_arrival_tool  # noqa: E402


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeHousing:
    def __init__(self):
        self.furniture = []

    def add_furniture(self, name):
        self.furniture.append(name)
        return {"ok": True, "big": True, "redraw": False}


class FakeSchedule:
    def __init__(self):
        self.items = []

    def add_schedule_item(self, day, item, **kwargs):
        self.items.append((day, item))


class FakeAmo:
    def __init__(self):
        self.date_now = ""
        self.date_fact = ""
        self.current_action = ""
        self.last_event = None
        self.housing = FakeHousing()
        self.state = type("State", (), {"add_schedule_item": lambda self, day, item, **kw: None})()
        self.pushed = []

    def add_hunger(self, *a, **k):
        pass

    def set_date_mood(self, line, until=0):
        pass

    def set_date_now(self, line, until=0):
        self.date_now = line

    def set_date_fact(self, line, until=0):
        self.date_fact = line

    def set_current_action(self, action, until=0, **kwargs):
        self.current_action = action

    def set_last_date_event(self, activity, ended_ts=0):
        self.last_event = activity

    def set_date_prep(self, *a, **k):
        pass

    def set_date_incident(self, *a, **k):
        pass

    def add_closet_item(self, *a, **k):
        pass

    async def push_to_owner(self, text):
        self.pushed.append(text)


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo
        self.moods = []
        self.diaries = []
        self.memories = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return 500000

    async def expense(self, *a, **k):
        return {"ok": True}

    def nudge_mood(self, delta, idem):
        self.moods.append(delta)

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)


class FakePlugin:
    def __init__(self):
        self.finance_repo = FakeRepo()
        self.amo = FakeAmo()
        self.finance = FakeFinance(self.amo)


NOW = dt.datetime(2026, 11, 14, 18, 0)


def seed_pending(plugin, when=NOW):
    de._save(plugin, [{"title": "逛街", "when": when.strftime("%Y-%m-%d %H:%M"),
                       "deposit": 0, "status": "pending", "created": when.timestamp()}])


class ArrivalTests(unittest.TestCase):
    def test_short_affirmatives(self):
        for text in ("好", "行", "到了", "我来了", "马上到", "等我", "出发了", "在路上了"):
            self.assertTrue(de.is_arrival(text), text)
        self.assertFalse(de.is_arrival("今天天气不错，我们下午再看看吧"))
        self.assertFalse(de.is_arrival("好累"))
        self.assertFalse(de.is_arrival("好饿"))
        self.assertFalse(de.is_arrival(""))

    def test_attend_on_short_yes(self):
        p = FakePlugin()
        seed_pending(p)
        out = asyncio.run(de.handle_date_text(p, "好", now=NOW))
        self.assertTrue(out["handled"])
        self.assertEqual(out["action"], "attend")
        self.assertEqual(de.active_date(p)["status"], "dating")
        self.assertEqual(p.amo.current_action, "正在和老公约会")
        self.assertIn("到你面前", p.amo.date_now)
        self.assertIsNone(de.find_pending(p, ""))

    def test_attend_on_arrived(self):
        p = FakePlugin()
        seed_pending(p)
        out = asyncio.run(de.handle_date_text(p, "到了", now=NOW))
        self.assertTrue(out["handled"])
        self.assertIn("终于来啦", out["message"])
        self.assertFalse(de.find_pending(p, ""))

    def test_interrupt_after_arrival(self):
        p = FakePlugin()
        seed_pending(p)
        asyncio.run(de.handle_date_text(p, "到了", now=NOW))
        out = asyncio.run(de.handle_date_text(
            p, "老婆对不起，老板突然叫我回去加班，我得走了", now=NOW + dt.timedelta(minutes=10)))
        self.assertTrue(out["handled"])
        self.assertEqual(out["action"], "interrupt")
        self.assertIsNone(de.active_date(p))
        self.assertTrue(any("委屈" in delta for delta in p.finance.moods))
        self.assertTrue(any("刚碰面" in line for line in p.finance.diaries))
        self.assertEqual(p.amo.current_action, "")

    def test_user_ends_date(self):
        p = FakePlugin()
        seed_pending(p)
        asyncio.run(de.handle_date_text(p, "到了", now=NOW))
        out = asyncio.run(de.handle_date_text(p, "我回家了", now=NOW + dt.timedelta(hours=1)))
        self.assertTrue(out["handled"])
        self.assertEqual(out["action"], "finish")
        self.assertIsNone(de.active_date(p))
        self.assertEqual(p.amo.last_event, "逛街")

    def test_tick_auto_finishes(self):
        p = FakePlugin()
        seed_pending(p)
        asyncio.run(de.handle_date_text(p, "到了", now=NOW))
        active = de._active(p)
        active["end"] = NOW.timestamp() - 1
        de._set_active(p, active)
        asyncio.run(de.tick(p, now=NOW + dt.timedelta(hours=3)))
        self.assertIsNone(de.active_date(p))

    def test_confirm_arrival_tool(self):
        p = FakePlugin()
        seed_pending(p)
        msg = asyncio.run(confirm_arrival_tool(p, "逛街"))
        self.assertIn("终于来啦", msg)
        self.assertEqual(de.active_date(p)["status"], "dating")


if __name__ == "__main__":
    unittest.main()
