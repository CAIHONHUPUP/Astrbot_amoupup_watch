# -*- coding: utf-8 -*-
"""基金实时估值：天天基金 FundValuationLast 批量接口。

- 替代已下线的 fundgz 单只 JSONP。
- 一次最多 50 只，返回 FCODE/SHORTNAME/GSZ/GSZZL/GZTIME/NAV/PDATE。
- 10 秒超时；网络/JSON/字段异常逐批跳过，绝不让 Tick 崩。
- GSZ（今日估值）为空 → tradeable=False，只保留 NAV 供展示，绝不用旧净值下单。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

try:  # 包模式
    from ..guard import isolated
except ImportError:  # 直接按文件加载 / 单测
    from guard import isolated

VALUATION_URL = "https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast"
FIELDS = "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE"
TIMEOUT = 10
BATCH_SIZE = 50
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Referer": "https://fund.eastmoney.com/"}


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_valuation(payload):
    """解析 FundValuationLast 响应；外壳/JSON 异常返回空 dict。"""
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict) or not data.get("success"):
        return {}
    items = data.get("data")
    if not isinstance(items, list):
        return {}
    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("FCODE") or "").strip()
        if not code:
            continue
        name = str(item.get("SHORTNAME") or "").strip()
        nav = _num(item.get("NAV")) or 0.0
        gsz = _num(item.get("GSZ")) or 0.0
        change = _num(item.get("GSZZL"))
        if change is None and gsz > 0 and nav > 0:
            change = (gsz / nav - 1.0) * 100.0
        tradeable = bool(gsz > 0)
        pdate = str(item.get("PDATE") or "").strip()
        out[code] = {
            "code": code,
            "name": name,
            "nav": nav,
            "estimate": gsz,
            "change_pct": change if change is not None else 0.0,
            "dwjz": nav,
            "gsz": gsz,
            "gszzl": change if change is not None else 0.0,
            "time": str(item.get("GZTIME") or pdate).strip(),
            "nav_date": pdate,
            "source": "fundcomapi",
            "tradeable": tradeable,
            "stale": not tradeable,
        }
    return out


def _fetch_batch(codes):
    params = urllib.parse.urlencode({"FCODES": ",".join(codes), "FIELDS": FIELDS})
    request = urllib.request.Request(VALUATION_URL + "?" + params, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def fetch_fund_quotes(codes):
    """批量抓取；单批失败只跳过该批，返回 {code: quote} 绝不抛异常。"""
    wanted = list(dict.fromkeys(str(c).strip() for c in (codes or []) if str(c).strip()))
    if not wanted:
        return {}
    out = {}
    for start in range(0, len(wanted), BATCH_SIZE):
        chunk = wanted[start:start + BATCH_SIZE]
        try:
            rows = parse_valuation(_fetch_batch(chunk))
        except Exception as exc:
            isolated("finance.fund.valuation", exc)
            continue
        wanted_set = set(chunk)
        out.update({code: row for code, row in rows.items() if code in wanted_set})
    return out


def get_fund_estimate(code):
    """单只基金；没有今日估值返回 None（绝不拿旧净值充数）。"""
    code = str(code or "").strip()
    if not code:
        return None
    row = fetch_fund_quotes([code]).get(code)
    if row and row.get("tradeable"):
        return row
    return None


# 兼容旧调用名：现在语义严格为「有今日估值才返回」
fetch_fund_quote = get_fund_estimate
