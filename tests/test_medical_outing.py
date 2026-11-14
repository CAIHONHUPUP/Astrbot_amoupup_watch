# -*- coding: utf-8 -*-
import asyncio, datetime as dt
from services.medical_engine import MedicalEngine
from services.event_engine import EventEngine

class Repo:
    def __init__(self): self.meta, self.tasks = {}, {}
    def meta_get(self, k, d=None): return self.meta.get(k, d)
    def meta_set(self, k, v): self.meta[k] = v
    def task_done(self, k): return k in self.tasks
    def mark_task(self, k, n=""): self.tasks[k] = 1
class Fin:
    def __init__(self, cash=500000): self.cash, self.diaries = cash, []
    def balance_cents(self): return self.cash
    async def expense(self, c, r, i, necessary=False):
        if c > self.cash: return {"ok": False}
        self.cash -= c; return {"ok": True}
    async def permanent_memory(self, *a, **k): return True
    def small_diary(self, t): self.diaries.append(t)
    def long_diary(self, t): self.diaries.append(t)
    def add_schedule(self, *a, **k): return True
    def nudge_mood(self, *a, **k): return True
class Work:
    def __init__(self): self.fired = []
    def daily_rate_cents(self): return 30000
    async def fire(self, kind, **kw): self.fired.append(kind); return {"ok": True}
class Med:
    def __init__(self, sick=False, bed=""): self.sick, self.bed = sick, bed
    def is_sick(self, *a): return self.sick
    def allow_medical_outing(self, *a): return self.sick
    def deny_entertainment_outing(self, *a): return self.sick or bool(self.bed)
class P: config = {}

def test_sick_allows_hospital_but_denies_fun():
    repo, fin = Repo(), Fin()
    med = MedicalEngine(P(), repo, fin)
    eng = EventEngine(P(), repo, fin, medical=med)
    repo.meta_set("is_sick_until", dt.date.today().isoformat())
    assert eng.allow_medical_outing() is True
    assert eng.deny_entertainment_outing() is True
    out = asyncio.run(eng.mall_with_bff(dt.date(2026, 11, 14)))
    assert out["ok"] is False
    hosp = asyncio.run(med.hospital_outing("light", "感冒", dt.date(2026, 11, 16)))
    assert hosp["ok"] and fin.cash < 500000
    assert repo.meta_get("bed_rest_until")

def test_heavy_hospital_asks_for_help_when_broke():
    repo, fin = Repo(), Fin(1000)
    med = MedicalEngine(P(), repo, fin)
    out = asyncio.run(med.hospital_outing("heavy", "肺炎", dt.date(2026, 11, 16)))
    assert out["ok"] is False and out["need_help"] is True
