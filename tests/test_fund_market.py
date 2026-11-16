# -*- coding: utf-8 -*-
"""14:50 刷新：交易日/节假日、幂等、数据库落盘、LLM 事实块。"""
import asyncio
import datetime as dt

import pytest

from models.db import Store
from models.ledger import FinanceRepo
import services.fund_market as fm


class FakeRuntime:
    async def run_io(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)

    async def run_db(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


class FakePlugin:
    def __init__(self, repo):
        self.finance_repo = repo
        self.config = {}


QUOTES = {
    "005827": {"code": "005827", "name": "易方达蓝筹精选混合", "dwjz": 2.0, "gsz": 2.1,
               "gszzl": 5.0, "nav": 2.0, "estimate": 2.1, "change_pct": 5.0,
               "time": "2026-11-16 14:50", "source": "fundgz", "tradeable": True},
    "110020": {"code": "110020", "name": "易方达沪深300ETF联接A", "dwjz": 1.5, "gsz": 1.455,
               "gszzl": -3.0, "nav": 1.5, "estimate": 1.455, "change_pct": -3.0,
               "time": "2026-11-16 14:50", "source": "fundgz", "tradeable": True},
}


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    yield repo, FakeRuntime(), store
    store.close()


def _market(repo, runtime):
    return fm.FundMarket(FakePlugin(repo), runtime, repo)


def test_weekend_is_skipped(env):
    repo, runtime, _ = env
    out = asyncio.run(_market(repo, runtime).refresh(dt.datetime(2026, 11, 15, 14, 50)))
    assert out["skipped"] == "holiday"


def test_before_1450_is_skipped(env):
    repo, runtime, _ = env
    out = asyncio.run(_market(repo, runtime).refresh(dt.datetime(2026, 11, 16, 14, 49)))
    assert out["skipped"] == "before_14:50"


def test_refresh_saves_db_and_is_idempotent(env, monkeypatch):
    repo, runtime, _ = env
    calls = []

    def fake_fetch(codes):
        calls.append(list(codes))
        return dict(QUOTES)

    monkeypatch.setattr(fm, "fetch_fund_quotes", fake_fetch)
    market = _market(repo, runtime)
    out = asyncio.run(market.refresh(dt.datetime(2026, 11, 16, 14, 50)))
    assert out["ok"] is True
    assert out["quotes"] == 2
    assert out["tradeable"] == 2
    assert len(calls) == 1
    rows = repo.fund_quotes("2026-11-16")
    assert len(rows) == 2
    row = repo.fund_quote("2026-11-16", "005827")
    assert row["dwjz_e4"] == 20000
    assert row["gsz_e4"] == 21000
    assert row["gszzl_bp"] == 500
    assert row["tradeable"] == 1
    assert repo.meta_get("fund_refresh:2026-11-16").startswith("ok:2/")
    latest = market.latest("2026-11-16")
    assert latest["005827"]["estimate"] == 2.1
    assert latest["005827"]["tradeable"] is True
    again = asyncio.run(market.refresh(dt.datetime(2026, 11, 16, 14, 52)))
    assert again["skipped"] == "done"
    assert len(calls) == 1


def test_no_quotes_retries_then_marks_failed(env, monkeypatch):
    repo, runtime, _ = env
    monkeypatch.setattr(fm, "fetch_fund_quotes", lambda codes: {})
    market = _market(repo, runtime)
    out = None
    for _ in range(3):
        out = asyncio.run(market.refresh(dt.datetime(2026, 11, 16, 14, 50)))
    assert out["ok"] is False and out["tries"] == 3
    assert repo.meta_get("fund_refresh:2026-11-16") == "failed:3"


def test_facts_has_prompt_and_balance(env):
    repo, runtime, _ = env
    text = _market(repo, runtime).facts("2026-11-16", 123456, QUOTES)
    assert "现在是14:50，距离基金申购截止还有10分钟" in text
    assert "今日基金行情" in text
    assert "你当前余额是 1234.56 元" in text
    assert "buy_fund" in text and "sell_fund" in text
    assert "不买也可以" in text


def test_facts_marks_stale_quote_not_tradeable(env):
    repo, runtime, _ = env
    quotes = {"005827": {"code": "005827", "name": "易方达蓝筹精选混合", "nav": 2.0,
                         "estimate": 2.0, "change_pct": 0.0, "source": "pingzhongdata",
                         "tradeable": False}}
    text = _market(repo, runtime).facts("2026-11-16", 100000, quotes)
    assert "今日估值不可用" in text


def test_refresh_does_not_block_event_loop(monkeypatch):
    import time as _time

    class SlowRepo:
        """模拟 sqlite：每次读写阻塞 50ms。"""

        def __init__(self):
            self.meta = {}
            self.saved = []

        def meta_get(self, key, default=None):
            _time.sleep(0.05)
            return self.meta.get(key, default)

        def meta_set(self, key, value):
            _time.sleep(0.05)
            self.meta[key] = value

        def save_fund_quotes(self, day, quotes):
            _time.sleep(0.05)
            self.saved.append((day, dict(quotes)))

        def positions(self, kind=""):
            return []

        def upsert_position(self, *args, **kwargs):
            return None

    class ThreadRuntime:
        async def run_io(self, fn, *args, timeout=None, **kwargs):
            return await asyncio.to_thread(fn, *args, **kwargs)

        async def run_db(self, fn, *args, timeout=None, **kwargs):
            return await asyncio.to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(fm, "fetch_fund_quotes", lambda codes: dict(QUOTES))
    repo = SlowRepo()
    market = fm.FundMarket(object(), ThreadRuntime(), repo)
    gaps = []

    async def scenario():
        loop = asyncio.get_running_loop()

        async def beat():
            last = loop.time()
            while True:
                await asyncio.sleep(0.005)
                now = loop.time()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(beat())
        try:
            return await market.refresh(dt.datetime(2026, 11, 16, 14, 50))
        finally:
            task.cancel()

    out = asyncio.run(scenario())
    assert out["ok"] is True and out["quotes"] == len(QUOTES)
    assert len(repo.saved) == 1
    # 旧实现同步写 sqlite 时事件循环会整段停住（≥50ms×3）；走 run_db 后保持毫秒级
    assert max(gaps) < 0.08
