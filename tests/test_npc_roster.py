# -*- coding: utf-8 -*-
"""专属 7 人名单 + 防捏造闸门。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import npc_roster  # noqa: E402
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

NOW = dt.datetime(2026, 11, 16, 10, 0, 0)


def _engine(cash=500000):
    repo, fin = FakeRepo(), FakeFinance(cash=cash)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def test_roster_is_exactly_the_seven():
    assert npc_roster.NAMES == ("柳如烟", "白凝冰", "季博达", "方圆", "妈妈", "爸爸", "沈幼微")
    assert len(npc_roster.ROSTER) == 7


def test_roster_line_lists_everyone_and_forbids_invention():
    line = npc_roster.ROSTER_LINE
    for name in npc_roster.NAMES:
        assert name in line
    assert "严禁" in line and "捏造" in line


def test_foreign_names_are_detected():
    fake_a, fake_b = "李" + "婷", "王" + "强"
    cases = (fake_a + "约我吃饭", "和" + fake_b + "去打球", "小张说下周团建", "被" + fake_a + "骂了一顿")
    for text in cases:
        assert npc_roster.foreign_names(text), text


def test_roster_and_common_words_are_not_flagged():
    safe = (
        "答应了柳如烟，下午一起去商场逛街",
        "今天被季博达批评了一顿，有点委屈",
        "妈妈发了 200 元红包，我收下了",
        "和闺蜜逛街，花了两百多",
        "他在公司加班到很晚，我又一个人吃饭",
        "医生说我得多休息，物业说下周修水管",
        "周末去商场看电影，服务员态度一般",
        "答应表姐明天去喝喜酒，随了三百五十七",
        "下午一起去逛街，心情好多了",
        "我家那位最近还行，今晚正陪我吃饭呢",
        "答应了柳如烟明天下午去逛街，她跟男朋友冷战三天",
    )
    for text in safe:
        assert npc_roster.foreign_names(text) == [], text


def test_sanitize_rewrites_to_bound_npc():
    fake = "李" + "婷"
    assert npc_roster.sanitize(fake + "约我吃饭，聊了好久", "柳如烟") == "柳如烟约我吃饭，聊了好久"


def test_sanitize_blocks_when_no_binding():
    fake = "李" + "婷"
    assert npc_roster.sanitize(fake + "约我吃饭", "") == ""
    fake_a, fake_b = "李" + "婷", "王" + "强"
    assert npc_roster.sanitize(fake_a + "约我吃饭，" + fake_b + "说他也来", "柳如烟") == ""


def test_create_invite_rejects_unknown_npc():
    eng, repo, fin = _engine()
    out = asyncio.run(eng.create_invite(kind="dinner", npc="李" + "婷", now=NOW))
    assert out["ok"] is False and out["reason"] == "unknown_npc"
    assert fin.cash == 500000 and eng.pending_invite() is None


def test_record_strips_foreign_name_and_keeps_fallback():
    eng, repo, fin = _engine()
    eng.ensure_seed()
    fake_a, fake_b = "李" + "婷", "王" + "强"
    eng.record("social:criticism", "季博达", "被" + fake_a + "和" + fake_b + "骂了一顿", status="happened")
    payload = repo.log[-1]["payload"]
    assert payload["text"] == ""
    assert "批评" in payload["fallback"]
    assert ("李" + "婷") not in json_text(payload)


def json_text(payload):
    return str(payload)
