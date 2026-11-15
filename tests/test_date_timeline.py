# -*- coding: utf-8 -*-
"""约会时间轴：中途离开 / 结束截断 / 剩余时间重排 / 买单地点。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import date_engine as de  # noqa: E402
from core.amo_bridge import FinanceLink  # noqa: E402

NOW = dt.datetime(2026, 11, 15, 19, 22)


class FakeDB:
    def __init__(self):
        self.data = {}

    def kv_get(self, key, default=None):
        return self.data.get(key, default)

    def kv_set(self, key, value):
        self.data[key] = value


class FakeState:
    def __init__(self):
        self.schedule = {"2026-11-15": [
            {"start": "19:22", "end": "21:22", "activity": "和老公火锅",
             "place": "楼下小馆", "category": "social", "zone": "restaurant"},
        ]}
        self.added = []
        self.commits = []

    def current_activity(self):
        return {"place": "楼下小馆", "activity": "吃火锅", "zone": "restaurant"}

    def add_schedule_item(self, day, item, source="", reason=""):
        self.added.append(dict(item))
        return {"changed": True}

    def commit_schedule_change(self, day, items, old_items=None, source="", reason=""):
        self.schedule[day] = list(items)
        self.commits.append({"day": day, "items": list(items), "reason": reason})
        return {"changed": True}


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    async def chat_raw(self, umo, system, prompt):
        return self.payload


class FakeAmo:
    def __init__(self, reply=""):
        self.db = FakeDB()
        self.state = FakeState()
        self.llm = FakeLLM(reply)
        self.facts = []
        self.closet = True
        self.last_event = ("", 0)

    def _owner_umos(self):
        return ["test:private:u1"]

    def set_date_mood(self, *a):
        pass

    def set_last_date_event(self, *a):
        pass

    def set_date_prep(self, *a):
        pass

    def set_date_incident(self, *a):
        pass

    def set_date_fact(self, fact, until=0):
        self.facts.append(fact)

    def set_date_home(self, *a):
        self.db.kv_set("date_home", {"title": a[0] if a else "", "until": 9999999999})

    def set_date_now(self, *a):
        pass

    def set_current_action(self, *a, **k):
        pass

    def set_date_ending(self, fact, reason=""):
        self.db.kv_set("date_ending", {"fact": fact, "reason": reason, "until": 9999999999})


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo
        self.moods = []
        self.diaries = []
        self.memories = []

    def _amo(self):
        return self.amo

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def balance_cents(self):
        return 500000

    def mood(self):
        return {"word": "开心"}

    def fatigue(self):
        return 40


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value


class FakePlugin:
    def __init__(self, reply=""):
        self.amo = FakeAmo(reply)
        self.finance = FakeFinance(self.amo)
        self.finance_repo = FakeRepo()


def test_pause_words_do_not_end_date():
    assert de.is_date_pause("老婆我去个洗手间")
    assert de.is_date_pause("我接个电话，马上回来")
    assert not de.is_date_pause("吃好了，我们回家吧")
    assert de.is_date_end("吃好了，我们回家吧")


def test_finish_truncates_date_item_to_now():
    p = FakePlugin()
    de._set_active(p, {"title": "火锅", "start": NOW.timestamp() - 3600,
                       "end": NOW.timestamp() + 3600, "payer": "host"})
    asyncio.run(de.finish_date(p, NOW, reason="user_done"))
    rows = p.amo.state.schedule["2026-11-15"]
    assert rows[0]["end"] == "19:22", "约会日程必须截断到当前分钟"
    assert p.amo.state.commits, "必须走 commit_schedule_change 落库"


def test_replan_writes_llm_segments_not_hardcoded_rest():
    payload = ('[{"start":"19:35","end":"20:05","activity":"和老公散步回家",'
               '"place":"路上","category":"social","zone":"road"},'
               '{"start":"20:05","end":"21:00","activity":"到家瘫在沙发上休息",'
               '"place":"家","category":"rest","zone":"home"}]')
    p = FakePlugin(reply=payload)
    segments = asyncio.run(de.replan_remaining(p, p.amo, NOW, "火锅"))
    assert [seg["activity"] for seg in segments] == ["和老公散步回家", "到家瘫在沙发上休息"]
    assert [item["activity"] for item in p.amo.state.added] == ["和老公散步回家", "到家瘫在沙发上休息"]
    assert all(item["end"] > item["start"] for item in p.amo.state.added)


def test_replan_ignores_bad_times():
    payload = '[{"start":"10:00","end":"11:00","activity":"倒着来"},'               '{"start":"19:40","end":"19:30","activity":"结束早于开始"}]'
    p = FakePlugin(reply=payload)
    assert asyncio.run(de.replan_remaining(p, p.amo, NOW, "火锅")) == []
    assert p.amo.state.added == []


def test_finance_schedule_uses_current_place():
    amo = FakeAmo()
    link = FinanceLink(type("P", (), {"config": {}})())
    link._amo = lambda: amo
    link._schedule(amo, "支出", 35000, note="火锅")
    item = amo.state.added[-1]
    assert item["place"] == "楼下小馆"
    assert item["zone"] == "restaurant"


def test_pause_timeout_finishes_date():
    p = FakePlugin()
    de._set_active(p, {"title": "火锅", "start": NOW.timestamp() - 600,
                       "end": NOW.timestamp() + 3600, "payer": "host"})
    p.amo.db.kv_set("date_pause", {"at": (NOW - dt.timedelta(minutes=20)).timestamp(),
                                   "until": (NOW - dt.timedelta(minutes=5)).timestamp()})
    asyncio.run(de.tick(p, NOW))
    assert not de._active(p).get("title"), "离开超过 15 分钟要判定约会结束"
