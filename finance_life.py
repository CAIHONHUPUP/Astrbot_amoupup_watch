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

    def total_assets_cents(self):
        cash = self.finance.balance_cents() or 0
        market = 0
        for row in self.repo.positions():
            market += int(row.get("last_cents") or 0) * float(row.get("qty") or 0)
        debt = sum(int(x.get("balance_cents") or 0) for x in self.repo.loans())
        return int(cash + market - debt)

    def ensure_seed(self):
        """首次启动：写入初始存款（默认 23000 元），只做一次。"""
        if self.repo.meta_get("seeded") == "1":
            return False
        try:
            initial = int(getattr(self.plugin, "config", {}).get("initial_deposit_cents", 2300000) or 2300000)
        except Exception:
            initial = 2300000
        if initial > 0:
            self.repo.meta_set("initial_deposit_cents", initial)
        self.repo.meta_set("seeded", "1")
        try:
            return bool(self.finance.credit_initial(initial))
        except Exception:
            return False

    def _half_key(self, now):
        return "%d-H%d" % (now.year, 1 if now.month <= 6 else 2)

    def _roll_half(self, now):
        key = self._half_key(now)
        if self.repo.meta_get("half_key") == key:
            return
        self.repo.meta_set("half_key", key)
        self.repo.meta_set("half_start_assets", self.total_assets_cents())
        self.repo.meta_set("half_income_cents", 0)
        self.repo.meta_set("half_expense_cents", 0)

    def _raise_salary(self, now):
        """1/7 月调薪：资产变化 60% + 收支比 40%，落在 ±5%–15%。"""
        if now.month not in (1, 7):
            return None
        half = self._half_key(now)
        if self.repo.meta_get("salary_raised") == half:
            return None
        old = self.salary_cents()
        start = float(self.repo.meta_get("half_start_assets", 0) or 0)
        now_assets = float(self.total_assets_cents())
        income = float(self.repo.meta_get("half_income_cents", 0) or 0)
        expense = float(self.repo.meta_get("half_expense_cents", 0) or 0)
        change = (now_assets - start) / start if start > 0 else 0.0
        change = max(-0.3, min(0.3, change))
        ratio = income / expense if expense > 0 else 1.0
        ratio_score = max(-1.0, min(1.0, ratio - 1.0))
        score = 0.6 * (change / 0.3) + 0.4 * ratio_score
        pct = 0.05 + 0.10 * ((score + 1) / 2.0)
        pct = max(0.05, min(0.15, pct))
        new = max(MIN_SALARY_CENTS, min(MAX_SALARY_CENTS, int(round(old * (1 + pct)))))
        self.repo.meta_set("salary_cents", new)
        self.repo.meta_set("salary_raised", half)
        return {"from": old, "to": new, "pct": pct}

    # ---------- 结算 ----------
    async def tick(self, now=None):
        now = now or _dt.datetime.now()
        self.ensure_seed()
        self._roll_half(now)
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
        """水 / 电 / 燃气 / 宽带 逐项扣；任何一项失败就停在那项，先预警再进 5% 流程。"""
        if now.day < 1:
            return "skip"
        summer = now.month in (6, 7, 8, 9)
        plan = (
            ("water", "水费", _hash_int("water:" + period, 1100, 2500)),
            ("electric", "电费", _hash_int("electric:" + period, 15000, 22000) if summer
             else _hash_int("electric:" + period, 6000, 13000)),
            ("gas", "燃气费", _hash_int("gas:" + period, 3000, 6000)),
            ("broadband", "宽带", _hash_int("broadband:" + period, 5000, 8000)),
        )
        for key, name, amount in plan:
            if self.repo.settled(period, key):
                continue
            result = await self.finance.expense(amount, name, "%s:%s" % (key, period), necessary=True)
            if not result.get("ok"):
                try:
                    await self.finance.remember_event("欠费", amount, "%s 扣款失败，先欠着，%s" % (
                        name, (await self.finance.memory.context(limit=1)) or ""), "arrears:%s:%s" % (key, period))
                except Exception:
                    pass
                return "fail"
            self.repo.mark_settled(period, key, str(amount))
        return "ok"
