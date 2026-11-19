# -*- coding: utf-8 -*-
"""迟到只在早晨：下午/晚上不允许再插一条迟到扣款。"""
import asyncio
import datetime as _dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.work_events import WorkEventEngine  # noqa: E402


class LateWindowTests(unittest.TestCase):
    def test_afternoon_late_rejected(self):
        engine = WorkEventEngine(None, None, None, None)
        out = asyncio.run(engine.fire("late", source="auto",
                                       now=_dt.datetime(2026, 11, 19, 15, 19)))
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("reason"), "late_out_of_window")

    def test_evening_medical_late_rejected(self):
        engine = WorkEventEngine(None, None, None, None)
        out = asyncio.run(engine.fire("late", source="medical",
                                       now=_dt.datetime(2026, 11, 19, 20, 5)))
        self.assertEqual(out.get("reason"), "late_out_of_window")


if __name__ == "__main__":
    unittest.main()
