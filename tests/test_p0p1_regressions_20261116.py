# -*- coding: utf-8 -*-
"""2026-11-16 P0/P1 回归：旧行情快照门禁 / 年终奖顺延 / 账单重复扣款 / T+1 份额释放。"""
import asyncio
import datetime as dt
import json

import pytest

from models.db import Store
from models.ledger import FinanceRepo
import services.fund_market as fm
import services.bill_flow as bf
from services.life_engine import FinanceLife
from services.trade_engine import Trading
from services.work_engine import FinanceSalary
import core.jobs as jobs


class SyncRuntime:
    """把 run_db / run_io 直接同步执行，和 FakeRuntime 一样不碰真线程。"""

    async def run_db(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)

    async def run_io(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture()
def repo(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    yield FinanceRepo(store)
    store.close()


# ---------- 1. 旧 meta 行情快照不能当今日估值 ----------
def test_legacy_fund_quotes_not_tradeable_on_another_day(repo):
    legacy = {"110020": {"code": "110020", "name": "沪深300", "nav": 1.5, "estimate": 1.52,
                         "change_pct": 1.3, "time": "2026-11-15 14:50", "tradeable": True}}
    repo.meta_set("fund_quotes", json.dumps(legacy))
    market = fm.FundMarket(object(), SyncRuntime(), repo)
    old = market.latest("2026-11-16")["110020"]
    assert old["tradeable"] is False and old["stale"] is True
    # 同一天的快照仍然可用（DB 没写成功时的兜底路径不能被误杀）
    same = market.latest("2026-11-15")["110020"]
    assert same["tradeable"] is True and same["stale"] is False


def test_legacy_fund_quotes_without_time_is_not_tradeable(repo):
    repo.meta_set("fund_quotes", json.dumps({"005827": {"name": "易方达", "nav": 2.18,
                                                        "change_pct": 1.2}}))
    market = fm.FundMarket(object(), SyncRuntime(), repo)
    quote = market.latest("2026-11-16")["005827"]
    assert quote["tradeable"] is False


# ---------- 2. 1 月发薪日顺延后年终奖不能丢 ----------
class _SalaryRepo:
    def __init__(self):
        self.meta = {}
        self.items = []

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def add_salary_item(self, period, kind, label, amount_cents):
        self.items.append((period, kind, label, int(amount_cents)))
        return True

    def salary_items(self, period):
        return [{"kind": kind, "label": label, "amount_cents": amount}
                for row_period, kind, label, amount in self.items if row_period == period]

    def settled(self, period, kind):
        return False

    def mark_settled(self, period, kind, payload="{}"):
        return True


class _SalaryFinance:
    def balance_cents(self):
        return 0

    async def income(self, *args, **kwargs):
        return {"ok": True}

    async def permanent_memory(self, *args, **kwargs):
        return True

    def small_diary(self, text):
        return True

    def long_diary(self, text):
        return True

    def nudge_mood(self, *args, **kwargs):
        return True


class _NoopEvents:
    async def grant_full_attendance(self, *args, **kwargs):
        return None


def test_year_end_bonus_survives_shifted_pay_day():
    # 2028-01-15 是周六，发薪日顺延到 2028-01-17（周一）；年终奖不能因此消失
    repo = _SalaryRepo()
    plugin = type("P", (), {"work_events": _NoopEvents()})()
    salary = FinanceSalary(plugin, repo, _SalaryFinance())
    assert bf.salary_pay_day(dt.date(2028, 1, 15)) == dt.date(2028, 1, 17)
    out = asyncio.run(salary.settle(dt.datetime(2028, 1, 17, 10, 0), "2028-01"))
    assert out["ok"] is True
    labels = [row[2] for row in repo.items]
    assert "年终奖" in labels
    assert out["summary"]["bonus_cents"] > 0


# ---------- 3. 手动交账单后生活结算不能按另一个幂等键再扣 ----------
class _LifeFinance:
    def __init__(self):
        self.expenses = []

    def _amo(self):
        return None

    def balance_cents(self):
        return 9999999

    async def expense(self, amount, name="", idem="", **kwargs):
        self.expenses.append((int(amount), str(name), str(idem)))
        return {"ok": True}

    async def permanent_memory(self, *args, **kwargs):
        return True

    async def income(self, *args, **kwargs):
        return {"ok": True}

    def small_diary(self, text):
        return True

    def long_diary(self, text):
        return True

    def nudge_mood(self, *args, **kwargs):
        return True


def _bill_plugin(repo, finance):
    plugin = type("P", (), {})()
    plugin.finance_repo = repo
    plugin.config = {}
    plugin.cfg_bool = None
    plugin.finance = finance
    return plugin


def test_pay_bill_marks_settled_so_life_does_not_charge_again(repo):
    period = "2026-11"
    plugin = _bill_plugin(repo, _LifeFinance())
    bf.save_due(plugin, period, [{"kind": "rent", "name": "房租", "amount_cents": 150000,
                                  "status": "due", "created_ts": 0.0, "penalty": 0}])
    paid = asyncio.run(bf.pay(plugin, kind="rent", now=dt.datetime(2026, 11, 2, 10, 0)))
    assert paid["ok"] is True
    assert repo.settled(period, "rent") is True
    finance = plugin.finance
    life = FinanceLife(plugin, repo, finance)
    again = asyncio.run(life._rent(dt.datetime(2026, 11, 2, 11, 0), period))
    assert again == "skip"
    assert len(finance.expenses) == 1


def test_auto_bill_payment_marks_settled_too(repo):
    period = "2026-11"
    plugin = _bill_plugin(repo, _LifeFinance())
    rows = [{"kind": "utilities", "name": "水电燃气宽带", "amount_cents": 30000,
             "status": "due", "created_ts": 0.0, "penalty": 0}]
    paid = asyncio.run(bf.force_settle(plugin, rows, period, dt.datetime(2026, 11, 2, 10, 0)))
    assert paid["paid"] == 1
    for key in ("water", "electric", "gas", "broadband"):
        assert repo.settled(period, key) is True


# ---------- 4. T+1/T+2 已到期份额要按定时任务放行，而不是只在启动时 ----------
class _PendingFinance:
    def _amo(self):
        return None

    def balance_cents(self):
        return 0


def test_process_pending_releases_available_shares(repo):
    repo.upsert_position("fund", "110020", "沪深300", 100.0, 200, 210, 0.0, "2026-11-01")
    plugin = type("P", (), {})()
    plugin.finance_repo = repo
    plugin.runtime = SyncRuntime()
    plugin.finance = _PendingFinance()
    plugin.trading = Trading(plugin, SyncRuntime(), repo, plugin.finance)
    asyncio.run(jobs.process_pending(plugin))
    position = repo.position("fund", "110020")
    assert position["available"] == 100.0
