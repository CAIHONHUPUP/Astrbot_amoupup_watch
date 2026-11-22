# -*- coding: utf-8 -*-
"""购物状态机纯规则（2026-11-22 task-24）：迁移/买了闸门/颜色去重/人话。

这些函数不碰库，单独测：状态链只能往前走；说「买了」只认完整购买事务记录。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import shopping_state as st  # noqa: E402


class TransitionTests(unittest.TestCase):
    def test_happy_path_is_forward_only(self):
        chain = [st.INTEREST, st.TRYON, st.APPROVED, st.PURCHASED]
        current = st.INTEREST
        for target in chain[1:]:
            self.assertTrue(st.can_transition(current, target), (current, target))
            current = st.advance(current, target)
            self.assertEqual(target, current)
        self.assertEqual(st.PURCHASED, current)

    def test_cannot_skip_to_purchased(self):
        self.assertFalse(st.can_transition(st.INTEREST, st.PURCHASED))
        self.assertFalse(st.can_transition(st.TRYON, st.PURCHASED))
        self.assertEqual(st.INTEREST, st.advance(st.INTEREST, st.PURCHASED))

    def test_terminal_states_do_not_move(self):
        for terminal in (st.PURCHASED, st.REJECTED, st.EXPIRED, st.FAILED):
            self.assertFalse(st.is_open(terminal))
            self.assertEqual(terminal, st.advance(terminal, st.TRYON))

    def test_unknown_state_normalizes_to_interest(self):
        self.assertEqual(st.INTEREST, st.normalize_state(""))
        self.assertEqual(st.INTEREST, st.normalize_state("bought?"))
        self.assertEqual(st.INTEREST, st.normalize_state(None))

    def test_approved_can_retry_or_fail(self):
        self.assertTrue(st.can_transition(st.APPROVED, st.TRYON))
        self.assertTrue(st.can_transition(st.APPROVED, st.FAILED))
        self.assertTrue(st.can_transition(st.APPROVED, st.PURCHASED))


class ClaimTests(unittest.TestCase):
    def _record(self, state_value=st.PURCHASED, **steps):
        base = {step: True for step in st.PURCHASE_STEPS}
        base.update(steps)
        return {"state": state_value, "steps": base}

    def test_tryon_only_in_interest_or_tryon(self):
        self.assertTrue(st.claim_allowed("tryon", st.INTEREST))
        self.assertTrue(st.claim_allowed("tryon", st.TRYON))
        self.assertFalse(st.claim_allowed("tryon", st.APPROVED))
        self.assertFalse(st.claim_allowed("tryon", st.PURCHASED))
        self.assertFalse(st.claim_allowed("tryon", st.EXPIRED))

    def test_home_requires_complete_purchase(self):
        self.assertTrue(st.claim_allowed("home", st.PURCHASED, self._record()))
        # 状态不对 / 记录缺失 / 任一步没完成 → 不许说买了
        self.assertFalse(st.claim_allowed("home", st.PURCHASED, None))
        self.assertFalse(st.claim_allowed("home", st.TRYON, self._record()))
        for step in st.PURCHASE_STEPS:
            broken = self._record(**{step: False})
            self.assertFalse(st.claim_allowed("home", st.PURCHASED, broken), step)

    def test_unknown_kind_never_claims(self):
        self.assertFalse(st.claim_allowed("", st.PURCHASED, self._record()))

    def test_purchase_complete_copies(self):
        self.assertTrue(st.purchase_complete(self._record()))
        self.assertFalse(st.purchase_complete(None))
        self.assertFalse(st.purchase_complete({"state": st.APPROVED,
                                               "steps": {s: True for s in st.PURCHASE_STEPS}}))


class HumanTextTests(unittest.TestCase):
    def test_item_label_dedupes_color(self):
        self.assertEqual("米白色针织开衫", st.item_label("米白色针织开衫", "米白色"))
        self.assertEqual("米白色针织开衫", st.item_label("针织开衫", "米白色"))
        self.assertEqual("针织开衫", st.item_label("针织开衫", ""))
        self.assertEqual("衣服", st.item_label("", ""))
        self.assertEqual("红色", st.item_label("", "红色"))

    def test_money_words_never_exact(self):
        cases = {98500: "快一千了", 64900: "小几百了", 407200: "快两千了",
                 5000: "几十块", 15000: "一百来块", 900000: "好几千了"}
        for cents, expected in cases.items():
            value = st.money_words(cents)
            self.assertEqual(expected, value)
            self.assertFalse(any(ch.isdigit() for ch in value), value)

    def test_cash_phrase_is_human(self):
        self.assertIn("够", st.cash_phrase(500000, 65000))
        self.assertIn("不太够", st.cash_phrase(30000, 65000))
        self.assertFalse(any(ch.isdigit() for ch in st.cash_phrase(407200, 98500)))


class RecordTests(unittest.TestCase):
    def test_pending_starts_interest(self):
        pending = st.pending_record("针织开衫", "米白色", "coat", "day", 65000, 500000,
                                    100.0, 200.0, scene={"scene_id": "e1", "place": "商场"})
        self.assertEqual(st.INTEREST, pending["state"])
        self.assertEqual(65000, pending["price"])
        self.assertEqual("e1", pending["scene_id"])

    def test_tryon_event_never_carries_purchase_claim(self):
        event = st.tryon_event({"name": "针织开衫", "color": "米白色", "price": 65000},
                               due=1.0, cash=435000)
        self.assertEqual("tryon", event["kind"])
        self.assertEqual(st.TRYON, event["state"])
        self.assertNotIn("purchase_id", event)

    def test_home_event_is_purchased_and_keyed(self):
        event = st.home_event({"name": "针织开衫", "color": "米白色", "price": 65000},
                              due=1.0, cash=435000, purchase_id="shop:purchase:1")
        self.assertEqual("home", event["kind"])
        self.assertEqual(st.PURCHASED, event["state"])
        self.assertEqual("shop:purchase:1", event["purchase_id"])

    def test_purchase_record_tracks_three_steps(self):
        record = st.purchase_record({"name": "针织开衫", "color": "米白色",
                                     "price": 65000}, "shop:purchase:1", 100.0)
        self.assertEqual(st.APPROVED, record["state"])
        self.assertEqual(set(st.PURCHASE_STEPS), set(record["steps"]))
        self.assertFalse(any(record["steps"].values()))
        self.assertFalse(st.purchase_complete(record))


if __name__ == "__main__":
    unittest.main()
