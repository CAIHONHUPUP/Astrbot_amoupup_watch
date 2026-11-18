# -*- coding: utf-8 -*-
"""收盘 500 元基金首次申购：与股票是否成交无关，只要还没基金持仓（2026-11-18 线上事故回归）。"""
import asyncio
import datetime as dt
import json

from core.agent_loop import FinanceDecision

NOW = dt.datetime(2026, 11, 18, 14, 50)
NOON = dt.datetime(2026, 11, 18, 11, 35)


class FakeMemory:
    def __init__(self):
        self.written = []

    async def remember(self, *args, **kwargs):
        self.written.append((args, kwargs))
        return True


class FakeFinance:
    def __init__(self, cash=660426):
        self.cash = cash
        self.memory = FakeMemory()

    def balance_cents(self):
        return self.cash

    def mood(self):
        return {"word": "平静", "score": 60}

    def _amo(self):
        return None


class FakeRepo:
    def __init__(self, positions=None):
        self.meta = {}
        self._positions = list(positions or [])

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def positions(self, kind=None):
        if kind:
            return [row for row in self._positions if row.get("kind") == kind]
        return list(self._positions)


class FakeTrading:
    def __init__(self, buy_ok=False):
        self.buy_ok = buy_ok
        self.calls = []

    async def buy(self, code, qty, price=None, idem=""):
        self.calls.append(("buy", str(code), int(qty)))
        return {"ok": self.buy_ok,
                "message": "成交" if self.buy_ok else "投资不能超过当前存款的 50%"}

    async def subscribe(self, code, amount_cents, nav=None, idem="", name=""):
        self.calls.append(("subscribe", str(code), int(amount_cents), nav, name))
        return {"ok": True, "message": "申购成功"}


def _dec(trading, positions=None, cash=660426):
    dec = FinanceDecision(None, None, FakeRepo(positions), FakeFinance(cash), trading)
    dec._fund_codes = {"110020"}
    dec._fund_quotes = {"110020": {"code": "110020", "name": "易方达沪深300ETF联接A",
                                   "estimate": 1.65, "tradeable": True}}
    return dec


BUY = {"action": "buy", "code": "601818", "qty": 100, "reason": "空仓试一笔"}
FUND_ROW = {"kind": "fund", "code": "110020", "qty": 300.0, "cost_cents": 1650}


def _fund_entry(dec):
    return json.loads(dec.repo.meta_get("last_decision"))["fund_entry"]


def test_failed_stock_buy_still_buys_fund():
    trading = FakeTrading(buy_ok=False)
    dec = _dec(trading)
    asyncio.run(dec._slot_settle("close", dict(BUY), NOW))
    assert ("subscribe", "110020", 50000, 1.65, "易方达沪深300ETF联接A") in trading.calls
    assert _fund_entry(dec)["executed"] is True


def test_successful_stock_buy_also_buys_fund():
    trading = FakeTrading(buy_ok=True)
    dec = _dec(trading)
    asyncio.run(dec._slot_settle("close", dict(BUY), NOW))
    assert [call[0] for call in trading.calls] == ["buy", "subscribe"]
    assert _fund_entry(dec)["executed"] is True


def test_existing_fund_position_skips_fund():
    trading = FakeTrading(buy_ok=False)
    dec = _dec(trading, positions=[FUND_ROW])
    asyncio.run(dec._slot_settle("close", dict(BUY), NOW))
    assert [call[0] for call in trading.calls] == ["buy"]
    assert "fund_entry" not in json.loads(dec.repo.meta_get("last_decision"))


def test_no_fund_when_not_close_slot():
    trading = FakeTrading(buy_ok=False)
    dec = _dec(trading)
    asyncio.run(dec._slot_settle("noon", dict(BUY), NOON))
    assert trading.calls == []


def test_no_fund_when_cash_below_500():
    trading = FakeTrading(buy_ok=False)
    dec = _dec(trading, cash=40000)
    asyncio.run(dec._slot_settle("close", dict(BUY), NOW))
    assert [call[0] for call in trading.calls] == ["buy"]
