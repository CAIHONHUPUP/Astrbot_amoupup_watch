# -*- coding: utf-8 -*-
"""跨插件契约快照（只读）：守护被允许调用的 amo / 能记 方法清单 + 版本指纹。

设计（Phase 14）：
- 只读 `tests/contracts/*_surface.json`，**绝不写快照**；
- 快照缺失/损坏/字段不对 → 降级成空表并 INFO/WARN，不抛异常；
- 调用方（core/amo_bridge.AmoBridge）每次调用前查一次：快照里没有就 WARN 一次
  然后继续调（能陪装了新方法时不能因为快照旧就崩），这叫「校验失败只降级」；
- `KNOWN_DRIFT` 记录 amo 源码里有、快照没收录、但守护确实在用的方法：只报告，
  本阶段不允许改快照（能陪/能记本阶段不动）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

#: 快照目录（相对本文件 -> 仓库根/tests/contracts）
CONTRACT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "contracts")

#: provider 名 -> 快照文件名
PROVIDER_FILES = {
    "forever": "Astrbot_amoupup_forever_surface.json",
    "data": "Astrbot_amoupup_data_surface.json",
}

#: 本插件支持的跨插件协议（Phase 19）：对方 protocol_info() 主版本相同才算 ok。
PROTOCOL_VERSION = "1.0"
PROTOCOL_MAJOR = 1
#: 老版本没有 protocol_info() 时按 v0 继续（legacy，只 WARN 一次）。
LEGACY_VERSION = "0"

#: 已知漂移（只报告，不改快照）：方法在对方源码里真实存在，但快照没收录。
#: Phase 15：两个真实漂移（set_date_ending / set_last_date_event）已经补进
#: tests/contracts/Astrbot_amoupup_forever_surface.json，这里清零。
#: 以后再出现漂移必须写回这里、在报告里单独说明，不许悄悄放过。
KNOWN_DRIFT: dict[str, str] = {}

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_warned: set[str] = set()


def _warn_once(key, message, *args):
    if key in _warned:
        return
    _warned.add(key)
    try:
        logger.warning(message, *args)
    except Exception:  # cleanup: 日志失败不能影响调用链
        pass


def _info_once(key, message, *args):
    if key in _warned:
        return
    _warned.add(key)
    try:
        logger.info(message, *args)
    except Exception:  # cleanup: 日志失败不能影响调用链
        pass


def snapshot_path(provider="forever"):
    return os.path.join(CONTRACT_DIR, PROVIDER_FILES.get(provider, ""))


def load_surface(provider="forever"):
    """读一份快照：{方法名: (参数名...)}；失败返回 {}（降级，不崩）。"""
    name = str(provider or "forever")
    if name not in PROVIDER_FILES:
        _warn_once("provider:" + name, "[WATCH][CONTRACT] 未知契约 provider=%s，按空表降级", name)
        return {}
    with _lock:
        if name in _cache:
            return _cache[name]
    path = snapshot_path(name)
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            data = {str(k): tuple(v or ()) for k, v in raw.items()}
        else:
            _warn_once("shape:" + name,
                       "[WATCH][CONTRACT] %s 结构不是对象，按空表降级", PROVIDER_FILES[name])
    except FileNotFoundError:
        _info_once("missing:" + name,
                   "[WATCH][CONTRACT] 缺少快照 %s，跨插件校验降级为放行",
                   PROVIDER_FILES[name])
    except (OSError, ValueError) as exc:
        _warn_once("bad:" + name,
                   "[WATCH][CONTRACT] 读取快照 %s 失败(%r)，校验降级为放行",
                   PROVIDER_FILES[name], exc)
    with _lock:
        _cache[name] = data
    return data


def surfaces():
    """两份快照：{"forever": {...}, "data": {...}}。"""
    return {name: load_surface(name) for name in PROVIDER_FILES}


def surface_version(provider="forever"):
    """快照版本指纹：内容变了指纹就变，日志里能对上「守护按哪版契约在跑」。"""
    payload = json.dumps(load_surface(provider), ensure_ascii=False,
                         sort_keys=True, default=list)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def methods(provider="forever"):
    return frozenset(load_surface(provider))


def drift_reason(method):
    return KNOWN_DRIFT.get(str(method or ""), "")


def check(method, provider="forever"):
    """(是否在快照里, 原因)。不在快照但属于已知漂移时 reason 非空。"""
    name = str(method or "")
    if not name:
        return False, "empty"
    surface = load_surface(provider)
    if not surface:
        return True, ""          # 没有契约信息 → 放行（降级，不误伤）
    if name in surface:
        return True, ""
    return False, drift_reason(name)


def describe():
    """给诊断/测试用的摘要：版本、方法数、已知漂移。"""
    return {
        "dir": CONTRACT_DIR,
        "version": {name: surface_version(name) for name in PROVIDER_FILES},
        "count": {name: len(load_surface(name)) for name in PROVIDER_FILES},
        "known_drift": dict(KNOWN_DRIFT),
    }


def _major(version):
    """协议版本主版本号：字符串 -> int；认不出来按 0（legacy）。"""
    text = str(version or "").strip().split(".")[0]
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _read_protocol(bridge, provider):
    """经 AmoBridge（或同形状对象）读对方 protocol_info()；失败返回 None。"""
    if bridge is None:
        return None
    reader = getattr(bridge, "protocol_info", None)
    if not callable(reader):
        return None
    try:
        return reader(provider)
    except Exception as exc:  # 对方异常只降级，不能让 /status 崩
        _warn_once("protocol:error:" + str(provider),
                   "[WATCH][CONTRACT] 读取 %s protocol_info 失败(%r)，按 legacy 继续",
                   provider, exc)
        return None


def compat(provider="forever", bridge=None):
    """跨插件协议兼容性（Phase 19）：ok / legacy / degraded，只报告不崩。

    - ok：主版本与本地 PROTOCOL_MAJOR 相同，按新协议继续；
    - legacy：对方没有 protocol_info()（旧版本）。只 WARN 一次，按 v0 继续；
    - degraded：主版本不同。差异写进返回字段，业务继续。
    """
    name = str(provider or "forever")
    info = _read_protocol(bridge, name)
    if not isinstance(info, dict) or not info:
        _warn_once("protocol:legacy:" + name,
                   "[WATCH][CONTRACT] %s 未提供 protocol_info，按协议 v0(legacy) 继续", name)
        return {"provider": name, "state": "legacy", "version": LEGACY_VERSION,
                "protocol": "", "api_hash": "", "major": 0,
                "detail": "对方没有 protocol_info（旧版本），按 v0 兼容继续"}
    version = str(info.get("protocol_version") or LEGACY_VERSION)
    major = _major(version)
    state = "ok" if major == PROTOCOL_MAJOR else "degraded"
    out = {"provider": name, "state": state, "version": version,
           "protocol": str(info.get("protocol") or ""),
           "api_hash": str(info.get("api_hash") or ""), "major": major, "detail": ""}
    if state == "degraded":
        _warn_once("protocol:degraded:" + name,
                   "[WATCH][CONTRACT] %s 协议主版本 %s 与本地 %s 不一致，按 degraded 继续",
                   name, version, PROTOCOL_VERSION)
        out["detail"] = "主版本不一致：对方 %s / 本地 %s" % (version, PROTOCOL_VERSION)
    return out


def protocol_summary(bridge=None):
    """三边 health/status 用的协议摘要：本地版本 + 各 provider 的 compat 状态。"""
    return {"role": "consumer", "protocol_version": PROTOCOL_VERSION,
            "providers": {name: compat(name, bridge) for name in PROVIDER_FILES}}
