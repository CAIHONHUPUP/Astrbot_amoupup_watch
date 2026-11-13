# -*- coding: utf-8 -*-
"""数据兜底：新闻每天最多 3 次、失败当日不交易；看病开销分类。"""
import asyncio
import datetime as dt

from core.agent_loop import FinanceDecision
from services.daily_engine import FinanceDaily


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def positions(self):
        return []


class FakeFinance:
    def balance_cents(self):
        return 1000000

    def mood(self):
        return {"word": "平静", "score": 60}


class FakeRuntime:
    async def run_io(self, fn, *a, **k):
        raise RuntimeError("network down")


class FakeTrading:
    pass


def test_quotes_failure_blocks_trading_for_the_day():
    repo = FakeRepo()
    dec = FinanceDecision(None, FakeRuntime(), repo, FakeFinance(), FakeTrading())

    async def _no_quotes(codes):
        return {}

    dec._refresh = _no_quotes
    result = asyncio.run(dec.run_slot("morning", dt.datetime(2026, 11, 16, 9, 30)))
    assert result["skipped"] == "quotes_failed"
    assert repo.meta_get("news_fail:2026-11-16") == "quotes"
    # 当天后续时点不再交易
    again = asyncio.run(dec.run_slot("close", dt.datetime(2026, 11, 16, 14, 50)))
    assert again["skipped"] == "data_failed"


def test_news_quota_is_three_per_day():
    repo = FakeRepo()
    repo.meta_set("news_fetch:2026-11-16", 3)
    dec = FinanceDecision(None, FakeRuntime(), repo, FakeFinance(), FakeTrading())

    async def _quotes(codes):
        return {"600519": {"price": 1650.0, "prev_close": 1620.0, "name": "贵州茅台"}}

    dec._refresh = _quotes
    result = asyncio.run(dec.run_slot("close", dt.datetime(2026, 11, 16, 14, 50)))
    assert result["skipped"] == "news_quota"


class FakeExp:
    def __init__(self):
        self.calls = []

    async def spend(self, category, label, amount, day=None, idem=""):
        self.calls.append((category, label, int(amount)))
        return {"ok": True}


class FakeConsume:
    async def buy_item(self, *a, **k):
        return {"ok": True}


class FakePlugin:
    def __init__(self, exp):
        self.finance_expense = exp
        self.finance_property = None
        self.finance = FakeFinance()


def test_medical_spending_has_category():
    exp = FakeExp()
    plugin = FakePlugin(exp)
    repo = FakeRepo()
    daily = FinanceDaily(plugin, repo, FakeConsume(), None)
    # 找一天会触发看病的（hash < 12/1000），直接调 _extras
    from services.daily_engine import _hash_int
    day = next(d for d in ("2026-11-%02d" % i for i in range(1, 29))
               if _hash_int("medical:" + d, 0, 1000) < 12)
    asyncio.run(daily._extras(dt.datetime.fromisoformat(day + "T12:00"), day))
    med = [c for c in exp.calls if c[0] == "medical"]
    assert med and 3000 <= med[0][2] <= 20000
