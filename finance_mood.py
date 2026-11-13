# -*- coding: utf-8 -*-
"""金额 / 盈亏 → 情绪词 + amo 十维心情增量。

规则来源：docs/INTEGRATION-MAP.md 第 1 节。
- 对外只给自然语言（狂喜 / 很开心 / 平静 / 有点低落 / 难过 / 崩溃），**不出现数值**；
- 数值只作为 amo 内部心情分的增量，写进 amo，不出现在回复、日记、日程里。
"""
from __future__ import annotations

#: amo 十维心情（amo_shared.py MOOD_KEYS）；只写这几个键
POSITIVE = ("开心", "兴奋")
NEGATIVE = ("郁闷", "委屈", "焦虑")

#: 对外情绪词（按分数区间，和 amo HumanState.mood_words 对齐）
def word_for_score(score: float) -> str:
    if score >= 85:
        return "狂喜、超级开心"
    if score >= 70:
        return "很开心、心情不错"
    if score >= 58:
        return "还算轻快"
    if score >= 45:
        return "平静、没什么波动"
    if score >= 35:
        return "有点低落、不太开心"
    if score >= 20:
        return "难过、委屈"
    return "崩溃、伤心，呜呜呜"


def yuan(cents: int) -> str:
    return "¥%s" % format(int(cents) / 100.0, ",.2f")


def _level(word, **deltas):
    return {"word": word, "deltas": {k: float(v) for k, v in deltas.items() if v}}


def income_level(cents: int, salary: bool = False):
    """入账分级：金额越大越开心。"""
    if cents >= 500000:
        return _level("狂喜、开心到转圈", 开心=10, 兴奋=6)
    if cents >= 100000:
        return _level("超级开心", 开心=8, 兴奋=4)
    if cents >= 30000:
        return _level("很开心", 开心=6, 兴奋=2)
    if cents >= 5000:
        return _level("心情不错", 开心=4)
    return _level("有点开心", 开心=2)


def pnl_level(pnl_cents: int, ratio: float):
    """交易盈亏分级：按比例（收益率）优先，金额兜底。"""
    if pnl_cents == 0:
        return _level("平静、没什么波动")
    if pnl_cents > 0:
        if ratio >= 0.10:
            return _level("狂喜、开心到转圈", 开心=10, 兴奋=6)
        if ratio >= 0.03:
            return _level("很开心", 开心=6, 兴奋=2)
        return _level("心情不错", 开心=3)
    if ratio <= -0.10:
        return _level("崩溃、伤心，呜呜呜", 郁闷=10, 委屈=8, 焦虑=5)
    if ratio <= -0.03:
        return _level("难过、委屈", 郁闷=6, 委屈=4)
    return _level("有点低落、不太开心", 郁闷=3)


def expense_level(cents: int, necessary: bool = False):
    """支出分级：只有大额必要支出才掉心情，日常吃喝不扣。"""
    if not necessary:
        return _level("")
    if cents >= 500000:
        return _level("有点低落、不太开心", 郁闷=5, 焦虑=3)
    if cents >= 100000:
        return _level("有点低落、不太开心", 郁闷=3)
    return _level("")


def low_balance_level(gap_cents: int):
    """余额不足预警：不论缺口多少，都是崩溃档。"""
    return _level("崩溃、伤心，呜呜呜", 焦虑=8, 委屈=6, 郁闷=4)
