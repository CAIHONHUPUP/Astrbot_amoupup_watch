# -*- coding: utf-8 -*-
"""约会状态以陪伴 session 为准（2026-11-20）。"""
import unittest

import services.date_engine as date_engine


class _Result:
    def __init__(self, value):
        self.ok = True
        self.value = value


class _Bridge:
    ready = True

    def __init__(self, session):
        self.session = session

    def call(self, name, *a, **k):
        if name == "date_session":
            return _Result(self.session)
        return _Result(None)


class _Repo:
    def meta_get(self, key, default=None):
        return default

    def meta_set(self, key, value):
        pass


class _Plugin:
    def __init__(self):
        self.finance_repo = _Repo()
        self.finance = None


class ActiveDateSessionTests(unittest.TestCase):
    def setUp(self):
        self._orig = date_engine._bridge

    def tearDown(self):
        date_engine._bridge = self._orig

    def test_session_active_wins_over_empty_meta(self):
        date_engine._bridge = lambda *a, **k: _Bridge(
            {"phase": "active", "title": "吃火锅", "start_ts": 1.0, "end_ts": 2.0})
        out = date_engine.active_date(_Plugin())
        self.assertIsNotNone(out)
        self.assertEqual("吃火锅", out["title"])

    def test_session_closed_is_not_dating(self):
        date_engine._bridge = lambda *a, **k: _Bridge({"phase": "closed"})
        self.assertIsNone(date_engine.active_date(_Plugin()))

    def test_bridge_unavailable_falls_back_to_meta(self):
        class _NoBridge:
            ready = False
        date_engine._bridge = lambda *a, **k: _NoBridge()
        self.assertIsNone(date_engine.active_date(_Plugin()))


if __name__ == "__main__":
    unittest.main()
