# -*- coding: utf-8 -*-
"""《模拟时钟测试清单》：发薪、买房换租、买车养车、求助、记忆压缩、重启不丢。"""
import asyncio
import datetime as dt

from models import FinanceRepo, Store
from services.credit_engine import FinanceHelp
from services.life_engine import FinanceLife
from services.property_engine import FinanceProperty


class FakeFinance:
    def __init__(self, cash=5000000, ok=True):
        self.cash = cash
        self.ok = ok
        self.calls = []
        self.memories = []
        self.diaries = []
        self.moods = []

    def balance_cents(self):
        return self.cash

    async def income(self, cents, reason, idem, salary=False):
        self.calls.append(("income", reason, int(cents)))
        if not self.ok:
            return {"ok": False}
        self.cash += int(cents)
        return {"ok": True}

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        self.calls.append(("expense", reason, int(cents)))
        if not self.ok:
            return {"ok": False}
        if self.cash < int(cents):
            return {"ok": False}
        self.cash -= int(cents)
        return {"ok": True}

    def mood(self):
        return {"word": "平静"}

    async def remember_event(self, kind, amount, text, idem):
        self.memories.append(text)
        return True

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def small_diary(self, text):
        self.diaries.append(text)
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True

    async def add_schedule(self, *a, **k):
        return True


class FakePlugin:
    config = {}

    def __init__(self):
        self.finance_property = None
        self.finance_salary = None
        self.work_events = None
        self.finance_expense = None


def make(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    fin = FakeFinance()
    return store, repo, fin


def test_salary_pays_once_on_the_15th():
    class Repo(FinanceRepo):
        pass

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        store = Store(root=d, min_free_mb=0)
        store.init()
        try:
            repo = FinanceRepo(store)
            fin = FakeFinance()
            life = FinanceLife(FakePlugin(), repo, fin)
            asyncio.run(life.tick(dt.datetime(2026, 11, 15, 10)))
            first = sum(1 for c in fin.calls if c[0] == "income")
            asyncio.run(life.tick(dt.datetime(2026, 11, 15, 11)))
            second = sum(1 for c in fin.calls if c[0] == "income")
            assert first == 1 and second == 1     # 同一天只发一次
        finally:
            store.close()


def test_house_removes_rent_and_adds_fee(tmp_path):
    store, repo, fin = make(tmp_path)
    try:
        life = FinanceLife(FakePlugin(), repo, fin)
        asyncio.run(life.tick(dt.datetime(2026, 11, 1)))
        reasons = [c[1] for c in fin.calls if c[0] == "expense"]
        assert "房租" in reasons
        fin.calls.clear()
        repo.add_asset("house", "房子", 96000000)
        plugin = FakePlugin()
        prop = FinanceProperty(plugin, repo, fin, life)
        plugin.finance_property = prop
        life.plugin = plugin
        asyncio.run(life.tick(dt.datetime(2026, 12, 1)))
        reasons = [c[1] for c in fin.calls if c[0] == "expense"]
        assert "房租" not in reasons
        assert "物业费" in reasons
        assert prop.property_fee_cents("2026-12") > 0
    finally:
        store.close()


def test_car_adds_monthly_cost(tmp_path):
    store, repo, fin = make(tmp_path)
    try:
        prop = FinanceProperty(FakePlugin(), repo, fin, None)
        assert prop.car_cost_cents("2026-12") == 0
        repo.add_asset("car", "车", 12000000)
        assert prop.car_cost_cents("2026-12") > 0
    finally:
        store.close()


def test_low_balance_enters_two_choice_help(tmp_path):
    store, repo, fin = make(tmp_path)
    try:
        repo.add_asset("house", "房子", 96000000)   # 有房但现金见底
        fin.cash = 100
        help_ = FinanceHelp(FakePlugin(), repo, fin)
        state = help_.status()
        assert state["low"] is True
        assert help_.prompt_text() or state["choice"]
    finally:
        store.close()


def test_restart_keeps_data(tmp_path):
    store, repo, fin = make(tmp_path)
    repo.meta_set("base_salary_cents", "880000")
    repo.add_expense("e1", "2026-11-13", "food", "面条", 1500)
    store.close()
    store2 = Store(root=tmp_path / "d", min_free_mb=0)
    store2.init()
    try:
        repo2 = FinanceRepo(store2)
        assert repo2.meta_get("base_salary_cents") == "880000"
        assert repo2.expense_totals("2026-11")[0]["total"] == 1500
    finally:
        store2.close()
