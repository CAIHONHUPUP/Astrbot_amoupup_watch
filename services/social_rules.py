# -*- coding: utf-8 -*-
"""社交纯规则（无 IO / 不写库）：邀约生命周期 + 冷却闸 + 常识改期 + 人话时间。

Phase 14：把 social_engine 里「只算不做」的部分抽出来，engine 只留副作用
（写事件/扣钱/写日程/情绪/记忆）。文案与冷却阈值全部保持原值，不漂移。
"""
from __future__ import annotations

import datetime as _dt

try:
    from ..utils.helpers import is_holiday
except ImportError:  # 按顶层包/单文件加载时
    from utils.helpers import is_holiday

#: 邀约标题和地点必须一致：地点不是家，就不能写「回家/来我家」（2026-11-21 线上）
_HOME_WORDS = ("回家", "来我家", "我家里", "家里吃", "回家吃")
_PUBLIC_WORDS = ("楼下小馆", "小馆", "商场", "酒店", "咖啡", "餐厅")


def align_topic_place(topic, place):
    """把邀约标题对齐到地点：避免「妈妈中午回家吃饭」却排在楼下小馆。"""
    text = str(topic or "").strip()
    target = str(place or "").strip()
    if not text:
        return text
    _is_home = bool(target) and (target == "家" or target.endswith("家"))
    if target and not _is_home and any(word in text for word in _HOME_WORDS):
        for word in _HOME_WORDS:
            text = text.replace(word, "一起")
        return text
    if _is_home and any(word in text for word in _PUBLIC_WORDS):
        for word in _PUBLIC_WORDS:
            text = text.replace(word, "")
        return (text or "回家吃饭").strip()
    return text


#: 邀约生命周期：pending -> accepted / rejected / expired / replaced，后四者为终态
PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
EXPIRED = "expired"
REPLACED = "replaced"
TERMINAL = frozenset({ACCEPTED, REJECTED, EXPIRED, REPLACED})

INVITE_WINDOW = (9, 22)      # 只在 9:00-22:00 抛邀约
MAX_PER_DAY = 2              # 一天最多 2 次邀约
MIN_GAP_HOURS = 2.5          # 两次邀约之间至少隔 2.5 小时
CEREMONY_LEAD_DAYS = 3       # 喜酒/婚礼至少提前 3 天
INVITE_TTL_HOURS = 8

_DAY_WORDS = (("今天", 0), ("明天", 1), ("后天", 2), ("大后天", 3))
_PERIODS = (("上午", ("09:00", "11:30")), ("中午", ("12:00", "13:30")),
            ("下午", ("14:00", "17:00")), ("傍晚", ("17:30", "19:00")),
            ("晚上", ("19:00", "21:00")), ("夜里", ("20:00", "22:00")))


def parse_when(text, now=None):
    """「明天下午」→ (日期, 14:00, 17:00)。大模型只给自然语言时用它兜底。"""
    now = now or _dt.datetime.now()
    body = str(text or "")
    offset = None
    for word, days in _DAY_WORDS:
        if word in body:
            offset = days
            break
    if offset is None and "周末" in body:
        ahead = (5 - now.weekday()) % 7
        offset = ahead or 7
    start, end = "14:00", "17:00"
    for word, span in _PERIODS:
        if word in body:
            start, end = span
            break
    target = (now + _dt.timedelta(days=int(offset or 0))).date().isoformat()
    return target, start, end


def is_off_day(day):
    """休息日：周末或法定节假日。"""
    return bool(day.weekday() >= 5 or is_holiday(day))


def day_label(day):
    """把日期翻成她聊天时会用的人话：工作日 / 周末 / 法定节假日。"""
    if is_holiday(day):
        return "法定节假日"
    return "周末" if day.weekday() >= 5 else "工作日"


def relative_label(target, now):
    delta = (target - now.date()).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return "%d月%d日" % (target.month, target.day)


