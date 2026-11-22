# -*- coding: utf-8 -*-
"""社交推送文案：她回 NPC 的话绝不能转给老公（2026-11-22 11:31 线上）。

事故复盘：
  11:31 妈妈约她中午回家吃饭，她回妈妈
  「妈，我地铁上呢，中午回家吃，随便做点就行，我马上到。」
  social_engine._apply 把 accept_invite 返回的 line（她对妈妈说的话）放进 out["push"]，
  core/jobs 又原样 request_push 给老公——老公收到的是她对妈妈发的微信。

口径（task-task-33）：
  1. _apply 不再把 NPC 台词塞进 push；
  2. push 里是她对老公说的转述：「老公，妈妈约我明天中午回家吃饭，我答应了」；
  3. social_ai.PROMPT 明确 now 只回 NPC、不会发给老公；
  4. 妈妈 / 朋友 / 同事三种样例都要对老公可读，且 push 里没有「妈，我……」。
"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import social_ai  # noqa: E402
from services.social_engine import SocialEngine, relay_line  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

NOW = dt.datetime(2026, 11, 22, 11, 31)  # 周日 11:31，线上事故时刻
MOM_REPLY = "妈，我地铁上呢，中午回家吃，随便做点就行，我马上到。"


def _engine():
    repo, fin = FakeRepo(), FakeFinance(cash=500000)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def _composer(npc, kind, topic, place, day, start, end, budget, ask):
    async def composer(facts):
        return {"npc": npc, "kind": kind, "topic": topic, "place": place,
                "day": day, "start": start, "end": end,
                "budget_cents": budget, "ask": ask}
    return composer


def _decider(now_line, action="accept", note=""):
    async def decider(invite, facts):
        return {"action": action, "ask": str(invite.get("ask") or ""),
                "reason": "想去" if action == "accept" else "不太想去",
                "now": now_line, "note": note or now_line}
    return decider


def _run(composer, decider, seed=None):
    eng, repo, fin = _engine()
    for name, circle, relation, affinity in (seed or []):
        repo.upsert_npc(name, circle, relation, affinity)
    out = asyncio.run(eng.scheduled(now=NOW, decider=decider, composer=composer))
    return eng, repo, fin, out


def _push_text(out):
    pushes = [str(item) for item in (out.get("push") or []) if str(item).strip()]
    assert pushes, "答应/婉拒之后必须给老公一句能看懂的转述：%r" % (out,)
    return pushes[0]


# ---------- 1. 11:31 妈妈约饭：push 不是她对妈妈说的那句 ----------

def test_mom_lunch_push_is_relay_not_her_reply_to_mom():
    eng, repo, fin, out = _run(
        _composer("妈妈", "meal", "回家吃饭", "父母家", "2026-11-22", "12:00", "13:30",
                  0, "中午回家吃，随便做点就行"),
        _decider(MOM_REPLY, note="中午回妈妈家吃饭，她说随便做点"))
    assert out["decision"]["ok"], out
    # 她对妈妈说的那句仍然留在事件/卡片的 line 里——它只属于「我 → 妈妈」这个方向
    assert out["decision"]["line"] == MOM_REPLY
    text = _push_text(out)
    assert MOM_REPLY not in text
    assert "妈，我" not in text and not text.startswith("妈")
    # 11:31 约 12:00 只剩不到 30 分钟，engine 会顺延到明天；转述必须把日子说清楚
    assert text == "老公，妈妈约我明天中午回家吃饭，我答应了"


# ---------- 2. 朋友 / 同事样例：同样只推「谁约我什么」 ----------

def test_friend_push_is_relay_not_her_reply():
    eng, repo, fin, out = _run(
        _composer("柳如烟", "shopping", "逛商场", "", "2026-11-22", "14:00", "17:00",
                  25000, "下午有空吗？陪我去商场逛逛呗"),
        _decider("如烟，我下午过去找你，商场见。", note="下午和如烟去逛商场"))
    assert out["decision"]["ok"], out
    text = _push_text(out)
    assert text == "老公，柳如烟约我今天下午逛商场，我答应了"
    assert "如烟，" not in text
    assert out["decision"]["line"] == "如烟，我下午过去找你，商场见。"


def test_colleague_push_is_relay_not_her_reply():
    eng, repo, fin, out = _run(
        _composer("季博达", "work", "加班", "公司", "2026-11-22", "19:00", "21:00",
                  0, "周日来公司加个班，赶一下项目"),
        _decider("季总，我明天晚上过去加班。", note="明天晚上去公司加班"),
        seed=[("季博达", "work", "领导", 90)])
    assert out["decision"]["ok"], out
    text = _push_text(out)
    assert text == "老公，季博达约我明天晚上加班，我答应了"
    assert "季总" not in text
    assert out["decision"]["line"] == "季总，我明天晚上过去加班。"


def test_reject_push_is_relay_not_her_reply():
    eng, repo, fin, out = _run(
        _composer("白凝冰", "meal", "吃火锅", "楼下小馆", "2026-11-23", "18:30", "20:00",
                  0, "明天晚上一起吃火锅呀"),
        _decider("凝冰，明天我去不了，改天我请你。", action="reject",
                 note="明天婉拒了凝冰，实在不想动"))
    assert out["decision"]["ok"], out
    text = _push_text(out)
    assert text == "老公，白凝冰约我明天晚上吃火锅，我婉拒了"
    assert "凝冰，" not in text
    assert out["decision"]["line"] == "明天婉拒了凝冰，实在不想动"


# ---------- 3. 转述本身的契约 ----------

def test_relay_line_is_husband_readable():
    assert relay_line("妈妈", "accept", day="2026-11-23", start="12:00",
                      topic="回家吃饭", now=NOW) == "老公，妈妈约我明天中午回家吃饭，我答应了"
    assert relay_line("柳如烟", "accept", day="2026-11-22", start="14:00",
                      topic="逛商场", now=NOW) == "老公，柳如烟约我今天下午逛商场，我答应了"
    assert relay_line("方圆", "reject", day="2026-11-23", start="19:00",
                      topic="加班", now=NOW) == "老公，方圆约我明天晚上加班，我婉拒了"
    # 没有 topic 时退到地点；地点也没有就只说见个面，不留半句
    assert relay_line("方圆", "accept", day="2026-11-23", start="19:00",
                      place="公司", now=NOW) == "老公，方圆约我明天晚上去公司，我答应了"


def test_prompt_says_now_only_replies_to_npc():
    assert "不会发给老公" in social_ai.PROMPT
    assert "只回对方" in social_ai.PROMPT
    assert "老公" in social_ai.PROMPT
    assert "不会发给老公" in " ".join(social_ai.SYSTEM)
