# -*- coding: utf-8 -*-
"""Phase 18：本插件测试里重复定义的 Fake* 统一收口到这里（只搬位置，断言不变）。

三插件的 tests/support 保持逐字节一致，本文件放插件特有 / 历史测试专用桩。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import os
import random
import sqlite3
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class CityReplyFakePlugin:
    config = {}

class FinanceDailyFakeFinance:
    def __init__(self):
        self.calls = []

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        self.calls.append(("expense", cents, reason))
        return {"ok": True, "mood": "平静"}

class FinanceDecisionFakeFinance:
    def __init__(self):
        self.cash = 1_000_000
        self.calls = []

    def balance_cents(self):
        return self.cash

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        self.cash -= cents
        self.calls.append(("expense", cents, reason))
        return {"ok": True, "mood": "平静"}

    async def income(self, cents, reason, idem, salary=False):
        self.cash += cents
        self.calls.append(("income", cents, reason))
        return {"ok": True, "mood": "开心"}

    async def borrow(self, cents, reason, idem, months=12):
        self.cash += cents
        self.calls.append(("borrow", cents, reason))
        return {"ok": True}

    async def remember_event(self, kind, cents, text, idem):
        self.calls.append(("remember", text))
        return True

class FinanceNewsMemoryFakeMemory:
    def __init__(self):
        self.written = []

    async def remember(self, content, kind="finance", idem="", verify=True):
        self.written.append((kind, idem, content))
        return {"ok": True}
