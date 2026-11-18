# -*- coding: utf-8 -*-
"""赴约结束：写日期锁、疲劳/饥饿、回家事实、战利品、[共同记忆]。"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import date_engine as de  # noqa: E402


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeState:
    def __init__(self):
        self.added = []

    def add_schedule_item(self, day, item, source="", reason=""):
        self.added.append(dict(item))


class FakeHousing:
    def __init__(self):
        self.furniture = []

    def add_furniture(self, name):
        self.furniture.append(name)
        return {"ok": True, "big": False}


class FakeAmo:
    def __init__(self):
        self.last_event = None
        self.prep = None
        self.incident = None
        self.facts = []
        self.closet = []
        self.housing = FakeHousing()
        self.state = FakeState()
        self.ending = None
        self.home = None
        self.action = None

    def set_date_mood(self, line, until=0):
        pass

    def set_last_date_event(self, activity, ended_ts=0):
        self.last_event = (activity, ended_ts)

    def set_date_prep(self, activity, start_ts=0):
        self.prep = (activity, start_ts)

    def set_date_incident(self, text, until_ts=0):
        self.incident = text

    def set_date_fact(self, line, until_ts=0):
        self.facts.append(line)

    def set_date_now(self, line, until_ts=0):
        pass

    def set_current_action(self, action, until_ts=0, **kwargs):
        self.action = action

    def set_date_ending(self, fact, reason=""):
        self.ending = (fact, reason)

    def set_date_home(self, title, at=0):
        self.home = (title, at)

    def add_closet_item(self, item):
        self.closet.append(item)


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


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.p = FakePlugin()
        self.now = dt.datetime(2026, 11, 14, 21, 0)

    def test_finish_writes_lock_fatigue_home_and_loot(self):
        de._set_active(self.p, {"title": "逛街", "start": self.now.timestamp() - 7200,
                                "end": self.now.timestamp(), "payer": "host"})
        with mock.patch.object(de.random, "random", return_value=0.1), \
                mock.patch.object(de.random, "choice", side_effect=lambda seq: seq[0]):
            out = asyncio.run(de.finish_date(self.p, self.now))
        self.assertTrue(out["ok"])
        self.assertEqual(self.p.amo.last_event[0], "逛街")
        self.assertIsNone(de._active(self.p).get("title"))
        self.assertTrue(any("睡衣" in fact and "卸了妆" in fact for fact in self.p.amo.facts))
        self.assertTrue(self.p.amo.closet)
        self.assertTrue(any(delta.get("疲惫") == 15 for delta in self.p.finance.moods))
        self.assertTrue(self.p.finance.memories[0].startswith("[共同记忆]"))

    def test_finish_schedules_alone_home_not_together(self):
        """B 方案：约会结束他不在身边，路上一个人、家只发消息/视频。"""
        de._set_active(self.p, {"title": "逛街", "start": self.now.timestamp() - 7200,
                                "end": self.now.timestamp(), "payer": "host"})
        asyncio.run(de.finish_date(self.p, self.now, reason="user_done"))
        joined = "\n".join(self.p.amo.facts)
        self.assertIn("他不在你身边", joined)
        self.assertIn("手机/视频", joined)
        for banned in ("一起回家", "共同的家", "住在一起", "同居"):
            self.assertNotIn(banned, joined)
        acts = [str(x.get("activity") or "") for x in self.p.amo.state.added]
        self.assertTrue(any("一个人" in a and "耳机" in a for a in acts), acts)
        self.assertTrue(any("给他发消息" in a or "视频" in a for a in acts), acts)
        self.assertFalse(any("一起回家" in a or "瘫在沙发上" in a for a in acts), acts)
        self.assertEqual(self.p.amo.ending[1], "user_done")
        self.assertIn("他不在你身边", self.p.amo.ending[0])
        self.assertNotIn("一起回家", self.p.amo.ending[0])
        self.assertIn("一个人到家", self.p.amo.action)

    def test_finish_without_active_returns_none(self):
        self.assertIsNone(asyncio.run(de.finish_date(self.p, self.now)))


if __name__ == "__main__":
    unittest.main()
