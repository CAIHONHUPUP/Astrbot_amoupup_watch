# -*- coding: utf-8 -*-
"""基金估值全挂：事实注入、LLM 自主行动解析、发消息/跳过/挪钱执行。"""
import asyncio
import datetime as dt
import inspect

import pytest

from core.agent_loop import FinanceDecision
from core.llm_decider import llm_decide_outage, parse_outage_decision


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def positions(self):
        return []

    def fund_quotes(self, day=""):
        return []

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)


class FakeFinance:
    def __init__(self):
        self.outages = []

    def balance_cents(self):
        return 100000

    def mood(self):
        return {"word": "焦虑"}

    async def record_fund_outage(self, fact, idem=""):
        self.outages.append((dict(fact), idem))
        return {"ok": True}


class FakeTrading:
    pass


class FakeProvider:
    def __init__(self, text):
        self.text = text

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
        class R:
            completion_text = self.text
        return R()


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    async def get_using_provider_async(self, umo=None):
        return self.provider


class FakePlugin:
    def __init__(self, provider=None):
        self.context = FakeContext(provider)
        self.finance_memory = None
        self.finance_repo = None
        self.config = {}


class FakeRuntime:
    async def run_io(self, fn, *args, timeout=None, **kwargs):
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


def test_parse_outage_send_message():
    d = parse_outage_decision('{"action":"send_message_to_user","text":"这破软件又崩了"}')
    assert d["action"] == "send_message_to_user"
    assert "崩了" in d["text"]


def test_parse_outage_skip_and_reallocate():
    d = parse_outage_decision('{"action":"skip_fund_trade","reason":"今天不买了"}')
    assert d["action"] == "skip_fund_trade"
    d = parse_outage_decision(
        '{"action":"reallocate_funds","target":"deposit_fixed","amount":500,"term_months":3,"note":"存定期"}')
    assert d["action"] == "reallocate_funds"
    assert d["target"] == "deposit_fixed"
    assert d["amount_cents"] == 50000
    assert d["term_months"] == 3
    assert parse_outage_decision(
        '{"action":"reallocate_funds","target":"shopping","amount":100}')["target"] == "shopping"


def test_parse_outage_rejects_bad():
    assert parse_outage_decision("not json") is None
    assert parse_outage_decision('{"action":"send_message_to_user","text":""}') is None
    assert parse_outage_decision(
        '{"action":"reallocate_funds","target":"buy_house","amount":1}') is None
    assert parse_outage_decision(
        '{"action":"reallocate_funds","target":"deposit_fixed","amount":500,"term_months":5}') is None


def test_llm_decide_outage_reads_provider():
    payload = '{"action":"skip_fund_trade","reason":"算了"}'
    d = asyncio.run(llm_decide_outage(FakePlugin(FakeProvider(payload)), "【突发状况】", "焦虑"))
    assert d["action"] == "skip_fund_trade"


def test_llm_decide_outage_without_provider_is_none():
    assert asyncio.run(llm_decide_outage(FakePlugin(None), "【突发状况】", "平静")) is None


def test_slot_outage_sends_llm_text_and_records_fact(monkeypatch):
    from core.agent_loop import FinanceDecision
    import core.llm_decider as decider
    import core.fund_actions as actions

    async def fake_decide(plugin, facts, mood, memory=""):
        assert "【突发状况】" in facts
        assert "焦虑" == mood
        return {"action": "send_message_to_user", "text": "老公，这破软件又崩了！"}

    sent = {}

    async def fake_send(plugin, text):
        sent["text"] = text
        return {"ok": True, "action": "send_message_to_user", "text": text, "message": "sent"}

    monkeypatch.setattr(decider, "llm_decide_outage", fake_decide)
    monkeypatch.setattr(actions, "send_message_to_user_action", fake_send)
    finance = FakeFinance()
    dec = FinanceDecision(FakePlugin(), None, FakeRepo(), finance, FakeTrading())
    out = asyncio.run(dec._slot_outage(
        dt.datetime(2026, 11, 16, 14, 50), "2026-11-16", 100000, {}, demo=True))
    assert out["skipped"] == "fund_outage"
    assert sent["text"] == "老公，这破软件又崩了！"
    fact, idem = finance.outages[-1]
    assert fact["action"] == "send_message_to_user"
    assert fact["reason"] == "fund_quote_unavailable"
    assert fact["balance_cents"] == 100000
    assert idem.startswith("decision:2026-11-16:close:outage")


def test_run_slot_close_enters_outage_when_all_estimates_missing(monkeypatch):
    import services.fund_market as fm
    import core.llm_decider as decider
    import core.fund_actions as actions

    async def no_quotes(codes):
        return {}

    async def fake_decide(plugin, facts, mood, memory=""):
        assert "【突发状况】" in facts
        return {"action": "send_message_to_user", "text": "估值全空了"}

    async def fake_send(plugin, text):
        return {"ok": True, "action": "send_message_to_user", "text": text, "message": "sent"}

    monkeypatch.setattr(fm, "fetch_fund_quotes", no_quotes)
    monkeypatch.setattr(decider, "llm_decide_outage", fake_decide)
    monkeypatch.setattr(actions, "send_message_to_user_action", fake_send)
    repo = FakeRepo()
    plugin = FakePlugin()
    plugin.finance_repo = repo
    finance = FakeFinance()
    dec = FinanceDecision(plugin, FakeRuntime(), repo, finance, FakeTrading())
    out = asyncio.run(dec.run_slot("close", dt.datetime(2026, 11, 16, 14, 50)))
    assert out["skipped"] == "fund_outage"
    assert finance.outages and finance.outages[-1][0]["action"] == "send_message_to_user"


def test_slot_outage_demo_does_not_reallocate(monkeypatch):
    import core.llm_decider as decider
    import core.fund_actions as actions

    async def fake_decide(plugin, facts, mood, memory=""):
        return {"action": "reallocate_funds", "target": "shopping",
                "amount_cents": 10000, "term_months": 0, "note": "买衣服"}

    called = []

    async def fake_alloc(*args, **kwargs):
        called.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(decider, "llm_decide_outage", fake_decide)
    monkeypatch.setattr(actions, "reallocate_funds_action", fake_alloc)
    finance = FakeFinance()
    dec = FinanceDecision(FakePlugin(), None, FakeRepo(), finance, FakeTrading())
    out = asyncio.run(dec._slot_outage(
        dt.datetime(2026, 11, 16, 14, 50), "2026-11-16", 100000, {}, demo=True))
    assert out["executed"]["message"] == "demo_no_reallocate"
    assert called == []
