# -*- coding: utf-8 -*-
"""基金故障时的自主行动：主动发消息 / 跳过 / 把钱挪作他用。

只执行 LLM 的决定，不生成任何情绪句子；文案由 LLM 自己给。
"""
from __future__ import annotations

import time

try:
    from ..guard import isolated
except ImportError:
    from guard import isolated

TARGETS = ("deposit_fixed", "deposit_current", "shopping", "keep")
FIXED_TERMS = (3, 6, 12, 36)
_TARGET_ALIASES = {
    "定期": "deposit_fixed", "定存": "deposit_fixed",
    "活期": "deposit_current",
    "购物": "shopping", "买东西": "shopping", "买衣服": "shopping",
    "留着": "keep", "先留着": "keep",
}


def _amo(plugin):
    finance = getattr(plugin, "finance", None)
    if finance is not None and hasattr(finance, "_amo"):
        try:
            return finance._amo()
        except Exception as exc:
            isolated("finance.fund_action.amo", exc)
    return None


async def _deliver(amo, text):
    """先走 amo 的 owner 通道；失败再落到最近活跃会话（webchat 优先）。"""
    if hasattr(amo, "push_to_owner"):
        try:
            if await amo.push_to_owner(text):
                return True, "owner"
        except Exception as exc:
            isolated("finance.fund_action.push", exc)
    last = getattr(amo, "_last_reply_at", {}) or {}

    def _stamp(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    candidates = [(str(umo), _stamp(last.get(umo))) for umo in last.keys() if str(umo).strip()]
    candidates.sort(key=lambda pair: (not pair[0].startswith("webchat:"), -pair[1]))
    for umo, _ in candidates[:6]:
        sender = getattr(amo, "_send_to_umo", None)
        if not callable(sender):
            break
        try:
            if await sender(umo, text):
                return True, umo
        except Exception as exc:
            isolated("finance.fund_action.session", exc)
    return False, ""


async def send_message_to_user_action(plugin, text):
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "action": "send_message_to_user", "message": "empty"}
    amo = _amo(plugin)
    if amo is None:
        return {"ok": False, "action": "send_message_to_user", "message": "chat_unavailable"}
    ok, route = await _deliver(amo, text)
    return {"ok": bool(ok), "action": "send_message_to_user", "text": text,
            "route": route, "message": "sent" if ok else "failed"}


async def skip_fund_trade_action(plugin, reason=""):
    return {"ok": True, "action": "skip_fund_trade",
            "reason": str(reason or "").strip()[:120]}


async def reallocate_funds_action(plugin, target, amount_cents=0, term_months=0, note=""):
    target = str(target or "").strip()
    target = _TARGET_ALIASES.get(target, target.lower())
    amount = int(amount_cents or 0)
    note = str(note or "").strip()[:120]
    if target not in TARGETS:
        return {"ok": False, "action": "reallocate_funds", "message": "bad_target"}
    if target == "keep":
        return {"ok": True, "action": "reallocate_funds", "target": target,
                "amount_cents": 0, "message": "kept"}
    if amount <= 0:
        return {"ok": False, "action": "reallocate_funds", "message": "bad_amount"}
    if target in ("deposit_fixed", "deposit_current"):
        bank = getattr(plugin, "bank", None)
        if bank is None:
            try:
                from ..services.bank_engine import build_bank
            except ImportError:
                from services.bank_engine import build_bank
            bank = build_bank(plugin)
            plugin.bank = bank
        if bank is None:
            return {"ok": False, "action": "reallocate_funds", "message": "bank_unavailable"}
        term = int(term_months or 0) if target == "deposit_fixed" else 0
        if target == "deposit_fixed" and term not in FIXED_TERMS:
            return {"ok": False, "action": "reallocate_funds", "message": "bad_term"}
        out = await bank.deposit(amount, term, diary=False)
        return {"ok": bool(out.get("ok")), "action": "reallocate_funds", "target": target,
                "amount_cents": amount, "term_months": term,
                "message": "deposited" if out.get("ok") else "failed"}
    finance = getattr(plugin, "finance", None)
    if finance is None or not hasattr(finance, "expense"):
        return {"ok": False, "action": "reallocate_funds", "message": "wallet_unavailable"}
    idem = "fund_alloc:%s:%d" % (time.strftime("%Y%m%d%H%M%S"), amount)
    out = await finance.expense(
        amount, note or "购物", idem, necessary=False, quiet=True,
        category="购物", sub_category="购物", item_name=note or "购物",
        trigger_reason="fund_outage", diary=False)
    return {"ok": bool(out.get("ok")), "action": "reallocate_funds", "target": "shopping",
            "amount_cents": amount, "message": "spent" if out.get("ok") else "failed"}
