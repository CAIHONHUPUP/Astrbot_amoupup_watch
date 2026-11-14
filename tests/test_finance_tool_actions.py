# -*- coding: utf-8 -*-
"""finance 工具新增 action 的专门单测：活期转可用 / 取定期 / 卖股票 / 卖基金 / 缴费 / 冻结拦截。"""
import asyncio
import datetime as dt
import json
import unittest

import main as watch_main
from services import bill_flow as bf


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value

    def settled(self, period, key):
        return False


class FakeFinance:
    def __init__(self, cash=100000):
        self.cash = int(cash)
        self.mem = []
        self.diaries = []
        self.moods = []

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, reason, idem, necessary=False):
        if self.cash < int(amount):
            return {"ok": False}
        self.cash -= int(amount)
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
    def __init__(self, finance):
        self.finance = finance
        self.calls = []

    async def withdraw(self, amount, term_months=None, now=None):
        self.calls.append((int(amount), term_months))
        await self.finance.income(int(amount), "取款", "wd")
        return {"ok": True, "amount": int(amount), "penalty": term_months is not None}


class FakeTrading:
    def __init__(self):
        self.sells = []
        self.redeems = []

    async def sell(self, code, qty, price=None, idem=""):
        self.sells.append((code, qty, idem))
        return {"ok": True}

    async def redeem(self, code, shares, nav=None, idem=""):
        self.redeems.append((code, shares, idem))
        return {"ok": True}


class FakeSelf:
    def __init__(self, frozen=False):
        self.finance_repo = FakeRepo()
        self.finance = FakeFinance()
        self.bank = FakeBank(self.finance)
        self.trading = FakeTrading()
        if frozen:
            self.finance_repo.meta_set(bf.FROZEN_KEY, json.dumps({"period": "2026-11"}))

    async def _bank(self):
        return self.bank


def _run(self, **kwargs):
    return asyncio.run(watch_main.WatchPlugin.finance_tool(self, None, **kwargs))


class FinanceToolActionTests(unittest.TestCase):
    def test_transfer_current_uses_current_account(self):
        plugin = FakeSelf()
        msg = _run(plugin, action="transfer_current_to_balance", amount=100)
        self.assertEqual(plugin.bank.calls[-1], (10000, None))
        self.assertIn("活期", msg)

    def test_withdraw_fixed_uses_term_deposit(self):
        plugin = FakeSelf()
        msg = _run(plugin, action="withdraw_fixed", amount=100)
        self.assertEqual(plugin.bank.calls[-1], (10000, 0))
        self.assertIn("定期", msg)

    def test_sell_stock_calls_trading(self):
        plugin = FakeSelf()
        msg = _run(plugin, action="sell_stock", code="600519", qty=100)
        self.assertEqual(plugin.trading.sells[0][0], "600519")
        self.assertIn("股票", msg)

    def test_sell_fund_calls_redeem(self):
        plugin = FakeSelf()
        msg = _run(plugin, action="sell_fund", code="000001", qty=10)
        self.assertEqual(plugin.trading.redeems[0][0], "000001")
        self.assertIn("基金", msg)

    def test_pay_bill_pays_due_row(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakeSelf()
        plugin.finance.cash = 400000
        bf.save_due(plugin, "2026-11", [{
            "kind": "rent", "name": "房租", "amount_cents": 300000,
            "status": "due", "created_ts": now.timestamp(),
        }])
        msg = _run(plugin, action="pay_bill", event_kind="rent")
        self.assertIn("交上了", msg)
        self.assertEqual(bf.load_due(plugin, "2026-11")[0]["status"], "paid")

    def test_frozen_account_blocks_asset_actions(self):
        plugin = FakeSelf(frozen=True)
        for action, kwargs in (
            ("sell_stock", {"code": "600519", "qty": 100}),
            ("sell_fund", {"code": "000001", "qty": 10}),
            ("withdraw_fixed", {"amount": 100}),
            ("transfer_current", {"amount": 100}),
        ):
            msg = _run(plugin, action=action, **kwargs)
            self.assertIn("冻结", msg, action)
        self.assertFalse(plugin.trading.sells)
        self.assertFalse(plugin.trading.redeems)
        self.assertFalse(plugin.bank.calls)

    def test_frozen_account_still_allows_pay_bill(self):
        now = dt.datetime(2026, 11, 2, 9)
        plugin = FakeSelf(frozen=True)
        plugin.finance.cash = 300000
        bf.save_due(plugin, "2026-11", [{
            "kind": "rent", "name": "房租", "amount_cents": 300000,
            "status": "due", "created_ts": now.timestamp(),
        }])
        msg = _run(plugin, action="pay_bill", event_kind="rent")
        self.assertIn("交上了", msg)


if __name__ == "__main__":
    unittest.main()
