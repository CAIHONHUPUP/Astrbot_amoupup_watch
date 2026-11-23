# -*- coding: utf-8 -*-
"""W5 决定页扩展 + 消费对账（2026-11-17）。

只读约定：不新增写路径；决定页金额和 wallet 流水块金额对不上时必须标 pending
（前端「待对账」），不能静默。覆盖：扩展字段、对账一致/不一致、空态、异常、前端静态。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api import decisions_ext  # noqa: E402
from api.handlers import ApiMixin  # noqa: E402
from api.routes import ROUTES  # noqa: E402

PANEL = os.path.join(ROOT, "views", "守护", "index.html")


def today():
    return time.strftime("%Y-%m-%d")


def ts_at(hour, day_offset=0):
    """今天某个整点的 epoch 秒（对账按本地时区分块）。"""
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, int(hour), 0, 0, 0, 0, -1)) \
        + int(day_offset) * 86400


class FakeResult:
    def __init__(self, ok, value=None, reason="ok"):
        self.ok = ok
        self.value = value
        self.reason = reason


class FakeBridge:
    """只读 kv/ledger 桩；boom=True 模拟 amo 抖动。"""

    def __init__(self, kv=None, ledger=None, boom=False):
        self.kv = dict(kv or {})
        self.rows = list(ledger or [])
        self.boom = boom
        self.calls = []

    def call(self, method, *args, **kwargs):
        self.calls.append(method)
        if self.boom:
            return FakeResult(False, reason="failed")
        if method == "kv_get":
            key = args[0] if args else ""
            default = args[1] if len(args) > 1 else None
            return FakeResult(True, self.kv.get(key, default))
        if method == "ledger":
            return FakeResult(True, list(self.rows))
        return FakeResult(False, reason="missing")


class FakeRepo:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def events(self, limit=10):
        return self.rows[:limit]


class BoomRepo:
    def events(self, limit=10):
        raise RuntimeError("db kaput")


class FakeDb:
    """amo.db 只读面：kv_get。"""

    def __init__(self, kv=None):
        self.kv = dict(kv or {})

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)


class FakeWallet:
    """amo.wallet 只读面：ledger。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def ledger(self, limit=50):
        return list(self.rows)[:max(1, int(limit or 50))]


class FakeAmo:
    """够决定页用的 amo 面：宵夜决定 + db.kv_get + wallet.ledger（与真 amo 同形）。"""

    def __init__(self, rows=None, kv=None, ledger=None):
        self.rows = list(rows or [])
        self.db = FakeDb(kv)
        self.wallet = FakeWallet(ledger)

    def late_snack_decisions(self, days=1, limit=50):
        return list(self.rows)


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo

    def _amo(self):
        return self.amo


class Plugin(ApiMixin):
    def __init__(self, amo=None, repo=None):
        self.finance = FakeFinance(amo) if amo is not None else None
        self.finance_repo = repo


def run(coro):
    return asyncio.run(coro)


# ---------------- 对账口径（纯函数） ----------------

def test_ledger_blocks_keeps_only_expenses_and_buckets_by_hour():
    rows = [
        {"delta_cents": -1500, "ts": ts_at(22)},
        {"delta_cents": -500, "ts": ts_at(22)},
        {"delta_cents": 680000, "ts": ts_at(9)},     # 工资入账不是消费
        {"delta_cents": -1200, "ts": "bad-ts"},      # 坏时间戳不猜
        "not-a-row",
    ]
    blocks = decisions_ext.ledger_blocks(rows)
    assert len(blocks) == 1
    assert blocks[0]["day"] == today()
    assert blocks[0]["hour"] == 22
    assert blocks[0]["amount_cents"] == 2000
    assert blocks[0]["count"] == 2


