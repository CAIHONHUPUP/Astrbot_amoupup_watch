# -*- coding: utf-8 -*-
import asyncio, datetime as dt
from services.bankruptcy_engine import BankruptcyEngine

class Repo:
    def __init__(self):
        self.meta, self.pos, self.loan, self.assets = {}, [{"code": "600519", "qty": 100, "cost_cents": 100000}], [{"balance_cents": 900000}], []
    def meta_get(self, k, d=None): return self.meta.get(k, d)
    def meta_set(self, k, v): self.meta[k] = v
    def positions(self): return self.pos
    def loans(self): return self.loan
    def assets_of(self, kind): return []
class Trading:
    def __init__(self): self.sold = []
    async def sell(self, *a, **k): self.sold.append(a); return {"ok": True}
class Fin:
    def __init__(self): self.diaries, self.moods = [], []
    def balance_cents(self): return 10000
    def nudge_mood(self, d, i): self.moods.append(d)
    def long_diary(self, t): self.diaries.append(t)
    async def permanent_memory(self, t, i): pass

def test_bankruptcy_liquidates_when_debt_over_3x_assets():
    repo, fin, tr = Repo(), Fin(), Trading()
    eng = BankruptcyEngine(None, repo, fin, tr)
    out = asyncio.run(eng.check(dt.datetime(2026, 12, 1)))
    assert out["ok"] and tr.sold and any(w in fin.diaries[0] for w in ("老公", "亲爱的"))
    assert repo.meta_get("bankruptcy:2026-12") == "liquidated"

def test_bankruptcy_skips_when_healthy():
    repo, fin, tr = Repo(), Fin(), Trading()
    repo.loan = [{"balance_cents": 100}]
    assert asyncio.run(BankruptcyEngine(None, repo, fin, tr).check(dt.datetime(2026, 12, 1)))["ok"] is False
