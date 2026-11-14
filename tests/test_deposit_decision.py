# -*- coding: utf-8 -*-
"""自主存款：工具校验、LLM 决策解析、agent loop 执行。"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.llm_tools import deposit_money_tool  # noqa: E402
from core.llm_decider import PROMPT, SYSTEM, parse_decision  # noqa: E402
from core.agent_loop import FinanceDecision  # noqa: E402


class FakeFinance:
    def __init__(self, cash=2000000):
        self.cash = cash

    def balance_cents(self):
        return self.cash


class FakeBank:
    def __init__(self):
        self.calls = []
        self.current = 0
        self.fixed = 0

    async def deposit(self, amount_cents, term_months=0):
        self.calls.append((amount_cents, term_months))
        if term_months:
            self.fixed += amount_cents
        else:
            self.current += amount_cents
        return {"ok": True, "amount": amount_cents, "term": term_months}

    def current_cents(self):
        return self.current

    def fixed_cents(self):
        return self.fixed


class FakePlugin:
    def __init__(self, cash=2000000):
        self.finance = FakeFinance(cash)
        self.bank = FakeBank()


class DepositToolTests(unittest.TestCase):
    def test_rejects_unknown_account(self):
        self.assertIn("current", asyncio.run(deposit_money_tool(FakePlugin(), 100, "股票")))

    def test_rejects_fixed_without_valid_term(self):
        self.assertIn("3 / 6 / 12 / 36", asyncio.run(deposit_money_tool(FakePlugin(), 100, "fixed", 5)))

    def test_rejects_zero_or_insufficient(self):
        self.assertIn("大于 0", asyncio.run(deposit_money_tool(FakePlugin(), 0, "current")))
        self.assertIn("存不了", asyncio.run(deposit_money_tool(FakePlugin(cash=100), 500, "current")))

    def test_current_and_fixed_success(self):
        plugin = FakePlugin()
        out = asyncio.run(deposit_money_tool(plugin, 800, "current", 12))
        self.assertIn("活期", out)
        self.assertEqual(plugin.bank.calls[-1], (80000, 0))
        out = asyncio.run(deposit_money_tool(plugin, 1000, "fixed", 12))
        self.assertIn("定期12个月", out)
        self.assertEqual(plugin.bank.calls[-1], (100000, 12))


class DepositDecisionTests(unittest.TestCase):
    def test_prompt_carries_bank_state_and_guidance(self):
        self.assertIn("活期", PROMPT)
        self.assertIn("定期", PROMPT)
        self.assertIn("deposit", PROMPT)
        self.assertIn("10000", SYSTEM)

    def test_parse_deposit(self):
        row = parse_decision('{"action":"deposit","account_type":"fixed","term_months":12,"amount":10000,"reason":"存点利息"}', set())
        self.assertEqual(row["action"], "deposit")
        self.assertEqual(row["amount_cents"], 1000000)
        self.assertEqual(row["term_months"], 12)

    def test_parse_deposit_current_ignores_term(self):
        row = parse_decision('{"action":"deposit","account_type":"current","term_months":12,"amount":500}', set())
        self.assertEqual(row["term_months"], 0)

    def test_parse_rejects_bad_terms(self):
        self.assertIsNone(parse_decision('{"action":"deposit","account_type":"fixed","term_months":5,"amount":100}', set()))
        self.assertIsNone(parse_decision('{"action":"deposit","account_type":"stock","amount":100}', set()))
        self.assertIsNone(parse_decision('{"action":"deposit","account_type":"current","amount":0}', set()))


class DepositExecuteTests(unittest.TestCase):
    def test_execute_calls_bank(self):
        plugin = FakePlugin()
        fd = FinanceDecision(plugin, None, None, None, None)
        out = asyncio.run(fd._execute("close", {
            "action": "deposit", "account_type": "fixed", "term_months": 6,
            "amount_cents": 500000, "reason": "存定期",
        }, dt.datetime.now()))
        self.assertTrue(out["ok"])
        self.assertIn("定期6个月", out["message"])
        self.assertEqual(plugin.bank.calls[-1], (500000, 6))


if __name__ == "__main__":
    unittest.main()
