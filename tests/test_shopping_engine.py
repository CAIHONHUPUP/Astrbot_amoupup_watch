# -*- coding: utf-8 -*-
"""逛街：余额档位、交通奶茶午饭、只逛不买，以及 task-24 的购买状态机/事务。

任务背景（work/audit/media_facts.md P1）：
- 11-21 声称"买了米白开衫 649"，但账本/衣柜都没有这笔；
- 11-22 又回到"985 买不买"，watch 预先排的 home 事件到点还会再说一次"买了"；
- 颜色+名字拼成"米白色米白色"，精确价格 985/4072 从 shopping_prompt 进了聊天。
所以这里断言：只有 purchased 才说买了；账本+衣橱+事件三步同事务；颜色去重；只说人话。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import shopping_engine as shop  # noqa: E402
from services import shopping_state as state  # noqa: E402


try:  # Phase 15：meta 桩改用 tests/fixtures.py 的共享实现（断言不变）
    from tests.fixtures import FakeRepo  # noqa: E402
except ImportError:
    from fixtures import FakeRepo  # noqa: E402


class FakeWallet:
    """钱包桩：只记 credit（退款用）与流水，_wallet_has_idem 靠 ledger() 查。"""

    def __init__(self):
        self.rows = []
        self.idems = set()

    def credit(self, amount, reason="", idem="", ref="", account="cash"):
        key = str(idem or "")
        if not key or key in self.idems:
            return False
        self.idems.add(key)
        self.rows.append({"idem": key, "delta_cents": int(amount),
                          "reason": str(reason), "ref": str(ref)})
        return True

    def ledger(self, limit=200):
        return list(reversed(self.rows))[:int(limit)]


class FakeAmo:
    def __init__(self, reason="周末逛街"):
        self.reason = reason
        self.items = []
        self.pushed = []
        self.shop_ok = True
        self.engage_ok = True
        self.wallet = FakeWallet()
        self.closet_ok = True
        self.purchases = []

    def shopping_trigger(self, now=None):
        return self.reason

    # 2026-11-22 唯一在场策略（陪伴侧 amo_presence.can_shop / can_proactive）
    def can_shop(self):
        return self.shop_ok

    def can_engage(self, kind=""):
        self.last_engage_kind = kind
        return self.engage_ok

    def add_closet_item(self, item):
        if not self.closet_ok:
            return {"ok": False, "msg": "断码了"}
        self.items.append(dict(item))
        return {"ok": True, "item": item}

    def note_purchase(self, name, price_cents):
        self.purchases.append((name, int(price_cents)))

    async def push_to_owner(self, text):
        self.pushed.append(text)
        return True

    def note_shopping_pending(self, line, until_ts=0, scene_id="", name="", color="", price_cents=0):
        # task-27：能陪侧已扩签名并落结构化字段（价格/颜色/写入时余额）
        self.pending_line = line
        self.pending_meta = {"scene_id": scene_id, "name": name, "color": color,
                             "price_cents": price_cents}

    def note_recent_topic(self, text, until_ts=0, key="", ttl_hours=36):
        self.topic_line = text


class FakeFinance:
    def __init__(self, amo, cash=500000):
        self.amo = amo
        self.cash = cash
        self.spent = []
        self.diaries = []
        self.memories = []
        self.expense_ok = True
        self.furniture = []
        self.idems = set()

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.cash

    async def expense(self, amount, reason, idem, necessary=False):
        key = str(idem or "")
        if not self.expense_ok:
            return {"ok": False, "reason": "wallet_error"}
        if key and key in self.idems:
            return {"ok": False, "reason": "duplicate"}
        if int(amount) > self.cash:
            return {"ok": False, "reason": "余额不足"}
        if key:
            self.idems.add(key)
            # 和真钱包一致：扣款也进流水，崩溃重放时 _wallet_has_idem 能查到
            self.amo.wallet.rows.append({"idem": key, "delta_cents": -int(amount),
                                         "reason": str(reason)})
        self.cash -= int(amount)
        self.spent.append((int(amount), str(reason)))
        return {"ok": True}

    def small_diary(self, text):
        self.diaries.append(text)

    def furniture_bought(self, name):
        self.furniture.append(str(name))
        return True

    async def permanent_memory(self, text, idem):
        self.memories.append(text)


class FakePlugin:
    def __init__(self, cash=500000, reason="周末逛街"):
        self.finance_repo = FakeRepo()
        self.amo = FakeAmo(reason)
        self.finance = FakeFinance(self.amo, cash)


def _purchase_row(purchase_id, name="毛呢大衣", color="", price=60000, steps=None,
                  state_value=state.PURCHASED):
    return {"id": purchase_id, "state": state_value, "label": state.item_label(name, color),
            "name": name, "color": color, "price": int(price), "created": 1.0,
            "steps": dict(steps or {step: True for step in state.PURCHASE_STEPS}),
            "reason": ""}


class ShoppingTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from services import social_ai

        async def fake_phrase(plugin, facts, style=""):
            return "今天逛街发生的事，想跟你说说"

        self._patcher = mock.patch.object(social_ai, "phrase", fake_phrase)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def run_shop(self, cash=500000, reason="周末逛街"):
        plugin = FakePlugin(cash, reason)
        out = asyncio.run(shop.tick(plugin, dt.datetime(2026, 11, 14, 14, 0)))
        return plugin, out

    # ---------------------------------------------------------------- 基础
    def test_rich_buys_mall_item(self):
        from unittest import mock
        from services import social_ai

        async def fake_phrase(plugin, facts, style=""):
            return "这件挺好看的，我能买吗"

        with mock.patch.object(social_ai, "phrase", fake_phrase):
            plugin, out = self.run_shop()
        self.assertIsNotNone(out)
        self.assertEqual(plugin.amo.items, [])
        pending = json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}")
        self.assertEqual(state.TRYON, pending.get("state"))
        self.assertIn("这件挺好看的，我能买吗", plugin.amo.pushed)
        # 追问口径：价格/余额只说人话，精确数字（985/4072）绝不进 shopping_prompt
        self.assertIn("价格", plugin.amo.pending_line)
        self.assertIn("余额", plugin.amo.pending_line)
        self.assertFalse(any(ch.isdigit() for ch in plugin.amo.pending_line),
                         plugin.amo.pending_line)
        self.assertFalse(any(ch.isdigit() for ch in plugin.amo.topic_line),
                         plugin.amo.topic_line)
        # tick 只排 tryon 事件，不再预先排"到点说买了"的 home 事件
        events = json.loads(plugin.finance_repo.meta_get("shop:events") or "[]")
        self.assertTrue(events)
        self.assertEqual(["tryon"], [event.get("kind") for event in events])
        self.assertTrue(int(events[0].get("price") or 0) > 100, "event price 必须是分")
        # 颜色去重：不能出现"米白色米白色"
        for text in plugin.amo.pushed + [plugin.amo.topic_line, plugin.amo.pending_line]:
            self.assertNotIn("米白色米白色", text)

    def test_middle_buys_cheap(self):
        plugin, out = self.run_shop(cash=100000)
        self.assertEqual(plugin.amo.items, [])
        self.assertTrue(plugin.finance_repo.meta_get("shop:pending"))

    def test_low_balance_window_shopping(self):
        plugin, out = self.run_shop(cash=15000)
        self.assertIsNotNone(out)
        self.assertIsNone(out["item"])
        self.assertFalse(plugin.amo.items)
        self.assertTrue(any("只逛不买" in d for d in plugin.finance.diaries))

    def test_tryon_and_home_proactive(self):
        plugin, _ = self.run_shop()
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 31)))
        self.assertTrue(plugin.amo.pushed)
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 17, 1)))
        self.assertGreaterEqual(len(plugin.amo.pushed), 2)

    def test_no_trigger_no_shopping(self):
        plugin = FakePlugin(reason="")
        out = asyncio.run(shop.tick(plugin, dt.datetime(2026, 11, 14, 14, 0)))
        self.assertIsNone(out)

    def test_home_does_not_trigger_shopping(self):
        """2026-11-22 真实飞书：日程在家休息，绝不能发「在商场试开衫」。"""
        plugin = FakePlugin()
        plugin.amo.shop_ok = False   # 陪伴侧 can_shop 为 False（人在家）
        out = asyncio.run(shop.tick(plugin, dt.datetime(2026, 11, 14, 14, 0)))
        self.assertIsNone(out)
        self.assertEqual(plugin.amo.pushed, [])
        self.assertEqual(plugin.finance.spent, [])
        self.assertFalse(plugin.finance_repo.meta_get("shop:pending"))

    def test_silent_user_blocks_nudge_and_tryon(self):
        """沉默 6 小时：不追问「等了十五分钟」，也不发「在商场试」类。"""
        plugin, _ = self.run_shop()
        plugin.amo.pushed.clear()     # 只统计 flush 阶段的推送
        plugin.amo.engage_ok = False  # 陪伴侧 can_engage 为 False（用户沉默超 6 小时）
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 31)))
        self.assertEqual(plugin.amo.pushed, [])
        self.assertFalse(plugin.finance_repo.meta_get("shop:pending"))

    # ---------------------------------------------------------------- 未买不许说买了
    def test_timeout_returns_to_rack(self):
        plugin, _ = self.run_shop()
        # 第一次到点：还在犹豫（pending 保留，tryon 事件还能问"好不好看"）
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 16, 0)))
        self.assertTrue(plugin.amo.pushed)
        pending = json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}")
        self.assertEqual(state.TRYON, pending.get("state"))
        self.assertEqual(1, pending.get("nudges"))
        # 第二次到点（tick 后 180 分钟）仍没回音：真的放回衣架，state 终结
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 17, 30)))
        self.assertFalse(json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}"))

    def test_home_event_without_purchase_is_never_bought(self):
        """真实 11-22 的 home 事件没有购买记录：到点也绝不能说「买了」。"""
        plugin = FakePlugin(cash=100000)
        plugin.finance_repo.meta_set("shop:events", json.dumps([
            {"due": 1, "kind": "home", "name": "米白色针织开衫",
             "color": "米白色", "price": 98500, "cash": 407200}]))
        captured = []

        async def fake_phrase(target, facts, style=""):
            captured.append(facts)
            return "回家啦，买了一件开衫"

        from unittest import mock
        from services import social_ai
        with mock.patch.object(social_ai, "phrase", fake_phrase):
            asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 0)))
        self.assertEqual([], captured, "没有购买事务记录时不允许生成「买了」文案")
        self.assertEqual([], plugin.amo.pushed)

    def test_legacy_home_event_without_state_is_dropped(self):
        """旧版本排的 home 事件（无 state/purchase_id）一律作废，不许补说买了。"""
        plugin = FakePlugin(cash=100000)
        plugin.finance_repo.meta_set("shop:events", json.dumps([
            {"due": 1, "kind": "home", "name": "毛呢大衣", "price": 60000, "cash": 1500}]))
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 0)))
        self.assertEqual([], plugin.amo.pushed)

    def test_home_event_with_purchase_journal_says_bought(self):
        """有完整购买事务（账本+衣橱+事件）才允许回家炫耀「买了」。"""
        plugin = FakePlugin(cash=100000)
        pid = "shop:purchase:123"
        plugin.finance_repo.meta_set("shop:purchases", json.dumps([_purchase_row(pid)]))
        plugin.finance_repo.meta_set("shop:events", json.dumps([
            {"due": 1, "kind": "home", "state": state.PURCHASED,
             "purchase_id": pid, "name": "毛呢大衣", "price": 60000, "cash": 1500}]))
        captured = []

        async def fake_phrase(target, facts, style=""):
            captured.append(facts)
            return "回家啦，买了一件大衣"

        from unittest import mock
        from services import social_ai
        with mock.patch.object(social_ai, "phrase", fake_phrase):
            asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 17, 0)))
        self.assertTrue(captured)
        self.assertIn("买了", captured[0])
        self.assertIn("毛呢大衣", captured[0])
        self.assertTrue(plugin.amo.pushed)

    def test_guilt_when_big_spend_and_low_cash(self):
        plugin = FakePlugin(cash=100000)
        pid = "shop:purchase:456"
        plugin.finance_repo.meta_set("shop:purchases", json.dumps([_purchase_row(
            pid, name="毛呢大衣", price=60000)]))
        plugin.finance_repo.meta_set("shop:events", json.dumps([
            {"due": 1, "kind": "home", "state": state.PURCHASED, "purchase_id": pid,
             "name": "毛呢大衣", "price": 60000, "cash": 1500}]))
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 0)))
        self.assertTrue(plugin.amo.pushed)

    def test_home_event_facts_are_colloquial(self):
        """真实飞书「985/卡里4072」：主动推送只说人话，不报精确数字。"""
        from unittest import mock
        from services import social_ai

        plugin = FakePlugin(cash=100000)
        pid = "shop:purchase:789"
        plugin.finance_repo.meta_set("shop:purchases", json.dumps([_purchase_row(
            pid, name="米白色针织开衫", color="米白色", price=98500)]))
        plugin.finance_repo.meta_set("shop:events", json.dumps([
            {"due": 1, "kind": "home", "state": state.PURCHASED, "purchase_id": pid,
             "name": "米白色针织开衫", "color": "米白色",
             "price": 98500, "cash": 407200}]))
        captured = []

        async def fake_phrase(target, facts, style=""):
            captured.append(facts)
            return "回家啦，买了一件开衫"

        with mock.patch.object(social_ai, "phrase", fake_phrase):
            asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 0)))
        self.assertTrue(captured)
        self.assertNotIn("985", captured[0])
        self.assertNotIn("4072", captured[0])
        self.assertIn("开衫", captured[0])
        self.assertNotIn("米白色米白色", captured[0])

    def test_tryon_push_never_says_bought(self):
        plugin, _ = self.run_shop()
        plugin.amo.pushed.clear()
        captured = []

        async def fake_phrase(target, facts, style=""):
            captured.append(facts)
            return "在商场试衣服，你猜好不好看"

        from unittest import mock
        from services import social_ai
        with mock.patch.object(social_ai, "phrase", fake_phrase):
            asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 31)))
        self.assertTrue(captured)
        # 先 15 分钟追问（还在犹豫），再是 90 分钟的 tryon 事件；两句都不许说"买了"
        self.assertTrue(any("试了" in facts for facts in captured), captured)
        for facts in captured:
            self.assertNotIn("买了", facts)
            self.assertNotIn("花了", facts)

    # ---------------------------------------------------------------- 购买事务
    def test_accept_reply_settles(self):
        plugin, _ = self.run_shop()
        out = asyncio.run(shop.handle_reply(plugin, "买！好看"))
        self.assertTrue(out["bought"])
        self.assertEqual(len(plugin.amo.items), 1)
        self.assertTrue(any("买衣服" in r for _, r in plugin.finance.spent))
        # 事务三步都落盘：账本、衣橱、home 事件；pending 清空
        purchase_id = out["purchase_id"]
        rows = {row["id"]: row for row in json.loads(
            plugin.finance_repo.meta_get("shop:purchases") or "[]")}
        self.assertIn(purchase_id, rows)
        self.assertEqual(state.PURCHASED, rows[purchase_id]["state"])
        self.assertTrue(all(rows[purchase_id]["steps"].values()))
        self.assertFalse(json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}"))
        events = json.loads(plugin.finance_repo.meta_get("shop:events") or "[]")
        homes = [event for event in events if event.get("kind") == "home"]
        self.assertEqual(1, len(homes))
        self.assertEqual(purchase_id, homes[0]["purchase_id"])
        self.assertEqual(state.PURCHASED, homes[0]["state"])
        self.assertIn("买啦", plugin.amo.pushed[-1])

    def test_reject_reply_clears(self):
        plugin, _ = self.run_shop()
        out = asyncio.run(shop.handle_reply(plugin, "太贵了，不好看"))
        self.assertFalse(out["bought"])
        self.assertEqual(plugin.amo.items, [])
        self.assertFalse(plugin.finance_repo.meta_get("shop:pending"))
        self.assertFalse(any("买啦" in text for text in plugin.amo.pushed))

    def test_wardrobe_failure_refunds_and_never_says_bought(self):
        plugin, _ = self.run_shop()
        plugin.amo.closet_ok = False
        out = asyncio.run(shop.handle_reply(plugin, "买！好看"))
        self.assertFalse(out["bought"])
        self.assertEqual([], plugin.amo.items)
        # 钱原路退回（refund: 前缀，不进消费）
        refunds = [row for row in plugin.amo.wallet.rows
                   if str(row.get("idem") or "").startswith("refund:")]
        self.assertEqual(1, len(refunds))
        self.assertEqual(_shop_buy_amount(plugin), refunds[0]["delta_cents"])
        # 不许说「买啦/买了」，只能统一失败话术
        self.assertFalse(any("买啦" in text or "买了" in text for text in plugin.amo.pushed))
        self.assertEqual(shop.FAILURE_LINES["wardrobe"], plugin.amo.pushed[-1])
        rows = json.loads(plugin.finance_repo.meta_get("shop:purchases") or "[]")
        self.assertEqual(state.FAILED, rows[-1]["state"])
        self.assertFalse(json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}"))
        # 衣橱放最后一步：失败时 home 事件已撤掉，不能留下"买了"的演出
        events = json.loads(plugin.finance_repo.meta_get("shop:events") or "[]")
        self.assertFalse([event for event in events
                          if event.get("kind") == "home" and event.get("purchase_id")])

    def test_expense_failure_never_adds_wardrobe(self):
        plugin, _ = self.run_shop()
        plugin.finance.expense_ok = False
        out = asyncio.run(shop.handle_reply(plugin, "买！好看"))
        self.assertFalse(out["bought"])
        self.assertEqual([], plugin.amo.items)
        self.assertFalse(any("买啦" in text for text in plugin.amo.pushed))
        # 退回 tryon，钱没扣成功，可以再试
        pending = json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}")
        self.assertEqual(state.TRYON, pending.get("state"))

    def test_insufficient_keeps_pending(self):
        plugin, _ = self.run_shop()
        plugin.finance.cash = 1
        out = asyncio.run(shop.handle_reply(plugin, "买！好看"))
        self.assertFalse(out["bought"])
        self.assertEqual("insufficient", out["reason"])
        self.assertEqual([], plugin.amo.items)
        pending = json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}")
        self.assertEqual(state.TRYON, pending.get("state"))

    def test_double_reply_charges_once(self):
        plugin, _ = self.run_shop()
        first = asyncio.run(shop.handle_reply(plugin, "买！好看"))
        second = asyncio.run(shop.handle_reply(plugin, "买！好看"))
        self.assertTrue(first["bought"])
        # pending 已清空：第二次回复不再产生购买事实（也绝不二次扣款）
        self.assertIsNone(second)
        clothing = [row for row in plugin.finance.spent if "买衣服" in row[1]]
        self.assertEqual(1, len(clothing), "同一笔 pending 只能扣一次钱")
        self.assertEqual(1, len(plugin.amo.items))

    def test_recovery_completes_half_transaction(self):
        """崩溃在"已扣款、未入库"：下次 flush 收尾，不重复扣钱。"""
        plugin = FakePlugin(cash=100000)
        pending = state.pending_record("毛呢大衣", "", "coat", "day", 60000, 100000,
                                       1.0, 2.0)
        pending["state"] = state.APPROVED
        pending["created"] = 1.0
        plugin.finance_repo.meta_set("shop:pending", json.dumps(pending))
        idem = shop._purchase_idem(pending)
        row = _purchase_row(idem, name="毛呢大衣", price=60000,
                            steps={"debited": True, "event": True, "wardrobe": False},
                            state_value=state.APPROVED)
        plugin.finance_repo.meta_set("shop:purchases", json.dumps([row]))
        plugin.finance_repo.meta_set("shop:events", json.dumps([
            {"due": 1, "kind": "home", "state": state.PURCHASED, "purchase_id": idem,
             "name": "毛呢大衣", "price": 60000, "cash": 40000}]))
        # 钱包流水里已经有这笔扣款（崩溃前扣的）
        plugin.amo.wallet.rows.append({"idem": idem, "delta_cents": -60000,
                                       "reason": "买衣服"})
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 0)))
        self.assertEqual(1, len(plugin.amo.items), "收尾应补上衣橱入库")
        clothing = [row for row in plugin.finance.spent if "买衣服" in row[1]]
        self.assertEqual(0, len(clothing), "已经扣过款，收尾不能再扣")
        rows = {entry["id"]: entry for entry in json.loads(
            plugin.finance_repo.meta_get("shop:purchases") or "[]")}
        self.assertEqual(state.PURCHASED, rows[idem]["state"])
        self.assertTrue(all(rows[idem]["steps"].values()))

    def test_refund_retry_after_failed_refund(self):
        """衣橱失败但退款当时没退成：下次 flush 必须继续原路退回。"""
        plugin = FakePlugin(cash=100000)
        idem = "shop:purchase:9"
        row = _purchase_row(idem, name="毛呢大衣", price=60000,
                            steps={"debited": True, "event": False, "wardrobe": False},
                            state_value=state.FAILED)
        row["reason"] = "wardrobe_failed"
        plugin.finance_repo.meta_set("shop:purchases", json.dumps([row]))
        plugin.amo.wallet.rows.append({"idem": idem, "delta_cents": -60000,
                                       "reason": "买衣服"})
        asyncio.run(shop.flush(plugin, dt.datetime(2026, 11, 14, 15, 0)))
        refunds = [item for item in plugin.amo.wallet.rows
                   if str(item.get("idem") or "").startswith("refund:")]
        self.assertEqual(1, len(refunds))
        rows = json.loads(plugin.finance_repo.meta_get("shop:purchases") or "[]")
        self.assertFalse(rows[-1]["steps"]["debited"], "退款成功后不应再标记为已扣款")


def _shop_buy_amount(plugin):
    """本次 pending 的价格（分）：退款断言用，不依赖具体档位随机结果。"""
    pending = json.loads(plugin.finance_repo.meta_get("shop:pending") or "{}")
    if pending.get("price"):
        return int(pending["price"])
    rows = json.loads(plugin.finance_repo.meta_get("shop:purchases") or "[]")
    return int(rows[-1]["price"]) if rows else 0


if __name__ == "__main__":
    unittest.main()
