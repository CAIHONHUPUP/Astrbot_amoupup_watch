# -*- coding: utf-8 -*-
"""parse_when 时段校正（2026-11-21：晚上8:00 被解析成次日 08:00）。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.date_rules import parse_when  # noqa: E402

NOW = dt.datetime(2026, 11, 18, 15, 0)


class ParseWhenPeriodTests(unittest.TestCase):
    def test_evening_clock(self):
        self.assertEqual(dt.datetime(2026, 11, 18, 20, 0), parse_when("晚上8:00", NOW))

    def test_afternoon_clock(self):
        self.assertEqual(dt.datetime(2026, 11, 18, 15, 30), parse_when("下午3:30", NOW))

    def test_morning_clock_unchanged(self):
        early = dt.datetime(2026, 11, 18, 7, 0)
        self.assertEqual(dt.datetime(2026, 11, 18, 8, 30), parse_when("早上8:30", early))

    def test_tomorrow_evening(self):
        self.assertEqual(dt.datetime(2026, 11, 19, 20, 0), parse_when("明晚8:00", NOW))


if __name__ == "__main__":
    unittest.main()
