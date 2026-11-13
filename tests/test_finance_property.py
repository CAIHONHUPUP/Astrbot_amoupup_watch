# -*- coding: utf-8 -*-
"""买房 / 买车：价格、首付、月供、扣款顺序与三种资产状态。"""
import asyncio
import datetime as dt

from services.life_engine import FinanceLife
from services.property_engine import DOWN_PCT, FinanceProperty, amortize, monthly_payment


class FakeRepo:
    def __init__(self):
        self.meta = {}
        self._assets = []
        self._loans = []
        self.settled_keys = set()
        self._id = 0

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def asset(self, kind):
        rows = [a for a in self._assets if a["kind"] == kind]
        return rows[-1] if rows else None

    def assets(self):
        return list(self._assets)

    def add_asset(self, kind, label, value_cents):
        self._assets.append({"kind": kind, "label": label, "value_cents": int(value_cents)})
        return True

    def loan(self, kind):
        rows = [x for x in self._loans if x["kind"] == kind and x["balance_cents"] > 0]
        return rows[-1] if rows else None

    def loans(self):
        return list(self._loans)

    def add_loan(self, idem, kind, principal_cents, balance_cents, rate, months, monthly_cents):
        self._id += 1
        self._loans.append({"id": self._id, "kind": kind, "principal_cents": int(principal_cents),
                            "balance_cents": int(balance_cents), "rate": float(rate),
                            "months": int(months), "monthly_cents": int(monthly_cents), "status": ""})
        return self._id

    def update_loan(self, loan_id, balance_cents, status=""):
        for item in self._loans:
            if item["id"] == loan_id:
                item["balance_cents"] = int(balance_cents)
                if status:
                    item["status"] = status
        return True

    def settled(self, period, kind):
        return (str(period), str(kind)) in self.settled_keys

    def mark_settled(self, period, kind, payload="{}"):
        self.settled_keys.add((str(period), str(kind)))
        return True

    def positions(self):
        return []


class FakeFinance:
    def __init__(self, cash=0, log=None):
        self.cash = int(cash)
        self.calls = []
        self.log = log if log is not None else []
        self.low_calls = []
        self.memories = []
        self.diaries = []

    def balance_cents(self):
        return self.cash

    async def expense(self, cents, reason, idem, necessary=False):
        self.calls.append(("expense", reason, int(cents)))
        self.log.append("expense:" + reason)
        if self.cash < int(cents):
            return {"ok": False}
        self.cash -= int(cents)
        return {"ok": True}

    async def income(self, cents, reason, idem, salary=False):
        self.cash += int(cents)
        self.calls.append(("income", reason, int(cents)))
        return {"ok": True}

    def mood(self):
        return {"word": "平静", "score": 60}

    async def low_balance(self, need, idem):
        self.low_calls.append((int(need), idem))
        return {"ok": True}

    async def remember_event(self, kind, amount_cents, text, idem):
        self.memories.append(text)
        return True

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True


class FakePlugin:
    config = {}

    def __init__(self, prop=None):
        self.finance_property = prop


def make(cash=0):
    repo = FakeRepo()
    fin = FakeFinance(cash)
    prop = FinanceProperty(None, repo, fin, None)
    return repo, fin, prop


def test_monthly_payment_zero_interest_and_amortize():
    assert monthly_payment(1200000, 0.0, 12) == 100000
    pay, total_interest, rows = amortize(12000000, 0.039, 360)
    assert pay > 0 and total_interest > 0
    assert len(rows) == 360
    assert sum(r["principal"] for r in rows) == 12000000


def test_buy_house_creates_asset_loan_and_diary():
    repo, fin, prop = make()
    down = prop.down_cents("house")
    fin.cash = down + 50000
    result = asyncio.run(prop.buy("house", dt.date(2026, 11, 13)))
    assert result["ok"] is True
    assert repo.asset("house")["value_cents"] == prop.price_cents("house")
    loan = repo.loan("mortgage")
    assert loan["balance_cents"] == prop.price_cents("house") - down
    assert fin.cash == 50000
    assert fin.memories and fin.diaries
    assert "再也不" in fin.diaries[-1] or "房贷" in fin.diaries[-1]


def test_buy_house_refused_without_down_payment():
    repo, fin, prop = make()
    fin.cash = prop.down_cents("house") - 1
    result = asyncio.run(prop.buy("house", dt.date(2026, 11, 13)))
    assert result["ok"] is False and result["reason"] == "insufficient"
    assert repo.asset("house") is None
    assert repo.loan("mortgage") is None
    assert fin.diaries == []


