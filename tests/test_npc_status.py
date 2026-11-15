# -*- coding: utf-8 -*-
"""NPC 自己的生活轨迹（npc_status）。"""
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import npc_status  # noqa: E402
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

NOW = dt.datetime(2026, 11, 16, 10, 0, 0)


def _engine():
    repo, fin = FakeRepo(), FakeFinance()
    return SocialEngine(FakePlugin(), repo, fin), repo


def test_every_npc_gets_a_status():
    eng, repo = _engine()
    eng.ensure_seed()
    current = npc_status.status_map(repo)
    assert set(current) == set(npc_status.POOLS)
    assert all(current.values())


def test_status_is_stable_within_ttl_and_rolls_after_it():
    eng, repo = _engine()
    eng.ensure_seed()
    first = npc_status.status_map(repo)
    npc_status.refresh(repo, NOW + dt.timedelta(days=1))
    assert npc_status.status_map(repo) == first, "7-14 天内不该变"
    npc_status.refresh(repo, NOW + dt.timedelta(days=40))
    after = npc_status.status_map(repo)
    row = repo.npc_get("柳如烟")
    assert row["status_at"] > 0
    assert after["柳如烟"] in npc_status.POOLS["柳如烟"]


def test_relation_line_carries_status():
    eng, repo = _engine()
    eng.ensure_seed()
    line = eng.relation_line()
    assert line.startswith("【关系】")
    assert "柳如烟" in line
    status = repo.npc_get("柳如烟")["status"]
    assert status and status in line
