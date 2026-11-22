# -*- coding: utf-8 -*-
"""LifeStateSnapshot 消费端契约测试（task-27）。

守护只能通过 AmoBridge 读快照；这里钉住：
- 合法快照 -> available，字段取值稳定；
- 版本不符/字段缺失/调用失败 -> available=False + reason（业务退回旧逻辑，不崩）；
- 降级日志按 reason 去重（不刷屏）。
"""
from __future__ import annotations

import logging
import sys
import unittest
from types import SimpleNamespace

ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import life_state  # noqa: E402


def _snapshot(**overrides):
    base = {
        "contract": "amo-life-state",
        "version": 1,
        "source": "forever",
        "generated_at": 1795317120.0,
        "day": "2026-11-22",
        "principal": {"umo": "lark:FriendMessage:ou_x", "uid": "ou_x",
                      "platform": "lark", "scope": "private", "kind": "owner"},
        "location": {"place": "家", "zone": "home", "activity": "在家歇着",
                     "at_home": True, "in_shop": False, "event_id": "ev_home"},
        "current_action": {"action": "在家歇着", "place": "家", "zone": "home",
                           "source": "schedule"},
        "schedule": {"current": {"activity": "在家歇着"}, "reason": "covering",
                     "next": {}, "day": "2026-11-22"},
        "date_session": {"canonical_present": False, "phase": "", "together": False},
        "shopping": {"active": True,
                     "line": "你正在商场试米白色针织开衫，价格 985 元，卡里余额 4072 元",
                     "mentioned_balance_cents": 407200,
                     "wallet_balance_cents": 394725, "line_stale": True},
        "wallet": {"balance_cents": 394725, "withdrawable_cents": 394725,
                   "month": "2026-11", "month_consumption_cents": 533145},
        "outfit": {"day": "2026-11-22", "text": "卡其色风衣"},
        "mood": {"score": 62, "words": ["平和"]},
        "media": {"photos_today": 2},
        "consistency": {"ok": False, "issues": [
            {"code": "shopping_balance_stale", "detail": "4072 vs 3947.25"}]},
        "degraded": [],
    }
    base.update(overrides)
    return base


class SnapshotViewTests(unittest.TestCase):
    def test_valid_snapshot(self):
        view = life_state.view(_snapshot())
        self.assertTrue(view.available)
        self.assertEqual(view.version, 1)
        self.assertEqual(view.day, "2026-11-22")
        self.assertEqual(view.wallet_balance_cents(), 394725)
        self.assertEqual(view.month_consumption_cents(), 533145)
        self.assertEqual(view.place(), "家")
        self.assertFalse(view.in_shop())
        self.assertTrue(view.at_home())
        self.assertTrue(view.shopping_stale())
        self.assertIn("试米白色", view.shopping_line())
        self.assertEqual(view.photos_today(), 2)
        self.assertEqual(view.issues()[0]["code"], "shopping_balance_stale")

    def test_contract_mismatch(self):
        view = life_state.view(_snapshot(contract="other-contract"))
        self.assertFalse(view.available)
        self.assertEqual(view.reason, "contract_mismatch")
        # 降级视图不暴露数值（原文只在 raw 里给排障用）
        self.assertEqual(view.wallet_balance_cents(), 0)
        self.assertEqual(view.raw["wallet"]["balance_cents"], 394725)

    def test_version_unsupported(self):
        view = life_state.view(_snapshot(version=99))
        self.assertFalse(view.available)
        self.assertTrue(view.reason.startswith("version_unsupported"))

    def test_fields_missing(self):
        data = _snapshot()
        data.pop("wallet")
        view = life_state.view(data)
        self.assertFalse(view.available)
        self.assertTrue(view.reason.startswith("fields_missing"))

    def test_defaults_are_safe(self):
        view = life_state.SnapshotView(None, False, "not_ready")
        self.assertEqual(view.wallet_balance_cents(), 0)
        self.assertEqual(view.place(), "")
        self.assertIsNone(view.in_shop())
        self.assertEqual(view.shopping(), {})
        self.assertEqual(view.get("a.b.c", "x"), "x")


class _Finance:
    def __init__(self, amo):
        self._amo_obj = amo

    def _amo(self):
        return self._amo_obj


class _AmoWithSnapshot:
    def __init__(self, payload):
        self.payload = payload

    def life_state_snapshot(self, *args, **kwargs):
        return dict(self.payload)


class _AmoWithoutSnapshot:
    pass


def _plugin(amo):
    return SimpleNamespace(finance=_Finance(amo))


class ReadTests(unittest.TestCase):
    def test_read_ok(self):
        view = life_state.read(_plugin(_AmoWithSnapshot(_snapshot())))
        self.assertTrue(view.available)
        self.assertEqual(view.wallet_balance_cents(), 394725)

    def test_read_missing_method_degrades(self):
        view = life_state.read(_plugin(_AmoWithoutSnapshot()))
        self.assertFalse(view.available)
        self.assertEqual(view.reason, "missing")

    def test_read_not_ready_degrades(self):
        view = life_state.read(_plugin(None))
        self.assertFalse(view.available)
        self.assertEqual(view.reason, "not_ready")

    def test_read_invalid_payload_degrades(self):
        view = life_state.read(_plugin(_AmoWithSnapshot({"version": 2})))
        self.assertFalse(view.available)

    def test_contract_descriptor(self):
        descriptor = life_state.contract_descriptor()
        self.assertEqual(descriptor["contract"], "amo-life-state")
        self.assertEqual(descriptor["role"], "consumer")
        self.assertEqual(tuple(descriptor["fields"]), life_state.REQUIRED_FIELDS)


class FallbackLogTests(unittest.TestCase):
    def setUp(self):
        life_state._WARNED.clear()

    def test_warn_dedupe(self):
        class _Recorder:
            def __init__(self):
                self.messages = []

            def warning(self, message, *args):
                self.messages.append(message % tuple(args) if args else message)

        recorder = _Recorder()
        original = life_state.logger
        life_state.logger = recorder
        try:
            life_state._warn_fallback("missing", "a")
            life_state._warn_fallback("missing", "b")
            life_state._warn_fallback("other", "c")
        finally:
            life_state.logger = original
        self.assertEqual(len(recorder.messages), 2)
        self.assertTrue(all("CONTRACT_FALLBACK" in text for text in recorder.messages))


if __name__ == "__main__":
    unittest.main()