def test_property_fee_and_car_cost_only_after_purchase():
    repo, fin, prop = make()
    assert prop.property_fee_cents("2026-11") == 0
    assert prop.car_cost_cents("2026-11") == 0
    fin.cash = prop.down_cents("house") + prop.down_cents("car") + 100000
    asyncio.run(prop.buy("house", dt.date(2026, 11, 13)))
    asyncio.run(prop.buy("car", dt.date(2026, 11, 13)))
    assert prop.property_fee_cents("2026-11") > 0
    assert prop.car_cost_cents("2026-11") > 0


def test_settle_loans_pays_once_and_reduces_balance():
    repo, fin, prop = make()
    fin.cash = prop.down_cents("house") + 5000000
    asyncio.run(prop.buy("house", dt.date(2026, 11, 13)))
    loan = repo.loan("mortgage")
    before = loan["balance_cents"]
    period = "2026-11"
    fin.cash = 10000000
    result = asyncio.run(prop.settle_loans(dt.date(2026, 11, 13), period))
    assert result["ok"] is True
    after = repo.loan("mortgage")["balance_cents"]
    assert after < before
    assert repo.settled(period, "mortgage")
    assert asyncio.run(prop.settle_loans(dt.date(2026, 11, 13), period))["ok"] is True  # 同一期不再扣
    assert repo.loan("mortgage")["balance_cents"] == after


def test_settle_loans_warns_when_cash_short_and_keeps_asset():
    repo, fin, prop = make()
    fin.cash = prop.down_cents("house") + 100000
    asyncio.run(prop.buy("house", dt.date(2026, 11, 13)))
    loan = repo.loan("mortgage")
    before = loan["balance_cents"]
    fin.cash = 0
    result = asyncio.run(prop.settle_loans(dt.date(2026, 11, 13), "2026-12"))
    assert result["ok"] is False and result["reason"] == "insufficient"
    assert repo.loan("mortgage")["balance_cents"] == before
    assert not repo.settled("2026-12", "mortgage")
    assert fin.low_calls  # 进了 5% 求助流程
    assert repo.asset("house") is not None  # 不没收


def test_maybe_buy_only_when_cash_enough():
    repo, fin, prop = make()
    fin.cash = prop.down_cents("car") + 1000
    assert asyncio.run(prop.maybe_buy(dt.date(2026, 11, 13)))["ok"] is True
    assert repo.asset("car") is not None
    assert repo.asset("house") is None
    assert asyncio.run(prop.maybe_buy(dt.date(2026, 11, 13)))["ok"] is False


def test_buy_and_repay_persist_to_real_store(tmp_path):
    """用真的 Store/FinanceRepo 跑一遍：资产、贷款、结算都要落库。"""
    from models.ledger import FinanceRepo
    from models.db import Store

    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    try:
        repo = FinanceRepo(store)
        fin = FakeFinance(0)
        prop = FinanceProperty(None, repo, fin, None)
        down = prop.down_cents("house")
        fin.cash = down + 10000
        assert asyncio.run(prop.buy("house", dt.date(2026, 11, 13)))["ok"] is True
        assert repo.asset("house")["value_cents"] == prop.price_cents("house")
        loan = repo.loan("mortgage")
        assert loan["balance_cents"] == prop.price_cents("house") - down
        fin.cash = 10000000
        assert asyncio.run(prop.settle_loans(dt.date(2026, 12, 1), "2026-12"))["ok"] is True
        assert repo.settled("2026-12", "mortgage")
        assert repo.loan("mortgage")["balance_cents"] < loan["balance_cents"]
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_settlement_order_mortgage_rent_fee_utilities_car():
    log = []
    repo = FakeRepo()
    repo.meta["seeded"] = "1"
    fin = FakeFinance(100000000, log=log)

    class OrderProp:
        async def settle_loans(self, now, period):
            log.append("loans")
            return {}

        async def charge_property_fee(self, now, period):
            log.append("property_fee")
            return "ok"

        async def charge_car_cost(self, now, period):
            log.append("car_cost")
            return "ok"

        async def maybe_buy(self, now):
            log.append("maybe_buy")
            return {}

    life = FinanceLife(FakePlugin(OrderProp()), repo, fin)
    asyncio.run(life.tick(dt.datetime(2026, 11, 13, 10, 0)))
    order = [item for item in log if item in ("loans", "property_fee", "car_cost", "maybe_buy")
             or item.startswith("expense:")]
    assert order[0] == "loans"
    assert order[-1] == "maybe_buy"
    assert order.index("expense:房租") < order.index("property_fee")
    assert order.index("property_fee") < order.index("expense:水费")
    assert order.index("expense:宽带") < order.index("car_cost")
