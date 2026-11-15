# -*- coding: utf-8 -*-
"""NPC 自己的生活轨迹：每人一句极短的 npc_status，7-14 天换一次。

用户 2026-11-15 要求：柳如烟、白凝冰他们不能只在抽中事件时才出现，要有自己的状态，
她在聊天里提起时也要和这个状态对得上。
"""
from __future__ import annotations

import datetime as _dt

try:
    from ..utils.helpers import stable_int
except ImportError:  # 按顶层包/单文件加载时
    from utils.helpers import stable_int

POOLS = {
    "柳如烟": ("最近在相亲，见了两个都不满意", "刚染了个新发色，挺得意", "在学做提拉米苏",
               "跟男朋友冷战了三天", "在准备考编，天天刷题"),
    "白凝冰": ("刚换了工作，新公司加班凶", "在健身房办了卡，天天打卡", "养了只橘猫",
               "在攒钱买车，天天看行情"),
    "季博达": ("最近在外地出差", "老婆刚生了二胎", "在准备部门重组", "迷上了钓鱼，周末就跑水库"),
    "方圆": ("在备婚，天天看婚纱", "刚搬了家，还在收拾", "在考驾照，被教练骂惨了", "最近总请假"),
    "妈妈": ("在跳广场舞，还当了领队", "最近膝盖不太舒服", "跟邻居学做菜", "天天催我找对象"),
    "爸爸": ("在河边钓鱼", "退休后天天溜弯", "在修老家的房子"),
    "沈幼微": ("在准备结婚，忙得脚不沾地", "刚换了工作", "在减肥，一天只吃两顿", "准备出国读书"),
}


def _current(repo, name):
    rows = repo.npcs() if repo is not None else []
    for row in rows:
        if str(row.get("name") or "") == name:
            return row
    return None


def refresh(repo, now=None):
    """给每个人续上一次状态：没有就抽一个，超过 7-14 天就换一个（按日期稳定）。"""
    if repo is None:
        return {}
    now = now or _dt.datetime.now()
    stamp = now.timestamp()
    day = now.date().isoformat()
    out = {}
    for name, pool in POOLS.items():
        row = _current(repo, name)
        if row is None:
            continue
        current = str(row.get("status") or "")
        since = float(row.get("status_at") or 0)
        ttl = (7 + stable_int("npc-status-ttl:%s:%s" % (name, day), 0, 8)) * 86400
        if current and since and (stamp - since) < ttl:
            out[name] = current
            continue
        pick = pool[stable_int("npc-status:%s:%s" % (name, day), 0, len(pool))]
        try:
            repo.set_npc_status(name, pick, stamp)
        except Exception:
            pass
        out[name] = pick
    return out


def status_map(repo):
    rows = repo.npcs() if repo is not None else []
    out = {}
    for row in rows:
        value = str(row.get("status") or "").strip()
        if value:
            out[str(row.get("name") or "")] = value
    return out
