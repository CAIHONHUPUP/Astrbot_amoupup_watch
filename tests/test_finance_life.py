# -*- coding: utf-8 -*-
import asyncio
import datetime as dt

import pytest

from services.life_engine import DEFAULT_SALARY_CENTS, FinanceLife

try:  # Phase 15：tmp_path 真库改走共享 fixtures（跑完随 tmp_path 清理）
    from tests.fixtures import temp_store
except ImportError:
    from fixtures import temp_store


class FakeFinance:
    def __init__(self, ok=True, cash_cents=0):
        self.calls = []
        self.ok = ok
        self.cash_cents = int(cash_cents)

    def balance_cents(self):
        return self.cash_cents

    async def income(self, cents, reason, idem, salary=False):
        self.calls.append(("income", cents, reason, idem, salary))
        return {"ok": self.ok}

    async def expense(self, cents, reason, idem, necessary=False):
        self.calls.append(("expense", cents, reason, idem, necessary))
        return {"ok": self.ok}


@pytest.fixture()
def env(tmp_path):
    store, repo = temp_store(tmp_path)
    finance = FakeFinance()
    life = FinanceLife(None, repo, finance)
    yield life, repo, finance, store
    store.close()


def test_salary_defers_after_weekend_and_once(env):
    life, repo, finance, _ = env
    # 2026-11-15 是周日：周五/周六/周日都不发，顺延到 11-16 周一 10:00
    asyncio.run(life.tick(dt.datetime(2026, 11, 13, 10, 0)))
    asyncio.run(life.tick(dt.datetime(2026, 11, 14, 10, 0)))
    asyncio.run(life.tick(dt.datetime(2026, 11, 15, 10, 0)))
    assert not [c for c in finance.calls if c[0] == "income"]
    asyncio.run(life.tick(dt.datetime(2026, 11, 16, 10, 0)))
    incomes = [c for c in finance.calls if c[0] == "income"]
    assert len(incomes) == 1 and incomes[0][1] == DEFAULT_SALARY_CENTS
    assert repo.settled("2026-11", "salary")

    # 周一之后不能再发一次
    asyncio.run(life.tick(dt.datetime(2026, 11, 17, 10, 0)))
    assert len([c for c in finance.calls if c[0] == "income"]) == 1


def test_rent_and_utilities_once(env):
    life, repo, finance, _ = env
    # 2026-11-01 是周日：银行不扣，顺延到 11-02 周一 9 点
    asyncio.run(life.tick(dt.datetime(2026, 11, 1, 9, 0)))
    assert not [c for c in finance.calls if c[0] == "expense"]
    asyncio.run(life.tick(dt.datetime(2026, 11, 2, 9, 0)))
    expenses = [c for c in finance.calls if c[0] == "expense"]
    reasons = [c[2] for c in expenses]
    # 水电燃气宽带现在是逐项扣（水费/电费/燃气费/宽带），不是一笔
    assert len(expenses) == 5
    assert "房租" in reasons
    assert {"水费", "电费", "燃气费", "宽带"} <= set(reasons)


def test_house_skips_rent(env):
    life, repo, finance, _ = env
    repo.add_asset("house", "出租屋", 120000000)
    asyncio.run(life.tick(dt.datetime(2026, 11, 2, 9, 0)))
    reasons = [c[2] for c in finance.calls if c[0] == "expense"]
    assert "房租" not in reasons
    assert {"水费", "电费", "燃气费", "宽带"} <= set(reasons)


def test_failed_payment_does_not_mark_settled(env):
    life, repo, finance, _ = env
    finance.ok = False
    asyncio.run(life.tick(dt.datetime(2026, 11, 2, 9, 0)))
    assert not repo.settled("2026-11", "rent")


def test_raise_in_july(env):
    life, repo, _, _ = env
    old = life.salary_cents()
    asyncio.run(life.tick(dt.datetime(2026, 7, 15, 10, 0)))
    assert life.salary_cents() != old
    assert 300000 <= life.salary_cents() <= 2000000
    assert repo.meta_get("salary_raised") == "2026-H2"