def test_ledger_blocks_skips_credit_adjustment_and_non_cash():
    # 2026-11-23 线上：碗面 20 元，现金 -20 加上可取额度联动 -20，被显示成 40。
    rows = [
        {"account": "cash", "category": "吃的", "delta_cents": -2000, "ts": ts_at(7),
         "idem": "food:2026-11-23:7"},
        {"account": "withdrawable", "category": "账户调整", "delta_cents": -2000,
         "ts": ts_at(7), "idem": "withdrawable:cap:8000:10000"},
        {"account": "savings", "category": "存款", "delta_cents": -5000, "ts": ts_at(7),
         "idem": "save:2026-11-23"},
    ]
    blocks = decisions_ext.ledger_blocks(rows)
    assert len(blocks) == 1
    assert blocks[0]["amount_cents"] == 2000
    assert blocks[0]["count"] == 1


def test_reconcile_ok_when_decision_matches_block():
    items = [{"day": today(), "hour": 22, "amount_cents": 1500}]
    blocks = [{"day": today(), "hour": 22, "amount_cents": 1500, "count": 1}]
    rows, summary = decisions_ext.reconcile(items, blocks, day=today())
    assert summary["status"] == "ok"
    assert summary["checked"] == 1 and summary["pending"] == 0
    assert rows[0]["status"] == "ok"
    assert rows[0]["ledger_cents"] == 1500


def test_reconcile_pending_when_amount_differs():
    items = [{"day": today(), "hour": 22, "amount_cents": 1500}]
    blocks = [{"day": today(), "hour": 22, "amount_cents": 900, "count": 1}]
    rows, summary = decisions_ext.reconcile(items, blocks, day=today())
    assert summary["status"] == "pending"
    assert summary["pending"] == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["decision_cents"] == 1500
    assert rows[0]["ledger_cents"] == 900


def test_reconcile_pending_when_ledger_has_no_block():
    items = [{"day": today(), "hour": 23, "amount_cents": 1500}]
    rows, summary = decisions_ext.reconcile(items, [], day=today())
    assert summary["status"] == "pending"
    assert rows[0]["status"] == "pending"
    assert rows[0]["ledger_cents"] == 0


def test_reconcile_skips_zero_amount_decisions():
    items = [{"day": today(), "hour": 23, "amount_cents": 0}]
    rows, summary = decisions_ext.reconcile(items, [], day=today())
    assert rows[0]["status"] == decisions_ext.STATUS_NA
    assert summary["checked"] == 0
    assert summary["status"] == "ok"


def test_blocks_for_day_filters_other_days():
    blocks = decisions_ext.ledger_blocks([
        {"delta_cents": -1000, "ts": ts_at(10)},
        {"delta_cents": -2000, "ts": ts_at(10, day_offset=1)},
    ])
    only_today = decisions_ext.blocks_for_day(blocks, today())
    assert len(only_today) == 1
    assert only_today[0]["amount_cents"] == 1000


# ---------------- 扩展字段（约会/社交/购物/看病） ----------------

def test_collect_extra_reads_date_shopping_and_sick_kv():
    bridge = FakeBridge(kv={
        "last_date_event": {"activity": "逛公园", "day": today(), "ended_ts": ts_at(18)},
        "last_purchase": {"name": "新外套", "price": 12300, "ts": ts_at(15)},
        "sick_decision:" + today(): {"action": "take_sick_leave", "reason": "发烧"},
    })
    rows = decisions_ext.collect_extra(bridge, None, today())
    kinds = {row["kind"] for row in rows}
    assert kinds == {"date", "shopping", "sick"}
    shopping = next(row for row in rows if row["kind"] == "shopping")
    assert shopping["title"] == "新外套"
    assert shopping["amount_cents"] == 12300
    assert shopping["time"] == "15:00"
    assert shopping["source"] == "kv:last_purchase"
    date = next(row for row in rows if row["kind"] == "date")
    assert date["title"] == "逛公园"
    sick = next(row for row in rows if row["kind"] == "sick")
    assert sick["title"] == "请病假"
    assert sick["detail"] == "发烧"


