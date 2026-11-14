# -*- coding: utf-8 -*-
"""3 天档真实处置资产 / 7 天档冻结账户（2026-11-14）。"""
import asyncio
import datetime as dt
import unittest

from services import bill_flow as bf


class FakeRepo:
    def __init__(self):
        self.data = {}
        self.pos = []

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value

    def positions(self, kind=""):
        return [p for p in self.pos if not kind or p.get("kind") == kind]

    def settled(self, period, key):
        return False


class FakeFinance:
    def __init__(self, cash=0):
        self.cash = int(cash)
        self.mem = []
        self.diaries = []
        self.moods = []
        self.expenses = []

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, reason, idem, necessary=False):
        if self.cash < int(amount):
            return {"ok": False}
        self.cash -= int(amount)
        self.expenses.append((int(amount), reason, idem))
        return {"ok": True}

    async def income(self, amount, reason, idem):
        self.cash += int(amount)
        return {"ok": True}

    async def permanent_memory(self, text, key):
        self.mem.append(text)

    def small_diary(self, text):
        self.diaries.append(text)

    def nudge_mood(self, delta, key):
        self.moods.append(delta)


class FakeBank:
    def __init__(self, finance, fixed=0, current=0):
        self.finance = finance
        self.fixed = int(fixed)
        self.current = int(current)
        self.calls = []

    async def withdraw(self, amount, term_months=None, now=None):
        self.calls.append((int(amount), term_months))
        if term_months is None:
            if self.current < int(amount):
                return {"ok": False, "reason": "insufficient"}
            self.current -= int(amount)
            await self.finance.income(int(amount), "取款：活期", "wd-cur")
            return {"ok": True, "amount": int(amount), "penalty": False}
        take = min(int(amount), self.fixed)
        if take <= 0:
            return {"ok": False, "reason": "no_fixed"}
        self.fixed -= take
        await self.finance.income(take, "取款：定期", "wd-fixed")
        return {"ok": True, "amount": take, "penalty": True}


class FakeLife:
    def _has(self, kind):
        return False

    def rent_cents(self, period):
        return 300000

    def utility_cents(self, period):
        return 20000


class FakeTrading:
    def __init__(self, finance):
        self.finance = finance
        self.sells = []
        self.redeems = []

    async def sell(self, code, qty, price=None, idem=""):
        self.sells.append((code, qty, price))
        self.finance.cash += int(round(float(price) * qty * 100))
        return {"ok": True}

    async def redeem(self, code, shares, nav=None, idem=""):
        self.redeems.append((code, shares))
        return {"ok": True}


class FakePlugin:
    def __init__(self, cash=0, fixed=0):
        self.finance_repo = FakeRepo()
        self.finance = FakeFinance(cash)
        self.bank = FakeBank(self.finance, fixed=fixed)
        self.trading = FakeTrading(self.finance)

    async def _bank(self):
        return self.bank

    def cfg_str(self, key, default=""):
        return default


def _rows(tick, hours, amount=300000):
    return [{
        "kind": "rent", "name": "房租", "amount_cents": amount, "status": "due",
        "created_ts": (tick - dt.timedelta(hours=hours)).timestamp(),
    }]


def _seed(plugin, tick, hours, amount=300000):
    bf.save_due(plugin, "2026-11", _rows(tick, hours, amount))


class OwnedHomeTests(unittest.TestCase):
    def test_no_rent_when_amo_housing_says_own(self):
        class OwnHousing:
            def current(self):
                return {"kind": "own"}

        class OwnAmo:
            housing = OwnHousing()

        class OwnFinance(FakeFinance):
            def __init__(self):
                super().__init__(cash=500000)

            def _amo(self):
                return OwnAmo()

        plugin = FakePlugin(cash=500000, fixed=0)
        plugin.finance = OwnFinance()
        plugin.finance_life = FakeLife()
        rows = bf.create_due(plugin, dt.datetime(2026, 11, 2, 9))
        self.assertFalse(any(row["kind"] == "rent" for row in rows))


class ThreeDaySettlementTests(unittest.TestCase):
    def test_withdraws_fixed_deposit_and_pays(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakePlugin(cash=0, fixed=300000)
        _seed(plugin, now, 73)
        res = asyncio.run(bf.overdue_tick(plugin, now))
        self.assertEqual(res["stage"], 2)
        rows = bf.load_due(plugin, "2026-11")
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[0]["payer"], "auto")
        self.assertEqual(plugin.bank.calls[0][1], 0)  # 取的是定期
        self.assertEqual(plugin.finance.cash, 0)      # 取出来正好交完

    def test_sells_stock_when_no_deposit(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakePlugin(cash=0, fixed=0)
        plugin.finance_repo.pos = [{
            "kind": "stock", "code": "600519", "name": "贵州茅台",
            "qty": 100, "available": 100, "cost_cents": 250000, "last_cents": 3000,
        }]
        _seed(plugin, now, 73)
        res = asyncio.run(bf.overdue_tick(plugin, now))
        self.assertEqual(res["stage"], 2)
        self.assertTrue(plugin.trading.sells)
        self.assertEqual(plugin.trading.sells[0][0], "600519")
        self.assertEqual(bf.load_due(plugin, "2026-11")[0]["status"], "paid")

    def test_settlement_is_idempotent(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakePlugin(cash=0, fixed=600000)
        _seed(plugin, now, 73)
        asyncio.run(bf.overdue_tick(plugin, now))
        paid_calls = len(plugin.finance.expenses)
        asyncio.run(bf.overdue_tick(plugin, now))
        self.assertEqual(len(plugin.finance.expenses), paid_calls)

    def test_insufficient_assets_keeps_bill_due(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakePlugin(cash=0, fixed=0)
        _seed(plugin, now, 73)
        asyncio.run(bf.overdue_tick(plugin, now))
        row = bf.load_due(plugin, "2026-11")[0]
        self.assertEqual(row["status"], "due")
        self.assertTrue(row.get("forced"))


class SevenDayFreezeTests(unittest.TestCase):
    def test_seven_days_freezes_assets_instead_of_writing_off(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakePlugin(cash=0, fixed=0)
        _seed(plugin, now, 169)
        res = asyncio.run(bf.overdue_tick(plugin, now))
        self.assertEqual(res["stage"], 3)
        self.assertTrue(bf.is_frozen(plugin))
        self.assertEqual(bf.frozen_state(plugin)["period"], "2026-11")
        row = bf.load_due(plugin, "2026-11")[0]
        self.assertEqual(row["status"], "due")   # 不再按“终结”销账
        self.assertTrue(row.get("frozen"))

    def test_final_penalty_charged_once(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakePlugin(cash=50000, fixed=0)
        _seed(plugin, now, 169)
        asyncio.run(bf.overdue_tick(plugin, now))
        first = [e for e in plugin.finance.expenses if "最终违约金" in e[1]]
        asyncio.run(bf.overdue_tick(plugin, now))
        second = [e for e in plugin.finance.expenses if "最终违约金" in e[1]]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)

    def test_paying_all_bills_clears_freeze(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakePlugin(cash=0, fixed=0)
        _seed(plugin, now, 169)
        asyncio.run(bf.overdue_tick(plugin, now))
        self.assertTrue(bf.is_frozen(plugin))
        plugin.finance.cash = 300000
        out = asyncio.run(bf.pay(plugin, "rent", now))
        self.assertTrue(out["ok"])
        self.assertFalse(bf.is_frozen(plugin))


if __name__ == "__main__":
    unittest.main()
