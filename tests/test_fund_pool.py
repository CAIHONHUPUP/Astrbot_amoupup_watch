# -*- coding: utf-8 -*-
"""基金候选池：锚点、用户推荐、观察池。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.fund_pool import BASE_FUNDS, FundPool  # noqa: E402
from core.llm_decider import parse_decision  # noqa: E402


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value


class FakePlugin:
    def __init__(self):
        self.finance_repo = FakeRepo()


def test_anchor_pool_has_5_to_8():
    assert 5 <= len(BASE_FUNDS) <= 8


def test_user_fund_recommendation_goes_to_pool():
    pool = FundPool(FakePlugin())
    found = pool.recommend("老婆买点招商中证白酒基金")
    assert "161725" in found
    assert "161725" in pool.recommended()


def test_recommend_pool_reads_json_string_from_real_repo():
    import json
    plugin = FakePlugin()
    plugin.finance_repo.meta["fund_recommend"] = json.dumps(["161725"])
    assert FundPool(plugin).recommended() == ["161725"]


def test_observe_keeps_top_5():
    pool = FundPool(FakePlugin())
    quotes = {str(i): {"code": str(i), "change_pct": float(i)} for i in range(10)}
    assert len(pool.observe(quotes, "2026-11-16")) == 5


def test_fund_decision_parses_buy():
    data = parse_decision('{"action":"buy_fund","code":"110020","amount":500,"reason":"加仓"}',
                          {"110020"})
    assert data["action"] == "buy_fund"
    assert data["amount_cents"] == 50000


def test_fund_decision_parses_sell():
    data = parse_decision('{"action":"sell_fund","code":"110020","shares":100,"reason":"止盈"}',
                          {"110020"})
    assert data["action"] == "sell_fund"
    assert data["shares"] == 100
