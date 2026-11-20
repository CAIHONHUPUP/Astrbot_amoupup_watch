# -*- coding: utf-8 -*-
"""社交引擎测试用的最小假仓库 / 假财务（只实现被调用到的接口）。"""
from __future__ import annotations

import time


class FakeRepo:
    def __init__(self):
        self.npc = {}
        self.recv = []
        self.tasks = {}
        self.meta = {}
        self.log = []

    # NPC
    def npc_get(self, name):
        return self.npc.get(name)

    def npcs(self):
        return list(self.npc.values())

    def upsert_npc(self, name, circle, relation, affinity=50, tags=""):
        self.npc[name] = {"name": name, "circle": circle, "relation": relation,
                          "affinity": affinity, "tags": tags, "borrowed_from_her": 0,
                          "money_owed": 0, "last_event": "", "status": "", "status_at": 0.0}

    def set_affinity(self, name, value):
        row = self.npc.setdefault(name, {"name": name, "circle": "", "relation": "",
                                         "affinity": 50, "tags": "", "borrowed_from_her": 0,
                                         "money_owed": 0, "last_event": "", "status": "",
                                         "status_at": 0.0})
        row["affinity"] = value

    def set_npc_event(self, name, text):
        if name in self.npc:
            self.npc[name]["last_event"] = str(text)[:120]

    # 应收
    def prune_npcs(self, keep):
        allow = set(str(item) for item in (keep or []))
        for name in list(self.npc):
            if name not in allow:
                self.npc.pop(name, None)
        return 0

    def set_npc_status(self, name, status, at=0.0):
        if name in self.npc:
            self.npc[name]["status"] = str(status)[:60]
            self.npc[name]["status_at"] = float(at or 0)
        return True

    def add_receivable(self, npc, amount_cents, due_day=""):
        self.recv.append({"id": len(self.recv) + 1, "npc": npc,
                          "amount_cents": int(amount_cents), "due_day": str(due_day),
                          "status": "open"})

    def receivables(self, status="open"):
        return [row for row in self.recv if row["status"] == status]

    def settle_receivable(self, rid, status):
        for row in self.recv:
            if row["id"] == rid:
                row["status"] = status

    # 幂等 / meta
    def task_done(self, task_id):
        return task_id in self.tasks

    def mark_task(self, task_id, note=""):
        self.tasks[task_id] = note

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value

    # 事件流水
    def event_exists(self, idem):
        return bool(idem) and any(item.get("idem") == idem for item in self.log)

    def add_event(self, kind, text="", npc="", status="", amount_cents=0,
                  day="", extra=None, weight=0.0, idem=""):
        payload = {"text": str(text or ""), "npc": str(npc or ""),
                   "status": str(status or ""), "amount_cents": int(amount_cents or 0)}
        if isinstance(extra, dict):
            payload.update(extra)
        if idem and self.event_exists(idem):
            return False
        self.log.append({"id": len(self.log) + 1, "kind": kind, "day": day,
                         "payload": payload, "weight": weight, "created_at": time.time(),
                         "idem": str(idem or "")})
        return True

    def events(self, limit=10):
        return list(reversed(self.log))[:max(1, int(limit or 10))]

    def event_id_by_idem(self, idem):
        for row in self.log:
            if str(row.get("idem") or "") == str(idem or ""):
                return int(row["id"])
        return 0

    def update_event(self, event_id, payload):
        for row in self.log:
            if int(row["id"]) == int(event_id):
                row["payload"] = dict(payload)
                return True
        return False

    def add_npc_owed(self, name, delta_cents):
        row = self.npc.setdefault(name, {"name": name, "circle": "", "relation": "",
                                         "affinity": 50, "tags": "", "borrowed_from_her": 0,
                                         "money_owed": 0, "last_event": "", "status": "",
                                         "status_at": 0.0})
        row["money_owed"] = int(row.get("money_owed") or 0) + int(delta_cents)
        return True


class FakeFinance:
    def __init__(self, cash=500000, fatigue=42, amo=None):
        self.amo = amo
        self.cash = int(cash)
        self.schedules = []
        self.expenses = []
        self.add_ok = True
        self.fatigue_value = int(fatigue)
        self.expenses = []
        self.incomes = []
        self.moods = []
        self.diaries = []
        self.memories = []
        self.schedules = []
        self.actions = []
        self.windows = {}
        self.held = 0
        self.holds = {}

    def mood(self):
        return {"word": "平静", "score": 50}

    def schedule_windows(self, day):
        return list(self.windows.get(str(day), []))

    def _amo(self):
        return self.amo

    # 日程（社交答应必须真的写进去）
    def schedule_windows(self, day):
        rows = list(self.windows.get(str(day), []))
        rows.extend(row for row in self.schedules if str(row.get("day")) == str(day))
        return rows

    def add_schedule(self, day, title, detail="", place="", category="social",
                     start="", end="", zone=""):
        if not getattr(self, "add_ok", True):
            return False
        self.schedules.append({"day": day, "title": title, "detail": detail,
                               "place": place, "category": category,
                               "start": start, "end": end, "zone": zone,
                               "activity": title})
        return True

    def balance_cents(self):
        return self.cash

    # 冻结（答应饭局到点才扣的那笔）
    def available_cents(self):
        return self.cash - self.held

    def held_cents(self):
        return self.held

    def hold(self, amount_cents, reason, idem, expires_at=0.0):
        amount = int(amount_cents)
        key = str(idem or "")
        if key in self.holds:
            return True
        if amount <= 0 or amount > self.cash - self.held:
            return False
        self.holds[key] = amount
        self.held += amount
        return True

    def release_hold(self, idem):
        amount = self.holds.pop(str(idem or ""), 0)
        if amount:
            self.held -= amount
        return bool(amount)

    def fatigue(self):
        return self.fatigue_value

    async def expense(self, amount_cents, reason, idem, **kwargs):
        amount_cents = int(amount_cents)
        hold_idem = str(kwargs.pop("release_hold", "") or "")
        if hold_idem:
            self.release_hold(hold_idem)
        if amount_cents > self.cash:
            return {"ok": False, "reason": "余额不足"}
        self.cash -= amount_cents
        self.expenses.append({"amount_cents": amount_cents, "reason": reason,
                              "idem": idem, "kwargs": dict(kwargs)})
        return {"ok": True}

    async def income(self, amount_cents, reason, idem, **kwargs):
        self.cash += int(amount_cents)
        self.incomes.append({"amount_cents": int(amount_cents), "reason": reason})
        return {"ok": True}

    def nudge_mood(self, deltas, idem):
        self.moods.append(dict(deltas))
        return True

    def small_diary(self, text):
        self.diaries.append(str(text))
        return True

    async def permanent_memory(self, text, idem):
        self.memories.append(str(text))
        return True

    async def long_memory(self, text, idem):
        self.memories.append(str(text))
        return True

    def add_schedule(self, day, activity, detail="", place="家", category="leave",
                     start="00:00", end="23:59", zone="", status="leave"):
        if not getattr(self, "add_ok", True):
            return False
        self.schedules.append({"day": day, "activity": activity, "detail": detail,
                               "place": place, "category": category, "start": start,
                               "end": end, "zone": zone})
        return True

    def set_current_action(self, action, until_ts=0, place="外面",
                           zone="restaurant", category="social"):
        self.actions.append({"action": action, "until": until_ts, "place": place,
                             "zone": zone, "category": category})
        return True


class FakePlugin:
    config = {}
