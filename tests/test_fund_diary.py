# -*- coding: utf-8 -*-
"""基金日记：事实+情绪+余额交给 LLM，LLM 写什么就存什么，代码里没有模板句。"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import services.diary_engine as de  # noqa: E402


class FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def chat_raw(self, umo, system, prompt):
        self.calls.append({"umo": umo, "system": system, "prompt": prompt})
        return self.text


class FakeAmo:
    def __init__(self, text=None):
        self.llm = FakeLLM(text) if text is not None else None
        self.saved = []

    def _owner_umos(self):
        return ["u1"]


class FakeBridge:
    def __init__(self):
        self.saved = []

    def mood(self):
        return {"word": "焦虑"}

    def balance_cents(self):
        return 123456

    def _small_diary(self, amo, text):
        self.saved.append(text)
        return True


def test_rewrite_fund_fact_feeds_json_mood_balance_and_saves_llm_text():
    bridge = FakeBridge()
    amo = FakeAmo("今天看白酒跌太多，没忍住又补了500，买完就后悔了，接下来几天只能吃土了。")
    fact = {"action": "buy_fund", "fund_name": "易方达蓝筹", "amount": 500.0, "time": "14:50"}
    text = asyncio.run(de.rewrite_fund_fact(bridge, amo, fact))
    assert text.startswith("今天看白酒跌太多")
    prompt = amo.llm.calls[0]["prompt"]
    assert '"action": "buy_fund"' in prompt
    assert "易方达蓝筹" in prompt
    assert "500.0" in prompt
    assert "焦虑" in prompt
    assert "1234.56" in prompt
    assert "今天基金普遍跌了" not in prompt
    assert "希望明天能红" not in prompt
    assert bridge.saved == [text]


def test_rewrite_fund_fact_without_llm_writes_nothing():
    bridge = FakeBridge()
    amo = FakeAmo(None)
    text = asyncio.run(de.rewrite_fund_fact(bridge, amo, {"action": "buy_fund"}))
    assert text == ""
    assert bridge.saved == []


def test_build_outage_prompt_has_fact_mood_balance_without_template():
    prompt = de.build_outage_prompt(
        {"reason": "fund_quote_unavailable", "balance_cents": 123456}, "焦虑", 1234.56)
    assert "fund_quote_unavailable" in prompt
    assert "焦虑" in prompt
    assert "1234.56" in prompt
    assert "今天基金普遍跌了" not in prompt
    assert "希望明天能红" not in prompt


def test_rewrite_fund_outage_saves_llm_text():
    bridge = FakeBridge()
    amo = FakeAmo("气死我了，好不容易看准时机，结果接口挂了，错过一个亿！")
    text = asyncio.run(de.rewrite_fund_outage(
        bridge, amo, {"reason": "fund_quote_unavailable", "balance_cents": 123456}))
    assert text.startswith("气死我了")
    prompt = amo.llm.calls[0]["prompt"]
    assert "fund_quote_unavailable" in prompt
    assert "焦虑" in prompt
    assert "1234.56" in prompt
    assert bridge.saved == [text]


def test_fund_production_sources_have_no_diary_template_sentences():
    root = Path(ROOT)
    sources = [
        root / "services" / "fund_market.py",
        root / "services" / "fund_api.py",
        root / "services" / "diary_engine.py",
        root / "services" / "trade_engine.py",
        root / "core" / "agent_loop.py",
        root / "core" / "amo_bridge.py",
        root / "core" / "fund_actions.py",
        root / "core" / "jobs.py",
    ]
    forbidden = ("今天基金普遍跌了", "我申购了", "我赎回了", "希望明天能红",
                 "买完就后悔", "只能吃土")
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in text, "%s 里还有基金日记模板：%s" % (source.name, phrase)
