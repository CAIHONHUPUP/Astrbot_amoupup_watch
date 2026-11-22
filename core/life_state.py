# -*- coding: utf-8 -*-
"""LifeStateSnapshot 消费端（task-27）：守护只读陪伴插件的一份状态快照。

契约生产者：forever/services/life_state.py（与能陪 feature API
life_state_snapshot() 同步）。本模块只做三件事：

1. 经 AmoBridge（跨插件调用层）读 life_state_snapshot()，不碰对方表/文件；
2. 校验 contract/version/字段，坏形状/旧版本 -> 降级（available=False + reason）；
3. 给业务一个稳定的小视图：wallet_balance_cents / place / in_shop / shopping /
   month_consumption_cents / photos_today。

消费端约定：拿不到就退回旧逻辑，禁止自己再存一份“此刻/余额”副本；
每次降级打一次结构化 CONTRACT_FALLBACK 日志（按 reason 去重，不刷屏）。
"""
from __future__ import annotations

import logging

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

CONTRACT_NAME = "amo-life-state"
SUPPORTED_VERSION = 1
REQUIRED_FIELDS = (
    "contract", "version", "source", "generated_at", "day",
    "principal", "location", "current_action", "schedule",
    "date_session", "shopping", "wallet", "outfit", "mood", "media",
    "consistency", "degraded",
)
#: 降级日志去重（reason -> 已打过）；模块级即可，不写库
_WARNED: set = set()


def _warn_fallback(reason, detail=""):
    key = str(reason or "unknown")
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        logger.warning("[WATCH][CONTRACT_FALLBACK] life_state %s %s", key, detail)
    except Exception:  # cleanup: 日志失败不影响业务
        pass


def contract_descriptor():
    return {
        "contract": CONTRACT_NAME,
        "version": SUPPORTED_VERSION,
        "role": "consumer",
        "fields": list(REQUIRED_FIELDS),
        "source": "forever.life_state_snapshot",
    }


class SnapshotView:
    """只读视图：缺字段给默认值，不让调用方 KeyError。"""

    def __init__(self, snapshot=None, available=False, reason="", raw=None):
        # 不可用的快照不暴露给业务读取（accessor 返回默认值），原文只留在 raw 便于排障
        self.snapshot = dict(snapshot) if available and isinstance(snapshot, dict) else {}
        self.raw = dict(raw) if isinstance(raw, dict) else (
            dict(snapshot) if isinstance(snapshot, dict) else {})
        self.available = bool(available)
        self.reason = str(reason or "")

    # ---- 通用读取 ----
    def get(self, path, default=None):
        """按 a.b.c 取值；缺失返回 default。"""
        data = self.snapshot
        for part in str(path or "").split("."):
            if not part:
                continue
            if not isinstance(data, dict) or part not in data:
                return default
            data = data[part]
        return data if data is not None else default

    @property
    def version(self):
        try:
            return int(self.snapshot.get("version") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def day(self):
        return str(self.snapshot.get("day") or "")

    # ---- 业务小视图 ----
    def wallet_balance_cents(self):
        value = self.get("wallet.balance_cents")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def month_consumption_cents(self):
        try:
            return int(self.get("wallet.month_consumption_cents") or 0)
        except (TypeError, ValueError):
            return 0

    def place(self):
        return str(self.get("location.place") or self.get("current_action.place") or "")

    def zone(self):
        return str(self.get("location.zone") or "")

    def in_shop(self):
        value = self.get("location.in_shop")
        return bool(value) if value is not None else None

    def at_home(self):
        value = self.get("location.at_home")
        return bool(value) if value is not None else None

    def shopping(self):
        data = self.get("shopping")
        return dict(data) if isinstance(data, dict) else {}

    def shopping_line(self):
        return str(self.shopping().get("line") or "")

    def shopping_stale(self):
        return bool(self.shopping().get("line_stale"))

    def photos_today(self):
        try:
            return int(self.get("media.photos_today") or 0)
        except (TypeError, ValueError):
            return 0

    def issues(self):
        data = self.get("consistency.issues")
        return list(data) if isinstance(data, list) else []

    def as_dict(self):
        return dict(self.snapshot)


def view(snapshot):
    """把原始 dict 包成视图并做校验（测试/本地模拟用）。"""
    data = dict(snapshot) if isinstance(snapshot, dict) else {}
    if str(data.get("contract") or "") != CONTRACT_NAME:
        return SnapshotView(None, False, "contract_mismatch", raw=data)
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != SUPPORTED_VERSION:
        return SnapshotView(None, False, "version_unsupported:%s" % version, raw=data)
    missing = [field for field in REQUIRED_FIELDS if field not in data]
    if missing:
        return SnapshotView(None, False, "fields_missing:%s" % ",".join(missing[:4]), raw=data)
    return SnapshotView(data, True, "")


def _bridge(plugin):
    try:
        from .amo_bridge import AmoBridge
    except ImportError:
        from core.amo_bridge import AmoBridge
    return AmoBridge(plugin, scope="life_state")


def read(plugin, now=None):
    """读快照：成功返回 available=True 的视图；失败返回 available=False + reason。

    只通过 AmoBridge 调用（方法必须登记在 _CALLS），不 import 对方模块。
    """
    bridge = None
    try:
        bridge = _bridge(plugin)
        result = bridge.call("life_state_snapshot")
    except Exception as exc:
        _warn_fallback("call_error", "%s: %s" % (type(exc).__name__, exc))
        return SnapshotView(None, False, "call_error")
    ok = bool(getattr(result, "ok", False))
    if not ok:
        reason = str(getattr(result, "reason", "") or "unavailable")
        _warn_fallback(reason, "reason=%s" % reason)
        return SnapshotView(None, False, reason)
    value = getattr(result, "value", None)
    snapshot = view(value)
    if not snapshot.available:
        _warn_fallback(snapshot.reason, "day=%s" % snapshot.day)
    return snapshot
