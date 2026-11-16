# -*- coding: utf-8 -*-
"""基金交易只落结构化数据：表里能读回 action/name/amount/份额，不落拟人句。"""
import asyncio
import json
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from models.db import Store  # noqa: E402
from models.ledger import FinanceRepo  # noqa: E402
from core.amo_bridge import FinanceLink  # noqa: E402


class FakePlugin:
    def __init__(self, repo):
        self.finance_repo = repo
        self.finance_memory = None
        self.context = None


class FakeMemory:
    def __init__(self):
        self.calls = []

    async def remember(self, content, kind="", idem=""):
        self.calls.append({"content": content, "kind": kind, "idem": idem})
        return {"ok": True}


def test_repo_fund_trade_fact_roundtrip(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    try:
        repo.add_fund_trade_fact("f1", "buy_fund", "005827", "易方达蓝筹", 50000,
                                 shares=238.10, nav=2.1, fee_cents=60,
                                 day="2026-11-16", ts=1.0)
        rows = repo.fund_trade_facts("2026-11-16")
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "buy_fund"
        assert row["name"] == "易方达蓝筹"
        assert row["amount_cents"] == 50000
        assert row["nav_e4"] == 21000
    finally:
        store.close()


def test_record_fund_fact_memory_is_json_not_prose(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    try:
        link = FinanceLink(FakePlugin(repo))
        link.memory = FakeMemory()
        out = asyncio.run(link.record_fund_fact(
            "buy_fund", "易方达蓝筹", "005827", 50000, shares=238.10,
            nav=2.1, fee_cents=60, idem="fact:1", when="14:50"))
        assert out["ok"] is True
        rows = repo.fund_trade_facts(out["fact"]["day"])
        assert len(rows) == 1
        assert rows[0]["action"] == "buy_fund"
        assert rows[0]["amount_cents"] == 50000
        payload = json.loads(link.memory.calls[-1]["content"])
        assert payload["action"] == "buy_fund"
        assert payload["fund_name"] == "易方达蓝筹"
        assert payload["amount"] == 500.0
        assert payload["time"] == "14:50"
        assert "申购了" not in link.memory.calls[-1]["content"]
    finally:
        store.close()
