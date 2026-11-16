# -*- coding: utf-8 -*-
"""社交面板穿帮防线：基金/工具代码名绝不许进「最近关系动态」。"""
import asyncio
import time as _t

import core.amo_bridge as ab
from services.social_engine import (SocialEngine, humanize_dynamic, is_social_kind,
                                    panel_events)


class FakeRepo:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.added = []

    def add_event(self, kind, text="", npc="", status="", amount_cents=0, day="", extra=None,
                  weight=0.0, idem=""):
        self.added.append(kind)
        return True

    def events(self, limit=10):
        return self.rows[:limit]

    def meta_get(self, key, default=None):
        return default

    def meta_set(self, key, value):
        return True


def test_is_social_kind_allowlist():
    assert is_social_kind("social:invite")
    assert is_social_kind("social:criticism")
    assert not is_social_kind("fund_outage")
    assert not is_social_kind("social:status")
    assert not is_social_kind("")


def test_humanize_code_action():
    assert humanize_dynamic("fund_outage", {"status": "skip_fund_trade"}) == "今天不想买基金了"
    assert humanize_dynamic("fund_outage", {"text": "skip_fund_trade"}) == "今天不想买基金了"
    assert humanize_dynamic("fund_outage", {"text": "reallocate_funds"}) == "把钱挪去别处了"
    assert humanize_dynamic("fund_outage", {}) == ""


def test_humanize_strips_code_in_sentence():
    out = humanize_dynamic("social:invite", {"text": "她说 skip_fund_trade 今天不买了"})
    assert "_" not in out
    assert "今天不想买基金了" in out


def test_panel_events_drops_fund_and_code_rows():
    rows = [
        {"id": 33, "kind": "fund_outage", "payload": {"status": "skip_fund_trade"}, "created_at": 1},
        {"id": 34, "kind": "social:invite", "payload": {"text": "约饭 skip_fund_trade"},
         "display": "约饭 skip_fund_trade", "created_at": 2},
        {"id": 35, "kind": "social:reject", "payload": {"text": "在加班"},
         "display": "在加班", "created_at": 3},
    ]
    out = panel_events(rows)
    assert [r["id"] for r in out] == [34, 35]
    assert all("_" not in r["display"] for r in out)
    assert out[0]["display"].startswith("约饭")
    assert "今天不想买基金了" in out[0]["display"]


def test_record_rejects_non_social_kind():
    repo = FakeRepo()
    eng = SocialEngine(None, repo, None)
    assert eng.record("fund_outage", text="skip_fund_trade") is False
    assert repo.added == []
    assert eng.record("social:criticism", "季博达", "被批评了", status="happened") is True
    assert repo.added == ["social:criticism"]


def test_events_filters_fund_rows_even_if_db_has_them():
    now = _t.time()
    repo = FakeRepo([
        {"id": 33, "kind": "fund_outage", "payload": {"status": "skip_fund_trade"}, "created_at": now},
        {"id": 34, "kind": "social:invite", "payload": {"text": "约饭"}, "created_at": now},
    ])
    eng = SocialEngine(None, repo, None)
    rows = eng.events(10)
    assert [r["kind"] for r in rows] == ["social:invite"]
    assert rows[0]["display"] == "约饭"


def test_record_fund_outage_never_writes_events():
    class Repo:
        def __init__(self):
            self.calls = []

        def add_event(self, *a, **k):
            self.calls.append((a, k))
            return True

    class Plugin:
        def __init__(self):
            self.finance_repo = Repo()

    plugin = Plugin()
    bridge = object.__new__(ab.FinanceLink)
    bridge.plugin = plugin

    async def fake_tier(*a, **k):
        return True

    bridge._tier = fake_tier
    bridge._amo = lambda: None
    out = asyncio.run(bridge.record_fund_outage(
        {"day": "2026-11-16", "action": "skip_fund_trade", "time": "11:10"}, idem="x"))
    assert out["ok"] is True
    assert plugin.finance_repo.calls == []
