# -*- coding: utf-8 -*-
import asyncio
import datetime as dt
import random

import pytest

from finance_consume import FinanceConsume
from finance_decision import FinanceDecision
from finance_help import FinanceHelp, amortization
from finance_store import FinanceRepo
from store import Store


class FakeFinance:
    def __init__(self):
        self.cash = 1_000_000
        self.calls = []

    def balance_cents(self):
        return self.cash

    async def expense(self, cents, reason, idem, necessary=False):
        self.cash -= cents
        self.calls.append(("expense", cents, reason))
        return {"ok": True, "mood": "平静"}

    async def income(self, cents, reason, idem, salary=False):
        self.cash += cents
        self.calls.append(("income", cents, reason))
        return {"ok": True, "mood": "开心"}

    async def borrow(self, cents, reason, idem, months=12):
        self.cash += cents
        self.calls.append(("borrow", cents, reason))
        return {"ok": True}

    async def remember_event(self, kind, cents, text, idem):
        self.calls.append(("remember", text))
        return True


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    finance = FakeFinance()
    yield repo, finance, store
    store.close()


def test_amortization_sums_principal():
    payment, total_interest, rows = amortization(1200000, 12, 0.06)
    assert len(rows) == 12
    assert sum(r["principal"] for r in rows) == 1200000
    assert total_interest > 0
    assert rows[0]["payment"] == payment


def test_loan_creates_installments_and_closes_episode(env):
    repo, finance, _ = env
    help_ = FinanceHelp(None, repo, finance)
    repo.meta_set("lowfund_choice", "")
    res = asyncio.run(help_.take_loan(60000, 6, 0.06))
    assert res["ok"] is True
    assert len(repo.loans()) == 1
    loan_id = repo.loans()[0]["id"]
    rows = repo._rows("SELECT * FROM loan_installments WHERE loan_id=?", (loan_id,))
    assert len(rows) == 6
    assert repo.meta_get("lowfund_choice") == "loan"
    assert asyncio.run(help_.user_transfer(10000))["ok"] is False


def test_repay_tick_pays_due_installment(env):
    repo, finance, _ = env
    help_ = FinanceHelp(None, repo, finance)
    repo.meta_set("lowfund_choice", "")
    asyncio.run(help_.take_loan(120000, 3, 0.06))
    loan = repo.loans()[0]
    repo.store.write("UPDATE loan_installments SET due_day=? WHERE loan_id=? AND seq=1",
                     (dt.date.today().isoformat(), loan["id"]))
    before = finance.cash
    assert asyncio.run(help_.repay_tick()) == "paid"
    assert finance.cash < before
    assert int(repo.loans()[0]["balance_cents"]) < 120000


def test_consume_item_records_each_item(env):
    repo, finance, _ = env
    consume = FinanceConsume(None, repo, finance)
    res = asyncio.run(consume.buy_item("肠粉", 2))
    assert res["ok"] is True and res["amount_cents"] == 1200
    assert finance.calls[-1] == ("expense", 1200, "肠粉 × 2")
    asyncio.run(consume.buy_items([{"item": "豆浆", "qty": 1}, {"item": "包子", "qty": 2}]))
    assert len([c for c in finance.calls if c[0] == "expense"]) == 3


def test_decision_variety():
    engine = FinanceDecision(None, None, None, None, None, None)
    positions = [{"code": "600519", "qty": 100, "pnl_pct": 2.5, "cost_cents": 10000, "last_cents": 10250}]
    actions = set()
    reasons = set()
    for seed in range(40):
        d = engine.decide(1000000, positions, 70, {}, random.Random(seed))
        actions.add(d["action"])
        reasons.add(d["reason"].split("%")[0])
    assert "sell" in actions and "hold" in actions
    assert len(reasons) >= 2
    loss = [{"code": "000001", "qty": 100, "pnl_pct": -12.0, "cost_cents": 10000, "last_cents": 8800}]
    cut = [engine.decide(50000, loss, 30, {}, random.Random(s)) for s in range(20)]
    assert any(d["action"] == "sell" for d in cut)
