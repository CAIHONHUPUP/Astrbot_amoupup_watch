# -*- coding: utf-8 -*-
"""理财菜单：/理财、/理财 总资产|持仓|贷款|日程|关闭。"""
from __future__ import annotations

try:
    from ..guard import isolated
except ImportError:
    from guard import isolated


async def money_reply(plugin, text=""):
    try:
        text = str(text or "")
        parts = [p for p in text.split() if p]
        if parts and parts[0].lstrip("/").lower() in ("理财", "money"):
            parts = parts[1:]
        return plugin._format_money(parts[0] if parts else "")
    except Exception as exc:
        isolated("handler.money", exc)
        return "理财信息暂时读不到。"
