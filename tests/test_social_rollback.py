# -*- coding: utf-8 -*-
"""accept_invite 的失败回滚（2026-11-20）。

顺序是「写日程 → 冻结 → 扣款 → 记忆/情绪」，冻结失败时必须把刚写的日程撤掉，
否则会留下一条没付钱的行程。
"""
import asyncio
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

DAY = "2026-11-21"


def _invite():
    return {"token": "soc-rb", "kind": "meal", "topic": "下班吃火锅", "npc": "柳如烟",
            "relation": "闺蜜", "circle": "friend", "place": "楼下小馆",
            "day": DAY, "start": "14:00", "end": "17:00", "zone": "restaurant",
            "amount_cents": 20000, "ask": "晚上一起吃火锅？"}


def test_hold_failure_rolls_back_schedule_and_memory():
    repo, fin = FakeRepo(), FakeFinance(cash=1000)   # 钱不够冻结
    eng = SocialEngine(FakePlugin(), repo, fin)
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(note="好呀"))
    assert res["ok"] is False
    # 刚写的日程块被撤回
    rows = [row for row in (fin.windows.get(DAY) or [])
            if "柳如烟" in str(row.get("activity") or "")]
    assert rows == [], rows
    # 没有扣钱，也没有写「答应了」的记忆
    assert fin.expenses == [] and fin.cash == 1000
    assert fin.memories == []
