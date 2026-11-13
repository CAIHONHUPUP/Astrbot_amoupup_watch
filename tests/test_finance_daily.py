# -*- coding: utf-8 -*-
import asyncio
import datetime as dt

import pytest

from finance_consume import FinanceConsume
from finance_daily import FinanceDaily
from finance_store import FinanceRepo
from store import Store


class FakeFinance:
    def __init__(self):
        self.calls = []

    async def expense(self, cents, reason, idem, necessary=False):
        self.calls.append(("expense", cents, reason))
        return {"ok": True, "mood": "平静"}


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    finance = FakeFinance()
    daily = FinanceDaily(None, repo, finance, FinanceConsume(None, repo, finance))
    yield daily, repo, finance, store
    store.close()


def test_breakfast_items_once(env):
    daily, repo, finance, _ = env
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 13, 8, 0)))
    assert done and done[0]["slot"] == "breakfast"
    names = [c[2] for c in finance.calls]
    assert len(names) == 2 and all(" × " in n for n in names)
    asyncio.run(daily.tick(dt.datetime(2026, 11, 13, 8, 10)))
    assert len(finance.calls) == 2


def test_no_meal_outside_window(env):
    daily, _, finance, _ = env
    assert asyncio.run(daily.tick(dt.datetime(2026, 11, 13, 10, 0))) == []
    assert finance.calls == []
