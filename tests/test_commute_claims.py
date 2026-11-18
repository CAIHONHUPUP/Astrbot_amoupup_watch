# -*- coding: utf-8 -*-
"""守护侧通勤/claim：按陪伴日程买交通，不随机编共享单车。"""

import asyncio
import datetime as dt

import pytest

from services.consume_engine import FinanceConsume
from services.daily_engine import FinanceDaily
try:
    from tests.fixtures import temp_store
except ImportError:
    from fixtures import temp_store


class FakeAmo:
    def __init__(self, mode="地铁", rows=None):
        self.wallet = self
        self.rows = list(rows or [])
        self.mode = mode

    def ledger(self, limit=200):
        return list(self.rows)

    def current_commute_mode(self, now=None):
        return self.mode


class FakeExpense:
    async def spend(self, category, label, amount, day=None, idem="", necessary=False):
        return {"ok": True}

    def reset_meal_failure(self, day):
        return None

    def bump_meal_failure(self, day):
        return 0


class FakePlugin:
    finance_expense = FakeExpense()

class FakeFinance:
    def __init__(self):
        self.calls = []

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        self.calls.append((cents, reason, idem))
        return {"ok": True}


@pytest.fixture()
def env(tmp_path):
    store, repo = temp_store(tmp_path)
    finance = FakeFinance()
    daily = FinanceDaily(None, repo, finance, FinanceConsume(None, repo, finance))
    yield daily, repo, finance, store
    store.close()


def test_commute_mode_from_amo(env):
    daily, _, _, _ = env
    daily._amo = lambda: FakeAmo("地铁")
    assert daily._amo_commute_mode("2026-11-18", dt.datetime(2026, 11, 18, 7, 40)) == "地铁"


def test_traffic_buys_schedule_mode(env):
    daily, _, finance, _ = env
    daily.plugin = FakePlugin()
    daily._amo = lambda: FakeAmo("地铁")
    asyncio.run(daily.tick(dt.datetime(2026, 11, 18, 7, 40)))
    reasons = [c[1] for c in finance.calls]
    assert "地铁 × 1" in reasons
    assert not any("共享单车" in r for r in reasons)


def test_taxi_mode_buys_taxi(env):
    daily, _, finance, _ = env
    daily.plugin = FakePlugin()
    daily._amo = lambda: FakeAmo("打车")
    asyncio.run(daily.tick(dt.datetime(2026, 11, 18, 7, 40)))
    assert "打车 × 1" in [c[1] for c in finance.calls]


def test_drink_claim_sees_amo_tea(env):
    daily, _, _, _ = env
    ts = dt.datetime(2026, 11, 18, 12, 30).timestamp()
    daily._amo = lambda: FakeAmo(rows=[{"ts": ts, "delta_cents": -1500,
                                        "reason": "奶茶 × 1",
                                        "idem": "impulse:2026-11-18"}])
    assert daily._amo_claimed("2026-11-18", 11 * 60, 13 * 60, words=("奶茶", "果茶"))
