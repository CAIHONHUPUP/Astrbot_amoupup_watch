# -*- coding: utf-8 -*-
"""财务防重：同一场社交只能扣一次钱（事件级幂等键）+ resolved 生命周期。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402


def _invite(token):
    return {"token": token, "kind": "shopping", "topic": "逛街", "npc": "柳如烟",
            "relation": "闺蜜", "circle": "friend", "place": "商场",
            "day": "2026-11-16", "start": "14:00", "end": "17:00", "zone": "mall",
            "amount_cents": 27522, "ask": "明天下午有空不"}


def test_same_event_uses_one_idem():
    repo, fin = FakeRepo(), FakeFinance()
    seen = []
    orig = fin.expense

    async def spy(amount, reason, idem, **kw):
        seen.append(idem)
        return await orig(amount, reason, idem, **kw)

    fin.expense = spy
    eng = SocialEngine(FakePlugin(), repo, fin)
    eng.set_invite(_invite("soc-a"))
    asyncio.run(eng.accept_invite(note="好呀"))
    eng.set_invite(_invite("soc-b"))
    asyncio.run(eng.accept_invite(note="好呀"))
    assert len(seen) == 2
    assert seen[0] == seen[1], "同一人同一天同一时段必须共用一个幂等键"
    assert seen[0].startswith("social_event:柳如烟:2026-11-16:14:00")


def test_resolved_invite_is_skipped():
    repo, fin = FakeRepo(), FakeFinance()
    eng = SocialEngine(FakePlugin(), repo, fin)
    eng.mark_resolved("soc-x")
    eng.set_invite(_invite("soc-x"))
    assert eng._pending_for_decision() is None
