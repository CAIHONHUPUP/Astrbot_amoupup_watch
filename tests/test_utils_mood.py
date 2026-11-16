# -*- coding: utf-8 -*-
"""情绪分级：所有分数档 / 盈亏档 / 支出档都要有人话，且不出现数值。"""
from utils import mood


def test_word_for_score_bands():
    assert mood.word_for_score(90) == "狂喜、超级开心"
    assert mood.word_for_score(85) == "狂喜、超级开心"
    assert mood.word_for_score(70) == "很开心、心情不错"
    assert mood.word_for_score(58) == "还算轻快"
    assert mood.word_for_score(45) == "平静、没什么波动"
    assert mood.word_for_score(35) == "有点低落、不太开心"
    assert mood.word_for_score(20) == "难过、委屈"
    assert mood.word_for_score(0) == "崩溃、伤心，呜呜呜"


def test_yuan_formats_cents():
    assert mood.yuan(0) == "¥0.00"
    assert mood.yuan(123456) == "¥1,234.56"


def test_level_drops_zero_deltas():
    assert mood._level("平静", 开心=0, 兴奋=2)["deltas"] == {"兴奋": 2.0}
    assert mood._level("平静")["deltas"] == {}


def test_income_level_tiers():
    assert mood.income_level(500000)["word"] == "狂喜、开心到转圈"
    assert mood.income_level(100000)["word"] == "超级开心"
    assert mood.income_level(30000)["word"] == "很开心"
    assert mood.income_level(5000)["word"] == "心情不错"
    assert mood.income_level(100)["word"] == "有点开心"
    assert mood.income_level(500000)["deltas"]["兴奋"] == 6.0


def test_pnl_level_branches():
    assert mood.pnl_level(0, 0.0)["deltas"] == {}
    assert mood.pnl_level(1000, 0.10)["word"] == "狂喜、开心到转圈"
    assert mood.pnl_level(1000, 0.03)["word"] == "很开心"
    assert mood.pnl_level(1000, 0.01)["word"] == "心情不错"
    assert mood.pnl_level(-1000, -0.10)["deltas"]["焦虑"] == 5.0
    assert mood.pnl_level(-1000, -0.03)["word"] == "难过、委屈"
    assert mood.pnl_level(-1000, -0.01)["word"] == "有点低落、不太开心"


def test_expense_level_only_big_necessary():
    assert mood.expense_level(999999, necessary=False)["deltas"] == {}
    assert mood.expense_level(500000, necessary=True)["deltas"]["焦虑"] == 3.0
    assert mood.expense_level(100000, necessary=True)["deltas"]["郁闷"] == 3.0
    assert mood.expense_level(100, necessary=True)["deltas"] == {}


def test_low_balance_always_crisis():
    assert mood.low_balance_level(1)["word"] == "崩溃、伤心，呜呜呜"
    assert mood.low_balance_level(0)["deltas"]["焦虑"] == 8.0


def test_reaction_line_pools():
    happy = {mood.reaction_line("狂喜", "s%d" % i) for i in range(30)}
    sad = {mood.reaction_line("崩溃", "s%d" % i) for i in range(30)}
    neutral = {mood.reaction_line("", "s%d" % i) for i in range(30)}
    assert happy and sad and neutral
    assert happy.isdisjoint(sad)
    assert mood.reaction_line("很开心", "same") == mood.reaction_line("很开心", "same")
