# -*- coding: utf-8 -*-
"""账本事实：只写客观数据，不写情绪模板；发薪/逛街时间受工作时间约束。"""

import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils import facts  # noqa: E402
from services.shopping_engine import tick as shopping_tick  # noqa: E402
from services.work_engine import FinanceSalary  # noqa: E402


EMOTION_WORDS = ("开心", "狂喜", "委屈", "难过", "郁闷", "美滋滋", "心情", "幸福", "崩溃")


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
        self.diaries = []

    def balance_cents(self):
        return 2591814

    async def income(self, cents, reason, idem, salary=False, detail=None):
        self.incomes.append((int(cents), reason, salary, detail))
        return {"ok": True}

    def small_diary(self, text):
        self.diaries.append(text)
        return True

    def nudge_mood(self, deltas, idem):
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True


def test_expense_fact_has_no_emotion():
    text = facts.ledger_fact("支出", 2000, 2581619, item="餐饮/一碗热汤面")
    assert "支出 20.00 元" in text and "余额 25,816.19 元" in text
    assert "餐饮/一碗热汤面" in text
    for word in EMOTION_WORDS:
        assert word not in text


def test_income_fact_has_no_emotion():
    text = facts.ledger_fact("收入", 5000, 1870550, item="用户转账")
    assert "收入 50.00 元" in text and "余额 18,705.50 元" in text
    for word in EMOTION_WORDS:
        assert word not in text


def test_salary_fact_is_one_merged_record():
    text = facts.salary_fact(680000, 82528, 41264, 721264, 2591814)
    assert text.count("发工资") == 1
    for token in ("基本 6,800.00 元", "奖金 825.28 元", "扣款 412.64 元",
                  "实发 7,212.64 元", "余额 25,918.14 元"):
        assert token in text
    for word in EMOTION_WORDS:
        assert word not in text


def test_salary_pay_day_before_spring_festival():
    from services import bill_flow
    # 2026-02-15 周日，16-20 春节、21-22 周末；顺延到 02-23 周一
    assert bill_flow.salary_pay_day(dt.date(2026, 2, 15)) == dt.date(2026, 2, 23)


def test_salary_late_catchup_does_not_write_early_fact():
    repo, fin = FakeRepo(), FakeFinance()
    sal = FinanceSalary(None, repo, fin)
    # 发薪日 11-13 周五，周一 11-16 才补发：仍发，但不能写“明天是周末”的假事实
    out = asyncio.run(sal.settle(dt.datetime(2026, 11, 16, 10, 0), "2026-11"))
    assert out["ok"] is True
    assert repo.meta_get("salary_early_until") is None
    assert not any("提前" in text for text in fin.diaries)


def test_salary_settle_before_ten_is_refused():
    repo, fin = FakeRepo(), FakeFinance()
    sal = FinanceSalary(None, repo, fin)
    out = asyncio.run(sal.settle(dt.datetime(2026, 11, 13, 9, 0), "2026-11"))
    assert out["ok"] is False and out["reason"] == "not_pay_time"
    assert fin.incomes == [] and repo.settled("2026-11", "salary") is False


def test_salary_settle_on_weekend_is_refused():
    repo, fin = FakeRepo(), FakeFinance()
    sal = FinanceSalary(None, repo, fin)
    out = asyncio.run(sal.settle(dt.datetime(2026, 11, 15, 10, 0), "2026-11"))
    assert out["ok"] is False and out["reason"] == "not_pay_time"
    assert fin.incomes == [] and repo.settled("2026-11", "salary") is False


def test_salary_settle_at_ten_merges_detail():
    repo, fin = FakeRepo(), FakeFinance()
    sal = FinanceSalary(None, repo, fin)
    sal.set_base(680000)
    sal.add_item("2026-11", "bonus", "全勤奖", 30000)
    sal.add_item("2026-11", "deduction", "迟到/早退", 5000)
    # 15 号是周日，顺延到 16 号周一 10:00；不再有“提前发”那套
    out = asyncio.run(sal.settle(dt.datetime(2026, 11, 16, 10, 0), "2026-11"))
    assert out["paid"] == 705000
    cents, reason, salary, detail = fin.incomes[0]
    assert salary is True and isinstance(detail, dict)
    assert detail["base_cents"] == 680000 and detail["bonus_cents"] == 30000
    assert fin.diaries and "发工资" in fin.diaries[-1]
    assert "狂喜" not in fin.diaries[-1]
    assert repo.meta_get("salary_early_until") is None
    assert not any("提前" in text and "工资" in text for text in fin.diaries)


def test_shopping_never_runs_at_night():
    assert asyncio.run(shopping_tick(object(), now=dt.datetime(2026, 11, 15, 0, 0))) is None
    assert asyncio.run(shopping_tick(object(), now=dt.datetime(2026, 11, 15, 3, 30))) is None


def test_ledger_balance_is_continuous():
    """同一串流水的结余必须前后衔接。"""
    balance = 2591814
    lines = []
    for amount, item in ((3000, "逛街交通"), (2000, "奶茶"), (6000, "逛街午饭")):
        balance -= amount
        lines.append(facts.ledger_fact("支出", amount, balance, item=item))
    assert "余额 25,888.14 元" in lines[0]
    assert "余额 25,868.14 元" in lines[1]
    assert "余额 25,808.14 元" in lines[2]
