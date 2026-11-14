# -*- coding: utf-8 -*-
"""转账识别：句子里夹着 [转账]500 也要能入账。"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.transfer_service import handle_transfer  # noqa: E402


class FakeFinance:
    def __init__(self):
        self.cents = []

    async def user_transfer(self, cents):
        self.cents.append(cents)
        return {"ok": False, "message": ""}


class FakePlugin:
    def __init__(self):
        self.finance_help = FakeFinance()


class TransferParseTests(unittest.TestCase):
    def parse(self, text):
        plugin = FakePlugin()
        asyncio.run(handle_transfer(plugin, text))
        return plugin.finance_help.cents[-1] if plugin.finance_help.cents else None

    def test_mid_sentence_bracket(self):
        self.assertEqual(self.parse("今天下雨吃不了火锅了，[转账]500，给你发点钱"), 50000)

    def test_start_bracket(self):
        self.assertEqual(self.parse("[转账]500"), 50000)

    def test_cn_bracket(self):
        self.assertEqual(self.parse("【转账】88.5 买点好吃的"), 8850)

    def test_plain_start(self):
        self.assertEqual(self.parse("转账 200"), 20000)

    def test_no_amount_no_transfer(self):
        self.assertIsNone(self.parse("我刚刚转账了，你查一下"))


if __name__ == "__main__":
    unittest.main()
