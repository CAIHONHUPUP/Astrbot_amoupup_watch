# -*- coding: utf-8 -*-
"""约会误触发守卫（2026-11-20）。

现场：他说「你慢慢吃，我也去吃饭了」，旧逻辑（foods ∩ 同意词 子串匹配）同时
命中「吃饭」和「去吃」，凭空建了一场 20:39 的约会并回了「好呀，那说定啦」。
"""
import unittest

from services.date_rules import (CLAIM_WINDOWS, claim_slot, is_meal_text,
                                 looks_like_date_invite, slot_of_hhmm)


class InviteGuardTests(unittest.TestCase):
    def test_self_statement_is_not_invite(self):
        for text in ("你慢慢吃，我也去吃饭了", "我也去吃饭了", "你们去吃吧",
                     "我今天中午吃了面条", "我先吃了啊"):
            self.assertFalse(looks_like_date_invite(text), text)

    def test_together_wording_is_invite(self):
        for text in ("我们去吃火锅吧", "一起吃火锅吗", "你陪我吃火锅吧",
                     "晚上一块吃饭吗", "请你吃火锅"):
            self.assertTrue(looks_like_date_invite(text), text)

    def test_imperative_is_invite(self):
        for text in ("去吃火锅吧", "吃火锅吗", "看电影呢？"):
            self.assertTrue(looks_like_date_invite(text), text)

    def test_plain_statement_is_not_invite(self):
        for text in ("火锅真好吃", "这家烤肉不错", "好的", ""):
            self.assertFalse(looks_like_date_invite(text), text)


class MealSlotTests(unittest.TestCase):
    def test_slots(self):
        self.assertEqual({"breakfast", "lunch", "dinner"}, set(CLAIM_WINDOWS))
        self.assertEqual("dinner", slot_of_hhmm("19:09"))
        self.assertEqual("dinner", slot_of_hhmm("17:30"))
        self.assertEqual("lunch", slot_of_hhmm("11:30"))
        self.assertEqual("", slot_of_hhmm("15:30"))   # 加餐空档，不占位
        self.assertEqual("", slot_of_hhmm("22:30"))   # 夜宵，独立
        self.assertEqual("", slot_of_hhmm("bad"))

    def test_claim_slot_from_datetime(self):
        import datetime as dt
        self.assertEqual("dinner", claim_slot(dt.datetime(2026, 11, 20, 19, 9)))

    def test_meal_text(self):
        self.assertTrue(is_meal_text("柳如烟下班吃火锅"))
        self.assertTrue(is_meal_text("和闺蜜吃饭"))
        self.assertFalse(is_meal_text("和柳如烟逛街"))
        self.assertFalse(is_meal_text("和同事喝咖啡"))


if __name__ == "__main__":
    unittest.main()
