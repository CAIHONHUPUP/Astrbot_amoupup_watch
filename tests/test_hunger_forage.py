# -*- coding: utf-8 -*-
"""主动觅食：余额/健康/深夜/情绪分流，扣款、加饱腹、冷却、求助。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.life_engine import FinanceLife  # noqa: E402


class FakeAmo:
    def __init__(self, value=20, cooldown=0, sick=False):
        self.state = {"value": value, "cooldown_until": cooldown, "sick": sick}
        self.added = []
        self.meals = []
        self.asked = 0
        self.help_text = ""
        self.snacks = []

    def hunger_state(self):
        return dict(self.state)

    def add_hunger(self, target, amount):
        self.added.append(amount)

    def mark_meal(self, food, hours=4, mood="满足"):
        self.meals.append((food, hours, mood))

    def request_hunger_help(self):
        self.asked += 1

    async def hunger_help(self, text):
        self.asked += 1
        self.help_text = text

    async def late_snack_tick(self, now=None, roll=None, say=None):
        self.snacks.append(now)
        return {"ok": True, "eat": True, "charged": 2800, "food": "烧烤", "block_id": "b1"}


class NoSnackAmo:
    """老 amo：没有 late_snack_tick，宵夜必须降级成不动钱包。"""

    def hunger_state(self):
        return {"value": 20, "cooldown_until": 0, "sick": False}

    def add_hunger(self, target, amount):
        raise AssertionError("降级路径不该加饱腹")

    def mark_meal(self, food, hours=4, mood="满足"):
        raise AssertionError("降级路径不该记正餐")

    def request_hunger_help(self):
        pass


class FakeFinance:
    def __init__(self, cash=20000, amo=None, mood=None):
        self.cash = cash
        self.amo = amo or FakeAmo()
        self.mood_value = mood or {}
        self.spent = []
        self.diaries = []
        self.memories = []
        self.moods = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.cash

    def mood(self):
        return self.mood_value

    async def expense(self, amount, reason, idem, necessary=False):
        self.spent.append((amount, reason, idem))
        return {"ok": True}

    def nudge_mood(self, deltas, idem):
        self.moods.append((deltas, idem))

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)


class FakeRepo:
    def __init__(self, meta=None):
        self.meta = dict(meta or {})

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value


def run(life, when=None):
    return asyncio.run(life._check_hunger(when or dt.datetime(2026, 11, 14, 12, 0)))


class HungerForageTests(unittest.TestCase):
    def make(self, repo=None, **kwargs):
        finance = FakeFinance(**kwargs)
        return FinanceLife(None, repo, finance), finance

    def test_not_hungry_does_nothing(self):
        life, finance = self.make(amo=FakeAmo(value=60))
        self.assertIsNone(run(life))
        self.assertEqual(finance.spent, [])

    def test_cooldown_blocks(self):
        life, finance = self.make(amo=FakeAmo(value=10, cooldown=9e12))
        self.assertIsNone(run(life))
        self.assertEqual(finance.spent, [])

    def test_normal_meal(self):
        life, finance = self.make(cash=20000, amo=FakeAmo(value=20))
        out = run(life)
        self.assertEqual(out["food"], "一碗热汤面")
        self.assertEqual(finance.spent[0][0], 2000)
        self.assertEqual(finance.amo.added[-1], 60.0)   # 补到七分饱（80-20）
        self.assertEqual(finance.amo.meals[-1][1], 3)

    def test_cheap_meal_when_low_cash(self):
        life, finance = self.make(cash=3000, amo=FakeAmo(value=20))
        out = run(life)
        self.assertEqual(out["food"], "馒头和泡面")
        self.assertEqual(finance.spent[0][0], 800)
        self.assertEqual(finance.amo.added[-1], 60.0)   # 便宜餐也一样补到七分饱

    def test_sick_only_porridge(self):
        life, finance = self.make(cash=20000, amo=FakeAmo(value=20, sick=True))
        out = run(life)
        self.assertEqual(out["food"], "白粥和清淡面条")
        self.assertEqual(finance.spent[0][0], 1200)
        self.assertIn("生病了只能喝粥", finance.diaries[-1])

    def test_late_night_delegates_to_amo_entry(self):
        life, finance = self.make(cash=20000, amo=FakeAmo(value=20))
        out = run(life, dt.datetime(2026, 11, 14, 23, 40))
        self.assertTrue(out["late_snack"])
        self.assertEqual(len(finance.amo.snacks), 1, "宵夜只许调 amo 一次")
        self.assertEqual(finance.spent, [], "守护不许自己扣宵夜钱")
        self.assertEqual(finance.diaries, [], "守护不许自己写宵夜日记")
        self.assertEqual(finance.memories, [], "守护不许自己写宵夜记忆")

    def test_late_night_without_amo_entry_degrades_safely(self):
        life, finance = self.make(cash=20000, amo=NoSnackAmo())
        out = run(life, dt.datetime(2026, 11, 14, 23, 40))
        self.assertIsNone(out)
        self.assertEqual(finance.spent, [])
        self.assertEqual(finance.diaries, [])
        self.assertEqual(finance.memories, [])

    def test_low_mood_comfort_food(self):
        life, finance = self.make(cash=20000, amo=FakeAmo(value=20),
                                  mood={"郁闷": 70, "疲惫": 60})
        out = run(life)
        self.assertEqual(out["food"], "炸鸡和奶茶")
        self.assertEqual(finance.spent[0][0], 4000)

    def test_broke_asks_for_help(self):
        life, finance = self.make(cash=0, amo=FakeAmo(value=3))
        out = run(life)
        self.assertTrue(out["help"])
        self.assertEqual(finance.amo.asked, 1)
        self.assertIn("买个包子", finance.amo.help_text)
        self.assertIn("过期的饼干", finance.diaries[-1])



    def test_outside_meal_window_skips(self):
        # 15:30 既不是饭点、也没饿到 <15：不吃（以前这里会吃一碗热汤面）
        life, finance = self.make(cash=20000, amo=FakeAmo(value=20))
        self.assertIsNone(run(life, dt.datetime(2026, 11, 14, 15, 30)))
        self.assertEqual(finance.spent, [])

    def test_slot_already_eaten_skips(self):
        # 这一顿已经由 daily_engine 吃过（共享 meta 标记）：不再吃第二顿
        repo = FakeRepo({"meal:2026-11-14:lunch": "1"})
        life, finance = self.make(repo=repo, cash=20000, amo=FakeAmo(value=20))
        self.assertIsNone(run(life))
        self.assertEqual(finance.spent, [])

    def test_recent_meal_skips(self):
        when = dt.datetime(2026, 11, 14, 12, 0)
        repo = FakeRepo({"last_meal_at": when.timestamp() - 10 * 60})
        life, finance = self.make(repo=repo, cash=20000, amo=FakeAmo(value=20))
        self.assertIsNone(run(life, when))
        self.assertEqual(finance.spent, [])

    def test_starving_outside_window_still_eats(self):
        # 非饭点但快饿晕（<15）：兜底吃一顿，避免饿出事
        life, finance = self.make(cash=20000, amo=FakeAmo(value=8))
        out = run(life, dt.datetime(2026, 11, 14, 15, 30))
        self.assertEqual(out["food"], "一碗热汤面")
        self.assertEqual(len(finance.spent), 1)

if __name__ == "__main__":
    unittest.main()
