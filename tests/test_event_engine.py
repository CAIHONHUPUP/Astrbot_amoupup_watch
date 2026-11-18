# -*- coding: utf-8 -*-
import asyncio, datetime as dt
from services.event_engine import EventEngine

try:  # Phase 15：meta 桩用共享实现，任务表留在本文件
    from tests.fixtures import FakeRepo  # noqa: E402
except ImportError:
    from fixtures import FakeRepo  # noqa: E402


class Repo(FakeRepo):
    """meta 走共享桩；任务表是本用例自己的。"""

    def __init__(self):
        super().__init__()
        self.tasks = {}

    def task_done(self, k):
        return k in self.tasks

    def mark_task(self, k, n=""):
        self.tasks[k] = 1
class Fin:
    def __init__(self, cash=500000): self.cash, self.diaries, self.moods = cash, [], []
    def balance_cents(self): return self.cash
    async def expense(self, c, r, i, necessary=False):
        if c > self.cash: return {"ok": False}
        self.cash -= c; return {"ok": True}
    def nudge_mood(self, d, i): self.moods.append(d)
    def small_diary(self, t): self.diaries.append(t)
    def long_diary(self, t): self.diaries.append(t)
    async def permanent_memory(self, t, i): pass
    def add_schedule(self, *a, **k): return True
class Med:
    def __init__(self, sick=False): self.sick = sick
    def is_sick(self, *a): return self.sick
class P: config = {}

def test_parents_visit_weekend_and_deducts():
    repo, fin = Repo(), Fin()
    out = asyncio.run(EventEngine(P(), repo, fin).visit_parents(dt.date(2026, 11, 14)))
    assert out["ok"] and fin.cash < 500000 and "回家" in fin.diaries[-1]

def test_trip_needs_sponsor_when_short():
    repo, fin = Repo(), Fin(100000)
    out = asyncio.run(EventEngine(P(), repo, fin).trip("三亚", dt.date(2026, 10, 1)))
    assert out["ok"] is False and out["reason"] == "need_sponsor"

def test_sick_blocks_outing():
    repo, fin = Repo(), Fin()
    out = asyncio.run(EventEngine(P(), repo, fin, medical=Med(True)).visit_parents(dt.date(2026, 11, 14)))
    assert out["ok"] is False
