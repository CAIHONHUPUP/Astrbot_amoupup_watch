# -*- coding: utf-8 -*-
"""行情：腾讯免费接口 qt.gtimg.cn，每天只查两次（9:30 后 / 15:00 后）。

只查「实际持仓」的代码，不做自选池、不拉大盘。
"""
from __future__ import annotations

import re
import urllib.request

try:
    from .guard import isolated
except ImportError:  # 直接按文件加载时
    from guard import isolated

QUOTE_URL = "https://qt.gtimg.cn/q=%s"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def parse_quotes(text: str) -> dict:
    """解析 v_sh600519="1~贵州茅台~600519~1680.00~1670.00~..."。返回 {code: {name, price, prev_close}}。"""
    out = {}
    for line in str(text or "").splitlines():
        match = re.search(r'v_([A-Za-z0-9]+)="([^"]*)"', line)
        if not match:
            continue
        fields = match.group(2).split("~")
        if len(fields) < 6:
            continue
        try:
            code = fields[2].strip()
            out[code] = {
                "name": fields[1].strip(),
                "price": float(fields[3]),
                "prev_close": float(fields[4]),
            }
        except (TypeError, ValueError):
            continue
    return out


def fetch_quotes(codes):
    """同步抓取（调用方负责丢线程池）。返回 {code: quote}，失败返回空 dict。"""
    wanted = [str(c).strip() for c in (codes or []) if str(c).strip()]
    if not wanted:
        return {}
    symbols = []
    for code in wanted:
        if code.startswith(("6", "9")):
            symbols.append("sh" + code)
        elif code.startswith(("0", "3")):
            symbols.append("sz" + code)
        else:
            symbols.append("sh" + code)
    url = QUOTE_URL % ",".join(symbols)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(request, timeout=8) as response:
            text = response.read().decode("gbk", "ignore")
    except Exception as exc:
        isolated("finance.market.fetch", exc)
        return {}
    return parse_quotes(text)
