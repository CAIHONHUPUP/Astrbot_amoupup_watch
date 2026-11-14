# -*- coding: utf-8 -*-
"""社交借钱：借出记应收；每月 80% 还、20% 赖账。"""
from __future__ import annotations

import datetime as _dt
import json

try:
    from ..guard import isolated
    from ..utils.helpers import stable_int, yuan
except ImportError:
    from guard import isolated
    from utils.helpers import stable_int, yuan

KEY = "receivables"


class LendingEngine:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    def _load(self):
        try:
            return json.loads(self.repo.meta_get(KEY) or "[]")
        except Exception:
            return []

    def _save(self, rows):
        self.repo.meta_set(KEY, json.dumps(rows, ensure_ascii=False))

    async def lend(self, friend, amount_cents, months=6, now=None):
        now = now or _dt.datetime.now()
        if amount_cents <= 0:
            return {"ok": False, "reason": "zero"}
        result = await self.finance.expense(amount_cents, "借给%s" % friend, "lend:%s:%d" % (friend, amount_cents))
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "reason": result.get("reason") if isinstance(result, dict) else "failed"}
        rows = self._load()
        rows.append({"friend": friend, "amount": int(amount_cents), "months": max(1, int(months)),
                     "due": (now + _dt.timedelta(days=30 * max(1, int(months)))).strftime("%Y-%m"),
                     "status": "open", "id": "%s:%d" % (friend, int(now.timestamp()))})
        self._save(rows)
        self.finance.small_diary("【社交】%s 找我借钱，借了 %s，希望记得还……" % (friend, yuan(amount_cents)))
        return {"ok": True, "receivable": rows[-1]}

    async def repay_tick(self, now=None):
        now = now or _dt.datetime.now()
        month = now.strftime("%Y-%m")
        rows = self._load()
        out = []
        for row in rows:
            if row.get("status") != "open" or row.get("due", "") > month:
                continue
            key = "repay:%s:%s" % (row["id"], month)
            if self.repo.meta_get(key):
                continue
            friend, amount = row["friend"], int(row["amount"])
            if stable_int(key, 0, 100) < 80:
                await self.finance.income(amount, "%s还钱" % friend, key)
                row["status"] = "paid"
                self.finance.nudge_mood({"开心": 8}, key + ":mood")
                self.finance.small_diary("【社交】%s 还钱了 %s，还算讲信用。" % (friend, yuan(amount)))
                out.append({"friend": friend, "paid": True, "amount": amount})
            else:
                row["status"] = "defaulted"
                self.finance.nudge_mood({"生气": 12, "委屈": 8}, key + ":mood")
                self.finance.small_diary("【社交】%s 借钱不还还拉黑我！气死了，%s 打水漂了。" % (friend, yuan(amount)))
                try:
                    await self.finance.permanent_memory("%s 借了 %s 不还，拉黑了。" % (friend, yuan(amount)), key)
                except Exception as exc:
                    isolated("lending.memory", exc)
                out.append({"friend": friend, "paid": False, "amount": amount})
            self.repo.meta_set(key, "done")
        self._save(rows)
        return {"ok": True, "results": out}


def build_lending(plugin):
    return LendingEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None))
