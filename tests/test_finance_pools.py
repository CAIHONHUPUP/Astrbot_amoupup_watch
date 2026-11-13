# -*- coding: utf-8 -*-
import asyncio
import datetime as dt
import json

import pytest

from finance_candidates import CandidatePools, observation_pool, parse_recommendation
from finance_life import FinanceLife
from finance_store import FinanceRepo
from store import Store


class FakeFinance:
    def __init__(self, cash=2300000):
        self.cash = cash
        self.credited = []

    def balance_cents(self):
        return self.cash

    def credit_initial(self, cents):
        self.cash += cents
        self.credited.append(cents)
        return True


class FakePlugin:
    def __init__(self):
        self.config = {}


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    finance = FakeFinance()
    yield repo, finance, store
    store.close()


def test_parse_recommendation_code_and_name():
    assert parse_recommendation("建议买600519") == "600519"
    assert parse_recommendation("建议买贵州茅台", {"贵州茅台": "600519"}) == "600519"
    assert parse_recommendation("随便聊聊") == ""


def test_recommend_caps_at_ten(env):
    repo, _, _ = env
    pools = CandidatePools(FakePlugin(), repo)
    for i in range(12):
        pools.recommend("建议买%06d" % (600000 + i))
    assert len(repo.candidates()) <= 10


def test_observation_pool_ranks_and_caps():
    quotes = {
        "600519": {"price": 110, "prev_close": 100, "volume": 1000},
        "600036": {"price": 101, "prev_close": 100, "volume": 5000},
        "%06d" % 1: {"price": 0, "prev_close": 0, "volume": 0},
    }
    pool = observation_pool(quotes, limit=8)
    assert pool[0] == "600519" and "600036" in pool
    assert len(pool) <= 8


def test_seed_once(env):
    repo, finance, _ = env
    life = FinanceLife(FakePlugin(), repo, finance)
    assert life.ensure_seed() is True
    assert finance.credited == [2300000]
    assert life.ensure_seed() is False
    assert repo.meta_get("seeded") == "1"


def test_raise_formula_range(env):
    repo, finance, _ = env
    life = FinanceLife(FakePlugin(), repo, finance)
    repo.meta_set("half_start_assets", 1000000)
    repo.meta_set("half_income_cents", 800000)
    repo.meta_set("half_expense_cents", 400000)
    repo.meta_set("salary_cents", 680000)
    result = life._raise_salary(dt.datetime(2026, 7, 15))
    assert result and 0.05 <= result["pct"] <= 0.15
    assert result["to"] != result["from"]


def test_raise_only_twice_a_year(env):
    repo, finance, _ = env
    life = FinanceLife(FakePlugin(), repo, finance)
    repo.meta_set("half_start_assets", 1000000)
    repo.meta_set("half_income_cents", 800000)
    repo.meta_set("half_expense_cents", 400000)
    assert life._raise_salary(dt.datetime(2026, 7, 15)) is not None
    assert life._raise_salary(dt.datetime(2026, 7, 20)) is None
    assert life._raise_salary(dt.datetime(2026, 8, 1)) is None

def test_daily_anchors_rotate_within_pool():
    pools = CandidatePools(FakePlugin(), _repo_stub())
    a = pools.daily_anchors("2026-11-13")
    b = pools.daily_anchors("2026-11-13")
    c = pools.daily_anchors("2026-11-14")
    assert a == b and a != c
    assert 24 <= len(a) <= 27
    from finance_candidates import SEED_POOL
    assert set(a) <= set(SEED_POOL)


def test_affordable_filters_expensive():
    pools = CandidatePools(FakePlugin(), _repo_stub())
    quotes = {"600519": {"price": 1680.0}, "601398": {"price": 5.0}}
    codes = pools.affordable(quotes, 2300000)
    assert "601398" in codes and "600519" not in codes


class _Repo:
    def candidates(self):
        return []

    def meta_get(self, key, default=None):
        return default

    def meta_set(self, key, value):
        return None


def _repo_stub():
    return _Repo()
