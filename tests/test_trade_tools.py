# -*- coding: utf-8 -*-
"""LLM 交易工具：四个动作都要真的转到 Trading，不是空壳。"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.llm_tools import (buy_fund_tool, buy_stock_tool, sell_fund_tool,
                            sell_stock_tool)  # noqa: E402
from core.tool_wrappers import ToolWrappersMixin  # noqa: E402


class FakeTrading:
    def __init__(self):
        self.calls = []

    async def buy(self, code, qty, price=None, idem=""):
        self.calls.append(("buy", code, qty, price))
        return {"ok": True, "message": "买入成功"}

    async def sell(self, code, qty, price=None, idem=""):
        self.calls.append(("sell", code, qty, price))
        return {"ok": True, "message": "卖出成功"}

    async def subscribe(self, code, amount_cents, nav=None, idem=""):
        self.calls.append(("subscribe", code, amount_cents, nav))
        return {"ok": True, "message": "申购成功"}

    async def redeem(self, code, shares, nav=None, idem=""):
        self.calls.append(("redeem", code, shares, nav))
        return {"ok": True, "message": "赎回成功"}


class FakePlugin:
    def __init__(self):
        self.trading = FakeTrading()


def test_buy_stock_routes_to_trading():
    plugin = FakePlugin()
    out = asyncio.run(buy_stock_tool(plugin, None, "600519", 100, 10.0))
    assert "交易成功" in out
    assert plugin.trading.calls[0][:2] == ("buy", "600519")


def test_sell_stock_routes_to_trading():
    plugin = FakePlugin()
    out = asyncio.run(sell_stock_tool(plugin, None, "600519", 100))
    assert "交易成功" in out
    assert plugin.trading.calls[0][:2] == ("sell", "600519")


def test_buy_fund_uses_yuan_amount():
    plugin = FakePlugin()
    out = asyncio.run(buy_fund_tool(plugin, None, "005827", 100.0, 2.0))
    assert "交易成功" in out
    assert plugin.trading.calls[0][:2] == ("subscribe", "005827")
    assert plugin.trading.calls[0][2] == 10000


def test_sell_fund_routes_to_redeem():
    plugin = FakePlugin()
    out = asyncio.run(sell_fund_tool(plugin, None, "005827", 50.0, 2.0))
    assert "交易成功" in out
    assert plugin.trading.calls[0][:2] == ("redeem", "005827")


def test_tool_wrappers_expose_names():
    for name in ("buy_stock", "sell_stock", "buy_fund", "sell_fund"):
        assert callable(getattr(ToolWrappersMixin, name, None))