def retime_ask(text, target, now, start):
    """邀约日期/时段被常识规则修正后，把微信原话里的“明天/下午”一起改掉，避免文案穿帮。"""
    out = str(text or "")
    label = relative_label(target, now)
    for word in ("大后天", "后天", "明天", "今天"):
        if word in out:
            out = out.replace(word, label)
    if _minutes(start) >= 18 * 60:
        for word in ("上午", "中午", "下午", "白天"):
            out = out.replace(word, "晚上")
    return out


def minutes(text, default=0):
    try:
        hour, minute = str(text).split(":")[:2]
        return int(hour) * 60 + int(minute)
    except Exception:
        return int(default)


_minutes = minutes


# ---------------------------------------------------------------- 邀约生命周期
def invite_status(inv, now=None):
    """当前状态：dict -> pending/expired；空/非法 -> "none"。"""
    if not isinstance(inv, dict) or not inv.get("npc"):
        return "none"
    if now is not None and is_expired(inv, now):
        return EXPIRED
    return PENDING


def is_expired(inv, now):
    if not isinstance(inv, dict):
        return False
    try:
        expires = float(inv.get("expires") or 0)
    except (TypeError, ValueError):
        return False
    return bool(expires) and float(now.timestamp()) >= expires


def live_invite(inv, now):
    """还活着（未过期、未处理）的待决邀约；否则 None。"""
    return inv if invite_status(inv, now) == PENDING else None


def is_terminal(status):
    return str(status or "") in TERMINAL


def same_invite(a, b):
    """同一场邀约：同一个人、同一类事、同一天同一时段（token 不同也算重复）。"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return (str(a.get("npc") or "") == str(b.get("npc") or "")
            and str(a.get("kind") or "") == str(b.get("kind") or "")
            and str(a.get("day") or "") == str(b.get("day") or "")
            and str(a.get("start") or "") == str(b.get("start") or ""))


def invite_idem(npc, day, start):
    """事件级幂等键：同一个人、同一天、同一时段只可能扣一次钱。"""
    return "social_event:%s:%s:%s" % (npc, day, start or "any")


def can_invite_decision(pending=False, hour=0, count=0, last_ts=0.0, now_ts=0.0,
                        npc_done=False, window=INVITE_WINDOW, max_per_day=MAX_PER_DAY,
                        min_gap_hours=MIN_GAP_HOURS):
    """冷却闸纯决策：(ok, reason)。顺序与 2026-11-15 封板一致。"""
    if pending:
        return False, "pending"
    low, high = window
    if not (low <= int(hour) < high):
        return False, "quiet_hours"
    if int(count) >= int(max_per_day):
        return False, "daily_cap"
    if last_ts and (float(now_ts) - float(last_ts)) < float(min_gap_hours) * 3600:
        return False, "gap"
    if npc_done:
        return False, "npc_today"
    return True, ""


# ---------------------------------------------------------------- 常识改期
def common_sense(target, kind, circle, start, end, now):
    """酒席/领导/朋友约的硬常识；返回 (date, start, end) 或 None（21 天找不到休息日）。"""
    if kind == "ceremony":
        target = max(target, now.date() + _dt.timedelta(days=CEREMONY_LEAD_DAYS))
        for _ in range(21):
            if is_off_day(target):
                break
            target += _dt.timedelta(days=1)
        else:
            return None
        start, end = "11:00", "13:00"
    elif circle == "work":
        while is_off_day(target):
            target += _dt.timedelta(days=1)
        if kind in ("meal", "drink") and _minutes(start) < 18 * 60:
            start, end = "19:00", "20:30"
    elif circle == "friend" and kind in ("meal", "drink", "shopping", "other"):
        if not is_off_day(target) and _minutes(start) < 18 * 60:
            start, end = "19:00", "21:00"
    return target, start, end


def find_free_day(target, start, end, conflict, kind="", max_days=14):
    """冲突就往后挪，最多试 max_days 天；酒席仍只能落在休息日。找不到返回 None。"""
    for _ in range(int(max_days)):
        day_text = target.isoformat()
        if not start or not conflict(day_text, start, end):
            return target
        target += _dt.timedelta(days=1)
        if kind == "ceremony":
            while not is_off_day(target):
                target += _dt.timedelta(days=1)
    return None
