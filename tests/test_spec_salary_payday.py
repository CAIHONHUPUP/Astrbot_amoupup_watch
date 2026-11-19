# -*- coding: utf-8 -*-
"""产品级行为规格（弱点4）· 组 3：工资遇周末/节假日顺延、同月只发一次。

「给定 X → 必须 Y」：

1. 给定发薪日 15 日落在周六/周日或法定节假日 → 必须在下一个工作日发，
   15 日之前绝不发；非工作日绝不结算；
2. 给定同一个月 → 只发一次；跨月 → 每月各发一次，不补发两次；
3. 给定工作日 10 点前 → 不结算。

实现事实（2026-11-19 grep 后确认）：services/bill_flow.py::salary_pay_day
是「15 号遇非工作日顺延到下一个工作日」，不是「提前到前一个工作日」；
test_finance_facts.py::test_salary_pay_day_before_spring_festival 等现有用例
也是按顺延写死的。任务书里「现有实现是提前」与代码不符，本文件按真实契约写，
并用「15 日之前不发」的正向断言把「提前发」这种回归挡住。

时间：settle 的 now 由参数显式传入；本文件另外显式 freeze(2026-11-18 15:00)
+ finally unfreeze，防止 test_finance_facts 那类墙钟耦合。
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

import services.bill_flow as bf
from services.work_engine import FinanceSalary
from fixtures import FakeFinance, FakePlugin


@pytest.fixture(autouse=True)
def _spec_clock():
    from datetime import datetime as _dt
    from utils.clock import freeze, unfreeze

    freeze(_dt(2026, 11, 18, 15, 0))
    try:
        yield
    finally:
        unfreeze()


class _Repo:
    """工资仓储最小桩：meta / salary_items / settlements。"""

    def __init__(self):
        self.meta = {}
        self.items = []
        self.settled_keys = set()

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def add_salary_item(self, period, kind, label, amount_cents):
        self.items.append({"period": period, "kind": kind,
                           "label": label, "amount_cents": int(amount_cents)})
        return True

    def salary_items(self, period):
        return [row for row in self.items if row["period"] == str(period)]

    def settled(self, period, kind):
        return (str(period), str(kind)) in self.settled_keys

    def mark_settled(self, period, kind, payload="{}"):
        self.settled_keys.add((str(period), str(kind)))
        return True


def _scene(holidays=""):
    repo = _Repo()
    finance = FakeFinance(cash=0)
    config = {"bill_holidays": holidays} if holidays else {}
    plugin = FakePlugin(cash=0, config=config, finance=finance, finance_repo=repo)
    sal = FinanceSalary(plugin, repo, finance)
    sal.set_base(680000)            # 6800 元/月，明细干净时实发 = 680000
    return repo, finance, sal, plugin


def test_spec_salary_weekend_payday_defers_to_monday():
    """给定 2026-11-15 是周日 → 11-16 周一才发；13/14/15 都不发。"""
    assert bf.salary_pay_day(dt.date(2026, 11, 14)) == dt.date(2026, 11, 16)
    repo, finance, sal, _ = _scene()

    for moment in (dt.datetime(2026, 11, 13, 10, 0),    # 周五：15 号之前，绝不提前发
                   dt.datetime(2026, 11, 14, 10, 0),    # 周六
                   dt.datetime(2026, 11, 15, 10, 0)):   # 周日（发薪日）
        out = asyncio.run(sal.settle(moment, "2026-11"))
        assert out["ok"] is False and out["reason"] == "not_pay_time"
    assert finance.incomes == []
    assert not repo.settled("2026-11", "salary")

    out = asyncio.run(sal.settle(dt.datetime(2026, 11, 16, 10, 0), "2026-11"))
    assert out["ok"] is True
    assert len(finance.incomes) == 1


def test_spec_salary_holiday_payday_defers_to_next_workday():
    """给定 15 日是法定节假日（配置 2026-04-15）→ 04-16 才发。"""
    repo, finance, sal, plugin = _scene(holidays="2026-04-15")
    holiday_set = bf.holidays(plugin)
    assert bf.salary_pay_day(dt.date(2026, 4, 15), holiday_set) == dt.date(2026, 4, 16)

    out = asyncio.run(sal.settle(dt.datetime(2026, 4, 15, 10, 0), "2026-04"))
    assert out["ok"] is False and out["reason"] == "not_pay_time"
    assert finance.incomes == []

    out = asyncio.run(sal.settle(dt.datetime(2026, 4, 16, 10, 0), "2026-04"))
    assert out["ok"] is True
    assert len(finance.incomes) == 1


def test_spec_salary_workday_15th_pays_on_the_15th():
    """给定 15 日就是工作日（2026-04-15 周三）→ 当天发。"""
    assert bf.salary_pay_day(dt.date(2026, 4, 15)) == dt.date(2026, 4, 15)
    repo, finance, sal, _ = _scene()
    out = asyncio.run(sal.settle(dt.datetime(2026, 4, 15, 10, 0), "2026-04"))
    assert out["ok"] is True and len(finance.incomes) == 1


def test_spec_salary_never_pays_before_ten():
    """给定工作日 10 点前 → 不结算；发薪日凌晨那一跳不能记成半夜到账。"""
    repo, finance, sal, _ = _scene()
    for moment in (dt.datetime(2026, 11, 16, 0, 0),
                   dt.datetime(2026, 11, 16, 9, 59),
                   dt.datetime(2026, 11, 18, 9, 0)):
        out = asyncio.run(sal.settle(moment, "2026-11"))
        assert out["ok"] is False and out["reason"] == "not_pay_time"
    assert finance.incomes == []
    assert not repo.settled("2026-11", "salary")


def test_spec_salary_same_month_pays_only_once():
    """给定同一个月 → 只发一次（重复 tick / 隔天补 tick 都不再发）。"""
    repo, finance, sal, _ = _scene()
    first = asyncio.run(sal.settle(dt.datetime(2026, 11, 16, 10, 0), "2026-11"))
    assert first["ok"] is True
    paid = first["paid"]
    cash = finance.balance_cents()
    assert cash == paid

    for moment in (dt.datetime(2026, 11, 16, 10, 30),
                   dt.datetime(2026, 11, 17, 10, 0),
                   dt.datetime(2026, 11, 20, 9, 0),
                   dt.datetime(2026, 11, 30, 23, 0)):
        again = asyncio.run(sal.settle(moment, "2026-11"))
        assert again["ok"] is False and again["reason"] == "settled"

    assert len(finance.incomes) == 1
    assert finance.incomes[0][2] == "salary:2026-11"
    assert finance.balance_cents() == cash


def test_spec_salary_cross_month_pays_once_per_month():
    """给定跨月 → 每月各发一次；同月重复 tick 不补发第二次。"""
    repo, finance, sal, _ = _scene()
    asyncio.run(sal.settle(dt.datetime(2026, 11, 16, 10, 0), "2026-11"))
    asyncio.run(sal.settle(dt.datetime(2026, 11, 17, 10, 0), "2026-11"))   # 同月补 tick
    asyncio.run(sal.settle(dt.datetime(2026, 12, 15, 10, 0), "2026-12"))   # 跨月再发
    asyncio.run(sal.settle(dt.datetime(2026, 12, 16, 10, 0), "2026-12"))   # 12 月重复 tick

    assert [row[2] for row in finance.incomes] == ["salary:2026-11", "salary:2026-12"]
    assert repo.settled("2026-11", "salary") and repo.settled("2026-12", "salary")
    assert finance.balance_cents() == sum(row[0] for row in finance.incomes)
