# -*- coding: utf-8 -*-
"""FundValuationLast：批量解析、10 秒超时、失败空结果、旧净值不可交易。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import services.fund_api as fa  # noqa: E402

PAYLOAD = {
    "success": True,
    "data": [
        {"FCODE": "005827", "SHORTNAME": "易方达蓝筹精选混合", "NAV": 1.3732,
         "GSZ": None, "GSZZL": None, "GZTIME": None, "PDATE": "2026-11-13"},
        {"FCODE": "110020", "SHORTNAME": "易方达沪深300ETF联接A", "NAV": 1.6653,
         "GSZ": 1.6528, "GSZZL": -0.75, "GZTIME": "2026-11-16 14:50", "PDATE": "2026-11-13"},
        {"FCODE": "161725", "SHORTNAME": "招商中证白酒指数(LOF)A", "NAV": 0.5802,
         "GSZ": 0.5836, "GSZZL": None, "GZTIME": "2026-11-16 14:50", "PDATE": "2026-11-13"},
    ],
}


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_parse_valuation_keeps_nav_but_marks_no_estimate_untradeable():
    rows = fa.parse_valuation(PAYLOAD)
    assert set(rows) == {"005827", "110020", "161725"}
    assert rows["005827"]["nav"] == 1.3732
    assert rows["005827"]["estimate"] == 0.0
    assert rows["005827"]["tradeable"] is False
    assert rows["110020"]["estimate"] == 1.6528
    assert rows["110020"]["change_pct"] == -0.75
    assert rows["110020"]["tradeable"] is True
    # GSZZL 缺失时用 GSZ/NAV 反推
    assert rows["161725"]["change_pct"] > 0


def test_parse_valuation_bad_payload_is_empty():
    assert fa.parse_valuation("not json") == {}
    assert fa.parse_valuation({"success": False, "data": []}) == {}
    assert fa.parse_valuation({"success": True, "data": "bad"}) == {}


def test_fetch_quotes_uses_ten_second_timeout_and_batch_url(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        seen["url"] = request.full_url
        return FakeResponse(__import__("json").dumps(PAYLOAD))

    monkeypatch.setattr(fa.urllib.request, "urlopen", fake_urlopen)
    rows = fa.fetch_fund_quotes(["005827", "110020"])
    assert set(rows) == {"005827", "110020"}
    assert seen["timeout"] == 10
    assert "FundValuationLast" in seen["url"]
    assert "FCODES=005827%2C110020" in seen["url"]


def test_fetch_quotes_network_error_returns_empty(monkeypatch):
    def boom(request, timeout=None):
        raise TimeoutError("timeout")

    monkeypatch.setattr(fa.urllib.request, "urlopen", boom)
    assert fa.fetch_fund_quotes(["005827"]) == {}


def test_get_fund_estimate_none_when_no_gsz(monkeypatch):
    monkeypatch.setattr(fa, "fetch_fund_quotes", lambda codes: fa.parse_valuation(PAYLOAD))
    assert fa.get_fund_estimate("005827") is None
    assert fa.get_fund_estimate("110020")["estimate"] == 1.6528
