# -*- coding: utf-8 -*-
"""约会收尾截断要留痕（2026-11-21）。"""
import datetime as dt
import unittest

from services.date_rules import truncate_finished_blocks


def block(start, end, activity, category="social"):
    return {"start": start, "end": end, "activity": activity, "category": category}


NOW = dt.datetime(2026, 11, 20, 20, 39)


class TruncateMarkTests(unittest.TestCase):
    def test_ongoing_social_block_gets_marker(self):
        rows, changed = truncate_finished_blocks(
            [block("20:00", "22:00", "和老公吃饭")], NOW)
        self.assertTrue(changed)
        self.assertEqual("20:39", rows[0]["end"])
        self.assertEqual("date_finish", rows[0]["truncated_by"])
        self.assertEqual("22:00", rows[0]["origin_end"])

    def test_future_date_block_is_removed(self):
        # 还没开始的「和老公」约会块按既有规则直接去掉（不是截断）
        rows, changed = truncate_finished_blocks(
            [block("21:00", "22:00", "和老公吃饭")], NOW)
        self.assertTrue(changed)
        self.assertEqual([], rows)


if __name__ == "__main__":
    unittest.main()
