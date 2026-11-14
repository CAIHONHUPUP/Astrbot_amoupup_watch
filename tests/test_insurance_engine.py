# -*- coding: utf-8 -*-
import asyncio
from services.insurance_engine import InsuranceEngine

class Repo:
    def __init__(self): self.meta = {}
    def meta_get(self, k, d=None): return self.meta.get(k, d)
    def meta_set(self, k, v): self.meta[k] = v
class Fin:
    def __init__(self, c=500000): self.c, self.moods, self.diaries = c, [], []
    def balance_cents(self): return self.c
    async def expense(self, n, r, i, necessary=False):
        if n > self.c: return {"ok": False}
        self.c -= n; return {"ok": True}
    async def income(self, n, r, i): self.c += n; return {"ok": True}
    def nudge_mood(self, d, i): self.moods.append(d)
    def small_diary(self, t): self.diaries.append(t)
class P: config = {}

def test_buy_and_claim_medical():
    repo, fin = Repo(), Fin()
    eng = InsuranceEngine(P(), repo, fin)
    out = asyncio.run(eng.buy("e生保"))
    assert out["ok"] and eng.active("e生保")
    extra, payout, note = eng.claim("mid", 2000000)
    assert extra == 1000000 and "e生保" in note

def test_critical_payout_and_lapse():
    repo, fin = Repo(), Fin()
    eng = InsuranceEngine(P(), repo, fin)
    asyncio.run(eng.buy("重疾"))
    extra, payout, note = eng.claim("heavy", 500000)
    assert payout == 10000000
    repo2, fin2 = Repo(), Fin(1000)
    eng2 = InsuranceEngine(P(), repo2, fin2)
    out = asyncio.run(eng2.buy("重疾"))
    assert out["ok"] is False and out.get("lapsed")
