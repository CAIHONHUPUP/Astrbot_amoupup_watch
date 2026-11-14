# -*- coding: utf-8 -*-
"""账单：按大类分组，今日/本月合计；同时给聊天 Prompt 提供当日摘要。"""
from __future__ import annotations

import datetime as _dt


def _items(plugin):
    finance = getattr(plugin, "finance", None)
    amo = finance._amo() if finance is not None and hasattr(finance, "_amo") else None
    if amo is None:
        return []
    try:
        return amo.wallet.ledger(300) or []
    except Exception:
        return []


def summary(plugin, now=None):
    now = now or _dt.datetime.now()
    today = now.date()
    month = (now.year, now.month)
    today_cents = month_cents = 0
    groups = {}
    for it in _items(plugin):
        try:
            when = _dt.datetime.fromtimestamp(float(it.get("ts") or 0))
        except Exception:
            continue
        delta = int(it.get("delta_cents") or 0)
        if delta >= 0:
            continue
        if (when.year, when.month) == month:
            month_cents += -delta
        if when.date() == today:
            today_cents += -delta
            cat = str(it.get("category") or "其他")
            groups.setdefault(cat, []).append(it)
    return {"today_cents": today_cents, "month_cents": month_cents, "groups": groups}


def text(plugin, now=None):
    data = summary(plugin, now)
    lines = ["【今日账单】总支出 %.2f 元" % (data["today_cents"] / 100.0)]
    for cat, rows in data["groups"].items():
        lines.append("【%s】" % cat)
        for it in rows:
            try:
                when = _dt.datetime.fromtimestamp(float(it.get("ts") or 0))
                stamp = when.strftime("%m-%d %H:%M")
            except Exception:
                stamp = "--"
            item = str(it.get("item_name") or it.get("reason") or "开销")
            sub = str(it.get("sub_category") or "")
            lines.append("  %s %s%s：-%0.2f 元" % (
                stamp, (sub + "·") if sub else "", item,
                abs(int(it.get("delta_cents") or 0)) / 100.0))
    lines.append("本月总支出 %.2f 元" % (data["month_cents"] / 100.0))
    return "\n".join(lines)


def prompt_line(plugin, now=None):
    data = summary(plugin, now)
    bits = []
    for cat, rows in data["groups"].items():
        names = [str(it.get("item_name") or it.get("reason") or "") for it in rows[:5]]
        names = [n for n in names if n]
        if names:
            bits.append("%s(%s)" % (cat, "、".join(names)))
    return "今日总支出 %.2f 元；支出明细：%s" % (
        data["today_cents"] / 100.0, "；".join(bits) or "暂无")
