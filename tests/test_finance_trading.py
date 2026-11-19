# -*- coding: utf-8 -*-
import asyncio

import pytest

import services.trade_engine as ft
from services.market_api import parse_quotes
from models.ledger import FinanceRepo
from services.trade_engine import Trading
from models.db import Store


class FakeWallet:
    def __init__(self, cents=2000000):
        self.cents = cents
        self.seen = []

    def balance(self):
        return self.cents

    def debit(self, cents, reason, idem, ref=""):
        if self.cents < cents:
            return False
        self.cents -= cents
        self.seen.append(("debit", cents, idem))
        return True

    def credit(self, cents, reason, idem, ref=""):
        self.cents += cents
        self.seen.append(("credit", cents, idem))
        return True


class FakeAmo:
    def __init__(self, cents=2000000):
        self.wallet = FakeWallet(cents)


class FakeFinance:
    def __init__(self, cents=2000000):
        self.amo = FakeAmo(cents)
        self.calls = []

    def _amo(self):
        return self.amo

    async def record_trade(self, *args, **kwargs):
        self.calls.append(("record_trade", args, kwargs))
        return {"ok": True}

    async def record_fund_fact(self, *args, **kwargs):
        self.calls.append(("record_fund_fact", args, kwargs))
        return {"ok": True}

    async def trade_pnl(self, *args, **kwargs):
        self.calls.append(("trade_pnl", args, kwargs))
        return {"ok": True}


class FakeRuntime:
    async def run_io(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    finance = FakeFinance()
    trading = Trading(None, FakeRuntime(), repo, finance)
    yield trading, repo, finance, store
    store.close()


def test_buy_costs_fees_and_updates_position(env, monkeypatch):
    trading, repo, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    res = asyncio.run(trading.buy("600519", 100, price=100.0, idem="b1"))
    assert res["ok"] is True, res
    # 100 元 * 100 股 = 1,000,000 分；佣金 500 + 过户费 10
    pos = repo.position("stock", "600519")
    assert pos["qty"] == 100 and pos["available"] == 0
    assert finance.amo.wallet.cents == 2000000 - 1000000 - 510
    assert finance.calls and finance.calls[0][0] == "record_trade"


def test_buy_rejects_non_lot(env, monkeypatch):
    trading, _, _, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    assert asyncio.run(trading.buy("600519", 150, price=100.0))["ok"] is False


def test_buy_rejects_outside_session(env, monkeypatch):
    trading, _, _, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: False)
    res = asyncio.run(trading.buy("600519", 100, price=100.0))
    assert res["ok"] is False and "交易时段" in res["message"]


def test_buy_rejects_price_limit(env, monkeypatch):
    trading, _, _, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    monkeypatch.setattr(trading, "_quote", lambda code, price=None: {"price": 120.0, "prev_close": 100.0, "name": "x"})
    res = asyncio.run(trading.buy("600519", 100, price=120.0))
    assert res["ok"] is False and "涨跌幅" in res["message"]


def test_buy_rejects_insufficient_balance(env, monkeypatch):
    trading, _, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    finance.amo.wallet.cents = 100
    res = asyncio.run(trading.buy("600519", 100, price=100.0))
    # 钱不够时先被「单笔 ≤ 当前现金 50%」挡住，不会透支
    assert res["ok"] is False and ("50%" in res["message"] or "余额不足" in res["message"])


def test_sell_needs_t_plus_1(env, monkeypatch):
    trading, repo, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    asyncio.run(trading.buy("600519", 100, price=100.0, idem="b2"))
    res = asyncio.run(trading.sell("600519", 100, price=105.0))
    assert res["ok"] is False and "T+1" in res["message"]
    # T+1 到期：把「今天」拨到 ready_day 当天，定时任务才会把份额放出来。
    # （2026-11-19：refresh_available 修好 ready_day 过滤前，这里一直靠 bug 立即放行。）
    monkeypatch.setattr(trading, "_day",
                        lambda: repo.position("stock", "600519")["ready_day"])
    trading.refresh_available()
    pos = repo.position("stock", "600519")
    assert pos["available"] == 100
    res = asyncio.run(trading.sell("600519", 100, price=105.0, idem="s2"))
    assert res["ok"] is True, res
    assert repo.position("stock", "600519") is None
    assert finance.calls[-1][0] == "trade_pnl"


