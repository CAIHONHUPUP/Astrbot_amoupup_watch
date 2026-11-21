# -*- coding: utf-8 -*-
"""答应时不写未来事实 / 结束后才回忆 / 金额有上限 / 对方请客不扣钱（2026-11-20）。

线上事故：09:56 答应柳如烟 18:30 的火锅——
  ① 长期记忆里已经写「她还聊起你：我说他最近还是老样子」（18:30 才见的面）；
  ② 钱包当场扣 200，笔记里还写着「还是跟如烟吃了火锅」；
  ③ 如烟说「我请」，钱却从她钱包出。
"""
import asyncio
import datetime as dt
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.social_engine import (SocialEngine, cap_budget, grounded_ask,
                                    panel_events)  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

DAY = "2026-11-21"
START = dt.datetime(2026, 11, 21, 14, 0, 0).timestamp()
END = dt.datetime(2026, 11, 21, 17, 0, 0).timestamp()


def _invite(**over):
    inv = {"token": "soc-truth", "kind": "meal", "topic": "下班吃火锅", "npc": "柳如烟",
           "relation": "闺蜜", "circle": "friend", "place": "楼下小馆",
           "day": DAY, "start": "14:00", "end": "17:00", "zone": "restaurant",
           "amount_cents": 20000, "ask": "晚上一起吃火锅？"}
    inv.update(over)
    return inv


def _engine(cash=500000):
    repo, fin = FakeRepo(), FakeFinance(cash=cash)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def test_accept_memory_is_plan_only():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="今天累归累，还是跟如烟吃了火锅",
                                  about_user="我说他最近还是老样子"))
    assert fin.memories and "答应了柳如烟" in fin.memories[0]
    assert "聊起你" not in fin.memories[0] and "吃了" not in fin.memories[0]


def test_recall_after_event_end_writes_memory_once():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="还是跟如烟吃了火锅",
                                  about_user="我说他最近还是老样子"))
    asyncio.run(eng.settle_charges(now=START))
    before = len(fin.memories)
    out = asyncio.run(eng.recollect_past(now=END + 60))
    assert out["recalled"] == 1
    assert len(fin.memories) == before + 1
    assert "吃了火锅" in fin.memories[-1] and "聊起你" in fin.memories[-1]
    assert asyncio.run(eng.recollect_past(now=END + 120))["recalled"] == 0


def test_cancelled_event_never_recalled():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    fin.schedules = []
    fin.windows[DAY] = [{"start": "14:00", "end": "17:00", "activity": "在家躺着"}]
    asyncio.run(eng.settle_charges(now=START))
    before = len(fin.memories)
    assert asyncio.run(eng.recollect_past(now=END + 60))["recalled"] == 0
    assert len(fin.memories) == before


def test_cancelled_card_says_didnt_go():
    row = {"id": 1, "kind": "social:accept", "day": DAY, "created_at": 1.0,
           "payload": {"text": "好呀", "npc": "柳如烟", "day": DAY, "start": "14:00",
                       "end": "17:00", "cancelled": True, "note": "还是跟如烟吃了火锅"}}
    assert "没去成" in panel_events([row])[0]["display"]


def test_card_shows_event_time_and_accept_time():
    row = {"id": 1, "kind": "social:accept", "day": DAY,
           "created_at": dt.datetime(2026, 11, 20, 9, 56).timestamp(),
           "payload": {"text": "好呀", "npc": "柳如烟", "day": DAY, "start": "14:00",
                       "end": "17:00"}}
    item = panel_events([row])[0]
    assert item["when_text"] == "11-21 14:00"
    assert item["created_text"] == "11-20 09:56"


def test_budget_is_capped():
    assert cap_budget("meal", 999999) == 30000
    assert cap_budget("meal", 20000) == 20000
    assert cap_budget("shopping", 999999) == 60000


def test_treat_words_make_it_free():
    eng, repo, fin = _engine()

    async def composer(facts):
        return {"npc": "柳如烟", "kind": "meal", "topic": "吃火锅",
                "place": "楼下小馆", "day": DAY, "start": "14:00",
                "end": "17:00", "budget_cents": 20000,
                "ask": "晚上一起吃火锅？我请。"}

    out = asyncio.run(eng.create_invite(now=dt.datetime(2026, 11, 21, 10, 0),
                                        composer=composer))
    assert out["ok"] and out["invite"]["amount_cents"] == 0
    res = asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    assert res["ok"] and res["steps"]["wallet"] == "free"
    assert fin.expenses == [] and fin.cash == 500000
    # 0 元也排一条，只为到点标记「真的去了」
    assert [c["amount_cents"] for c in eng.pending_charges()] == [0]


def test_grounded_ask_fixes_subject():
    fixed = grounded_ask("听说你跟对象冷战好几天了，晚上一起吃火锅？", "柳如烟",
                         "跟男朋友冷战了三天", "吃火锅")
    assert "冷战" in fixed
    assert not re.search(r"你[^，。]{0,8}冷战", fixed)
    # 正常文案不动
    assert grounded_ask("最近累死了，晚上一起吃火锅？", "柳如烟",
                        "跟男朋友冷战了三天", "吃火锅") == "最近累死了，晚上一起吃火锅？"
