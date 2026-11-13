# -*- coding: utf-8 -*-
import asyncio
import datetime as dt

import pytest

from finance_life import DEFAULT_SALARY_CENTS, FinanceLife
from finance_store import FinanceRepo
from store import Store


class FakeFinance:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    async def income(self, cents, reason, idem, salary=False):
        self.calls.append(("income", cents, reason, idem, salary))
        return {"ok": self.ok}

    async def expense(self, cents, reason, idem, necessary=False):
        self.calls.append(("expense", cents, reason, idem, necessary))
        return {"ok": self.ok}


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    finance = FakeFinance()
    life = FinanceLife(None, repo, finance)
    yield life, repo, finance, store
    store.close()


def test_salary_only_from_15th_and_once(env):
    life, repo, finance, _ = env
    asyncio.run(life.tick(dt.datetime(2026, 11, 14)))
    assert not [c for c in finance.calls if c[0] == "income"]
    asyncio.run(life.tick(dt.datetime(2026, 11, 15)))
    asyncio.run(life.tick(dt.datetime(2026, 11, 16)))
    incomes = [c for c in finance.calls if c[0] == "income"]
    assert len(incomes) == 1 and incomes[0][1] == DEFAULT_SALARY_CENTS
    assert repo.settled("2026-11", "salary")


def test_rent_and_utilities_once(env):
    life, repo, finance, _ = env
    asyncio.run(life.tick(dt.datetime(2026, 11, 1)))
    asyncio.run(life.tick(dt.datetime(2026, 11, 2)))
    expenses = [c for c in finance.calls if c[0] == "expense"]
    assert len(expenses) == 2
    assert {c[2] for c in expenses} == {"房租", "水电燃气"}


def test_house_skips_rent(env):
    life, repo, finance, _ = env
    repo.add_asset("house", "出租屋", 120000000)
    asyncio.run(life.tick(dt.datetime(2026, 11, 1)))
    reasons = [c[2] for c in finance.calls if c[0] == "expense"]
    assert "房租" not in reasons and "水电燃气" in reasons


def test_failed_payment_does_not_mark_settled(env):
    life, repo, finance, _ = env
    finance.ok = False
    asyncio.run(life.tick(dt.datetime(2026, 11, 1)))
    assert not repo.settled("2026-11", "rent")


def test_raise_in_july(env):
    life, repo, _, _ = env
    old = life.salary_cents()
    asyncio.run(life.tick(dt.datetime(2026, 7, 15)))
    assert life.salary_cents() != old
    assert 300000 <= life.salary_cents() <= 2000000
    assert repo.meta_get("salary_raised") == "2026-H2"
