# -*- coding: utf-8 -*-
"""妈妈/爸爸约饭默认地点（2026-11-21 线上：标题「妈妈中午回家吃饭」地点却是楼下小馆）。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

SAT = dt.datetime(2026, 11, 21, 10, 0, 0)


def _engine():
    repo, fin = FakeRepo(), FakeFinance(cash=500000, fatigue=42)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def _invite(now=SAT, **over):
    data = {"npc": "妈妈", "kind": "meal", "topic": "回家吃饭",
            "place": "", "day": "2026-11-21", "start": "18:00", "end": "20:00",
            "budget_cents": 12000, "ask": "晚上回家吃饭吗？我炖了汤。"}
    data.update(over)

    async def composer(facts):
        return dict(data)

    eng, _repo, _fin = _engine()
    out = asyncio.run(eng.create_invite(now=now, composer=composer))
    assert out["ok"], out
    return out["invite"]


def test_mom_meal_empty_place_defaults_to_parents_home():
    inv = _invite(npc="妈妈", kind="meal", place="")
    assert (inv["place"], inv["zone"]) == ("父母家", "home")
    # 地点对了，标题里的「回家吃饭」就不用被改写
    assert inv["topic"] == "回家吃饭"


def test_dad_meal_place_home_defaults_to_parents_home():
    inv = _invite(npc="爸爸", kind="meal", place="家")
    assert (inv["place"], inv["zone"]) == ("父母家", "home")


def test_mom_meal_explicit_mall_is_respected():
    inv = _invite(npc="妈妈", kind="meal", place="商场", topic="去商场吃饭")
    assert (inv["place"], inv["zone"]) == ("商场", "mall")


def test_other_npc_meal_keeps_restaurant_default():
    inv = _invite(npc="柳如烟", kind="meal", place="", topic="吃个饭",
                  ask="晚上有空一起吃个饭吗？")
    assert (inv["place"], inv["zone"]) == ("楼下小馆", "restaurant")
