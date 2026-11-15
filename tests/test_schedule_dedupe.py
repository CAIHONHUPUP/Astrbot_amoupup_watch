# -*- coding: utf-8 -*-
"""日程写入去重：同一条邀约重复结算不该堆出一排一样的日程。"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.amo_bridge import FinanceLink  # noqa: E402


class FakeState:
    def __init__(self):
        self.schedule = {}
        self.calls = 0

    def add_schedule_item(self, day, item, source="", reason=""):
        self.calls += 1
        self.schedule.setdefault(day, []).append(dict(item))
        return {"changed": True}


class FakeAmo:
    def __init__(self):
        self.state = FakeState()


def test_identical_schedule_item_is_not_duplicated():
    amo = FakeAmo()
    bridge = FinanceLink(SimpleNamespace())
    bridge._amo = lambda: amo
    assert bridge.add_schedule("2026-11-16", "和柳如烟逛街", place="商场",
                               category="social", start="14:00", end="17:00")
    assert bridge.add_schedule("2026-11-16", "和柳如烟逛街", place="商场",
                               category="social", start="14:00", end="17:00")
    assert amo.state.calls == 1
    assert len(amo.state.schedule["2026-11-16"]) == 1


def test_different_window_is_kept():
    amo = FakeAmo()
    bridge = FinanceLink(SimpleNamespace())
    bridge._amo = lambda: amo
    bridge.add_schedule("2026-11-16", "和柳如烟逛街", place="商场",
                        category="social", start="14:00", end="17:00")
    bridge.add_schedule("2026-11-16", "和柳如烟吃饭", place="楼下小馆",
                        category="social", start="18:00", end="20:00")
    assert amo.state.calls == 2