def test_collect_extra_social_keeps_today_social_kinds_only():
    repo = FakeRepo([
        {"kind": "social:accept", "day": today(), "created_at": ts_at(17),
         "payload": {"npc": "小林", "text": "一起吃饭"}},
        {"kind": "social:invite", "day": today(), "created_at": ts_at(12),
         "payload": {"npc": "阿杰", "amount_cents": 8800}},
        {"kind": "finance:spend", "day": today(), "created_at": ts_at(12), "payload": {}},
        {"kind": "social:reject", "day": "2026-11-10", "created_at": ts_at(12),
         "payload": {"npc": "老同学"}},
    ])
    rows = decisions_ext.collect_extra(FakeBridge(), repo, today())
    social = [row for row in rows if row["kind"] == "social"]
    assert len(social) == 2
    assert {row["title"] for row in social} == {"答应了邀约", "收到邀约"}
    assert any(row["amount_cents"] == 8800 for row in social)


def test_collect_extra_tolerates_broken_sources():
    rows = decisions_ext.collect_extra(FakeBridge(boom=True), BoomRepo(), today())
    assert rows == []


# ---------------- 接口层 ----------------

def test_api_decisions_returns_extended_fields():
    rows = [{"day": today(), "hour": 22, "eat": True, "amount_cents": 1500,
             "block_id": "abc12345", "food": "烧烤", "factors": {"hunger": 43.0}}]
    amo = FakeAmo(rows=rows, kv={"last_purchase": {"name": "奶茶", "price": 1800,
                                                   "ts": ts_at(15)}},
                  ledger=[{"delta_cents": -1500, "ts": ts_at(22)}])
    result = run(Plugin(amo)._api_decisions())
    assert result["ok"] is True
    assert result["items"] == rows                       # 宵夜字段/顺序不变
    assert result["day"] == today()
    assert [row["kind"] for row in result["kinds"]] == ["shopping"]
    assert result["blocks"][0]["amount_cents"] == 1500
    assert result["reconcile"]["checked"] == 1
    assert result["reconcile"]["items"][0]["status"] == "ok"


def test_api_decisions_marks_mismatch_pending():
    rows = [{"day": today(), "hour": 22, "eat": True, "amount_cents": 1500}]
    amo = FakeAmo(rows=rows, ledger=[{"delta_cents": -900, "ts": ts_at(22)}])
    result = run(Plugin(amo)._api_decisions())
    assert result["ok"] is True
    assert result["reconcile"]["status"] == "pending"
    assert result["reconcile"]["pending"] == 1
    assert result["reconcile"]["items"][0]["status"] == "pending"


def test_api_decisions_empty_day_is_stable():
    result = run(Plugin(FakeAmo())._api_decisions())
    assert result["ok"] is True
    assert result["items"] == [] and result["kinds"] == [] and result["blocks"] == []
    assert result["reconcile"]["status"] == "ok"


def test_api_decisions_not_ready_keeps_extension_keys():
    result = run(Plugin()._api_decisions())
    assert result["ok"] is False
    assert "稍后" in result["message"]
    assert result["kinds"] == [] and result["blocks"] == []
    assert result["reconcile"]["status"] == "ok"


def test_decisions_route_still_get_only():
    paths = [row[0] for row in ROUTES]
    assert paths.count("/decisions") == 1
    row = next(row for row in ROUTES if row[0] == "/decisions")
    assert row[2] == ["GET"]


# ---------------- 前端静态 ----------------

def _panel():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def test_panel_has_reconcile_and_extension_sections():
    body = _panel()
    assert "待对账" in body
    assert 'id="decisionRecon"' in body
    assert 'id="decisionKinds"' in body
    assert 'id="decisionBlocks"' in body
    assert "function renderDecisionRecon" in body
    assert "function renderDecisionKinds" in body
    assert "function decisionPending" in body
    assert "今天各时段消费" in body
    assert "约会 · 社交 · 购物 · 看病" in body
    assert "apiGet(\"decisions\")" in body


def test_panel_empty_state_still_present():
    body = _panel()
    assert "今天还没有需要她决定的事" in body
    assert "今天没有宵夜决定" in body
