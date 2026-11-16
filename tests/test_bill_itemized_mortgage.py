# -*- coding: utf-8 -*-
"""2026-11-16 第二轮：房贷代缴摊销 + 水电燃气宽带按单项立项/迁移/防双扣。"""
import asyncio
import datetime as dt

import pytest

import services.bill_flow as bf
from services.life_engine import FinanceLife
from services.property_engine import FinanceProperty

PERIOD = "2026-11"
NOW = dt.datetime(2026, 11, 2, 10, 0)   # 周一 10:00，账单日、银行办理时间


class FakeRepo:
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


class FakeFinance:
    def __init__(self, cash=5000000):
        self.cash = int(cash)
        self.expenses = []
        self.memories = []
        self.diaries = []
        self.moods = []

    def _amo(self):
        return None

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, name="", idem="", **kwargs):
        amount = int(amount)
        if self.cash < amount:
            return {"ok": False}
        self.cash -= amount
        self.expenses.append((amount, str(name), str(idem)))
        return {"ok": True}

    async def permanent_memory(self, text, key):
        self.memories.append(text)
        return True

    async def remember_event(self, *args, **kwargs):
        return True

    async def low_balance(self, need, key=""):
        return True

    def small_diary(self, text):
        self.diaries.append(text)
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True

    def nudge_mood(self, delta, key):
        self.moods.append(delta)
        return True

    def mood(self):
        return {"word": "平静"}


UTILITY_ITEMS = (
    {"key": "water", "name": "水费", "amount_cents": 1929},
    {"key": "electric", "name": "电费", "amount_cents": 10917},
    {"key": "gas", "name": "燃气费", "amount_cents": 4578},
    {"key": "broadband", "name": "宽带", "amount_cents": 6359},
)


class FakeLife:
    def __init__(self, items=UTILITY_ITEMS, has_house=True):
        self.items = tuple(dict(item) for item in items)
        self.has_house = has_house

    def _has(self, kind):
        return bool(self.has_house) and kind == "house"

    def rent_cents(self, period):
        return 300000

    def utility_plan(self, period):
        return tuple(dict(item) for item in self.items)


class FakePlugin:
    def __init__(self, repo, finance, life, prop=None):
        self.finance_repo = repo
        self.finance = finance
        self.finance_life = life
        self.finance_property = prop
        self.config = {}

    def cfg_str(self, key, default=""):
        return default

    def cfg_bool(self, key, default=True):
        return bool(default)


def _scene(cash=5000000, items=UTILITY_ITEMS):
    repo = FakeRepo()
    finance = FakeFinance(cash)
    life = FakeLife(items)
    prop = FinanceProperty(None, repo, finance, life)
    plugin = FakePlugin(repo, finance, life, prop)
    return repo, finance, prop, plugin


# ---------- 房贷：现金一次 + 本金一次 + 自动代扣不重复 ----------
def test_manual_mortgage_pay_deducts_cash_and_principal_once():
    repo, finance, prop, plugin = _scene()
    repo.loans["mortgage"] = {"id": 7, "kind": "mortgage", "balance_cents": 100000000,
                              "monthly_cents": 600000, "rate": 0.049, "status": ""}
    bf.save_due(plugin, PERIOD, [{"kind": "mortgage", "name": "房贷", "amount_cents": 600000,
                                  "status": "due", "created_ts": 0.0, "penalty": 0}])
    out = asyncio.run(bf.pay(plugin, "mortgage", NOW))
    assert out["ok"] is True
    assert [row[0] for row in finance.expenses] == [600000]
    assert finance.expenses[0][2] == "mortgage:" + PERIOD   # 和自动代扣同一个幂等键
    interest = int(round(100000000 * 0.049 / 12.0))
    assert int(repo.loans["mortgage"]["balance_cents"]) == 100000000 - (600000 - interest)
    assert repo.settled(PERIOD, "mortgage") is True

    # 当月再跑自动代扣：不能再扣钱，也不能再减本金
    again = asyncio.run(prop.settle_loans(NOW, PERIOD))
    assert again["ok"] is True and again["done"] == []
    assert len(finance.expenses) == 1
    assert int(repo.loans["mortgage"]["balance_cents"]) == 100000000 - (600000 - interest)

    # 手动再点一次房贷：账单已收口，不能再扣
    second = asyncio.run(bf.pay(plugin, "mortgage", NOW))
    assert second["ok"] is False and len(finance.expenses) == 1


