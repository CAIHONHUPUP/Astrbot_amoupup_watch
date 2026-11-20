# -*- coding: utf-8 -*-
"""男性 NPC 边界 + 好感度分档（2026-11-20）。

口径：
- 季博达、方圆是男生：只能谈工作/普通社交，不单独吃饭喝酒逛街；
- 好感度只决定「答不答应邀约」：≥60 正常、40-59 一半概率、<40 基本婉拒。
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

DAY = "2026-11-21"


def _invite(npc="柳如烟", kind="meal", **over):
    inv = {"token": "soc-bound", "kind": kind, "topic": "下班吃火锅", "npc": npc,
           "relation": "", "circle": "friend", "place": "楼下小馆",
           "day": DAY, "start": "14:00", "end": "17:00", "zone": "restaurant",
           "amount_cents": 20000, "ask": "晚上一起吃饭？"}
    inv.update(over)
    return inv


def _engine(affinity=None, name="柳如烟"):
    repo, fin = FakeRepo(), FakeFinance(cash=500000)
    if affinity is not None:
        repo.upsert_npc(name, "friend", "", affinity)
    eng = SocialEngine(FakePlugin(), repo, fin)
    return eng, repo, fin


def test_male_npc_meal_is_refused():
    eng, repo, fin = _engine(affinity=90, name="季博达")
    eng.set_invite(_invite(npc="季博达"))
    res = asyncio.run(eng.accept_invite(note="好"))
    assert res["ok"] is False and res["reason"] == "boundary"
    assert fin.expenses == []


def test_male_npc_work_invite_allowed():
    eng, repo, fin = _engine(affinity=90, name="方圆")
    eng.set_invite(_invite(npc="方圆", kind="work", topic="加班"))
    res = asyncio.run(eng.accept_invite(note="好"))
    assert res["ok"] is True


def test_low_affinity_declines():
    eng, repo, fin = _engine(affinity=10)
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(note="好"))
    assert res["ok"] is False and res["reason"] == "boundary"


def test_high_affinity_accepts():
    eng, repo, fin = _engine(affinity=88)
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(note="好"))
    assert res["ok"] is True


def test_no_affinity_row_does_not_gate():
    eng, _, _ = _engine(affinity=None)
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(note="好"))
    assert res["ok"] is True
