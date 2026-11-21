# -*- coding: utf-8 -*-
"""社交全链路：频率闸 / 常识 / 冲突 / 人话文案。"""
import asyncio
import datetime as dt
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import llm_tools  # noqa: E402
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

MON = dt.datetime(2026, 11, 16, 10, 0, 0)
SAT = dt.datetime(2026, 11, 21, 10, 0, 0)


def _engine(cash=500000, fatigue=42):
    repo, fin = FakeRepo(), FakeFinance(cash=cash, fatigue=fatigue)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def _composer(**over):
    async def composer(facts):
        data = {"npc": "柳如烟", "kind": "shopping", "topic": "逛商场",
                "place": "", "day": "2026-11-21", "start": "14:00", "end": "17:00",
                "budget_cents": 25000, "ask": "如烟约我下午去逛商场，说好久没一起买衣服了。"}
        data.update(over)
        return data
    return composer


def test_invite_is_fact_only():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.create_invite(now=SAT, composer=_composer()))
    assert out["ok"]
    inv = out["invite"]
    assert inv["npc"] == "柳如烟" and inv["start"] == "14:00" and inv["end"] == "17:00"
    assert fin.cash == 500000 and fin.expenses == [] and fin.schedules == []
    assert repo.npc["柳如烟"]["last_event"] == ""
    assert eng.pending_invite()["token"] == inv["token"]


def test_accept_keeps_money_until_event_starts():
    eng, repo, fin = _engine()
    invite = asyncio.run(eng.create_invite(now=SAT, composer=_composer()))["invite"]
    note = "答应了如烟，下午一起去商场逛逛"
    res = asyncio.run(eng.accept_invite(now=SAT, note=note))
    assert res["ok"]
    # 2026-11-20 用户要求：答应那一刻只占日程，到点（事件开始）才真的扣钱
    assert fin.cash == 500000 and fin.expenses == []
    assert res["steps"]["wallet"] == "pending"
    assert [c["idem"] for c in eng.pending_charges()] == [
        "social_event:柳如烟:2026-11-21:14:00"]
    assert fin.schedules[0]["start"] == "14:00" and fin.schedules[0]["end"] == "17:00"
    assert fin.moods[0]["开心"] > 0 and fin.moods[0]["疲惫"] < 0
    assert fin.memories and fin.diaries
    # 2026-11-19：last_event 只存第三人称客观句，绝不再把她的第一人称日记回灌给模型
    assert repo.npc["柳如烟"]["last_event"].startswith("她答应了柳如烟")
    assert "我" not in repo.npc["柳如烟"]["last_event"]
    assert eng.pending_invite() is None
    assert res["steps"]["schedule"]
    # 14:00 到点才结算，且和事件共用同一个幂等键
    out = asyncio.run(eng.settle_charges(now=dt.datetime(2026, 11, 21, 14, 0, 0).timestamp()))
    assert out["paid"] == 1 and eng.pending_charges() == []
    assert fin.cash == 500000 - invite["amount_cents"]
    assert fin.expenses[0]["idem"] == "social_event:柳如烟:2026-11-21:14:00"


def test_short_money_cannot_be_accepted():
    eng, repo, fin = _engine(cash=1000)
    asyncio.run(eng.create_invite(now=SAT, composer=_composer()))
    res = asyncio.run(eng.accept_invite(now=SAT, note="想去"))
    assert res["ok"] is False and res["reason"] == "insufficient"
    assert fin.cash == 1000 and fin.expenses == []


def test_reject_records_reason_without_charge():
    eng, repo, fin = _engine()
    asyncio.run(eng.create_invite(now=SAT, composer=_composer()))
    note = "跟如烟说今天不去了，实在太累"
    res = asyncio.run(eng.reject_invite(reason="太累了，想在家躺着", note=note, now=SAT))
    assert res["ok"]
    assert fin.cash == 500000 and fin.expenses == [] and fin.schedules == []
    assert fin.moods[0] == {"郁闷": 4}
    assert repo.npc["柳如烟"]["last_event"].startswith("她婉拒了柳如烟")
    assert "我" not in repo.npc["柳如烟"]["last_event"]
    assert eng.pending_invite() is None


def test_global_gap_and_daily_cap():
    eng, repo, fin = _engine()
    assert asyncio.run(eng.create_invite(now=SAT, kind="meal", npc="季博达"))["ok"]
    asyncio.run(eng.reject_invite(reason="不想去", note="婉拒了季博达"))
    late = SAT + dt.timedelta(minutes=30)
    out = asyncio.run(eng.create_invite(now=late, kind="meal", npc="方圆"))
    assert out["ok"] is False and out["reason"] == "gap"
    later = SAT + dt.timedelta(hours=3)
    assert asyncio.run(eng.create_invite(now=later, kind="meal", npc="方圆"))["ok"]
    asyncio.run(eng.reject_invite(reason="不去", note="又婉拒了方圆"))
    third = SAT + dt.timedelta(hours=6)
    out3 = asyncio.run(eng.create_invite(now=third, kind="meal", npc="妈妈"))
    assert out3["ok"] is False and out3["reason"] == "daily_cap"


