# -*- coding: utf-8 -*-
"""职场事件：数值规则、五步联动、全勤判定、每日概率、prompt 注入文本。"""
import asyncio
import datetime as dt

from services.work_events import FULL_BONUS_CENTS, WorkEventEngine


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)


class FakeFinance:
    def __init__(self):
        self.schedules = []
        self.memories = []
        self.diaries = []
        self.moods = []

    def add_schedule(self, day, activity, detail="", place="家", category="work"):
        self.schedules.append({"day": day, "activity": activity, "detail": detail,
                               "category": category})
        return True

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def small_diary(self, text):
        self.diaries.append(text)
        return True

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True


class FakeSalary:
    def __init__(self, base_cents=680000):
        self.base = base_cents
        self.items_rows = []

    def base_cents(self):
        return self.base

    def add_item(self, period, kind, label, amount_cents):
        self.items_rows.append({"period": period, "kind": kind, "label": label,
                                "amount_cents": int(amount_cents)})
        return True

    def items(self, period):
        return [i for i in self.items_rows if i["period"] == period]

    def summary(self, period):
        return {"net_cents": self.base + sum(i["amount_cents"] for i in self.items(period))}


class FakePlugin:
    finance_expense = None


def make():
    repo = FakeRepo()
    fin = FakeFinance()
    sal = FakeSalary()
    eng = WorkEventEngine(FakePlugin(), repo, fin, sal)
    return repo, fin, sal, eng


def test_late_deducts_50_and_runs_pipeline():
    repo, fin, sal, eng = make()
    result = asyncio.run(eng.fire("late", day=dt.date(2026, 11, 13)))
    assert result["ok"] and result["amount_cents"] == -5000
    assert result["card"] == "[迟到/早退] -¥50.00"
    assert sal.items_rows[-1]["amount_cents"] == -5000
    assert fin.schedules[0]["activity"].startswith("迟到/早退")
    assert "迟到" in fin.memories[0] and "50.00" in fin.memories[0]
    assert "打工好难" in fin.diaries[0]
    assert fin.moods and "郁闷" in fin.moods[0]
    assert eng.today_events("2026-11-13")[0]["kind"] == "late"
    assert "今日职场" in eng.prompt_line("2026-11-13")


def test_project_bonus_in_range_and_happy():
    repo, fin, sal, eng = make()
    result = asyncio.run(eng.fire("project", day=dt.date(2026, 11, 13)))
    assert 50000 <= result["amount_cents"] <= 100000
    assert "奖金" in result["diary"]
    assert any("开心" in d for d in fin.moods)


def test_overtime_pays_double_day_rate():
    repo, fin, sal, eng = make()
    rate = int(round(680000 / 21.75))
    result = asyncio.run(eng.fire("overtime", day=dt.date(2026, 11, 14)))
    assert result["amount_cents"] == rate * 2


def test_sick_deducts_one_day_rate():
    repo, fin, sal, eng = make()
    rate = int(round(680000 / 21.75))
    result = asyncio.run(eng.fire("sick", day=dt.date(2026, 11, 13)))
    assert result["amount_cents"] == -rate


def test_full_attendance_only_without_absence():
    repo, fin, sal, eng = make()
    asyncio.run(eng.fire("late", day=dt.date(2026, 11, 3)))
    assert asyncio.run(eng.grant_full_attendance("2026-11")) is None   # 迟到过，不给
    repo2, fin2, sal2, eng2 = make()
    got = asyncio.run(eng2.grant_full_attendance("2026-11", day=dt.date(2026, 11, 15)))
    assert got and got["amount_cents"] == FULL_BONUS_CENTS
    # 只给一次
    assert asyncio.run(eng2.grant_full_attendance("2026-11")) is None


def test_work_notes_are_labelled_for_daily_diary():
    repo, fin, sal, eng = make()
    asyncio.run(eng.fire("late", day=dt.date(2026, 11, 13)))
    assert fin.diaries[0].startswith("【职场】")          # 小时小日记带标记
    assert "打工好难" in fin.diaries[0]
    assert "【职场】" in fin.schedules[0]["activity"] + fin.schedules[0].get("detail", "")


def test_daily_recap_writes_once_with_totals():
    repo, fin, sal, eng = make()
    asyncio.run(eng.fire("late", day=dt.date(2026, 11, 13)))
    asyncio.run(eng.fire("overtime", day=dt.date(2026, 11, 13)))
    fin.memories.clear()
    first = asyncio.run(eng.daily_recap(dt.datetime(2026, 11, 13, 21, 30)))
    assert first and "+" in first["text"]
    assert any(d.startswith("【职场小结】") for d in fin.diaries)
    second = asyncio.run(eng.daily_recap(dt.datetime(2026, 11, 13, 22, 0)))
    assert second is None                                 # 一天只写一次
    # 22 点前不写（避免白天就把总结写了）
    repo2, fin2, sal2, eng2 = make()
    asyncio.run(eng2.fire("late", day=dt.date(2026, 11, 13)))
    assert asyncio.run(eng2.daily_recap(dt.datetime(2026, 11, 13, 15, 0))) is None


def test_daily_random_is_once_per_day_and_skips_weekend():
    repo, fin, sal, eng = make()
    saturday = dt.datetime(2026, 11, 14, 8, 0)
    assert asyncio.run(eng.daily_random(saturday)) is None
    assert repo.meta_get("work_random:2026-11-14") == "off"
    monday = dt.datetime(2026, 11, 16, 8, 0)
    first = asyncio.run(eng.daily_random(monday))
    second = asyncio.run(eng.daily_random(monday))
    assert second is None
    assert repo.meta_get("work_random:2026-11-16") == "1"
