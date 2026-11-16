# -*- coding: utf-8 -*-
"""转账入账：成功/失败/补交搬家费/回写 amo 的分支。"""
import asyncio
from types import SimpleNamespace

import services.city as city_mod
from services.transfer_service import handle_transfer


class FakeFinanceHelp:
    def __init__(self, result=None, raises=False):
        self.result = result if result is not None else {"ok": False, "message": ""}
        self.raises = raises
        self.cents = []

    async def user_transfer(self, cents):
        self.cents.append(cents)
        if self.raises:
            raise RuntimeError("wallet down")
        return self.result


class FakeAmo:
    def __init__(self, raises=False):
        self.notes = []
        self.raises = raises

    def note_transfer_event(self, cents):
        if self.raises:
            raise RuntimeError("amo down")
        self.notes.append(cents)


def test_handle_transfer_no_match():
    plugin = SimpleNamespace(finance_help=FakeFinanceHelp())
    assert asyncio.run(handle_transfer(plugin, "今天没转账")) == ""


def test_handle_transfer_without_finance_module():
    plugin = SimpleNamespace(finance_help=None)
    assert asyncio.run(handle_transfer(plugin, "[转账]500")) == "金融模块未就绪"


def test_handle_transfer_ok_notifies_amo_and_settles(monkeypatch):
    amo = FakeAmo()
    plugin = SimpleNamespace(finance_help=FakeFinanceHelp({"ok": True}), amo=amo, finance=None)

    async def fake_settle(plugin):
        return "；搬家费 ¥100.00 已结清"

    monkeypatch.setattr(city_mod, "settle_pending_fee", fake_settle)
    text = asyncio.run(handle_transfer(plugin, "给你转点钱 [转账]500"))
    assert text.startswith("已收到")
    assert "搬家费" in text
    assert amo.notes == [50000]


def test_handle_transfer_uses_finance_amo(monkeypatch):
    amo = FakeAmo()
    plugin = SimpleNamespace(finance_help=FakeFinanceHelp({"ok": True}),
                             finance=SimpleNamespace(_amo=lambda: amo))

    async def fake_settle(plugin):
        return ""

    monkeypatch.setattr(city_mod, "settle_pending_fee", fake_settle)
    asyncio.run(handle_transfer(plugin, "【转账】88.5"))
    assert amo.notes == [8850]


def test_handle_transfer_note_failure_is_silent(monkeypatch):
    amo = FakeAmo(raises=True)
    plugin = SimpleNamespace(finance_help=FakeFinanceHelp({"ok": True}), amo=amo)

    async def fake_settle(plugin):
        return ""

    monkeypatch.setattr(city_mod, "settle_pending_fee", fake_settle)
    assert asyncio.run(handle_transfer(plugin, "[转账]1")) == "已收到"


def test_handle_transfer_failure_message():
    plugin = SimpleNamespace(finance_help=FakeFinanceHelp(raises=True))
    assert asyncio.run(handle_transfer(plugin, "[转账]10")) == "转账入账失败"
