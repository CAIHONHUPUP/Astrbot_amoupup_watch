# -*- coding: utf-8 -*-
"""工资：基本月薪持久化、奖金扣款明细、实发、调薪与省钱模式。"""
import asyncio
import datetime as dt

from services.work_engine import BASE_MAX_CENTS, DEFAULT_BASE_CENTS, FinanceSalary, WORK_DAYS


class FakeRepo:
    def __init__(self):
        self.meta = {}
        self.items = []
        self.settled_keys = set()

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def add_salary_item(self, period, kind, label, amount_cents):
        self.items.append({"period": period, "kind": kind, "label": label,
                           "amount_cents": int(amount_cents)})
        return True

    def salary_items(self, period):
        return [i for i in self.items if i["period"] == str(period)]

    def settled(self, period, kind):
        return (str(period), str(kind)) in self.settled_keys

    def mark_settled(self, period, kind, payload="{}"):
        self.settled_keys.add((str(period), str(kind)))
        return True


class FakeFinance:
    def __init__(self):
        self.incomes = []
        self.memories = []
        self.diaries = []
        self.moods = []

    def balance_cents(self):
        return 0

    async def income(self, cents, reason, idem, salary=False):
        self.incomes.append((int(cents), reason, salary))
        return {"ok": True}

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True


def make():
    repo = FakeRepo()
    fin = FakeFinance()
    return repo, fin, FinanceSalary(None, repo, fin)


def test_default_base_and_persistence():
    repo, fin, sal = make()
    assert sal.base_cents() == DEFAULT_BASE_CENTS
    sal.set_base(880000)
    again = FinanceSalary(None, repo, fin)   # 模拟重启
    assert again.base_cents() == 880000
    assert repo.meta_get("salary_cents") == "880000"   # 老字段同步


def test_set_base_is_clamped():
    repo, fin, sal = make()
    assert sal.set_base(1) == 300000
    assert sal.set_base(999999999) == BASE_MAX_CENTS


def test_day_rate_is_base_over_work_days():
    repo, fin, sal = make()
    sal.set_base(870000)          # 8700 元
    assert sal.day_rate_cents() == int(round(870000 / WORK_DAYS))


def test_summary_adds_bonus_and_subtracts_deduction():
    repo, fin, sal = make()
    sal.set_base(680000)
    sal.add_item("2026-11", "deduction", "迟到/早退", -5000)
    sal.add_item("2026-11", "bonus", "全勤奖", 10000)
    summary = sal.summary("2026-11")
    assert summary["base_cents"] == 680000
    assert summary["bonus_cents"] == 10000
    assert summary["deduction_cents"] == 5000
    assert summary["net_cents"] == 685000
    assert [i["label"] for i in summary["items"]] == ["迟到/早退", "全勤奖"]


def test_settle_pays_net_and_writes_memory_and_diary():
    repo, fin, sal = make()
    sal.set_base(680000)
    sal.add_item("2026-11", "bonus", "全勤奖", 30000)
    sal.add_item("2026-11", "deduction", "迟到/早退", 5000)
    result = asyncio.run(sal.settle(dt.date(2026, 11, 15), "2026-11"))
    assert result["paid"] == 680000 + 30000 - 5000
    assert fin.incomes == [(705000, "工资", True)]
    assert fin.memories and fin.diaries
    assert repo.settled("2026-11", "salary")


def test_settle_zero_net_writes_white_work_month():
    repo, fin, sal = make()
    sal.set_base(300000)
    sal.add_item("2026-11", "deduction", "绩效不达标(100%)", -300000)
    result = asyncio.run(sal.settle(dt.date(2026, 11, 15), "2026-11"))
    assert result["zero"] is True and result["paid"] == 0
    assert fin.incomes == []
    assert "白干" in fin.diaries[-1]
    assert repo.settled("2026-11", "salary")


def test_deduction_triggers_thrifty_and_sad_mood():
    repo, fin, sal = make()
    sal.add_item("2026-11", "deduction", "事假一天", -30000)
    asyncio.run(sal.settle(dt.date(2026, 11, 15), "2026-11"))
    assert sal.thrifty_active(dt.date(2026, 11, 17)) is True
    assert sal.thrifty_active(dt.date(2027, 1, 1)) is False
    assert any("委屈" in d for d in fin.moods)


def test_bonus_triggers_happy_mood_without_thrifty():
    repo, fin, sal = make()
    sal.add_item("2026-11", "bonus", "项目超额完成", 30000)
    asyncio.run(sal.settle(dt.date(2026, 11, 15), "2026-11"))
    assert sal.thrifty_until() == ""
    assert any("开心" in d for d in fin.moods)


def test_raise_and_cut_change_base_and_record():
    repo, fin, sal = make()
    sal.set_base(680000)
    up = asyncio.run(sal.raise_or_cut(0.20, dt.date(2026, 11, 20)))
    assert up["ok"] is True and sal.base_cents() == int(round(680000 * 1.2))
    down = asyncio.run(sal.raise_or_cut(-0.08, dt.date(2026, 11, 25)))
    assert down["up"] is False
    assert sal.base_cents() == int(round(816000 * 0.92))
    assert sal.thrifty_active(dt.date(2026, 11, 26)) is True
    assert len(fin.diaries) == 2 and len(fin.memories) == 2


def test_roll_month_is_idempotent():
    repo, fin, sal = make()
    first = asyncio.run(sal.roll_month(dt.date(2026, 11, 5), "2026-11"))
    second = asyncio.run(sal.roll_month(dt.date(2026, 11, 6), "2026-11"))
    assert second.get("skipped") is True
    assert "rolled" in first
    assert len(repo.items) == len(first["rolled"])
