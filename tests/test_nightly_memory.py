# -*- coding: utf-8 -*-
"""凌晨记忆提炼：只在 03:00、失败/空不归档、成功才写回。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import nightly_memory as nm  # noqa: E402


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value


class FakeData:
    def __init__(self, rows):
        self.rows = rows
        self.applied = []

    def nightly_memory_candidates(self, hours=24, limit=300):
        return list(self.rows)

    def nightly_apply(self, meta, facts, ids):
        self.applied.append((meta, facts, ids))
        return {"written": len(facts), "archived": len(ids)}


class FakeResponse:
    def __init__(self, text):
        self.completion_text = text


class FakeProvider:
    def __init__(self, text="", error=None):
        self.text = text
        self.error = error
        self.calls = 0

    async def text_chat(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return FakeResponse(self.text)


class FakeMeta:
    def __init__(self, star):
        self.star_cls = star


class FakeContext:
    def __init__(self, data):
        self.data = data

    def get_registered_star(self, name):
        return FakeMeta(self.data) if self.data is not None else None


class FakePlugin:
    def __init__(self, data, provider):
        self.context = FakeContext(data)
        self.finance_repo = FakeRepo()
        self._provider_impl = provider

    async def _provider(self):
        return self._provider_impl


def rows(uid="u1"):
    return [{"id": 1, "bot_id": "bot", "uid": uid, "scope": "private", "group_id": "",
             "subject": "user", "content": "爱吃螺蛳粉"},
            {"id": 2, "bot_id": "bot", "uid": uid, "scope": "private", "group_id": "",
             "subject": "user", "content": "螺蛳粉真香"}]


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider("喜欢吃螺蛳粉\n喜欢猫")
        self.data = FakeData(rows())
        self.plugin = FakePlugin(self.data, self.provider)

    def run_at(self, when):
        async def fake_provider(plugin):
            return self.provider
        old = nm._provider
        nm._provider = fake_provider
        try:
            return asyncio.run(nm.nightly_tick(self.plugin, now=when))
        finally:
            nm._provider = old

    def test_runs_at_three_and_writes(self):
        out = self.run_at(dt.datetime(2026, 11, 15, 3, 0))
        self.assertTrue(out["ok"])
        self.assertEqual(out["written"], 2)
        self.assertTrue(self.data.applied)
        meta, facts, ids = self.data.applied[0]
        self.assertEqual(meta["uid"], "u1")
        self.assertIn("喜欢吃螺蛳粉", facts)
        self.assertEqual(self.plugin.finance_repo.meta_get("nightly_memory_day"), "2026-11-15")

    def test_empty_llm_keeps_streams(self):
        self.provider.text = ""
        out = self.run_at(dt.datetime(2026, 11, 15, 3, 0))
        self.assertFalse(out["ok"])
        self.assertFalse(self.data.applied)
        self.assertEqual(self.plugin.finance_repo.meta_get("nightly_memory_day"), "2026-11-15")

    def test_failure_does_not_archive(self):
        self.provider.error = TimeoutError("timeout")
        out = self.run_at(dt.datetime(2026, 11, 15, 3, 0))
        self.assertFalse(out["ok"])
        self.assertFalse(self.data.applied)
        self.assertEqual(self.plugin.finance_repo.meta_get("nightly_memory_day"), "2026-11-15")

    def test_not_three_am_skips(self):
        out = self.run_at(dt.datetime(2026, 11, 15, 12, 0))
        self.assertEqual(out["reason"], "not_night")
        self.assertEqual(self.provider.calls, 0)

    def test_once_per_day(self):
        self.run_at(dt.datetime(2026, 11, 15, 3, 0))
        out = self.run_at(dt.datetime(2026, 11, 15, 3, 1))
        self.assertEqual(out["reason"], "done")

    def test_parse_filters_bullets_and_duplicates(self):
        self.assertEqual(nm._parse_facts("- 喜欢猫\n- 喜欢猫\n无"), ["喜欢猫"])


if __name__ == "__main__":
    unittest.main()
