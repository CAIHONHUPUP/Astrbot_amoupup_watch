# -*- coding: utf-8 -*-
"""社交引擎基础：种子 / 手动邀约不扣钱 / 未知 NPC / 借款应还幂等。"""
import asyncio, datetime as dt, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

NOW = dt.datetime(2026, 11, 21, 10, 0, 0)


def _engine(cash=500000):
    repo, fin = FakeRepo(), FakeFinance(cash=cash)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def test_seed_has_seven_npcs_across_circles():
    eng, repo, _ = _engine()
    eng.ensure_seed()
    assert len(repo.npcs()) == 7
    assert {row["circle"] for row in repo.npcs()} >= {"friend", "work", "family"}


def test_manual_invite_never_charges_but_marks_cooldown():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.create_invite(now=NOW, kind="meal", npc="季博达"))
    assert out["ok"]
    assert fin.cash == 500000 and fin.expenses == [] and fin.schedules == []
    assert repo.meta_get("social:last_ts")
    assert repo.task_done("social:npc:2026-11-21:季博达")
    assert repo.meta_get("social:count:2026-11-21") == "1"


def test_unknown_npc_is_rejected():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.create_invite(now=NOW, kind="meal", npc="李" + "婷"))
    assert out["ok"] is False and out["reason"] == "unknown_npc"
    assert eng.pending_invite() is None and fin.cash == 500000


def test_borrow_accept_creates_receivable_and_repay_is_idempotent():
    eng, repo, fin = _engine()
    made = asyncio.run(eng.create_invite(now=NOW, kind="borrow", npc="柳如烟", amount_cents=50000))
    assert made["ok"]
    res = asyncio.run(eng.accept_invite(note="借给柳如烟周转一下", now=NOW))
    assert res["ok"] and repo.receivables()
    assert fin.cash == 450000
    asyncio.run(eng.repay_tick(dt.date(2026, 12, 1)))
    before = len(repo.receivables())
    asyncio.run(eng.repay_tick(dt.date(2026, 12, 1)))
    assert len(repo.receivables()) == before
