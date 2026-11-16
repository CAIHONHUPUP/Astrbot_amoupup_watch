# -*- coding: utf-8 -*-
"""决策脑到交易：只用当日可交易估值，并把名称一起传下去。"""
import asyncio
import datetime as dt
from types import SimpleNamespace

from core.agent_loop import FinanceDecision


class FakeTrading:
    def __init__(self):
        self.calls = []

    async def subscribe(self, code, amount_cents, nav=None, idem="", name=""):
        self.calls.append((code, amount_cents, nav, name))
        return {"ok": True, "message": "申购成功"}

    async def redeem(self, code, shares, nav=None, idem=""):
        self.calls.append(("redeem", code, shares, nav))
        return {"ok": True, "message": "赎回成功", "pnl": 10.0}


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)


class FakeFinance:
    def _amo(self):
        return None


def _decision(trading):
    return FinanceDecision(None, None, FakeRepo(), FakeFinance(), trading)


def test_buy_fund_uses_today_estimate_and_name():
    trading = FakeTrading()
    dec = _decision(trading)
    dec._fund_quotes = {"005827": {"code": "005827", "name": "易方达蓝筹精选混合",
                                   "estimate": 2.1, "tradeable": True}}
    out = asyncio.run(dec._execute("close", {
        "action": "buy_fund", "code": "005827", "amount_cents": 50000, "reason": "加一点",
    }, dt.datetime(2026, 11, 16, 14, 50)))
    assert out["ok"] is True
    assert trading.calls == [("005827", 50000, 2.1, "易方达蓝筹精选混合")]


def test_buy_fund_rejects_stale_quote():
    trading = FakeTrading()
    dec = _decision(trading)
    dec._fund_quotes = {"005827": {"code": "005827", "name": "易方达",
                                   "estimate": 2.1, "tradeable": False}}
    out = asyncio.run(dec._execute("close", {
        "action": "buy_fund", "code": "005827", "amount_cents": 50000, "reason": "加一点",
    }, dt.datetime(2026, 11, 16, 14, 50)))
    assert out["ok"] is False and "估值" in out["message"]
    assert trading.calls == []


def test_sell_fund_uses_today_estimate():
    trading = FakeTrading()
    dec = _decision(trading)
    dec._fund_quotes = {"005827": {"code": "005827", "name": "易方达",
                                   "estimate": 2.1, "tradeable": True}}
    out = asyncio.run(dec._execute("close", {
        "action": "sell_fund", "code": "005827", "shares": 100, "reason": "止盈",
    }, dt.datetime(2026, 11, 16, 14, 50)))
    assert out["ok"] is True
    assert trading.calls == [("redeem", "005827", 100.0, 2.1)]


class FakeDiaryDb:
    def __init__(self):
        self.saved = []

    def save_diary(self, day, hour, text):
        self.saved.append((day, hour, text))
        return True


class FakeMemory:
    async def remember(self, *args, **kwargs):
        return True


class DiaryFinance:
    def __init__(self):
        self.db = FakeDiaryDb()
        self.memory = FakeMemory()

    def _amo(self):
        return SimpleNamespace(db=self.db)

    def mood(self):
        return {"word": "平静"}


def test_record_does_not_write_fund_diary():
    finance = DiaryFinance()
    dec = FinanceDecision(None, None, FakeRepo(), finance, FakeTrading())
    dec._fund_quotes = {
        "005827": {"code": "005827", "name": "易方达蓝筹精选混合", "estimate": 1.94,
                   "nav": 2.0, "change_pct": -3.0, "tradeable": True},
    }
    asyncio.run(dec._record("close", {
        "action": "buy_fund", "code": "005827", "amount_cents": 50000, "reason": "跌了加一点",
    }, {"ok": True, "message": "申购成功"}, dt.datetime(2026, 11, 16, 14, 50)))
    # 基金日记只能由 FinanceLink.record_fund_fact → LLM 生成，_record 不许拼模板
    assert finance.db.saved == []
