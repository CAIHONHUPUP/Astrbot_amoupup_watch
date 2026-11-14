# -*- coding: utf-8 -*-
"""读 amo 的健康状态（跨包隔离：只读 state.health dict，不 import 陪伴插件）。"""
from __future__ import annotations

SICK = ("轻微不适", "感冒", "低烧", "发烧", "肠胃炎")
SEVERITY = {"健康": 0, "疲惫": 1, "轻微不适": 1, "感冒": 2, "低烧": 2,
            "发烧": 3, "肠胃炎": 3}


def health_dict(amo):
    state = getattr(amo, "state", None)
    try:
        return dict(getattr(state, "health", None) or {})
    except Exception:
        return {}


def is_sick(amo):
    return str(health_dict(amo).get("status") or "健康") in SICK


def get_sick_status(amo):
    return str(health_dict(amo).get("status") or "健康")


def get_severity(amo):
    return int(SEVERITY.get(get_sick_status(amo), 0))


def get_sick_duration(amo):
    import time
    h = health_dict(amo)
    if str(h.get("status")) not in SICK:
        return 0
    try:
        return max(0, int(round((time.time() - float(h.get("since") or time.time())) / 3600.0)))
    except Exception:
        return 0
