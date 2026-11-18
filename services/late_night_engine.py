# -*- coding: utf-8 -*-
"""熬夜的财务/职场后果：宵夜决定、迟到罚款、转账哄睡清空惩罚。

2026-11-17 收口：宵夜的决定/日程块/扣款/日记/主动消息全部在 amo_food_event
（amo 的 late_snack_tick 单一入口），守护这里不再自己拼 schedule/ledger/日记，
只负责触发并把结果带出去。迟到罚款与哄睡仍属守护侧财务行为。
"""
from __future__ import annotations

import logging
logger = logging.getLogger("amo.watch")

import datetime as _dt

try:
    from ..utils.helpers import stable_int
except ImportError:
    from utils.helpers import stable_int

try:
    from ..core.amo_bridge import AmoBridge
except ImportError:
    from core.amo_bridge import AmoBridge


def _amo(plugin):
    finance = getattr(plugin, "finance", None)
    return finance._amo() if finance is not None and hasattr(finance, "_amo") else None


async def _late_snack(plugin, finance, now):
    """调 amo 单一入口；只把决定/块/扣款结果带出去，不在这里做任何领域判断。"""
    bridge = AmoBridge(plugin, finance=finance)
    result = await bridge.call_async("late_snack_tick", now=now)
    if not result.ok or not isinstance(result.value, dict):
        return {}
    info = result.value
    out = {"snack_decision": "eat" if info.get("eat") else str(info.get("skipped") or "skip")}
    if info.get("charged"):
        out["snack"] = int(info.get("charged") or 0)
    if info.get("block_id"):
        out["snack_block"] = str(info.get("block_id"))
    return out


async def tick(plugin, now=None):
    now = now or _dt.datetime.now()
    amo = _amo(plugin)
    finance = getattr(plugin, "finance", None)
    repo = getattr(plugin, "finance_repo", None)
    if amo is None or finance is None or not hasattr(amo, "late_night_state"):
        return None
    try:
        from .health_bridge import is_sick
    except ImportError:
        from services.health_bridge import is_sick
    if is_sick(amo):
        return None  # 生病不熬夜、不吃宵夜、不记迟到罚款
    try:
        state = amo.late_night_state() or {}
    except Exception:
        return None
    out = {}
    # 宵夜窗口（22:00-06:00）和「0-6 不主动说话」都由 amo 领域入口判；
    # 这里只在窗口内触发，避免白天每个 tick 都跨插件调用。
    if state.get("active") and (22 <= now.hour < 24 or 0 <= now.hour < 6):
        try:
            out.update(await _late_snack(plugin, finance, now))
        except Exception as exc:
            logger.debug("[WATCH] late_snack_tick failed: %r", exc)
    # 第二天迟到：工作日、熬夜未哄睡、60% 概率
    if state.get("late_yesterday") and not state.get("cleared") and now.weekday() < 5:
        try:
            fined = repo.meta_get("late_fine:" + now.date().isoformat(), False) if repo is not None else False
        except Exception:
            fined = False
        if not fined and not state.get("fined"):
            if stable_int(now.date().isoformat(), 0, 100) < 60:
                fine = 5000 + stable_int(now.date().isoformat() + ":f", 0, 5000)
                # 非宵夜路径：迟到罚款仍属守护侧财务；宵夜扣款只在 amo late_snack_tick。
                try:
                    res = await finance.expense(fine, "迟到罚款", "late-fine:" + now.date().isoformat(), necessary=True)
                except Exception:
                    res = {"ok": False}
                if isinstance(res, dict) and res.get("ok"):
                    try:
                        amo.note_late_fine(fine)
                        finance.small_diary("今天起晚了，上班迟到扣了%d块，好心疼。" % (fine // 100))
                    except Exception as exc:
                        logger.debug("[WATCH] optional step failed: %r", exc)
                    out["fine"] = fine
            try:
                repo.meta_set("late_fine:" + now.date().isoformat(), "1")
            except Exception as exc:
                logger.debug("[WATCH] optional step failed: %r", exc)
    return out


async def maybe_soothe(plugin, text):
    """他转账买夜宵/哄睡：清空熬夜惩罚。"""
    import re
    match = re.search(r"转账\]?\s*([0-9]+(?:\.[0-9]+)?)", str(text or ""))
    if not match:
        return None
    cents = int(round(float(match.group(1)) * 100))
    if cents < 5000:
        return None
    amo = _amo(plugin)
    if amo is None or not hasattr(amo, "late_night_state"):
        return None
    try:
        state = amo.late_night_state() or {}
    except Exception:
        return None
    if not state.get("active"):
        return None
    try:
        amo.soothe_late_night("夜宵转账")
        finance = getattr(plugin, "finance", None)
        if finance is not None:
            finance.nudge_mood({"开心": 20, "感动": 20}, "late:soothe:%s" % cents)
            finance.small_diary("熬夜的时候老公转账让我买夜宵，心里暖暖的。")
    except Exception as exc:
        logger.debug("[WATCH] optional step failed: %r", exc)
    return {"ok": True, "message": "谢谢老公，我吃完就睡~"}
