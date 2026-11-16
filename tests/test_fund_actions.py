# -*- coding: utf-8 -*-
"""自主行动工具：发消息、跳过、存定期/购物/保留。"""
import asyncio

import core.fund_actions as fa


class FakeAmo:
    def __init__(self):
        self.sent = []

    async def push_to_owner(self, text):
        self.sent.append(text)
        return True


class FakeFinance:
    def __init__(self):
        self.amo = FakeAmo()
        self.expenses = []

    def _amo(self):
        return self.amo

    async def expense(self, amount, reason, idem, **kwargs):
        self.expenses.append((amount, reason, idem, kwargs))
        return {"ok": True}


class FakeBank:
    def __init__(self):
        self.deposits = []

    async def deposit(self, amount, term=0, diary=True):
        self.deposits.append((amount, term, diary))
        return {"ok": True}


class FakePlugin:
    def __init__(self):
        self.finance = FakeFinance()
        self.bank = FakeBank()


def test_send_message_goes_through_amo_push():
    plugin = FakePlugin()
    out = asyncio.run(fa.send_message_to_user_action(plugin, "这破软件又崩了"))
    assert out["ok"] is True
    assert plugin.finance.amo.sent == ["这破软件又崩了"]


class FakeAmoFallback:
    def __init__(self):
        self.sent = []
        self._last_reply_at = {"webchat:FriendMessage:webchat!302578963!desk-pet": 1.0}

    async def push_to_owner(self, text):
        return False

    async def _send_to_umo(self, umo, text):
        self.sent.append((umo, text))
        return True


def test_send_falls_back_to_current_active_session():
    plugin = FakePlugin()
    plugin.finance.amo = FakeAmoFallback()
    out = asyncio.run(fa.send_message_to_user_action(plugin, "这破软件又崩了"))
    assert out["ok"] is True
    assert out["route"] == "webchat:FriendMessage:webchat!302578963!desk-pet"
    assert plugin.finance.amo.sent == [
        ("webchat:FriendMessage:webchat!302578963!desk-pet", "这破软件又崩了")]


def test_send_empty_message_rejected():
    plugin = FakePlugin()
    out = asyncio.run(fa.send_message_to_user_action(plugin, "  "))
    assert out["ok"] is False
    assert plugin.finance.amo.sent == []


def test_skip_is_noop():
    out = asyncio.run(fa.skip_fund_trade_action(FakePlugin(), "今天不买"))
    assert out["ok"] is True and out["reason"] == "今天不买"


def test_reallocate_deposit_uses_structured_bank_without_diary():
    plugin = FakePlugin()
    out = asyncio.run(fa.reallocate_funds_action(plugin, "deposit_fixed", 50000, 3, "存定期"))
    assert out["ok"] is True
    assert plugin.bank.deposits == [(50000, 3, False)]


def test_reallocate_shopping_uses_expense_without_diary():
    plugin = FakePlugin()
    out = asyncio.run(fa.reallocate_funds_action(plugin, "shopping", 10000, 0, "买件衣服"))
    assert out["ok"] is True
    amount, reason, idem, kwargs = plugin.finance.expenses[-1]
    assert amount == 10000 and reason == "买件衣服"
    assert kwargs["diary"] is False
    assert kwargs["category"] == "购物"


def test_reallocate_keep_and_invalid():
    plugin = FakePlugin()
    assert asyncio.run(fa.reallocate_funds_action(plugin, "keep"))["ok"] is True
    assert asyncio.run(fa.reallocate_funds_action(plugin, "unknown", 100))["ok"] is False
    assert asyncio.run(fa.reallocate_funds_action(plugin, "shopping", 0))["ok"] is False
