# -*- coding: utf-8 -*-
"""守护三餐反向守卫：陪伴这个饭点已经记过一顿，守护不再买（2026-11-18）。"""

import asyncio
import datetime as dt

import pytest

from services.consume_engine import FinanceConsume
from services.daily_engine import FinanceDaily
try:
    from tests.fixtures import temp_store
except ImportError:
    from fixtures import temp_store


class WalletAmo:
    def __init__(self, rows):
        self.wallet = self
        self.rows = list(rows)

    def ledger(self, limit=200):
        return list(self.rows)


class FakeFinance:
    def __init__(self):
        self.calls = []

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        self.calls.append((cents, reason, idem))
        return {"ok": True}


def row(hour, minute, reason, delta=-2500, idem="", day="2026-11-18"):
    ts = dt.datetime(2026, 11, 18, hour, minute).timestamp() if day == "2026-11-18" \
        else dt.datetime(2026, 11, 17, hour, minute).timestamp()
    return {"ts": ts, "delta_cents": delta, "reason": reason,
            "idem": idem or "food:micro:%s:%s:0000" % (day, reason)}


@pytest.fixture()
def env(tmp_path):
    store, repo = temp_store(tmp_path)
    finance = FakeFinance()
    daily = FinanceDaily(None, repo, finance, FinanceConsume(None, repo, finance))
    async def _no_extras(now, day):
        return []
    daily._extras = _no_extras
    yield daily, repo, finance, store
    store.close()


def test_amo_meal_skips_watch_lunch(env):
    daily, repo, finance, _ = env
    daily._amo = lambda: WalletAmo([row(11, 5, "今天不想做饭，点了个外卖",
                                        idem="food:micro:2026-11-18:外卖:1105")])
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 18, 11, 30)))
    assert done == []
    assert finance.calls == []
    assert repo.meta_get("meal:2026-11-18:lunch")


def test_own_meal_does_not_skip(env):
    daily, repo, finance, _ = env
    daily._amo = lambda: WalletAmo([row(11, 1, "面条 × 1",
                                        idem="meal:2026-11-18:lunch:0")])
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 18, 11, 30)))
    assert done and done[0]["slot"] == "lunch"
    assert finance.calls


def test_drink_does_not_block_lunch(env):
    daily, repo, finance, _ = env
    daily._amo = lambda: WalletAmo([row(12, 30, "公司下午茶发了杯奶茶",
                                        idem="food:micro:2026-11-18:奶茶:1230")])
    assert daily._amo_meal_today("2026-11-18", "lunch", dt.datetime(2026, 11, 18, 12, 40)) is False
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 18, 12, 40)))
    assert done and done[0]["slot"] == "lunch"


def test_other_day_does_not_block(env):
    daily, _, _, _ = env
    daily._amo = lambda: WalletAmo([row(11, 5, "今天不想做饭，点了个外卖", day="2026-11-17")])
    assert daily._amo_meal_today("2026-11-18", "lunch", dt.datetime(2026, 11, 18, 11, 30)) is False
