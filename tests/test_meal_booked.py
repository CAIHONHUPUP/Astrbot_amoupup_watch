# -*- coding: utf-8 -*-
"""守护三餐：日程里已经排了饭局就不再买（2026-11-20）。

现场：18:00 买了面条，19:09 和柳如烟吃火锅。社交消费到点才扣，18:00 账本干净。
"""
import asyncio
import datetime as dt

import pytest

from services.consume_engine import FinanceConsume
from services.daily_engine import FinanceDaily

try:
    from tests.fixtures import temp_store
except ImportError:
    from fixtures import temp_store


class FakeFinance:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = list(rows or [])

    def schedule_windows(self, day):
        return list(self.rows)

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        self.calls.append((cents, reason, idem))
        return {"ok": True}


def win(start, end, activity, category="social"):
    return {"start": start, "end": end, "activity": activity, "category": category}


@pytest.fixture()
def env(tmp_path):
    store, repo = temp_store(tmp_path)
    finance = FakeFinance()
    daily = FinanceDaily(None, repo, finance, FinanceConsume(None, repo, finance))
    async def _no_extras(now, day):
        return []
    daily._extras = _no_extras
    daily._amo = lambda: None
    yield daily, repo, finance
    store.close()


def test_dinner_booked_skips_dinner(env):
    daily, repo, finance = env
    finance.rows = [win("19:09", "20:00", "柳如烟下班吃火锅")]
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 20, 18, 0)))
    assert done == []
    assert finance.calls == []
    # 占位不写死 flag：饭局在窗口内取消后还要能补买
    assert not repo.meta_get("meal:2026-11-20:dinner")


def test_no_booking_still_eats(env):
    daily, repo, finance = env
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 20, 18, 0)))
    assert done and done[0]["slot"] == "dinner"
    assert finance.calls


def test_lunch_shopping_does_not_block(env):
    daily, repo, finance = env
    finance.rows = [win("12:00", "14:00", "和柳如烟逛街")]
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 20, 11, 30)))
    assert done and done[0]["slot"] == "lunch"


def test_drink_does_not_block(env):
    daily, repo, finance = env
    finance.rows = [win("14:30", "15:00", "和同事喝咖啡")]
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 20, 11, 30)))
    assert done and done[0]["slot"] == "lunch"


def test_other_slot_does_not_block(env):
    daily, repo, finance = env
    finance.rows = [win("12:00", "13:00", "同事聚餐")]
    done = asyncio.run(daily.tick(dt.datetime(2026, 11, 20, 18, 0)))
    assert done and done[0]["slot"] == "dinner"
