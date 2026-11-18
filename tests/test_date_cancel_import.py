# -*- coding: utf-8 -*-
"""回归：on_date_cancel 依赖的 date_engine 公开名必须存在。

2026-11-17 线上曾报 `No module named services`，根因是 CANCEL_WORDS 移到了
`services/date_rules.py`，date_engine 没有 re-export，相对导入失败后走绝对兜底再失败。
"""
from services import date_engine


def test_date_engine_keeps_cancel_contract():
    assert date_engine.CANCEL_WORDS, "CANCEL_WORDS 必须可导入"
    assert callable(date_engine.cancel_date)
    assert callable(date_engine.find_pending)


def test_cancel_words_are_strings():
    assert all(isinstance(word, str) and word for word in date_engine.CANCEL_WORDS)