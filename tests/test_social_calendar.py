# -*- coding: utf-8 -*-
"""社交作息常识 / 名字硬约束 / 最近关系动态数据源回归。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import social_ai  # noqa: E402
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

MON = dt.datetime(2026, 11, 16, 10, 0, 0)   # 周一
SAT = dt.datetime(2026, 11, 21, 10, 0, 0)   # 周六


def _engine():
    repo, fin = FakeRepo(), FakeFinance()
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def _composer(**data):
    async def composer(_facts):
        return dict(data)
    return composer


def test_ceremony_is_weekend_and_lead_word_is_fixed():
    eng, repo, _ = _engine()
    out = asyncio.run(eng.create_invite(now=SAT, composer=_composer(
        npc="沈幼微", kind="ceremony", day="2026-11-22", start="11:00", end="13:00",
        topic="喝喜酒", ask="明天我结婚，来喝喜酒呀")))
    assert out["ok"], out
    invite = out["invite"]
    day = dt.date.fromisoformat(invite["day"])
    assert day.weekday() >= 5 and (day - SAT.date()).days >= 3
    assert "明天" not in invite["ask"] and "11月28日" in invite["ask"]


def test_friend_weekday_daytime_moves_to_evening_same_day():
    eng, repo, _ = _engine()
    out = asyncio.run(eng.create_invite(now=MON, composer=_composer(
        npc="柳如烟", kind="shopping", day="2026-11-16", start="14:00", end="17:00",
        topic="逛街", ask="下午去逛街吗")))
    assert out["ok"], out
    invite = out["invite"]
    assert invite["day"] == "2026-11-16"
    assert int(invite["start"][:2]) >= 18
    assert "下午" not in invite["ask"] and "晚上" in invite["ask"]


def test_leader_meal_on_weekday_after_work():
    eng, repo, _ = _engine()
    out = asyncio.run(eng.create_invite(now=SAT, composer=_composer(
        npc="季博达", kind="meal", day="2026-11-22", start="12:00", end="13:30",
        topic="吃川菜", ask="明晚吃川菜")))
    assert out["ok"], out
    invite = out["invite"]
    assert dt.date.fromisoformat(invite["day"]).weekday() < 5
    assert int(invite["start"][:2]) >= 18


def test_misspelled_npc_is_repaired_to_roster_name():
    eng, repo, _ = _engine()
    out = asyncio.run(eng.create_invite(now=MON, composer=_composer(
        npc="李博达", kind="meal", day="2026-11-16", start="19:00", end="20:30",
        topic="吃个饭", ask="李博达约你吃饭")))
    assert out["ok"], out
    invite = out["invite"]
    assert invite["npc"] == "季博达"
    assert "李博达" not in invite["ask"]


def test_statuses_are_not_in_event_stream_but_invites_are():
    eng, repo, _ = _engine()
    eng.ensure_seed()
    assert not [row for row in eng.events(20) if row["kind"] == "social:status"]
    repo.add_event("social:status", "旧状态", npc="柳如烟", extra={"text": "旧状态"}, idem="legacy-status")
    assert not [row for row in eng.events(20) if row["kind"] == "social:status"]
    made = asyncio.run(eng.create_invite(now=MON, kind="meal", npc="柳如烟"))
    assert made["ok"]
    assert eng.events(5)[0]["kind"] == "social:invite"


def test_legacy_next_day_ceremony_text_is_cleared():
    eng, repo, _ = _engine()
    eng.ensure_seed()
    repo.set_npc_event("沈幼微", "答应表姐明天去喝喜酒，随了三百五十七")
    eng.ensure_seed()
    assert "明天" not in str(repo.npc_get("沈幼微").get("last_event") or "")


def test_compose_prompt_carries_calendar_facts():
    eng, repo, _ = _engine()
    eng.ensure_seed()
    facts = eng.compose_facts(MON, repo.npcs())
    assert "工作日" in facts["day_facts"]
    prompt = social_ai.COMPOSE_PROMPT.format(**facts)
    assert "当前时间事实" in prompt and "工作" in prompt


def test_social_page_uses_one_current_item_per_card():
    page = open(os.path.join(ROOT, "views", "守护", "index.html"), encoding="utf-8").read()
    assert "n.last_event || n.status" in page
    assert '[n.last_event, n.status]' not in page
