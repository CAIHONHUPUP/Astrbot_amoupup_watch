# -*- coding: utf-8 -*-
"""账单：分类分组、今日/本月合计、Prompt 摘要。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import bill_engine as bill  # noqa: E402

NOW = dt.datetime(2026, 11, 14, 15, 0)


class FakeWallet:
    def __init__(self):
        self.rows = [
            {"ts": NOW.replace(hour=8).timestamp(), "delta_cents": -750,
             "category": "餐饮", "sub_category": "早餐", "item_name": "豆浆+包子", "reason": "餐饮：豆浆+包子"},
            {"ts": NOW.replace(hour=12).timestamp(), "delta_cents": -2500,
             "category": "交通", "sub_category": "打车", "item_name": "打车", "reason": "交通：打车"},
            {"ts": (NOW - dt.timedelta(days=40)).timestamp(), "delta_cents": -1000,
             "category": "其他", "sub_category": "日常", "item_name": "话费", "reason": "其他：话费"},
        ]

    def ledger(self, limit=300):
        return list(self.rows)


class FakeAmo:
    wallet = FakeWallet()


class FakeFinance:
    def _amo(self):
        return FakeAmo()


class FakePlugin:
    finance = FakeFinance()


class BillTests(unittest.TestCase):
    def test_summary_today_and_month(self):
        data = bill.summary(FakePlugin(), NOW)
        self.assertEqual(data["today_cents"], 3250)
        self.assertEqual(data["month_cents"], 3250)
        self.assertIn("餐饮", data["groups"])
        self.assertIn("交通", data["groups"])

    def test_text_grouped(self):
        text = bill.text(FakePlugin(), NOW)
        self.assertIn("【餐饮】", text)
        self.assertIn("豆浆+包子", text)
        self.assertIn("【交通】", text)
        self.assertIn("本月总支出 32.50 元", text)

    def test_prompt_line(self):
        line = bill.prompt_line(FakePlugin(), NOW)
        self.assertIn("今日总支出 32.50 元", line)
        self.assertIn("豆浆+包子", line)
        self.assertIn("打车", line)


if __name__ == "__main__":
    unittest.main()
