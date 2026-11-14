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


def run(life, when=None):
    return asyncio.run(life._check_hunger(when or dt.datetime(2026, 11, 14, 12, 0)))


class HungerForageTests(unittest.TestCase):
    def make(self, **kwargs):
        finance = FakeFinance(**kwargs)
        return FinanceLife(None, None, finance), finance

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
        self.assertEqual(finance.amo.added[-1], 40)
        self.assertEqual(finance.amo.meals[-1][1], 4)

    def test_cheap_meal_when_low_cash(self):
        life, finance = self.make(cash=3000, amo=FakeAmo(value=20))
        out = run(life)
        self.assertEqual(out["food"], "馒头和泡面")
        self.assertEqual(finance.spent[0][0], 800)
        self.assertEqual(finance.amo.added[-1], 15)

    def test_sick_only_porridge(self):
        life, finance = self.make(cash=20000, amo=FakeAmo(value=20, sick=True))
        out = run(life)
        self.assertEqual(out["food"], "白粥和清淡面条")
        self.assertEqual(finance.spent[0][0], 1200)
        self.assertIn("生病了只能喝粥", finance.diaries[-1])

    def test_late_night_snack(self):
        life, finance = self.make(cash=20000, amo=FakeAmo(value=20))
        out = run(life, dt.datetime(2026, 11, 14, 23, 40))
        self.assertEqual(out["food"], "烧烤和泡面")
        self.assertEqual(finance.spent[0][0], 2800)
        self.assertIn("半夜", finance.diaries[-1])
        self.assertTrue(finance.memories)

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


if __name__ == "__main__":
    unittest.main()
