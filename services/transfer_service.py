# -*- coding: utf-8 -*-
"""用户 [转账] 入账：注资 + 补交搬家费。"""
from __future__ import annotations

import re

try:
    from ..guard import isolated
except ImportError:
    from guard import isolated


async def handle_transfer(plugin, text):
    # 允许「…[转账]500…」这种夹在句子里的话；只认带括号的 [转账]，或句首/标点后的“转账”
    pattern = r"(?:[\[【]\s*转账\s*[\]】]|(?:^|[\s，,。.!！?？])\s*转账)\s*([0-9]+(?:\.[0-9]+)?)"
    match = re.search(pattern, str(text or ""))
    if not match:
        return ""
    cents = int(round(float(match.group(1)) * 100))
    try:
        result = await plugin.finance_help.user_transfer(cents) if plugin.finance_help else {
            "ok": False, "message": "金融模块未就绪"}
    except Exception as exc:
        isolated("finance.transfer", exc)
        result = {"ok": False, "message": "转账入账失败"}
    message = result.get("message") or "已收到"
    if result.get("ok"):
        try:
            from .city import settle_pending_fee
        except ImportError:
            from services.city import settle_pending_fee
        message += await settle_pending_fee(plugin)
    return message
