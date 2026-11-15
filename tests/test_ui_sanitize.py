# -*- coding: utf-8 -*-
"""面板消毒：None/NaN 不露、报错不外溢、调试词变人话。"""
from utils.ui_sanitize import (FRIENDLY, sanitize_for_ui, sanitize_ledger_item,
                               strip_label_prefix)


def test_empty_values_become_blank():
    assert sanitize_for_ui(None) == ""
    assert sanitize_for_ui(float("nan")) == ""
    assert sanitize_for_ui(float("inf")) == ""
    assert sanitize_for_ui({"a": None}) == {"a": ""}


def test_debug_words_become_human():
    out = sanitize_for_ui("退回：原路退回（系统误记，微型事件误触发）")
    assert "系统误记" not in out
    assert "微型事件" not in out
    assert "原路退回" not in out
    assert "退款" in out


def test_jit_prefix_is_stripped():
    assert sanitize_for_ui("JIT:点了外卖") == "点了外卖"


def test_error_words_are_masked():
    assert sanitize_for_ui("KeyError: 'x'") == FRIENDLY
    assert sanitize_for_ui("Traceback (most recent call last)") == FRIENDLY
    assert sanitize_for_ui("今天没出门") == "今天没出门"


def test_label_prefix_is_stripped():
    assert strip_label_prefix("餐饮：面条 × 1") == "面条 × 1"
    assert strip_label_prefix("其他：自己做饭 × 1") == "自己做饭 × 1"
    assert strip_label_prefix("日常：点了个外卖") == "点了个外卖"
    assert strip_label_prefix("面条 × 1") == "面条 × 1"


def test_ledger_item_is_clean():
    row = sanitize_ledger_item({
        "reason": "餐饮：面条 × 1", "item_name": "其他：面条 × 1",
        "sub_category": "日常", "delta_cents": -1500,
    })
    assert row["reason"] == "面条 × 1"
    assert row["item_name"] == "面条 × 1"
    assert row["delta_cents"] == -1500


def test_lists_keep_shape():
    out = sanitize_for_ui([{"a": None, "b": [1, 2.5]}, "JIT:点了外卖"])
    assert out[0]["a"] == "" and out[0]["b"] == [1, 2.5]
    assert out[1] == "点了外卖"
