# -*- coding: utf-8 -*-
"""Phase 2 契约测试：守护侧约会状态只通过「约会会话单一源」API。

ContractAmo 故意同时提供新会话 API 与旧的 set_date_now/set_date_home：
新 API 被调用时记账；万一代码改回直写旧接口，legacy_calls / db.writes 会暴露。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import date_engine as de  # noqa: E402
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance as SocialFinance  # noqa: E402
from tests.social_fakes import FakePlugin as SocialPlugin  # noqa: E402
from tests.social_fakes import FakeRepo as SocialRepo  # noqa: E402

SAT = dt.datetime(2026, 11, 14, 21, 0)


class RecordingDB:
    """记录所有 kv 写入：守护不该再自己写 date_now / date_home / date_ending。"""

    def __init__(self):
        self.kv = {}
        self.writes = []

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.writes.append(key)
        self.kv[key] = value


class FakeState:
    def __init__(self):
        self.schedule = {}
        self.added = []
        self.commits = []

    def current_activity(self):
        return {"place": "楼下小馆", "activity": "吃火锅", "zone": "restaurant"}

    def add_schedule_item(self, day, item, source="", reason=""):
        self.added.append(dict(item))

    def commit_schedule_change(self, day, items, old_items=None, source="", reason=""):
        self.commits.append((day, list(items), source, reason))
        self.schedule[day] = [dict(x) for x in items]


class ContractAmo:
    """带会话 API 的假 amo：只记账，不替守护写投影。"""

    def __init__(self):
        self.db = RecordingDB()
        self.state = FakeState()
        self.calls = []
        self.legacy_calls = []
        self.finish_args = None
        self.cancel_args = None
        self.pause_args = None
        self.ending = None
        self.digest = {"phase": "none"}

    # ---------- 会话单一源（新 API） ----------
    def finish_date_session(self, title="", reason="", now=None):
        self.calls.append("finish_date_session")
        self.finish_args = (title, reason, now)

    def cancel_date_session(self, reason="", now=None):
        self.calls.append("cancel_date_session")
        self.cancel_args = (reason, now)

    def set_date_pause(self, minutes=15):
        self.calls.append("set_date_pause")
        self.pause_args = minutes

    def clear_date_pause(self):
        self.calls.append("clear_date_pause")

    def date_ending(self, now=None):
        self.calls.append("date_ending")
        return self.ending

    def clear_date_ending(self):
        self.calls.append("clear_date_ending")

    def date_session(self, now=None):
        self.calls.append("date_session")
        return self.digest

    # ---------- 旧接口：被调用即说明没走会话 ----------
    def set_date_now(self, line, until_ts=0):
        self.legacy_calls.append("set_date_now")

    def set_date_home(self, title, at=0):
        self.legacy_calls.append("set_date_home")

    # ---------- 收尾流程里的其它接口 ----------
    def set_date_mood(self, *args, **kwargs):
        pass

    def set_last_date_event(self, *args, **kwargs):
        pass

    def set_date_prep(self, *args, **kwargs):
        pass

    def set_date_incident(self, *args, **kwargs):
        pass

    def set_date_fact(self, *args, **kwargs):
        pass

    def set_date_ending(self, fact, reason=""):
        self.calls.append("set_date_ending")

    def set_current_action(self, *args, **kwargs):
        pass

    def add_hunger(self, *args, **kwargs):
        pass


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo
        self.diaries = []
        self.memories = []
        self.moods = []

    def _amo(self):
        return self.amo

    def nudge_mood(self, *args, **kwargs):
        self.moods.append(args)

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)

    def mood(self):
        return {"word": "开心"}

    def fatigue(self):
        return 30


class FakePlugin:
    def __init__(self):
        self.amo = ContractAmo()
        self.finance = FakeFinance(self.amo)
        self.finance_repo = FakeRepo()


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


def _dating(plugin, now=SAT, title="逛街"):
    de._set_active(plugin, {"title": title, "status": "dating",
                            "start": now.timestamp() - 3600,
                            "end": now.timestamp() + 3600})


class FinishContractTests(unittest.TestCase):
    def setUp(self):
        self.p = FakePlugin()
        _dating(self.p)

    def test_finish_goes_through_session_api_only(self):
        out = asyncio.run(de.finish_date(self.p, SAT, reason="user_done"))
        self.assertTrue(out["ok"])
        self.assertIn("finish_date_session", self.p.amo.calls)
        self.assertEqual(self.p.amo.finish_args[0], "逛街")
        self.assertEqual(self.p.amo.finish_args[1], "user_done")
        self.assertEqual(self.p.amo.legacy_calls, [],
                         "守护不能直写 set_date_now / set_date_home")
        for key in ("date_now", "date_home"):
            self.assertNotIn(key, self.p.amo.db.writes, "守护不能直接写 %s" % key)

    def test_finish_still_truncates_schedule_and_clears_ghost(self):
        self.p.amo.state.schedule["2026-11-14"] = [
            {"start": "19:00", "end": "23:00", "activity": "和老公逛街", "category": "social"}]
        asyncio.run(de.finish_date(self.p, SAT, reason="user_done"))
        row = self.p.amo.state.schedule["2026-11-14"][0]
        self.assertEqual(row["end"], "21:00")
        self.assertTrue(self.p.amo.state.commits, "必须走 commit_schedule_change")
        self.assertIn("date_pause", self.p.amo.db.writes, "幽灵键仍要清")

    def test_interrupt_uses_cancel_api(self):
        asyncio.run(de.interrupt_date(self.p, "老板叫我回去加班", SAT))
        self.assertIn("cancel_date_session", self.p.amo.calls)
        self.assertEqual(self.p.amo.legacy_calls, [])

    def test_tick_pause_timeout_reads_session(self):
        self.p.amo.digest = {"phase": "paused", "paused": None}
        asyncio.run(de.tick(self.p, SAT))
        self.assertIn("date_session", self.p.amo.calls)
        self.assertIn("finish_date_session", self.p.amo.calls)
        self.assertEqual(self.p.amo.legacy_calls, [])


class PauseContractTests(unittest.TestCase):
    def test_pause_uses_session_api(self):
        p = FakePlugin()
        _dating(p)
        out = asyncio.run(de.handle_date_text(p, "老婆我去个洗手间", SAT))
        self.assertEqual(out.get("action"), "pause")
        self.assertIn("set_date_pause", p.amo.calls)
        self.assertEqual(p.amo.pause_args, de.PAUSE_MAX_MINUTES)
        self.assertNotIn("date_pause", p.amo.db.writes, "暂停不能直写 kv")

    def test_resume_uses_session_api(self):
        p = FakePlugin()
        _dating(p)
        asyncio.run(de.handle_date_text(p, "我回来了", SAT))
        self.assertIn("clear_date_pause", p.amo.calls)
        self.assertNotIn("date_pause", p.amo.db.writes)


class AgreementContractTests(unittest.TestCase):
    def test_agreed_reads_date_ending_api(self):
        p = FakePlugin()
        p.amo.ending = {"fact": "该回去了", "sent": True}
        self.assertTrue(de._agreed_to_end(p, "好", SAT))
        self.assertIn("date_ending", p.amo.calls)
        self.assertNotIn("date_ending", p.amo.db.writes)

    def test_unsent_ending_is_not_agreement(self):
        p = FakePlugin()
        p.amo.ending = {"fact": "还没说", "sent": False}
        self.assertFalse(de._agreed_to_end(p, "好", SAT))

    def test_agreement_finish_clears_ending_through_api(self):
        p = FakePlugin()
        _dating(p)
        p.amo.ending = {"fact": "该回去了", "sent": True}
        out = asyncio.run(de.handle_date_text(p, "好", SAT))
        self.assertEqual(out.get("action"), "finish")
        self.assertIn("clear_date_ending", p.amo.calls)
        self.assertNotIn("date_ending", p.amo.db.writes)


class SocialContractTests(unittest.TestCase):
    def test_busy_state_prefers_is_dating_api(self):
        reads = []

        class SocialAmo:
            def __init__(self):
                self.db = self

            def kv_get(self, key, default=None):
                reads.append(key)
                return default

            def kv_set(self, key, value):
                return None

            def is_dating(self, now=None):
                reads.append("is_dating")
                return True

        repo, fin = SocialRepo(), SocialFinance(cash=100000)
        fin.amo = SocialAmo()
        eng = SocialEngine(SocialPlugin(), repo, fin)
        self.assertEqual(eng.busy_state(SAT), "dating")
        self.assertIn("is_dating", reads)
        self.assertNotIn("date_now", reads, "有新 API 时不该再读 date_now")


if __name__ == "__main__":
    unittest.main()
