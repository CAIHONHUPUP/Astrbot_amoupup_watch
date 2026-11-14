# -*- coding: utf-8 -*-
"""回归：/watch 诊断名、定时任务单步隔离、滞纳金账实一致（2026-11-15）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import main as entry
from main import WatchPlugin
from services import bill_flow
from ui.cards import CardMixin


class FakeFinance:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []
        self.moods = []
        self.memories = []

    async def expense(self, amount, reason, idem, necessary=False, **kwargs):
        self.calls.append((amount, reason, idem))
        return {"ok": self.ok}

    def nudge_mood(self, deltas, key=""):
        self.moods.append(key)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)

    def small_diary(self, text):
        pass


def _plugin(finance):
    return SimpleNamespace(finance=finance, finance_repo=object())


def test_fmt_diag_without_display_name():
    text = CardMixin._fmt_diag(None, {})
    assert "我能永远守护你" in text


def test_guard_step_isolates_failure():
    plugin = object.__new__(WatchPlugin)
    hit = []

    async def boom():
        raise RuntimeError("quote timeout")

    with mock.patch.object(entry, "isolated", lambda name, exc: hit.append(name)):
        result = asyncio.run(plugin._guard_step("life", boom()))
    assert result is None
    assert hit == ["finance.life"]


def test_penalty_not_recorded_when_expense_fails():
    row = {"name": "房租", "amount_cents": 100000, "kind": "rent"}
    finance = FakeFinance(ok=False)
    with mock.patch.object(bill_flow, "load_due", lambda plugin, period: [row]), \
            mock.patch.object(bill_flow, "unpaid", lambda rows: rows), \
            mock.patch.object(bill_flow, "stage_of", lambda rows, ts: 1), \
            mock.patch.object(bill_flow, "save_due", lambda *a, **k: None):
        asyncio.run(bill_flow.overdue_tick(_plugin(finance)))
    assert "penalty" not in row
    assert finance.memories == []
    assert finance.calls and finance.calls[0][0] > 0


def test_penalty_recorded_when_expense_succeeds():
    row = {"name": "房租", "amount_cents": 100000, "kind": "rent"}
    finance = FakeFinance(ok=True)
    with mock.patch.object(bill_flow, "load_due", lambda plugin, period: [row]), \
            mock.patch.object(bill_flow, "unpaid", lambda rows: rows), \
            mock.patch.object(bill_flow, "stage_of", lambda rows, ts: 1), \
            mock.patch.object(bill_flow, "save_due", lambda *a, **k: None):
        asyncio.run(bill_flow.overdue_tick(_plugin(finance)))
    assert row["penalty"] == 5000
    assert finance.memories and "滞纳金" in finance.memories[0]
