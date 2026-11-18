# -*- coding: utf-8 -*-
"""守护视图「明细流水」：全局时间倒序、完整日期、缺字段容错、前端模板。"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api.handlers import ApiMixin  # noqa: E402

VIEW = os.path.join(ROOT, "views", "守护", "index.html")


class _Wallet:
    def __init__(self, rows):
        self._rows = rows

    def ledger(self, limit):
        return list(self._rows)[:int(limit)]


class _Amo:
    def __init__(self, rows):
        self.wallet = _Wallet(rows)


class _Finance:
    def __init__(self, rows):
        self._rows = rows

    def _amo(self):
        return _Amo(self._rows)


class _Host(ApiMixin):
    def __init__(self, rows):
        self.finance = _Finance(rows)


def _api(rows):
    return asyncio.run(_Host(rows)._api_ledger(None))


def _ts(year, month, day, hour, minute):
    return dt.datetime(year, month, day, hour, minute).timestamp()


def _row(ts, name, category="餐饮", delta=-1200):
    return {"ts": ts, "category": category, "item_name": name, "delta_cents": delta}


def test_ledger_api_sorts_by_ts_desc_and_keeps_original_fields():
    rows = [
        _row(_ts(2026, 11, 16, 9, 0), "早餐"),
        _row(_ts(2026, 11, 17, 8, 30), "午餐", "购物", -2500),
        _row(_ts(2026, 11, 15, 20, 10), "夜宵"),
    ]
    data = _api(rows)
    assert data["ok"] is True
    assert [row["item_name"] for row in data["items"]] == ["午餐", "早餐", "夜宵"]
    first = data["items"][0]
    assert first["category"] == "购物"
    assert first["delta_cents"] == -2500
    assert first["ts"] == _ts(2026, 11, 17, 8, 30)


def test_ledger_api_adds_local_date_and_hh_mm():
    ts = _ts(2026, 11, 17, 9, 5)
    row = _api([_row(ts, "咖啡")])["items"][0]
    local = dt.datetime.fromtimestamp(ts)
    assert row["date"] == local.strftime("%Y-%m-%d")
    assert row["time"] == local.strftime("%H:%M")
    assert len(row["date"]) == 10 and row["date"].count("-") == 2
    assert len(row["time"]) == 5 and row["time"][2] == ":"


def test_ledger_api_orders_across_midnight_not_by_category():
    rows = [
        _row(_ts(2026, 11, 15, 23, 55), "睡前"),
        _row(_ts(2026, 11, 16, 0, 5), "跨天", "交通"),
    ]
    items = _api(rows)["items"]
    assert [row["item_name"] for row in items] == ["跨天", "睡前"]
    assert items[0]["date"] == "2026-11-16" and items[0]["time"] == "00:05"
    assert items[1]["date"] == "2026-11-15" and items[1]["time"] == "23:55"


def test_ledger_api_tolerates_missing_ts_and_old_fields():
    rows = [
        {"category": "其他", "item_name": "无时间"},
        {"ts": None, "reason": "旧数据"},
        {"ts": "bad", "item_name": "坏时间", "delta_cents": 100},
        _row(_ts(2026, 11, 17, 10, 0), "正常"),
        None,
    ]
    data = _api(rows)
    assert data["ok"] is True
    assert len(data["items"]) == 4
    assert data["items"][0]["item_name"] == "正常"
    for row in data["items"][1:]:
        assert row["date"] == "" and row["time"] == ""


def test_ledger_api_empty_ledger_and_sanitized_label():
    assert _api([]) == {"ok": True, "items": []}
    row = _api([_row(_ts(2026, 11, 17, 12, 0), "餐饮：一碗热汤面")])["items"][0]
    assert row["item_name"] == "一碗热汤面"


def test_view_uses_date_time_and_flat_rows():
    with open(VIEW, encoding="utf-8") as fh:
        html = fh.read()
    assert "ledgerStamp" in html
    assert "it.date" in html and "it.time" in html
    assert "it.category" in html
    assert "groups[cat]" not in html
    assert "Object.keys(groups)" not in html
    assert "getFullYear" in html
