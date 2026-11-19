# -*- coding: utf-8 -*-
"""关系动态 / 社交事实回归（2026-11-19）。

两个线上穿帮：
1. 中午刚答应晚上的饭局，面板就写「晚上和如烟去吃了烤肉」——把未来的事写成了过去；
2. 她已经恢复健康，社交卡片还在写她发烧——第一人称日记被当成「NPC 最近的状态」回灌。
"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import social_ai  # noqa: E402
from services.social_engine import (LAST_EVENT_MIGRATION, SocialEngine, claims_sick,  # noqa: E402
                                    humanize_dynamic, objective_last, plan_line)
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

NOW = dt.datetime(2026, 11, 19, 11, 30, 0)


class FakeAmo:
    """只带 amo 的健康 dict，够 health_bridge 读。"""

    def __init__(self, status="健康"):
        self.state = type("S", (), {"health": {"status": status, "since": 0, "until": 0}})()

    def _owner_umos(self):
        return []


def _engine(cash=500000, amo=None):
    repo, fin = FakeRepo(), FakeFinance(cash=cash, amo=amo)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def _composer(**over):
    async def composer(facts):
        data = {"npc": "柳如烟", "kind": "meal", "topic": "吃烤肉",
                "place": "楼下小馆", "day": "2026-11-19", "start": "18:30", "end": "20:00",
                "budget_cents": 12000, "ask": "今晚下班去吃烤肉呗？顺便让你看看我新染的头发！"}
        data.update(over)
        return data
    return composer


def _invite(eng, now=None, **over):
    base = dt.datetime.now() + dt.timedelta(days=1)
    over.setdefault("day", base.date().isoformat())
    if now is None:
        now = base.replace(hour=12, minute=0, second=0, microsecond=0)
    out = asyncio.run(eng.create_invite(now=now, composer=_composer(**over)))
    assert out["ok"], out
    return out["invite"]


def _payload(**over):
    data = {"text": "好呀，晚上 18:30 见～", "npc": "柳如烟", "topic": "吃烤肉",
            "place": "楼下小馆", "day": "2026-11-19", "start": "18:30", "end": "20:00",
            "note": "晚上和柳如烟去吃了烤肉，她那个新发色还挺显白的，聊得特别开心。"}
    data.update(over)
    return data


# ---------- 1. 未来的事不许当天就写成「已经发生」 ----------

def test_accept_card_before_event_is_plan_not_recap():
    payload = _payload()
    noon = dt.datetime(2026, 11, 19, 12, 0, 0).timestamp()
    text = humanize_dynamic("social:accept", payload, now=noon)
    assert text == payload["text"], text
    assert "吃了" not in text


def test_accept_card_after_event_shows_recap():
    payload = _payload()
    later = dt.datetime(2026, 11, 19, 21, 0, 0).timestamp()
    assert humanize_dynamic("social:accept", payload, now=later) == payload["note"]


def test_accept_card_without_said_text_falls_back_to_plan():
    payload = _payload(text="", note="晚上和柳如烟去吃了烤肉")
    noon = dt.datetime(2026, 11, 19, 12, 0, 0).timestamp()
    text = humanize_dynamic("social:accept", payload, now=noon)
    assert text == plan_line("柳如烟", "2026-11-19", "18:30", "吃烤肉", "楼下小馆")
    assert text.startswith("答应了柳如烟") and "吃了" not in text


def test_accept_records_objective_last_event_and_plan_memory():
    eng, repo, fin = _engine()
    _invite(eng)
    note = "晚上和柳如烟去吃了烤肉，她那个新发色还挺显白的。"
    res = asyncio.run(eng.accept_invite(note=note, now_line="好呀，晚上 18:30 见～"))
    assert res["ok"]
    assert res["line"] == "好呀，晚上 18:30 见～"
    row = repo.npc["柳如烟"]
    assert row["last_event"].startswith("她答应了柳如烟")
    assert "我" not in row["last_event"]
    # 记忆写的是「答应了、接下来去」，不是「已经吃过」
    assert fin.memories and "答应了柳如烟" in fin.memories[0]
    assert "吃了" not in fin.memories[0]
    # 事件里 still 保留事后那句，等日程过完再由面板显示
    evt = [row for row in repo.log if row["kind"] == "social:accept"][-1]
    assert evt["payload"]["note"] == note
    assert evt["payload"]["text"] == "好呀，晚上 18:30 见～"


# ---------- 2. 健康了就不许再写她生病 ----------

def test_health_guard_blocks_fake_sickness_when_healthy():
    eng, repo, fin = _engine(amo=FakeAmo("健康"))
    _invite(eng)
    res = asyncio.run(eng.reject_invite(reason="烧得难受", note="我烧得难受，改天再陪她挑吧"))
    assert res["ok"]
    assert res["line"] == "她婉拒了柳如烟"
    assert "烧" not in res["line"]
    assert repo.npc["柳如烟"]["last_event"].startswith("她婉拒了柳如烟")
    assert not claims_sick(repo.npc["柳如烟"]["last_event"])


def test_health_guard_keeps_text_when_actually_sick():
    eng, repo, fin = _engine(amo=FakeAmo("感冒"))
    _invite(eng)
    res = asyncio.run(eng.reject_invite(reason="感冒", note="我发烧还没退，今天不去了"))
    assert res["ok"]
    assert res["line"] == "我发烧还没退，今天不去了"


def test_compose_facts_labels_npc_history_and_real_health():
    eng, repo, fin = _engine(amo=FakeAmo("健康"))
    eng.ensure_seed()
    repo.set_npc_event("柳如烟", objective_last("social:reject", "柳如烟", "逛商场",
                                               "2026-11-18", "18:30"))
    facts = eng.compose_facts(NOW, repo.npcs())
    assert "最近一次：" in facts["status"]
    assert "她婉拒了柳如烟" in facts["status"]
    assert "身体健康" in facts["her_state"]


def test_migration_clears_first_person_last_event_only_once():
    eng, repo, fin = _engine()
    repo.upsert_npc("柳如烟", "friend", "闺蜜", 88, "#社交_柳如烟")
    repo.set_npc_event("柳如烟", "如烟约我下班逛商场，我实在烧得难受，改天再陪她挑吧。")
    eng.ensure_seed()
    assert repo.npc["柳如烟"]["last_event"] == ""
    assert repo.meta[LAST_EVENT_MIGRATION] == "1"
    # 迁移只跑一次：之后写进去的客观句不会再被清
    repo.set_npc_event("柳如烟", objective_last("social:reject", "柳如烟", "逛商场"))
    eng.ensure_seed()
    assert repo.npc["柳如烟"]["last_event"].startswith("她婉拒了柳如烟")


# ---------- 3. 提示词契约 ----------

def test_prompt_requires_role_split_and_future_tense():
    system = "\n".join(social_ai.COMPOSE_SYSTEM)
    assert "不要替她说话" in system
    assert '"now"' in social_ai.PROMPT
    assert "已经去过" in social_ai.PROMPT
    assert "不许说她发烧" in " ".join(social_ai.SYSTEM)
    assert "已经发生" in " ".join(social_ai.SYSTEM)
