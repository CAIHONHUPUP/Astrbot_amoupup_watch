# -*- coding: utf-8 -*-
"""生活结算：每月 15 日工资、每月 1 日房租水电；半年调薪。

幂等：按期（YYYY-MM）+ 项目写 settlements，同一周期只结算一次。
钱全部走 FinanceLink → amo.wallet；本模块只决定「该扣多少、该发多少」。
"""
from __future__ import annotations

import datetime as _dt
import hashlib

DEFAULT_SALARY_CENTS = 680000          # 6800 元
MIN_SALARY_CENTS, MAX_SALARY_CENTS = 300000, 2000000
RENT_RANGE = (80000, 250000)           # 800–2500 元
UTILITY_RANGE = (10000, 80000)         # 100–800 元


def _hash_int(text, low, high):
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    span = max(1, high - low)
    return low + int(digest[:8], 16) % span


class FinanceLife:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    # ---------- 参数 ----------
    def salary_cents(self):
        try:
            value = int(self.repo.meta_get("salary_cents", DEFAULT_SALARY_CENTS) or DEFAULT_SALARY_CENTS)
        except Exception:
            value = DEFAULT_SALARY_CENTS
        return max(MIN_SALARY_CENTS, min(MAX_SALARY_CENTS, value))

    def _has(self, kind):
        try:
            return any(str(item.get("kind")) == kind for item in self.repo.assets())
        except Exception:
            return False

    def rent_cents(self, period):
        return _hash_int("rent:" + str(period), *RENT_RANGE)

    def utility_cents(self, period):
        return _hash_int("util:" + str(period), *UTILITY_RANGE)

    def _raise_salary(self, now):
        """1 月和 7 月调薪：按周期确定性 ±5%–15%，写回 meta。"""
        half = "%d-H%d" % (now.year, 1 if now.month <= 6 else 2)
        if now.month not in (1, 7):
            return None
        if self.repo.meta_get("salary_raised") == half:
            return None
        old = self.salary_cents()
        pct = _hash_int("raise:" + half, 5, 16) / 100.0
        sign = 1 if _hash_int("raise-sign:" + half, 0, 2) == 1 else -1
        new = max(MIN_SALARY_CENTS, min(MAX_SALARY_CENTS, int(round(old * (1 + sign * pct)))))
        self.repo.meta_set("salary_cents", new)
        self.repo.meta_set("salary_raised", half)
        return {"from": old, "to": new}

    # ---------- 结算 ----------
    async def tick(self, now=None):
        now = now or _dt.datetime.now()
        period = now.strftime("%Y-%m")
        steps = {}
        steps["rent"] = await self._rent(now, period)
        steps["utilities"] = await self._utilities(now, period)
        steps["salary"] = await self._salary(now, period)
        return steps

    async def _salary(self, now, period):
        if now.day < 15 or self.repo.settled(period, "salary"):
            return "skip"
        raised = self._raise_salary(now)
        amount = self.salary_cents()
        result = await self.finance.income(amount, "工资", "salary:" + period, salary=True)
        if not result.get("ok"):
            return "fail"
        self.repo.mark_settled(period, "salary", str({"amount": amount, "raised": raised}))
        return "ok"

    async def _rent(self, now, period):
        if now.day < 1 or self.repo.settled(period, "rent"):
            return "skip"
        if self._has("house"):
            self.repo.mark_settled(period, "rent", "has_house")
            return "has_house"
        amount = self.rent_cents(period)
        result = await self.finance.expense(amount, "房租", "rent:" + period, necessary=True)
        if not result.get("ok"):
            return "fail"
        self.repo.mark_settled(period, "rent", str(amount))
        return "ok"

    async def _utilities(self, now, period):
        if now.day < 1 or self.repo.settled(period, "utilities"):
            return "skip"
        amount = self.utility_cents(period)
        result = await self.finance.expense(amount, "水电燃气", "utilities:" + period, necessary=True)
        if not result.get("ok"):
            return "fail"
        self.repo.mark_settled(period, "utilities", str(amount))
        return "ok"
