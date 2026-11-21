# -*- coding: utf-8 -*-
"""推送文案由模型按事实现生成（2026-11-21）。"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import social_ai  # noqa: E402


class PushPhraseTests(unittest.TestCase):
    def test_phrase_uses_facts(self):
        seen = {}

        async def fake_chat(plugin, system, prompt):
            seen["system"] = system
            seen["prompt"] = prompt
            return "外面突然下雨了，我没带伞，快来救我"

        with mock.patch.object(social_ai, "_chat", fake_chat):
            line = asyncio.run(social_ai.phrase(
                object(), "外面突然下大雨，她没带伞", "撒娇"))
        self.assertIn("没带伞", seen["prompt"])
        self.assertIn("第一人称", seen["system"])
        self.assertEqual("外面突然下雨了，我没带伞，快来救我", line)


if __name__ == "__main__":
    unittest.main()
