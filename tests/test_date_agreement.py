# -*- coding: utf-8 -*-
"""她提「该回家了」、他回短肯定词 → 约会收尾并生成回家日程。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

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


class FakeDB:
    def __init__(self):
        self.kv = {}

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


class FakeState:
    def __init__(self):
        self.schedule = {}
        self.added = []
        self.commits = []

    def add_schedule_item(self, day, item, source="", reason=""):
        self.added.append(dict(item))
        self.schedule.setdefault(day, []).append(dict(item))

    def commit_schedule_change(self, day, items, old_items=None, source="", reason=""):
        self.commits.append((day, items, source, reason))
        self.schedule[day] = [dict(x) for x in items]


class FakeAmo:
    def __init__(self):
        self.db = FakeDB()
        self.state = FakeState()
        self.facts = []
        self.last_event = None
        self.prep = None
        self.incident = None

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
        pass


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo
        self.diaries = []
        self.memories = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return 500000

    async def expense(self, *a, **k):
        return {"ok": True}

    def nudge_mood(self, delta, idem):
        pass

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)


class FakePlugin:
    def __init__(self):
        self.finance_repo = FakeRepo()
        self.amo = FakeAmo()
        self.finance = FakeFinance(self.amo)


class AgreedEndTests(unittest.TestCase):
    def setUp(self):
        self.p = FakePlugin()
        self.now = dt.datetime(2026, 11, 14, 21, 0)
        de._set_active(self.p, {"title": "逛街", "status": "dating",
                                "start": self.now.timestamp() - 3600,
                                "end": self.now.timestamp() + 3600})

    def _sent_ending(self):
        self.p.amo.db.kv_set("date_ending", {"sent": True,
                                              "until": self.now.timestamp() + 600,
                                              "fact": "该回去了"})

    def test_short_agreement_finishes_date_and_writes_transit(self):
        self._sent_ending()
        out = asyncio.run(de.handle_date_text(self.p, "好", self.now))
        self.assertEqual(out.get("action"), "finish")
        self.assertIsNone(de.active_date(self.p))
        acts = [str(x.get("activity") or "") for x in self.p.amo.state.added]
        self.assertTrue(any("走回家" in a for a in acts), acts)
        self.assertTrue(any("到家" in a for a in acts), acts)
        self.assertTrue(self.p.amo.db.kv_get("date_ending", {}) == {})

    def test_finish_writes_alone_route_after_agreement(self):
        """同意收尾后也只能一个人回家：没有「一起回家」。"""
        self._sent_ending()
        asyncio.run(de.handle_date_text(self.p, "好", self.now))
        acts = [str(x.get("activity") or "") for x in self.p.amo.state.added]
        self.assertTrue(any("一个人" in a and "耳机" in a for a in acts), acts)
        self.assertTrue(any("给他发消息" in a or "视频" in a for a in acts), acts)
        self.assertFalse(any("一起回家" in a for a in acts), acts)

    def test_not_agreement_keeps_date(self):
        self._sent_ending()
        out = asyncio.run(de.handle_date_text(self.p, "好远啊", self.now))
        self.assertNotEqual(out.get("action"), "finish")
        self.assertIsNotNone(de.active_date(self.p))

    def test_agreement_without_her_proposal_keeps_date(self):
        out = asyncio.run(de.handle_date_text(self.p, "好", self.now))
        self.assertNotEqual(out.get("action"), "finish")
        self.assertIsNotNone(de.active_date(self.p))


if __name__ == "__main__":
    unittest.main()
