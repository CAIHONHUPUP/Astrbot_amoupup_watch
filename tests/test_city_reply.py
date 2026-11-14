# -*- coding: utf-8 -*-
"""回归：city_reply 用 yuan() 格式化搬家费，模块必须导入 yuan（2026-11-15 修）。"""
from __future__ import annotations

from services.city import city_reply


class FakePlugin:
    config = {}


def test_city_reply_pending_fee_uses_yuan():
    moved = {"moved": {"weather": {"name": "杭州"}}, "fee_cents": 120000, "pending_cents": 120000}
    text = city_reply(FakePlugin(), moved, saved=True)
    assert "搬家费" in text
    assert "¥1200.00" in text
    assert "[转账]1200.00" in text


def test_city_reply_paid_fee_uses_yuan():
    moved = {"moved": {}, "fee_cents": 50000, "pending_cents": 0}
    text = city_reply(FakePlugin(), moved, saved=False)
    assert "¥500.00" in text
    assert "没存上" in text
