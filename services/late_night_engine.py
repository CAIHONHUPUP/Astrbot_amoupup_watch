# -*- coding: utf-8 -*-
"""熬夜的财务/职场后果：夜宵冲动、迟到罚款、转账哄睡清空惩罚。"""
from __future__ import annotations

import datetime as _dt

try:
    from ..utils.helpers import stable_int
except ImportError:
    from utils.helpers import stable_int

SNACKS = ("烧烤", "泡面", "炸鸡", "小蛋糕")


def _amo(plugin):
    finance = getattr(plugin, "finance", None)
    return finance._amo() if finance is not None and hasattr(finance, "_amo") else None


async def tick(plugin, now=None):
    now = now or _dt.datetime.now()
    amo = _amo(plugin)
    finance = getattr(plugin, "finance", None)
    repo = getattr(plugin, "finance_repo", None)
    if amo is None or finance is None or not hasattr(amo, "late_night_state"):
        return None
    try:
        state = amo.late_night_state() or {}
    except Exception:
        return None
    out = {}
    # 夜宵：00:30-05:00，饿且有钱，每小时最多一次
    late_snack = (now.hour == 0 and now.minute >= 30) or 1 <= now.hour < 5
    if state.get("active") and late_snack:
        key = "late_snack:%s:%02d" % (now.date().isoformat(), now.hour)
        try:
            done = repo.meta_get(key, False) if repo is not None else False
        except Exception:
            done = False
        if not done:
            try:
                hunger = float(amo.get_hunger())
            except Exception:
                hunger = 100.0
            cash = int(finance.balance_cents() or 0)
            if hunger < 40 and cash >= 2000:
                food = SNACKS[stable_int(key, 0, len(SNACKS))]
                amount = 2000 + stable_int(key + ":m", 0, 3000)
                try:
                    res = await finance.expense(amount, "夜宵：%s" % food, "late:" + key, necessary=True)
                except Exception:
                    res = {"ok": False}
                if isinstance(res, dict) and res.get("ok"):
                    try:
                        amo.add_hunger("amo", 15)
                        amo.note_late_snack(amount, food)
                    except Exception:
                        pass
                    try:
                        finance.small_diary("今天熬夜看盘，肚子饿得咕咕叫，没忍住点了份%s，花了%d块，呜呜。" % (food, amount // 100))
                    except Exception:
                        pass
                try:
                    repo.meta_set(key, "1")
                except Exception:
                    pass
                out["snack"] = amount
    # 第二天迟到：工作日、熬夜未哄睡、60% 概率
    if state.get("late_yesterday") and not state.get("cleared") and now.weekday() < 5:
        try:
            fined = repo.meta_get("late_fine:" + now.date().isoformat(), False) if repo is not None else False
        except Exception:
            fined = False
        if not fined and not state.get("fined"):
            if stable_int(now.date().isoformat(), 0, 100) < 60:
                fine = 5000 + stable_int(now.date().isoformat() + ":f", 0, 5000)
                try:
                    res = await finance.expense(fine, "迟到罚款", "late-fine:" + now.date().isoformat(), necessary=True)
                except Exception:
                    res = {"ok": False}
                if isinstance(res, dict) and res.get("ok"):
                    try:
                        amo.note_late_fine(fine)
                        finance.small_diary("今天起晚了，上班迟到扣了%d块，好心疼。" % (fine // 100))
                    except Exception:
                        pass
                    out["fine"] = fine
            try:
                repo.meta_set("late_fine:" + now.date().isoformat(), "1")
            except Exception:
                pass
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
    except Exception:
        pass
    return {"ok": True, "message": "谢谢老公，我吃完就睡~"}