def test_fund_subscribe_fee_and_shares(env):
    trading, repo, _, _ = env
    res = asyncio.run(trading.subscribe("005827", 100000, nav=2.0, idem="f1"))
    assert res["ok"] is True
    # 1000 元，申购费 0.12% -> 1.2 元；998.8 / 2.0 = 499.4 份
    assert abs(res["shares"] - 499.4) < 0.01
    assert abs(repo.position("fund", "005827")["qty"] - 499.4) < 0.01


def test_quote_parser():
    text = 'v_sh600519="1~贵州茅台~600519~1680.00~1670.00~1685.00~";'
    rows = parse_quotes(text)
    assert rows["600519"]["name"] == "贵州茅台"
    assert rows["600519"]["price"] == 1680.0
    assert rows["600519"]["prev_close"] == 1670.0
def test_invest_cap_is_half_of_deposit(env, monkeypatch):
    trading, _, finance, _ = env
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    # 现金 20,000 元：买 100 股 × 100 元 = 10,000 元（正好 50%）
    ok = asyncio.run(trading.buy("600519", 100, price=100.0, idem="cap1"))
    assert ok["ok"] is True
    # 再买 100 股 = 10,000 元，超过当前现金 9,490 的 50%，仍会被拒
    res = asyncio.run(trading.buy("600519", 100, price=100.0, idem="cap2"))
    assert res["ok"] is False and "50%" in res["message"]


def _trading_cash(repo, cents):
    finance = FakeFinance(cents)
    return Trading(None, FakeRuntime(), repo, finance), finance


def test_buy_clips_oversized_order_to_half_cash(env, monkeypatch):
    """现金 6000 元 → 单笔上限 3000 元：下 400 股（4000 元）应裁到 300 股。"""
    _, repo, _, _ = env
    trading, finance = _trading_cash(repo, 600000)
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    res = asyncio.run(trading.buy("601818", 400, price=10.0, idem="clip1"))
    assert res["ok"] is True, res
    assert repo.position("stock", "601818")["qty"] == 300
    assert finance.amo.wallet.cents >= 0


def test_second_order_allowed_per_order_cap(env, monkeypatch):
    """旧口径累计超 50% 会拒第二单；新口径只卡单笔，第二单 1000 元 ≤ 当前现金一半。"""
    _, repo, _, _ = env
    trading, _ = _trading_cash(repo, 600000)
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    assert asyncio.run(trading.buy("601818", 300, price=10.0, idem="po1"))["ok"] is True
    res = asyncio.run(trading.buy("601988", 100, price=10.0, idem="po2"))
    assert res["ok"] is True, res


def test_reject_when_one_lot_exceeds_half_cash(env, monkeypatch):
    _, repo, _, _ = env
    trading, _ = _trading_cash(repo, 600000)
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    res = asyncio.run(trading.buy("601988", 100, price=40.0, idem="big1"))
    assert res["ok"] is False and "50%" in res["message"]


def test_incident_20261118_both_orders_fit(env, monkeypatch):
    """11-18 真实参数：现金 6604.26 元，601988 7.36 元；500 股应裁到 400 股，随后 500 元基金也放得下。"""
    _, repo, _, _ = env
    trading, _ = _trading_cash(repo, 660426)
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    res = asyncio.run(trading.buy("601988", 500, price=7.36, idem="inc1"))
    assert res["ok"] is True, res
    assert repo.position("stock", "601988")["qty"] == 400
    fund = asyncio.run(trading.subscribe("110020", 50000, nav=1.65, idem="inc2"))
    assert fund["ok"] is True, fund


def test_fund_subscribe_uses_per_order_cap(env, monkeypatch):
    """已持有 3000 元股票时，500 元基金申购仍应通过（单笔口径）。"""
    _, repo, _, _ = env
    trading, _ = _trading_cash(repo, 600000)
    monkeypatch.setattr(ft, "in_session", lambda now: True)
    assert asyncio.run(trading.buy("601818", 300, price=10.0, idem="pf1"))["ok"] is True
    res = asyncio.run(trading.subscribe("110020", 50000, nav=1.5, idem="pf2"))
    assert res["ok"] is True, res
