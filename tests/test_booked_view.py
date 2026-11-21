# -*- coding: utf-8 -*-
"""未来待办也要参与冲突检测（2026-11-21）。"""
from __future__ import annotations

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.amo_bridge import FinanceLink  # noqa: E402


class BookedViewTests(unittest.TestCase):
    def test_todo_window_is_booked(self):
        amo = types.SimpleNamespace()
        amo.state = types.SimpleNamespace(
            schedule={"2026-11-22": [{"start": "10:00", "end": "12:00", "activity": "上班"}]},
            todos=lambda day: [{"start": "18:00", "end_time": "20:00", "activity": "和妈妈吃饭",
                                "category": "social"}],
        )
        link = FinanceLink(None)
        link._amo = lambda: amo
        rows = link.schedule_windows("2026-11-22")
        self.assertIn("18:00", [row["start"] for row in rows])
        self.assertIn("10:00", [row["start"] for row in rows])


if __name__ == "__main__":
    unittest.main()
