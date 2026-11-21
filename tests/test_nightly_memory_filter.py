# -*- coding: utf-8 -*-
"""夜间提炼只碰守护自己的记忆（2026-11-21）。"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.nightly_memory import _watch_rows  # noqa: E402


class NightlyFilterTests(unittest.TestCase):
    def test_forever_activity_not_archived(self):
        rows = [
            {"id": 1, "kind": "activity", "content": "在咖啡店"},
            {"id": 2, "kind": "diary", "content": "长日记"},
            {"id": 3, "kind": "finance", "content": "今天花了 30"},
        ]
        self.assertEqual([3], [row["id"] for row in _watch_rows(rows)])

    def test_unknown_kind_skipped(self):
        self.assertEqual([], _watch_rows([{"id": 1, "kind": ""}, {"id": 2}]))


if __name__ == "__main__":
    unittest.main()
