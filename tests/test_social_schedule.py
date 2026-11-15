# -*- coding: utf-8 -*-
"""社交答应 → 未来日程强绑定：自然语言解析 / 冲突改期 / 幂等 / 写失败不扣钱。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import social_engine as se  # noqa: E402
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

NOW = dt.datetime(2026, 11, 15, 21, 0)


def _engine(schedules=None, add_ok=True, cash=500000):
    repo, fin = FakeRepo(), FakeFinance(cash=cash)
    fin.schedules = list(schedules or [])
    fin.add_ok = add_ok
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def _invite(**over):
    data = {"token": "soc-t1", "kind": "shopping", "topic": "逛街",
            "npc": "柳如烟", "relation": "闺蜜", "circle": "friend",
            "place": "商场", "day": "2026-11-16", "start": "14:00", "end": "17:00",
            "zone": "mall", "amount_cents": 25000,
            "ask": "明天下午有空不？陪我去商场逛逛呗"}
    data.update(over)
    return data


def test_parse_when_chinese_time():
    assert se.parse_when("明天下午", NOW) == ("2026-11-16", "14:00", "17:00")
    assert se.parse_when("后天晚上", NOW) == ("2026-11-17", "19:00", "21:00")
    assert se.parse_when("周末", NOW)[0] == "2026-11-21"


def test_accept_writes_future_schedule():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(note="好呀"))
    assert res["ok"] and res["schedule"]["day"] == "2026-11-16"
    assert fin.schedules[-1]["day"] == "2026-11-16"
    assert fin.schedules[-1]["start"] == "14:00"


def test_accept_parses_when_invite_has_no_time():
    eng, repo, fin = _engine()
    eng.set_invite(_invite(day="", start="", end=""))
    res = asyncio.run(eng.accept_invite(note="好呀"))
    assert res["ok"]
    assert fin.schedules[-1]["start"] == "14:00"
    assert fin.schedules[-1]["day"] > NOW.date().isoformat()


def test_accept_shifts_when_conflict():
    busy = [{"start": "14:00", "end": "17:00", "activity": "去医院看病", "place": "医院"}]
    eng, repo, fin = _engine()
    eng.set_invite(_invite(day="2026-11-16"))
    eng._conflict = lambda day, start, end: day == "2026-11-16"
    res = asyncio.run(eng.accept_invite(note="好呀"))
    assert res["ok"]
    assert fin.schedules[-1]["day"] == "2026-11-17"


def test_accept_is_idempotent():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(note="好呀"))
    count = len(fin.schedules)
    eng.set_invite(_invite(token="soc-t2"))
    res = asyncio.run(eng.accept_invite(note="好呀"))
    assert res["ok"]
    assert len(fin.schedules) == count, "同一场约会只能有一条日程"


def test_accept_refuses_when_schedule_fails():
    eng, repo, fin = _engine(add_ok=False)
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(note="好呀"))
    assert res["ok"] is False and res["reason"] == "schedule_failed"
    assert fin.expenses == [], "日程没写进去就不能扣钱"
