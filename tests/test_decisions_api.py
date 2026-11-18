# -*- coding: utf-8 -*-
"""面板「决定」页：只读 API + bridge 登记 + 前端静态检查（2026-11-17）。"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api.handlers import ApiMixin  # noqa: E402
from api.routes import ROUTES  # noqa: E402
from core.amo_bridge import _CALLS, AmoBridge  # noqa: E402

PANEL = os.path.join(ROOT, "views", "守护", "index.html")


class FakeAmo:
    def __init__(self, rows=None, boom=False):
        self.rows = rows if rows is not None else []
        self.boom = boom
        self.calls = []

    def late_snack_decisions(self, days=1, limit=50):
        self.calls.append({"days": days, "limit": limit})
        if self.boom:
            raise RuntimeError("kaput")
        return self.rows


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo

    def _amo(self):
        return self.amo


class FakeRequest:
    def __init__(self, query=None):
        self.query_params = dict(query or {})


class Plugin(ApiMixin):
    def __init__(self, amo=None):
        self.finance = FakeFinance(amo) if amo is not None else None


def run(coro):
    return asyncio.run(coro)


def test_bridge_registers_late_snack_decisions():
    assert "late_snack_decisions" in _CALLS
    amo = FakeAmo([{"hour": 23}])
    result = AmoBridge(type("P", (), {})(), amo=amo).call(
        "late_snack_decisions", days=1, limit=5)
    assert result.ok, result.reason
    assert result.value == [{"hour": 23}]
    assert amo.calls == [{"days": 1, "limit": 5}]


def test_api_keeps_backend_order_and_fields():
    rows = [{"day": "2026-11-17", "hour": 23, "eat": True, "score": 71.5,
             "roll": 12, "factors": {"hunger": 43.0}, "food": "烧烤",
             "amount_cents": 1500, "block_id": "abc12345"},
            {"day": "2026-11-17", "hour": 22, "eat": False, "score": 40.0,
             "roll": 88, "factors": {}, "food": "", "amount_cents": 0,
             "block_id": ""}]
    result = run(Plugin(FakeAmo(rows))._api_decisions())
    assert result["ok"] is True
    assert result["items"] == rows


def test_api_defaults_to_today_and_fifty():
    amo = FakeAmo([])
    result = run(Plugin(amo)._api_decisions())
    assert result["ok"] is True
    assert result["items"] == []
    # W5：决定页扩展了 kinds/blocks/reconcile，旧字段契约不变
    assert result["kinds"] == [] and result["blocks"] == []
    assert result["reconcile"]["status"] == "ok"
    assert amo.calls == [{"days": 1, "limit": 50}]


def test_api_accepts_query_params_for_tests():
    amo = FakeAmo([])
    run(Plugin(amo)._api_decisions(FakeRequest({"days": "2", "limit": "10"})))
    assert amo.calls == [{"days": 2, "limit": 10}]


def test_api_amo_failure_is_friendly():
    result = run(Plugin(FakeAmo(boom=True))._api_decisions())
    assert result["ok"] is False
    assert result["items"] == []
    assert "稍后" in result["message"]


def test_api_not_ready_is_friendly():
    result = run(Plugin()._api_decisions())
    assert result["ok"] is False
    assert result["items"] == []
    assert "稍后" in result["message"]


def test_api_non_list_payload_is_empty():
    amo = FakeAmo("nonsense")
    result = run(Plugin(amo)._api_decisions())
    assert result["ok"] is True
    assert result["items"] == []
    assert result["kinds"] == [] and result["blocks"] == []


def test_route_registered_once_as_get():
    paths = [row[0] for row in ROUTES]
    assert paths.count("/decisions") == 1
    row = next(row for row in ROUTES if row[0] == "/decisions")
    assert row[1] == "_api_decisions"
    assert row[2] == ["GET"]


def _panel():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def test_nav_order_housing_decisions_status():
    body = _panel()
    housing = body.index('data-tab="housing"')
    decisions = body.index('data-tab="decisions"')
    status = body.index('data-tab="status"')
    assert housing < decisions < status


def test_decisions_panel_renderer_and_empty_state():
    body = _panel()
    assert 'id="tab-decisions"' in body
    assert "今天的决定" in body
    assert "今天还没有需要她决定的事" in body
    assert "function renderDecisions" in body
    assert "function loadDecisions" in body
    assert 'apiGet("decisions")' in body
    assert 'btn.dataset.tab === "decisions"' in body


def test_eat_and_skip_have_distinct_tags():
    body = _panel()
    assert ".decision-tag.eat" in body
    assert ".decision-tag.skip" in body
    assert '"吃了"' in body and '"忍住了"' in body
    assert "amount_cents" in body and "block_id" in body
