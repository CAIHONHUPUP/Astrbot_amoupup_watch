# -*- coding: utf-8 -*-
"""回归：_expense_meta 里用了 re.search，模块必须导入 re（2026-11-15 修）。"""
from __future__ import annotations

from core.amo_bridge import FinanceLink


def test_expense_meta_classifies_and_extracts_item():
    category, sub, item, raw = FinanceLink._expense_meta("午餐：牛肉面")
    assert category == "餐饮"
    assert sub == "午餐"
    assert item == "牛肉面"
    assert raw == "午餐：牛肉面"


def test_expense_meta_without_colon_falls_back_to_text():
    category, sub, item, _ = FinanceLink._expense_meta("打车")
    assert category == "交通"
    assert sub == "打车"
    assert item == "打车"


def test_expense_meta_unknown_goes_other():
    category, sub, item, _ = FinanceLink._expense_meta("买个东西")
    assert category == "其他"
