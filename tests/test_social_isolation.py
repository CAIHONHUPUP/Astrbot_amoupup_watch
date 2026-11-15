# -*- coding: utf-8 -*-
"""约会/睡觉期间社交物理隔绝 + 待机队列 + 时间锚定 + 幂等。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

SAT = dt.datetime(2026, 11, 21, 20, 41, 0)


class FakeKV:
    def __init__(self):
        self.data = {}

    def kv_get(self, key, default=None):
        return self.data.get(key, default)

    def kv_set(self, key, value):
        self.data[key] = value


class FakeState:
    def __init__(self, category="work"):
        self.category = category

    def current_activity(self):
        return {"place": "楼下小馆", "activity": "吃火锅",
                "zone": "restaurant", "category": self.category}


class FakeAmo:
    def __init__(self, category="work", dating=False):
        self.db = FakeKV()
        self.state = FakeState(category)
        if dating:
            self.db.kv_set("date_now", {"line": "和老公吃火锅",
                                        "until": SAT.timestamp() + 3600})


def _engine(cash=500000, category="work", dating=False):
    repo, fin = FakeRepo(), FakeFinance(cash=cash)
    fin.amo = FakeAmo(category=category, dating=dating)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def _composer(**over):
    async def composer(facts):
        data = {"npc": "柳如烟", "kind": "shopping", "topic": "逛商场",
                "place": "", "day": "2026-11-21", "start": "14:00", "end": "17:00",
                "budget_cents": 25000, "ask": "明天下午有空不？陪我去商场逛逛呗"}
        data.update(over)
        return data
    return composer


def test_busy_state_detects_dating_and_sleeping():
    eng, _, _ = _engine(dating=True)
    assert eng.busy_state(SAT) == "dating"
    eng2, _, _ = _engine(category="sleep")
    assert eng2.busy_state(SAT) == "sleeping"
    eng3, _, _ = _engine()
    assert eng3.busy_state(SAT) == ""


def test_no_invite_or_decision_during_date():
    eng, repo, fin = _engine(dating=True)
    calls = []

    async def decider(invite, facts):
        calls.append(facts)
        return {"action": "accept", "note": "去了"}

    out = asyncio.run(eng.scheduled(now=SAT, decider=decider, composer=_composer()))
    assert out.get("busy") == "dating"
    assert not calls, "约会期间绝不能触发她的决策"
    assert eng.pending_invite() is None
    assert fin.expenses == []


def test_invite_is_queued_then_decided_after_date():
    eng, repo, fin = _engine(dating=True)
    made = asyncio.run(eng.create_invite(now=SAT, composer=_composer(day="2026-11-22")))
    assert made["ok"]
    invite = made["invite"]
    assert invite.get("deferred"), "约会期间收到的消息要进待机队列"
    assert invite.get("deferred_reason") == "dating"

    # 约会结束：状态恢复 normal
    fin.amo.db.kv_set("date_now", {})
    seen = {}

    async def decider(inv, facts):
        seen["facts"] = facts
        return {"action": "reject", "reason": "太累了", "note": "约会回来才看到，婉拒了柳如烟"}

    out = asyncio.run(eng.scheduled(now=SAT, decider=decider))
    assert out.get("decision", {}).get("ok")
    assert "手机静音" in seen["facts"]
    assert "不是现在出门" in seen["facts"], "明天的约会不能写成现在就出门"


def test_accept_is_blocked_while_busy():
    eng, repo, fin = _engine(dating=True)
    made = asyncio.run(eng.create_invite(now=SAT, composer=_composer()))
    res = asyncio.run(eng.accept_invite(note="去了"))
    assert res["ok"] is False and res["reason"] == "busy"
    assert fin.cash == 500000
    assert eng.pending_invite() is not None


def test_events_are_idempotent():
    eng, repo, fin = _engine()
    eng.record("social:criticism", "季博达", "被季博达批评了", status="happened",
               day="2026-11-21", idem="evt:crit:2026-11-21")
    eng.record("social:criticism", "季博达", "被季博达批评了", status="happened",
               day="2026-11-21", idem="evt:crit:2026-11-21")
    assert len([row for row in repo.log if row["kind"] == "social:criticism"]) == 1


def test_future_day_invite_is_anchored_to_that_day():
    eng, repo, fin = _engine()
    made = asyncio.run(eng.create_invite(now=SAT, composer=_composer(day="2026-11-22")))
    invite = made["invite"]
    facts = eng.invite_facts(invite, SAT)
    assert "不是现在出门" in facts
