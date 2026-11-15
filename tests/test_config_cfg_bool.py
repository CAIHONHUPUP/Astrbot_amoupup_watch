# -*- coding: utf-8 -*-
"""watch 的配置读取：账单开关画在能陪面板上，watch 也要读得到。"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from main import WatchPlugin  # noqa: E402


class FakeAmo:
    def __init__(self, config):
        self.config = config


class FakeMeta:
    def __init__(self, amo):
        self.star_cls = amo
        self.activated = True


class FakeContext:
    def __init__(self, amo):
        self.amo = amo

    def get_registered_star(self, name):
        return FakeMeta(self.amo) if self.amo is not None and name == "Astrbot_amoupup_forever" else None


def make(own=None, amo=None):
    plugin = WatchPlugin.__new__(WatchPlugin)
    plugin.config = dict(own or {})
    plugin.context = FakeContext(FakeAmo(dict(amo or {})) if amo is not None else None)
    return plugin


class CfgBoolTests(unittest.TestCase):
    def test_reads_own_config_first(self):
        plugin = make(own={"bill_autopay": False}, amo={"bill_autopay": True})
        self.assertFalse(plugin.cfg_bool("bill_autopay", True))

    def test_falls_back_to_amo_config(self):
        plugin = make(own={}, amo={"bill_autopay": False})
        self.assertFalse(plugin.cfg_bool("bill_autopay", True))

    def test_default_when_nowhere_configured(self):
        plugin = make(own={}, amo=None)
        self.assertTrue(plugin.cfg_bool("bill_autopay", True))
        self.assertFalse(plugin.cfg_bool("bill_autopay"))


if __name__ == "__main__":
    unittest.main()
