# -*- coding: utf-8 -*-
import asyncio
import json

from core.llm_decider import llm_decide, parse_decision


def test_parse_valid_buy():
    text = '{"action":"buy","code":"600519","qty":100,"reason":"抄底试试"}'
    d = parse_decision(text, {"600519"})
    assert d["action"] == "buy" and d["qty"] == 100


def test_parse_rejects_bad():
    assert parse_decision("not json", {"600519"}) is None
    assert parse_decision('{"action":"buy","code":"600519","qty":150}', {"600519"}) is None
    assert parse_decision('{"action":"buy","code":"000001","qty":100}', {"600519"}) is None
    assert parse_decision('{"action":"sell","code":"600519","qty":0}', {"600519"})["action"] == "sell"


def test_parse_hold():
    d = parse_decision('{"action":"hold","reason":"再拿拿"}', set())
    assert d["action"] == "hold" and d["reason"] == "再拿拿"


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
    def __init__(self, provider):
        self.context = FakeContext(provider)


def test_llm_decide_ok():
    payload = json.dumps({"action": "sell", "code": "600519", "qty": 100, "reason": "小赚先落袋"})
    d = asyncio.run(llm_decide(FakePlugin(FakeProvider(payload)), 100000,
                               [{"name": "贵州茅台", "code": "600519", "qty": 100, "pnl_pct": 3.0}],
                               {"600519": {"name": "贵州茅台", "price": 1680.0}}, "开心"))
    assert d["action"] == "sell" and d["reason"] == "小赚先落袋"


def test_llm_decide_falls_back_when_provider_missing():
    class NoProvider:
        async def get_using_provider_async(self, umo=None):
            return None
    assert asyncio.run(llm_decide(FakePlugin(NoProvider()), 0, [], {}, "平静")) is None
