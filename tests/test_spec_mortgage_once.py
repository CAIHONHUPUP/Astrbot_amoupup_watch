# -*- coding: utf-8 -*-
"""产品级行为规格（弱点4）· 组 1：房贷同月只扣一次 / 跨月再扣一次。

「给定 X → 必须 Y」：

1. 给定 2026-11 同一笔房贷，手动代缴重复触发、自动代扣重复 tick
   → 必须只扣一次钱（流水只有一行）、只摊销一次本金、余额只减一次；
2. 给定跨月（2026-12）→ 必须能再扣一次，且 12 月仍然只扣一次；
3. 给定幂等键冲突（settlements 行丢了、但钱包里的 idem 还在）
   → 不得再扣第二笔钱。

时间：工资/账单代码里大量 datetime.now()，本文件显式用
utils.clock.freeze(2026-11-18 15:00) 固定，finally unfreeze；
被投机器的 now 由调用参数显式传入，不读墙钟。
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

import services.bill_flow as bf
from services.property_engine import FinanceProperty
from fixtures import FakeFinance

PERIOD = "2026-11"
NOW = dt.datetime(2026, 11, 2, 10, 0)        # 周一 10:00：账单日 + 银行办理时间
DEC = "2026-12"
DEC_NOW = dt.datetime(2026, 12, 1, 10, 0)    # 周二 10:00
MORTGAGE_CENTS = 600000
HOUSE_BALANCE = 100000000
HOUSE_RATE = 0.049


@pytest.fixture(autouse=True)
def _spec_clock():
    """显式冻结时钟：本组用例本身不读墙钟，但账本/日志会打时间戳。"""
    from datetime import datetime as _dt
    from utils.clock import freeze, unfreeze

    freeze(_dt(2026, 11, 18, 15, 0))
    try:
        yield
    finally:
        unfreeze()


class _Repo:
    """房贷仓储最小桩：meta / settlements / loans，签名与 models.ledger.FinanceRepo 一致。"""

    def __init__(self):
        self.data = {}
        self.settled_rows = set()
        self.loans = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value

    def settled(self, period, key):
        return (str(period), str(key)) in self.settled_rows

    def mark_settled(self, period, key, payload="{}"):
        self.settled_rows.add((str(period), str(key)))
        return True

    def assets(self):
        return []

    def asset(self, kind):
        return None

    def positions(self, kind=""):
        return []

    def loan(self, kind):
        return self.loans.get(str(kind))

    def update_loan(self, loan_id, balance_cents, status=""):
        for loan in self.loans.values():
            if int(loan.get("id") or 0) == int(loan_id):
                loan["balance_cents"] = int(balance_cents)
                if status:
                    loan["status"] = status
        return True


class _Life:
    """有房：不再产生房租；本组只关心房贷，水电为空。"""

    def _has(self, kind):
        return str(kind) == "house"

    def rent_cents(self, period):
        return 300000

    def utility_plan(self, period):
        return ()


class _Plugin:
    def __init__(self, repo, finance, prop):
        self.finance_repo = repo
        self.finance = finance
        self.finance_property = prop
        self.finance_life = _Life()
        self.config = {}

    def cfg_str(self, key, default=""):
        return str(self.config.get(key, default))

    def cfg_bool(self, key, default=True):
        return bool(default)


def _scene():
    repo = _Repo()
    repo.loans["mortgage"] = {"id": 7, "kind": "mortgage",
                              "balance_cents": HOUSE_BALANCE,
                              "monthly_cents": MORTGAGE_CENTS,
                              "rate": HOUSE_RATE, "status": ""}
    finance = FakeFinance(cash=5000000)      # 5 万元现金，够扣 6000 元房贷
    prop = FinanceProperty(None, repo, finance, _Life())
    plugin = _Plugin(repo, finance, prop)
    return repo, finance, prop, plugin


def _due_row():
    return {"kind": "mortgage", "name": "房贷", "amount_cents": MORTGAGE_CENTS,
            "status": "due", "created_ts": 0.0, "penalty": 0}


def _mortgage_ledger(finance):
    return [row for row in finance.ledger() if row["reason"] == "房贷"]


def test_spec_mortgage_same_month_repeat_settle_charges_once():
    """给定：同月同一笔房贷，手动重复交 + 自动代扣重复 tick → 只扣一次。"""
    repo, finance, prop, plugin = _scene()
    bf.save_due(plugin, PERIOD, [_due_row()])
    start = finance.balance_cents()

    first = asyncio.run(bf.pay(plugin, "mortgage", NOW))
    assert first["ok"] is True
    cash_after = finance.balance_cents()
    assert start - cash_after == MORTGAGE_CENTS      # 余额只减一次

    # 手动再点两次
    for _ in range(2):
        again = asyncio.run(bf.pay(plugin, "mortgage", NOW))
        assert again["ok"] is False and again.get("already_settled") is True
    # 自动代扣重复 tick 三次
    for _ in range(3):
        tick = asyncio.run(prop.settle_loans(NOW, PERIOD))
        assert tick["ok"] is True and tick["done"] == []

    assert finance.balance_cents() == cash_after      # 钱没有再动
    assert len(_mortgage_ledger(finance)) == 1        # 账本只有一条
    assert len(finance.expenses) == 1
    interest = int(round(HOUSE_BALANCE * HOUSE_RATE / 12.0))
    assert int(repo.loans["mortgage"]["balance_cents"]) == HOUSE_BALANCE - (MORTGAGE_CENTS - interest)


def test_spec_mortgage_cross_month_charges_again_once():
    """给定：跨到 2026-12 → 必须能再扣一次；12 月重复触发仍不扣。"""
    repo, finance, prop, plugin = _scene()
    bf.save_due(plugin, PERIOD, [_due_row()])
    asyncio.run(bf.pay(plugin, "mortgage", NOW))
    nov_balance = int(repo.loans["mortgage"]["balance_cents"])
    nov_cash = finance.balance_cents()

    bf.save_due(plugin, DEC, [_due_row()])
    out = asyncio.run(bf.pay(plugin, "mortgage", DEC_NOW))
    assert out["ok"] is True
    assert nov_cash - finance.balance_cents() == MORTGAGE_CENTS
    assert len(_mortgage_ledger(finance)) == 2
    assert int(repo.loans["mortgage"]["balance_cents"]) < nov_balance

    dec_cash = finance.balance_cents()
    again = asyncio.run(bf.pay(plugin, "mortgage", DEC_NOW))
    assert again["ok"] is False and again.get("already_settled") is True
    assert asyncio.run(prop.settle_loans(DEC_NOW, DEC))["done"] == []
    assert finance.balance_cents() == dec_cash
    assert len(finance.expenses) == 2


def test_spec_mortgage_idem_collision_does_not_double_charge():
    """给定：settlements 丢失、钱包 idem 仍在 → 幂等冲突不得再扣钱。"""
    repo, finance, prop, plugin = _scene()
    bf.save_due(plugin, PERIOD, [_due_row()])
    asyncio.run(bf.pay(plugin, "mortgage", NOW))
    cash_after = finance.balance_cents()

    repo.settled_rows.clear()          # 模拟幂等门丢失，只剩钱包那条 idem
    out = asyncio.run(prop.settle_loan("house", NOW, PERIOD))
    assert str(out.get("status")) != "paid"
    assert finance.balance_cents() == cash_after
    assert len(finance.expenses) == 1
    assert len(_mortgage_ledger(finance)) == 1


def test_spec_mortgage_auto_tick_then_leftover_bill_row_not_charged():
    """给定：自动代扣已扣、旧数据还留一条 due 房贷行 → 只收口，不再扣钱。"""
    repo, finance, prop, plugin = _scene()
    auto = asyncio.run(prop.settle_loans(NOW, PERIOD))
    assert auto["ok"] is True and len(auto["done"]) == 1
    cash_after = finance.balance_cents()

    bf.save_due(plugin, PERIOD, [_due_row()])       # 遗留的待交行
    out = asyncio.run(bf.pay(plugin, "mortgage", NOW))
    assert out["ok"] is False and out.get("already_settled") is True
    assert finance.balance_cents() == cash_after
    assert len(finance.expenses) == 1
    assert bf.load_due(plugin, PERIOD)[0]["status"] == "paid"