def test_same_npc_once_per_day():
    eng, repo, fin = _engine()
    assert asyncio.run(eng.create_invite(now=SAT, kind="meal", npc="柳如烟"))["ok"]
    asyncio.run(eng.reject_invite(reason="不去", note="婉拒了柳如烟"))
    later = SAT + dt.timedelta(hours=3)
    out = asyncio.run(eng.create_invite(now=later, kind="meal", npc="柳如烟"))
    assert out["ok"] is False and out["reason"] == "npc_today"


def test_ceremony_is_at_least_three_days_ahead():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.create_invite(now=SAT, composer=_composer(
        npc="沈幼微", kind="ceremony", day="2026-11-22", start="11:00", end="13:00",
        topic="喝喜酒", ask="我下个月摆酒，你可得来啊")))
    assert out["ok"]
    day = dt.date.fromisoformat(out["invite"]["day"])
    assert (day - SAT.date()).days >= 3


def test_leader_invite_moves_to_weekday():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.create_invite(now=SAT, composer=_composer(
        npc="季博达", kind="work", day="2026-11-22", start="10:00", end="12:00")))
    assert out["ok"]
    assert dt.date.fromisoformat(out["invite"]["day"]).weekday() < 5


def test_friend_weekday_daytime_moves_to_evening():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.create_invite(now=MON, composer=_composer(
        day="2026-11-17", start="14:00", end="17:00")))
    assert out["ok"]
    invite = out["invite"]
    assert dt.date.fromisoformat(invite["day"]).weekday() < 5
    assert int(invite["start"][:2]) >= 18


def test_conflicting_slot_moves_to_next_free_day():
    eng, repo, fin = _engine()
    fin.windows["2026-11-21"] = [{"start": "14:00", "end": "17:00", "activity": "和柳如烟逛街"}]
    out = asyncio.run(eng.create_invite(now=SAT, composer=_composer(
        npc="白凝冰", day="2026-11-21", start="14:00", end="17:00")))
    assert out["ok"]
    assert out["invite"]["day"] != "2026-11-21"


def test_accept_conflict_moves_to_next_free_day():
    eng, repo, fin = _engine()
    asyncio.run(eng.create_invite(now=SAT, composer=_composer()))
    fin.windows["2026-11-21"] = [{"start": "14:00", "end": "17:00", "activity": "别的约"}]
    res = asyncio.run(eng.accept_invite(now=SAT, note="想去"))
    assert res["ok"]
    assert res["schedule"]["day"] == "2026-11-22", "撞车要自动改期，不能写重叠日程"
    assert fin.schedules[-1]["day"] == "2026-11-22"


def test_display_is_human_words_without_tags_or_cents():
    eng, repo, fin = _engine()
    asyncio.run(eng.create_invite(now=SAT, composer=_composer()))
    assert not eng.events(5)[0]["display"].startswith("[")
    asyncio.run(eng.accept_invite(now=SAT, note="答应了如烟，下午去商场逛，预算两百多"))
    row = eng.events(5)[0]
    assert not row["display"].startswith("[")
    assert "¥" not in row["display"]
    assert not re.search(r"\d+\.\d{1,2}", row["display"])


def test_structured_fallback_has_no_money_or_tags():
    eng, repo, fin = _engine()
    line = eng.fallback_line("social:accept", npc="柳如烟", topic="逛商场", amount_cents=27522)
    assert line == "她答应了柳如烟"
    line2 = eng.fallback_line("social:invite", npc="柳如烟", topic="逛商场", amount_cents=27522)
    assert "¥" not in line2 and "[" not in line2


def test_scheduled_full_chain_with_composer_and_decider():
    eng, repo, fin = _engine()
    seen = {}

    async def decider(invite, facts):
        seen["facts"] = facts
        return {"action": "accept", "ask": "如烟约我下午逛商场",
                "reason": "想出去透透气",
                "now": "好呀，下午商场见",
                "note": "答应了如烟，下午去逛商场"}

    out = asyncio.run(eng.scheduled(now=SAT, decider=decider, composer=_composer()))
    assert out["invite"] and out["decision"] and out["decision"]["ok"]
    assert "余额" in seen["facts"] and "疲惫值" in seen["facts"]
    assert fin.schedules and eng.pending_charges() and fin.cash == 500000
    assert out["push"] == ["好呀，下午商场见"]


def test_scheduled_without_llm_keeps_pending():
    eng, repo, fin = _engine()

    async def composer(facts):
        return None

    out = asyncio.run(eng.scheduled(now=SAT, composer=composer))
    assert out.get("refused") == "compose_failed"
    assert eng.pending_invite() is None and fin.cash == 500000


def test_tools_accept_and_reject():
    eng, repo, fin = _engine()
    from types import SimpleNamespace
    plugin = SimpleNamespace(social=eng)
    asyncio.run(eng.create_invite(now=SAT, composer=_composer()))
    msg = asyncio.run(llm_tools.accept_social_event_tool(plugin, note="和柳如烟去逛街了", now=SAT))
    assert "要等到" in msg and "现在先别说" in msg
    assert fin.expenses == [] and eng.pending_charges()
    assert eng.pending_invite() is None


def test_red_packet_records():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.red_packet())
    assert out["ok"] and fin.cash > 500000
    assert repo.npc["妈妈"]["last_event"]
    assert not eng.events(5)[0]["display"].startswith("[")
