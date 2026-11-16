# -*- coding: utf-8 -*-
"""场外基金实时估值：天天基金 fundgz JSONP，失败逐只跳过。"""
from __future__ import annotations

import json
import re
import time
import urllib.request


def _one(code):
    url = "https://fund.eastmoney.com/pingzhongdata/%s.js?v=%d" % (str(code), int(time.time()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://fund.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        text = resp.read().decode("utf-8", "replace")
    name_match = re.search(r'fS_name\s*=\s*"([^"]*)"', text)
    trend_match = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
    if not trend_match:
        return None
    trend = json.loads(trend_match.group(1))
    if len(trend) < 2:
        return None
    last, prev = trend[-1], trend[-2]
    nav = float(last.get("y") or 0)
    prev_nav = float(prev.get("y") or 0)
    if nav <= 0 or prev_nav <= 0:
        return None
    stamp = last.get("x")
    try:
        day = time.strftime("%Y-%m-%d", time.localtime(float(stamp) / 1000.0))
    except Exception:
        day = ""
    return {
        "code": str(code),
        "name": name_match.group(1) if name_match else "",
        "nav": nav, "estimate": nav,
        "change_pct": (nav / prev_nav - 1.0) * 100.0,
        "time": day,
    }


def fetch_fund_quotes(codes):
    out = {}
    for code in codes or []:
        try:
            row = _one(code)
            if row and row.get("nav"):
                out[str(code)] = row
        except Exception:
            continue
    return out
