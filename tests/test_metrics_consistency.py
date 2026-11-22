# -*- coding: utf-8 -*-
"""面板口径一致性（task-27）：守护首页 / 生活卡 / 分类小卡只能有一个“本月支出”。

生产事故：首页 ¥473.00（watch.expenses 只含小额日常）、生活卡 ¥16,016.45
（amo wallet ledger 按 reason 前缀排除，把定期 10,000/基金 500 当支出）。
修复后两个入口都走 services.metrics.month_consumption()：首选能陪
metrics_snapshot()（唯一计算函数），旧版能陪时按同一口径本地重算。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import metrics  # noqa: E402
from services.dashboard import dashboard_text  # noqa: E402
from ui.cards import CardMixin  # noqa: E402

TODAY = dt.date(2026, 11, 18)


def _row(idem, delta, reason, category="其他", ts=None, account="cash"):
    stamp = ts if ts is not None else dt.datetime(2026, 11, 15, 12, 0).timestamp()
    return {"idem": idem, "account": account, "delta_cents": delta, "reason": reason,
            "category": category, "ts": stamp}


def _ledger():
    return [
        _row("meal:1", -1500, "面条", "餐饮"),
        _row("shop:item", -134997, "买衣服：一条绿色碎花连衣裙", "生活"),
        _row("rent:2026-11", -185303, "房租", "其他"),
        _row("bank:dep", -1000000, "存款：定期3个月", "存款"),
        _row("decision:fundbuy", -50000, "buy_fund", "投资"),
        _row("social:prepaid", -20000, "下班一起吃饭（预扣，已退回）", "社交"),
        _row("cleanup:refund:1", 20000, "退款：预扣已退回", "退款"),
    ]


EXPECTED_CENTS = 1500 + 134997


class _Amo:
    def __init__(self, snapshot=None, ledger=None):
        self._snapshot = snapshot
        self._ledger = ledger or _ledger()

    def metrics_snapshot(self, *args, **kwargs):
        if self._snapshot is None:
            raise AttributeError("no metrics")
        return dict(self._snapshot)

    def wallet(self):
        return None

    def life_state_snapshot(self, *args, **kwargs):
        return {}


class _AmoLegacy:
    """旧版能陪：没有 metrics_snapshot，只有钱包流水。"""

    def __init__(self, ledger=None):
        self._ledger = ledger or _ledger()

    class _Wallet:
        def __init__(self, rows):
            self.rows = rows

        def ledger(self, limit=200):
            return list(self.rows)

    @property
    def wallet(self):
        return self._Wallet(self._ledger)


class _ContractAmo:
    """新能陪：metrics_snapshot 返回契约快照。"""

    def __init__(self, cents=EXPECTED_CENTS, month="2026-11"):
        self.cents = cents
        self.month = month

    def metrics_snapshot(self, *args, **kwargs):
        return {"contract": "amo-metrics", "version": 1, "source": "forever",
                "wallet": {"balance_cents": 394725, "month": self.month,
                           "month_consumption_cents": self.cents,
                           "month_assets_change_cents": 1050000},
                "photos": {"today": 0}}

    @property
    def wallet(self):
        return None


class _Finance:
    def __init__(self, amo, ledger=None):
        self._amo_obj = amo
        self._ledger = ledger or _ledger()

    def _amo(self):
        return self._amo_obj

    def balance_cents(self):
        return 394725

    def mood(self):
        return {"word": "平和"}

    def ledger(self, limit=200):
        return list(self._ledger)


class _Repo:
    def positions(self):
        return []

    def loans(self):
        return []

    def expenses(self, period=""):
        # 旧口径：watch.expenses 合计 473 元（只覆盖小额日常）
        return [{"amount_cents": 47300}]

    def assets(self):
        return []

    def assets_of(self, kind):
        return []

    def settlements(self, month):
        return []

    def meta_keys(self, prefix):
        return []

    def meta_get(self, key, default=None):
        return default


class _Social:
    def __init__(self):
        self.repo = SimpleNamespace(npcs=lambda: [])


class _Media:
    def is_sick(self):
        return False


class _Life:
    def salary_cents(self):
        return 680000

    def summary(self, period):
        return {"base_cents": 680000, "bonus_cents": 0, "deduction_cents": 0,
                "net_cents": 680000, "items": []}

    def thrifty_until(self):
        return 0


class _Expense:
    def month_summary(self):
        # 分类小卡：watch.expenses 自己的汇总，不许再覆盖 month_expense
        return {"total_cents": 47300,
                "categories": [{"key": "food", "label": "餐饮饮食", "amount_cents": 47300}]}


class _Plugin(CardMixin):
    def __init__(self, amo):
        self.finance = _Finance(amo)
        self.finance_repo = _Repo()
        self.finance_life = _Life()
        self.finance_expense = _Expense()
        self.config = {"city": "上海"}
        self.medical = _Media()
        self.social = _Social()


class PanelConsistencyTests(unittest.TestCase):
    def test_dashboard_and_life_card_use_same_metric(self):
        plugin = _Plugin(_ContractAmo())
        card_life, _market = plugin._snapshot_life()
        self.assertEqual(card_life["month_expense"], EXPECTED_CENTS / 100.0)
        self.assertEqual(card_life["month_expense_source"], metrics.SOURCE_CONTRACT)
        text = dashboard_text(plugin)
        self.assertIn("%.2f" % (EXPECTED_CENTS / 100.0), text)
        self.assertNotIn("473.00", text)

    def test_legacy_amo_falls_back_to_same_rule(self):
        plugin = _Plugin(_AmoLegacy())
        card_life, _market = plugin._snapshot_life()
        self.assertEqual(card_life["month_expense_source"], metrics.SOURCE_LOCAL)
        self.assertEqual(round(card_life["month_expense"] * 100), EXPECTED_CENTS)
        text = dashboard_text(plugin)
        self.assertIn("%.2f" % (EXPECTED_CENTS / 100.0), text)

    def test_expense_engine_total_does_not_override_month_expense(self):
        plugin = _Plugin(_ContractAmo())
        card_life, _market = plugin._snapshot_life()
        plugin._snapshot_expense(card_life)
        self.assertEqual(card_life["month_expense"], EXPECTED_CENTS / 100.0)
        # 分类小卡单列，不能把 473 覆盖回本周消费
        self.assertEqual(card_life["expense_category_total"], 473.0)

    def test_contract_month_mismatch_falls_back(self):
        plugin = _Plugin(_ContractAmo(month="2026-10"))
        metric = metrics.month_consumption(plugin)
        self.assertEqual(metric["source"], metrics.SOURCE_LOCAL)
        self.assertEqual(metric["cents"], EXPECTED_CENTS)

    def test_life_block_accepts_explicit_consumption(self):
        from ui.report import life_block
        life = life_block([], _ledger(), 680000, TODAY, consumption_cents=12345)
        self.assertEqual(life["month_expense"], 123.45)


if __name__ == "__main__":
    unittest.main()