# -*- coding: utf-8 -*-
import asyncio, datetime as dt
from services.social_engine import SocialEngine

class Repo:
    def __init__(self): self.npc, self.recv, self.tasks = {}, [], {}
    def npc_get(self, n): return self.npc.get(n)
    def npcs(self): return list(self.npc.values())
    def upsert_npc(self, name, circle, relation, affinity=50, tags=""):
        self.npc[name] = {"name": name, "circle": circle, "relation": relation, "affinity": affinity, "tags": tags,
                          "borrowed_from_her": 0, "money_owed": 0, "last_event": ""}
    def set_affinity(self, n, v): self.npc[n]["affinity"] = v
    def set_npc_event(self, n, t): self.npc[n]["last_event"] = t
    def add_receivable(self, n, a, due=""): self.recv.append({"id": len(self.recv)+1, "npc": n, "amount_cents": a, "due_day": due, "status": "open"})
    def receivables(self, status="open"): return [r for r in self.recv if r["status"] == status]
    def settle_receivable(self, rid, s):
        for r in self.recv:
            if r["id"] == rid: r["status"] = s
    def task_done(self, k): return k in self.tasks
    def mark_task(self, k, note=""): self.tasks[k] = 1

class Fin:
    def __init__(self, cash=500000): self.cash, self.diaries, self.moods, self.mems = cash, [], [], []
    def balance_cents(self): return self.cash
    async def expense(self, c, r, i, **k):
        if c > self.cash: return {"ok": False}
        self.cash -= c; return {"ok": True}
    async def income(self, c, r, i, **k): self.cash += c; return {"ok": True}
    def nudge_mood(self, d, i): self.moods.append(d)
    def small_diary(self, t): self.diaries.append(t)
    async def permanent_memory(self, t, i): self.mems.append(t)
    def add_schedule(self, *a, **k): return True

class P: config = {}

def test_seed_and_gift():
    repo, fin = Repo(), Fin()
    eng = SocialEngine(P(), repo, fin)
    eng.ensure_seed()
    assert len(repo.npcs()) >= 6
    out = asyncio.run(eng.gift_money("方圆"))
    assert out["ok"] and fin.cash < 500000
    assert repo.npc_get("方圆")["affinity"] > 55

def test_poor_escape_dinner():
    repo, fin = Repo(), Fin(10000)
    eng = SocialEngine(P(), repo, fin)
    eng.ensure_seed()
    out = asyncio.run(eng.dinner("柳如烟"))
    assert out["ok"] is False and out["reason"] in ("escape", "insufficient")

def test_borrow_creates_receivable_and_repay_is_idempotent():
    repo, fin = Repo(), Fin()
    eng = SocialEngine(P(), repo, fin)
    eng.ensure_seed()
    out = asyncio.run(eng.borrow_request("柳如烟", 50000, days=0))
    assert out["ok"] and repo.receivables()
    asyncio.run(eng.repay_tick(dt.date(2026, 12, 1)))
    before = len(repo.receivables())
    asyncio.run(eng.repay_tick(dt.date(2026, 12, 1)))
    assert len(repo.receivables()) == before
