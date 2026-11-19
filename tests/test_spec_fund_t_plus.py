# -*- coding: utf-8 -*-
"""产品级行为规格（弱点4）· 组 2：基金 T+1/T+2 释放。

「给定 X → 必须 Y」：

1. 给定基金（场外）买入确认日 → 份额按 T+2 释放：确认当天不可赎回，
   T+1 仍不可赎回，T+2 当天可赎回（股票 T+1 作为对照）；
2. 给定未到账的赎回款 → 到账日之前不可用，到账日当天可用；
3. 给定重复释放 / 重复 tick → 份额不翻倍、钱不重复加。

实现事实（2026-11-19 grep 后确认）：
- 份额：services/trade_engine.py::Trading._ready_day —— 基金 T+2、股票 T+1，
  Trading.refresh_available 到期才把 available 放出来；
- 赎回款：Trading.redeem 写 pending_credits(kind="cash_withdrawable", due=+7 天)，
  core/jobs.py::process_pending 到期 credit 一次并 finish_pending。
  （规格里写的「货币/债基 T+1」在现有实现里不存在：所有场外基金统一 T+2，
   赎回款统一 T+7 到账；本文件按真实行为写规格，并在报告里注明这条偏差。）

时间：Trading._now 直接读 datetime.now()，用例里 monkeypatch Trading._now
（只改测试侧，不动生产代码）；process_pending 读 time.strftime，用例里
把 core.jobs.time 换成固定日期的 shim。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import types

import pytest

import core.jobs as jobs
import services.trade_engine as te
from services.trade_engine import Trading
from fixtures import FakeFinance, temp_store

FUND = "005827"
STOCK = "600000"


class _Runtime:
    async def run_db(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)

    async def run_io(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


class _Finance(FakeFinance):
    """补上交易侧需要的 record_trade / trade_pnl（FakeFinance 只覆盖账本入口）。"""

    def __init__(self, cash=2000000):
        super().__init__(cash=cash)
        self.trades = []

    async def record_trade(self, *args, **kwargs):
        self.trades.append((args, kwargs))
        return {"ok": True}

    async def trade_pnl(self, *args, **kwargs):
        return {"ok": True}


class _Plugin:
    def __init__(self, finance, repo, trading):
        self.finance = finance
        self.finance_repo = repo
        self.trading = trading
        self.runtime = _Runtime()


@pytest.fixture(autouse=True)
def _spec_clock():
    from datetime import datetime as _dt
    from utils.clock import freeze, unfreeze

    freeze(_dt(2026, 11, 18, 15, 0))
    try:
        yield
    finally:
        unfreeze()


@pytest.fixture()
def spec(tmp_path, monkeypatch):
    store, repo = temp_store(tmp_path)          # 真 SQLite（临时库，随 tmp_path 清理）
    finance = _Finance(cash=2000000)
    clock = {"now": dt.datetime(2026, 11, 16, 15, 0)}
    monkeypatch.setattr(te.Trading, "_now", lambda self: clock["now"])
    trading = Trading(None, _Runtime(), repo, finance)
    plugin = _Plugin(finance, repo, trading)

    def at(moment):
        clock["now"] = moment
        return moment

    def release(day, kind="fund", code=FUND):
        at(day)
        trading.refresh_available()
        pos = repo.position(kind, code)
        return float(pos["available"]) if pos else 0.0

    yield {"repo": repo, "finance": finance, "trading": trading,
           "plugin": plugin, "at": at, "release": release, "wallet": finance.amo.wallet}
    store.close()


def _set_today(monkeypatch, day):
    monkeypatch.setattr(jobs, "time", types.SimpleNamespace(
        strftime=lambda fmt, *args, **kwargs: day))


def test_spec_fund_share_release_waits_for_t_plus_two(spec):
    """给定基金确认日 11-16 → 份额 11-18（T+2）才可用；之前赎回被拒。"""
    at, trading, repo, release = spec["at"], spec["trading"], spec["repo"], spec["release"]

    at(dt.datetime(2026, 11, 16, 15, 0))
    out = asyncio.run(trading.subscribe(FUND, 100000, nav=2.0, idem="buy-1", name="易方达"))
    assert out["ok"] is True
    pos = repo.position("fund", FUND)
    qty = float(pos["qty"])
    assert qty > 0
    assert pos["ready_day"] == "2026-11-18"      # T+2：11-17 再下一交易日
    assert float(pos["available"]) == 0.0        # 确认当天不可赎回

    assert release(dt.datetime(2026, 11, 16, 16, 0)) == 0.0    # T+0
    assert release(dt.datetime(2026, 11, 17, 10, 0)) == 0.0    # T+1 仍不可用
    early = asyncio.run(trading.redeem(FUND, 1, nav=2.0, idem="redeem-early"))
    assert early["ok"] is False and "不足" in early["message"]

    assert release(dt.datetime(2026, 11, 18, 9, 0)) == pytest.approx(qty)   # T+2 当天可用
    ok = asyncio.run(trading.redeem(FUND, 1, nav=2.0, idem="redeem-ok"))
    assert ok["ok"] is True


def test_spec_fund_release_never_doubles_shares(spec):
    """给定：重复 tick / 到期后隔天再 tick → 可用份额不翻倍。"""
    at, trading, repo, release = spec["at"], spec["trading"], spec["repo"], spec["release"]
    at(dt.datetime(2026, 11, 16, 15, 0))
    asyncio.run(trading.subscribe(FUND, 100000, nav=2.0, idem="buy-1"))
    qty = float(repo.position("fund", FUND)["qty"])

    for day in (dt.datetime(2026, 11, 18, 9, 0),
                dt.datetime(2026, 11, 18, 10, 0),
                dt.datetime(2026, 11, 19, 10, 0)):
        assert release(day) == pytest.approx(qty)
    assert float(repo.position("fund", FUND)["available"]) == pytest.approx(qty)


def test_spec_stock_share_release_is_t_plus_one(spec):
    """对照：股票 T+1（买入次日）才可卖；基金是 T+2。"""
    at, trading, repo, release = spec["at"], spec["trading"], spec["repo"], spec["release"]
    at(dt.datetime(2026, 11, 16, 10, 0))
    out = asyncio.run(trading.buy(STOCK, 100, price=10.0, idem="stock-1"))
    assert out["ok"] is True
    pos = repo.position("stock", STOCK)
    assert pos["ready_day"] == "2026-11-17"
    assert float(pos["available"]) == 0.0
    assert release(dt.datetime(2026, 11, 16, 14, 0), "stock", STOCK) == 0.0
    assert release(dt.datetime(2026, 11, 17, 10, 0), "stock", STOCK) == pytest.approx(100.0)


def _redeem_ready_fund(spec):
    """在 T+2 当天赎回 50 份，返回净到账金额（分）。"""
    repo, trading = spec["repo"], spec["trading"]
    repo.upsert_position("fund", FUND, "易方达", 100.0, 200000, 250000, 100.0, "")
    spec["at"](dt.datetime(2026, 11, 18, 10, 0))
    out = asyncio.run(trading.redeem(FUND, 50, nav=2.5, idem="redeem-1"))
    assert out["ok"] is True
    amount = int(round(2.5 * 50 * 100))
    return amount - int(round(amount * 0.005))


def test_spec_fund_redemption_cash_waits_for_due_day(spec, monkeypatch):
    """给定赎回确认日 11-18（到账 11-25）→ 11-24 不可用，11-25 当天可用且只加一次。"""
    repo, wallet = spec["repo"], spec["wallet"]
    net = _redeem_ready_fund(spec)
    assert net == 12438

    pending = repo.due_pending("2026-11-25")
    assert [row["kind"] for row in pending] == ["cash_withdrawable"]

    cash_before = wallet.balance("cash")
    withdraw_before = wallet.balance("withdrawable")

    _set_today(monkeypatch, "2026-11-24")
    asyncio.run(jobs.process_pending(spec["plugin"]))
    assert wallet.balance("cash") == cash_before                 # 未到账不可用
    assert wallet.balance("withdrawable") == withdraw_before

    _set_today(monkeypatch, "2026-11-25")
    asyncio.run(jobs.process_pending(spec["plugin"]))
    assert wallet.balance("cash") == cash_before + net           # 到账日当天可用
    assert wallet.balance("withdrawable") == withdraw_before + net

    asyncio.run(jobs.process_pending(spec["plugin"]))             # 同日重复 tick
    assert wallet.balance("cash") == cash_before + net
    assert wallet.balance("withdrawable") == withdraw_before + net


def test_spec_fund_duplicate_release_credit_is_blocked(spec, monkeypatch):
    """给定：重复释放（finish_pending 丢了、重放同一 idem）→ 钱只加一次。"""
    wallet = spec["wallet"]
    net = _redeem_ready_fund(spec)
    cash_before, withdraw_before = wallet.balance("cash"), wallet.balance("withdrawable")

    _set_today(monkeypatch, "2026-11-25")
    asyncio.run(jobs.process_pending(spec["plugin"]))
    assert wallet.balance("cash") == cash_before + net

    # 崩溃重放：同一 idem 再 credit 一次，钱包必须当重复丢掉
    assert wallet.credit(net, "fund_redeem_cash", "redeem-1") is False
    assert wallet.credit(net, "fund_redeem_withdrawable", "redeem-1:w",
                         account="withdrawable") is False
    assert wallet.balance("cash") == cash_before + net
    assert wallet.balance("withdrawable") == withdraw_before + net