def test_auto_mortgage_then_leftover_bill_row_is_not_charged_twice():
    repo, finance, prop, plugin = _scene()
    repo.loans["mortgage"] = {"id": 8, "kind": "mortgage", "balance_cents": 50000000,
                              "monthly_cents": 400000, "rate": 0.04, "status": ""}
    first = asyncio.run(prop.settle_loans(NOW, PERIOD))
    assert first["ok"] is True and len(finance.expenses) == 1
    balance_after_auto = int(repo.loans["mortgage"]["balance_cents"])
    # 旧数据/时序留下的房贷账单行仍然是 due：只收口，绝不再扣
    bf.save_due(plugin, PERIOD, [{"kind": "mortgage", "name": "房贷", "amount_cents": 400000,
                                  "status": "due", "created_ts": 0.0, "penalty": 0}])
    out = asyncio.run(bf.pay(plugin, "mortgage", NOW))
    assert out["ok"] is False and out.get("already_settled") is True
    assert len(finance.expenses) == 1
    assert int(repo.loans["mortgage"]["balance_cents"]) == balance_after_auto
    assert bf.load_due(plugin, PERIOD)[0]["status"] == "paid"


# ---------- 水电燃气宽带：逐项立项 ----------
def test_bill_plan_and_create_due_are_itemized():
    repo, finance, prop, plugin = _scene()
    rows = bf.bill_plan(plugin, PERIOD)
    amounts = {row["kind"]: row["amount_cents"] for row in rows}
    assert "utilities" not in amounts
    assert amounts == {item["key"]: item["amount_cents"] for item in UTILITY_ITEMS}
    created = bf.create_due(plugin, dt.datetime(2026, 11, 2, 9, 0))
    assert {row["kind"] for row in created} == {"water", "electric", "gas", "broadband"}
    assert {row["kind"]: row["amount_cents"] for row in created} == \
        {item["key"]: item["amount_cents"] for item in UTILITY_ITEMS}


def test_partially_paid_period_still_bills_remaining_items():
    repo, finance, prop, plugin = _scene()
    repo.mark_settled(PERIOD, "water", "auto")   # 水费已经自动扣了
    created = bf.create_due(plugin, dt.datetime(2026, 11, 2, 9, 0))
    assert {row["kind"] for row in created} == {"electric", "gas", "broadband"}
    for row in created:
        expected = {item["key"]: item["amount_cents"] for item in UTILITY_ITEMS}[row["kind"]]
        assert row["amount_cents"] == expected


def test_pay_each_item_once_and_no_repeat():
    repo, finance, prop, plugin = _scene()
    bf.create_due(plugin, dt.datetime(2026, 11, 2, 9, 0))
    for key in ("water", "electric", "gas", "broadband"):
        out = asyncio.run(bf.pay(plugin, key, NOW))
        assert out["ok"] is True
    assert len(finance.expenses) == 4
    charged = {row[2].split(":")[-1] for row in finance.expenses}
    assert charged == {"water", "electric", "gas", "broadband"}
    assert repo.settled(PERIOD, "water") and repo.settled(PERIOD, "electric")
    # 付完之后本月不再重复收
    again = asyncio.run(bf.pay(plugin, "water", NOW))
    assert again["ok"] is False and again.get("already_settled") is True
    assert len(finance.expenses) == 4
    assert bf.create_due(plugin, dt.datetime(2026, 11, 3, 9, 0))  # 只回已有行，不再新增


