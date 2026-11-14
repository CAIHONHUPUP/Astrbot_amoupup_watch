# -*- coding: utf-8 -*-
"""小日记 LLM 改写：真实事件、防幻觉、失败回退。"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import diary_engine as de  # noqa: E402


class FakeLLM:
    def __init__(self, text="今天试了条粉裙子，有点贵，没舍得买，唉。"):
        self.text = text

    async def chat_raw(self, umo, system, prompt):
        self.last = prompt
        return self.text


class FakeAmo:
    def __init__(self, llm=None):
        self.llm = llm

    def _owner_umos(self):
        return ["umo://qq/1"]


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo

    def _amo(self):
        return self.amo


class FakePlugin:
    def __init__(self, llm):
        self.finance = FakeFinance(FakeAmo(llm))


class DiaryTests(unittest.TestCase):
    def test_llm_rewrite_used(self):
        plugin = FakePlugin(FakeLLM())
        out = asyncio.run(de.rewrite(plugin, "试了一件粉色裙子，太贵没买", "有点委屈"))
        self.assertIn("粉裙子", out)

    def test_prompt_forbids_hallucination(self):
        prompt = de.build_prompt("买了泡面", "委屈")
        self.assertIn("只能写这些", prompt)
        self.assertIn("绝不允许编造", prompt)
        self.assertIn("心情+5", prompt)

    def test_fallback_on_llm_error(self):
        class BadLLM:
            async def chat_raw(self, *a, **k):
                raise RuntimeError("boom")
        plugin = FakePlugin(BadLLM())
        out = asyncio.run(de.rewrite(plugin, "买了泡面"))
        self.assertEqual(out, "买了泡面")

    def test_no_llm_fallback(self):
        plugin = FakePlugin(None)
        out = asyncio.run(de.rewrite(plugin, "买了泡面"))
        self.assertEqual(out, "买了泡面")


if __name__ == "__main__":
    unittest.main()
