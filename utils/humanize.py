# -*- coding: utf-8 -*-
"""金额与数字的人话化：社交/动态里绝不出现 ¥275.22 这种机器人数字。"""
from __future__ import annotations

import re

_CN = "零一二三四五六七八九"


def _cn(n):
    n = int(n)
    if n < 10:
        return _CN[n]
    if n < 20:
        return "十" + (_CN[n - 10] if n > 10 else "")
    if n < 100:
        return _CN[n // 10] + "十" + (_CN[n % 10] if n % 10 else "")
    return str(n)


def human_money(cents):
    """27522 分 → 「两百多」；35707 分 → 「三百多」；8000 → 「八十多」。"""
    try:
        yuan = int(round(int(cents) / 100.0))
    except (TypeError, ValueError):
        return "一些钱"
    if yuan <= 0:
        return "没花钱"
    if yuan < 10:
        return "几块"
    if yuan < 20:
        return "十几块"
    if yuan < 100:
        return "%s十%s块" % (_CN[yuan // 10], "多" if yuan % 10 else "")
    if yuan < 1000:
        hundred = yuan // 100
        rest = yuan % 100
        head = "两" if hundred == 2 else _cn(hundred)
        tail = "" if 5 <= rest < 20 else ("多" if rest else "")
        return "%s百%s" % (head, tail)
    if yuan < 10000:
        thousand = yuan // 1000
        rest = yuan % 1000
        return "%s千%s" % (_cn(thousand), "多" if rest >= 100 else "")
    return "%s万%s" % (_cn(yuan // 10000), "多" if yuan % 10000 >= 1000 else "")


_MONEY_PATTERNS = (
    (re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)"), 100),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块)"), 100),
    (re.compile(r"(?<![\d:：-])(\d+\.\d{1,2})(?![\d:：])"), 100),
)


def humanize_numbers(text):
    """把句子里的精确金额换成「两百多」「三百块」；时间 14:00、日期 11-16 不动。"""
    value = str(text or "")
    if not value:
        return ""
    for pattern, scale in _MONEY_PATTERNS:
        def _swap(match):
            try:
                cents = int(round(float(match.group(1)) * scale))
            except (TypeError, ValueError):
                return match.group(0)
            return human_money(cents)
        value = pattern.sub(_swap, value)
    return value


def has_cents(text):
    """句子里还有没有 ¥275.22 / 275.22 元 这种带小数的金额。"""
    return bool(re.search(r"[¥￥]\s*\d+\.\d+|\d+\.\d{1,2}\s*元", str(text or "")))