def test_auto_paid_item_row_is_closed_without_charging():
    repo, finance, prop, plugin = _scene()
    bf.save_due(plugin, PERIOD, [{"kind": "electric", "name": "电费",
                                  "amount_cents": 10917, "status": "due",
                                  "created_ts": 0.0, "penalty": 0}])
    repo.mark_settled(PERIOD, "electric", "auto")
    rows = bf.load_due(plugin, PERIOD)
    assert rows[0]["status"] == "paid"
    out = asyncio.run(bf.pay(plugin, "electric", NOW))
    assert out["ok"] is False and out.get("already_settled") is True
    assert finance.expenses == []


# ---------- 旧聚合账单迁移 ----------
def test_legacy_aggregate_row_is_split_into_items():
    repo, finance, prop, plugin = _scene()
    bf.save_due(plugin, PERIOD, [{"kind": "utilities", "name": "水电燃气宽带",
                                  "amount_cents": 30000, "status": "due",
                                  "created_ts": 0.0, "penalty": 0}])
    rows = bf.load_due(plugin, PERIOD)
    assert {row["kind"] for row in rows} == {"water", "electric", "gas", "broadband"}
    assert {row["kind"]: row["amount_cents"] for row in rows} == \
        {item["key"]: item["amount_cents"] for item in UTILITY_ITEMS}
    again = bf.load_due(plugin, PERIOD)   # 幂等：不会再长出旧聚合行
    assert [row["kind"] for row in again] == [row["kind"] for row in rows]


def test_legacy_paid_aggregate_marks_every_item_settled():
    repo, finance, prop, plugin = _scene()
    bf.save_due(plugin, PERIOD, [{"kind": "utilities", "name": "水电燃气宽带",
                                  "amount_cents": 30000, "status": "paid",
                                  "created_ts": 0.0, "penalty": 0}])
    assert bf.load_due(plugin, PERIOD) == []
    for key in ("water", "electric", "gas", "broadband"):
        assert repo.settled(PERIOD, key) is True


# ---------- 自动扣款：水费成功、电费失败后仍会单独追 ----------
class _LifeRepo:
    def __init__(self):
        self.settled_rows = set()

    def settled(self, period, key):
        return (str(period), str(key)) in self.settled_rows

    def mark_settled(self, period, key, payload="{}"):
        self.settled_rows.add((str(period), str(key)))
        return True

    def assets(self):
        return []


class _FlakyFinance:
    def __init__(self):
        self.calls = []
        self.fail = {"electric"}
        self.cash = 10 ** 9

    def _amo(self):
        return None

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, name="", idem="", **kwargs):
        key = str(idem).split(":")[0]
        if key in self.fail:
            self.fail.discard(key)
            self.calls.append((key, int(amount), False))
            return {"ok": False}
        self.cash -= int(amount)
        self.calls.append((key, int(amount), True))
        return {"ok": True}

    async def remember_event(self, *args, **kwargs):
        return True

    def small_diary(self, text):
        return True

    def long_diary(self, text):
        return True


class _LifePlugin:
    def __init__(self, finance):
        self.finance = finance
        self.config = {}

    def cfg_str(self, key, default=""):
        return default

    def cfg_bool(self, key, default=True):
        return bool(default)


def test_utilities_retry_failed_item_on_next_tick():
    repo = _LifeRepo()
    finance = _FlakyFinance()
    life = FinanceLife(_LifePlugin(finance), repo, finance)
    plan = {item["key"]: item["amount_cents"] for item in life.utility_plan(PERIOD)}
    first = asyncio.run(life._utilities(dt.datetime(2026, 11, 2, 9, 0), PERIOD))
    assert first == "fail"
    assert repo.settled(PERIOD, "water") is True
    assert repo.settled(PERIOD, "electric") is False
    second = asyncio.run(life._utilities(dt.datetime(2026, 11, 3, 9, 0), PERIOD))
    assert second == "ok"
    assert repo.settled(PERIOD, "electric") is True
    electric_calls = [call for call in finance.calls if call[0] == "electric"]
    assert [call[2] for call in electric_calls] == [False, True]
    assert electric_calls[-1][1] == plan["electric"]   # 金额和单项账单同一口径
