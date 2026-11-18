# -*- coding: utf-8 -*-
import asyncio
import datetime as dt

import pytest

from services.consume_engine import FinanceConsume
from services.daily_engine import FinanceDaily
try:  # Phase 15：tmp_path 真库改走共享 fixtures（跑完随 tmp_path 清理）
    from tests.fixtures import temp_store
except ImportError:
    from fixtures import temp_store

from plugin_fakes import FinanceDailyFakeFinance as FakeFinance  # noqa: E402

@pytest.fixture()
def env(tmp_path):
    store, repo = temp_store(tmp_path)
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

class FakeAmo:
    def __init__(self, value=40):
        self.value = value
        self.added = []
        self.meals = []

    def hunger_state(self):
        return {"value": self.value}

    def add_hunger(self, target, amount):
        self.added.append(amount)
        self.value += amount

    def mark_meal(self, food, hours=4, mood="满足"):
        self.meals.append((food, hours, mood))


def test_dinner_window_aligned_to_evening(env):
    daily, repo, finance, _ = env
    assert asyncio.run(daily.tick(dt.datetime(2026, 11, 13, 17, 31))) == []
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 13, 18, 5)))
    assert done and done[0]["slot"] == "dinner"


def test_meal_feeds_hunger(env):
    daily, repo, finance, _ = env
    amo = FakeAmo(value=40)
    finance._amo = lambda: amo
    asyncio.run(daily.tick(dt.datetime(2026, 11, 13, 8, 0)))
    assert amo.added and abs(amo.added[-1] - (86 - 40)) < 0.01
    assert amo.meals and amo.meals[-1][1] == 3.0
