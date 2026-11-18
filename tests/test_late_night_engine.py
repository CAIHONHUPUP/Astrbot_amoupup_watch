# -*- coding: utf-8 -*-
"""熬夜后果：宵夜入口委托、迟到罚款、转账哄睡清空。"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import late_night_engine as late  # noqa: E402
from utils.helpers import stable_int  # noqa: E402


try:  # Phase 15：meta 桩改用 tests/fixtures.py 的共享实现（断言不变）
    from tests.fixtures import FakeRepo  # noqa: E402
except ImportError:
    from fixtures import FakeRepo  # noqa: E402


class FakeAmo:
    def __init__(self, active=True, late_yesterday=True, result=None):
        self.state = {"active": active, "late_yesterday": late_yesterday,
                      "cleared": False, "fined": False}
        self.calls = []
        self.result = result or {"ok": True, "eat": True, "charged": 2800,
                                 "block_id": "b123", "skipped": ""}
        self.fines = []
        self.soothed = []

    def late_night_state(self):
        return dict(self.state)

    async def late_snack_tick(self, now=None, roll=None):
        self.calls.append(now)
        return dict(self.result)

    def note_late_fine(self, amount):
        self.fines.append(amount)

    def soothe_late_night(self, reason=""):
        self.soothed.append(reason)


class IdempotentAmo(FakeAmo):
    """模拟 amo 按小时幂等：第二次 tick 只回 already_decided，不再扣款。"""

    def __init__(self):
        super().__init__()
        self.charged_cents = 0

    async def late_snack_tick(self, now=None, roll=None):
        self.calls.append(now)
        if self.charged_cents:
            return {"ok": True, "eat": False, "skipped": "already_decided"}
        self.charged_cents = 2800
        return {"ok": True, "eat": True, "charged": 2800, "block_id": "b123"}


class NoSnackAmo:
    """老版本 amo：有熬夜状态，但没有 late_snack_tick，必须安全降级。"""

    def late_night_state(self):
        return {"active": True, "late_yesterday": False, "cleared": False, "fined": False}


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo
        self.cash = 20000
        self.spent = []
        self.diaries = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, reason, idem, necessary=False):
        self.spent.append((amount, reason))
        return {"ok": True}

    def small_diary(self, text):
        self.diaries.append(text)

    def nudge_mood(self, deltas, idem):
        pass


class FakePlugin:
    def __init__(self, active=True, late_yesterday=True, result=None, amo=None):
        self.finance_repo = FakeRepo()
        self.amo = amo if amo is not None else FakeAmo(active, late_yesterday, result)
        self.finance = FakeFinance(self.amo)


class LateNightWatchTests(unittest.TestCase):
    def test_snack_delegates_to_amo_entry(self):
        plugin = FakePlugin()
        out = asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 23, 10)))
        self.assertEqual(len(plugin.amo.calls), 1, "宵夜必须走 amo 单一入口")
        self.assertEqual(plugin.amo.calls[0], dt.datetime(2026, 11, 15, 23, 10),
                         "把 now 交给 amo 判窗口")
        self.assertEqual(out.get("snack"), 2800)
        self.assertEqual(out.get("snack_block"), "b123")
        self.assertEqual(plugin.finance.spent, [], "watch 不许自己扣款")
        self.assertEqual(plugin.finance.diaries, [], "watch 不许自己写日记")

    def test_entry_called_in_quiet_hours(self):
        plugin = FakePlugin()
        out = asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 1, 10)))
        self.assertEqual(len(plugin.amo.calls), 1, "0-6 仍可自主决定，只是不主动")
        self.assertEqual(out.get("snack"), 2800)

    def test_no_call_outside_window(self):
        plugin = FakePlugin()
        asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 12, 10)))
        self.assertEqual(plugin.amo.calls, [])

    def test_no_call_when_not_active(self):
        plugin = FakePlugin(active=False)
        asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 23, 10)))
        self.assertEqual(plugin.amo.calls, [])

    def test_skipped_result_has_no_snack_amount(self):
        plugin = FakePlugin(result={"ok": True, "eat": False, "skipped": "sick"})
        out = asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 23, 10)))
        self.assertNotIn("snack", out)
        self.assertEqual(out.get("snack_decision"), "sick")
        self.assertNotIn("snack_reason", out, "watch 不再透传 amo 内部字段")

    def test_late_fine_on_workday(self):
        plugin = FakePlugin()
        day = None
        for i in range(14, 30):
            d = dt.date(2026, 11, i)
            if d.weekday() < 5 and stable_int(d.isoformat(), 0, 100) < 60:
                day = d
                break
        self.assertIsNotNone(day)
        asyncio.run(late.tick(plugin, dt.datetime.combine(day, dt.time(9, 0))))
        self.assertTrue(plugin.amo.fines)
        self.assertTrue(any("迟到" in d for d in plugin.finance.diaries))

    def test_transfer_soothes(self):
        plugin = FakePlugin()
        out = asyncio.run(late.maybe_soothe(plugin, "[转账]50 买个夜宵早点睡"))
        self.assertTrue(out["ok"])
        self.assertEqual(plugin.amo.soothed[-1], "夜宵转账")

    def test_snack_output_contract_keys(self):
        plugin = FakePlugin(result={"ok": True, "eat": True, "charged": 2800,
                                    "block_id": "b123", "food": "烧烤",
                                    "pushed": True, "decision": {"score": 1}})
        out = asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 23, 10)))
        self.assertLessEqual(set(out), {"snack", "snack_block", "snack_decision"},
                             "watch 只拿 amo 的 snack/snack_block/snack_decision")

    def test_snack_path_has_no_direct_finance_or_schedule_writes(self):
        source = inspect.getsource(late._late_snack)
        self.assertIn("late_snack_tick", source)
        for banned in ("expense(", "add_schedule_item", "commit_schedule_change",
                       "ledger", "save_diary", "push_to_owner", "wallet.debit"):
            self.assertNotIn(banned, source, "宵夜入口不许直接 %s" % banned)

    def test_missing_amo_entry_degrades_without_charge(self):
        plugin = FakePlugin(amo=NoSnackAmo())
        out = asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 23, 10)))
        self.assertEqual(out, {})
        self.assertEqual(plugin.finance.spent, [])
        self.assertEqual(plugin.finance.diaries, [])

    def test_repeated_tick_does_not_double_charge(self):
        amo = IdempotentAmo()
        plugin = FakePlugin(amo=amo)
        first = asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 23, 10)))
        second = asyncio.run(late.tick(plugin, dt.datetime(2026, 11, 15, 23, 20)))
        self.assertEqual(first.get("snack"), 2800)
        self.assertNotIn("snack", second)
        self.assertEqual(second.get("snack_decision"), "already_decided")
        self.assertEqual(amo.charged_cents, 2800, "同一小时只扣一次（amo 侧幂等）")
        self.assertEqual(len(amo.calls), 2, "每次 tick 只调一次入口")
        self.assertEqual(plugin.finance.spent, [], "watch 不许自己扣款")


if __name__ == "__main__":
    unittest.main()
