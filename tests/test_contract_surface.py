# -*- coding: utf-8 -*-
"""Phase 15 契约回归：快照必须覆盖守护 _CALLS 全部方法，kwargs 必须被能陪签名接受。

设计（只读，不运行能陪）：
- 用 ast 扫能陪源码得到「方法 -> 参数名 / 是否 **kwargs」，全程不 import 能陪；
- 用 ast 扫守护自己的 bridge.call(...) 调用点，收集真正在用的关键字参数；
- 断言三件事：_(1)_ 快照覆盖 _CALLS；_(2)_ 每个 _CALLS 方法在能陪源码里真实存在；
  _(3)_ 守护传的关键字参数被能陪签名接受（或对方本来就有 **kwargs）。

能陪目录不在本机时跳过源码相关的两条，仅剩快照自身的一致性检查（仍可离线跑）。
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import amo_contract  # noqa: E402
from core.amo_bridge import _CALLS, _HIDDEN  # noqa: E402

PLUGINS_DIR = os.path.dirname(ROOT)
FOREVER_DIR = os.path.join(PLUGINS_DIR, "Astrbot_amoupup_forever")
SKIP_DIRS = {"__pycache__", ".git", "tests", "tools", "_test_tmp", ".test-tmp",
             "dist-publish", "data", "views"}


def _py_files(root):
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _provider_surface():
    """{方法名: (参数名集合, 是否有 **kwargs)}；只读扫描能陪源码。"""
    out = {}
    if not os.path.isdir(FOREVER_DIR):
        return {}
    for path in _py_files(FOREVER_DIR):
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = list(node.args.args)
            if not args or args[0].arg not in ("self", "cls"):
                continue
            params = {a.arg for a in args + list(node.args.kwonlyargs)
                      if a.arg not in ("self", "cls")}
            entry = out.setdefault(node.name, [set(), False])
            entry[0] |= params
            entry[1] = entry[1] or node.args.kwarg is not None
    return {name: (frozenset(value[0]), bool(value[1])) for name, value in out.items()}


def _watch_bridge_calls():
    """[(method, {kwargs}, "file:line")]：守护里 bridge.call/call_async 的真实调用点。"""
    found = []
    for path in _py_files(ROOT):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("call", "call_async"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            method = node.args[0].value
            if not isinstance(method, str):
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            found.append((method, kwargs, "%s:%d" % (rel, node.lineno)))
    return found


# ---------------------------------------------------------------- 快照覆盖
def test_snapshot_covers_every_registered_call():
    """反过来的兜底：_CALLS 里出现的每个方法，快照都必须收录。"""
    snapshot = amo_contract.methods("forever")
    missing = sorted(set(_CALLS) - set(snapshot))
    assert not missing, "快照没覆盖 _CALLS 里的方法：%s" % missing


def test_drifted_methods_are_registered_and_snapshotted():
    """Phase 15：两处真实漂移补进快照后，不许再走 getattr 兜底。"""
    assert tuple(_HIDDEN) == (), "漂移清零后不该再有 _HIDDEN 兜底方法"
    snapshot = amo_contract.methods("forever")
    for name in ("set_date_ending", "set_last_date_event"):
        assert name in _CALLS, "%s 必须登记进调用层 _CALLS" % name
        assert name in snapshot, "%s 必须收录进 forever 快照" % name


def test_known_drift_is_empty():
    """KNOWN_DRIFT 清零：有漂移必须写回 amo_contract 并单独说明。"""
    assert amo_contract.KNOWN_DRIFT == {}
    assert amo_contract.describe()["known_drift"] == {}
    for method in _CALLS:
        assert amo_contract.drift_reason(method) == ""


def test_snapshot_is_sorted_readonly_and_fingerprinted():
    path = amo_contract.snapshot_path("forever")
    with open(path, "rb") as fh:
        before = hashlib.sha1(fh.read()).hexdigest()
    summary = amo_contract.describe()
    with open(path, "rb") as fh:
        after = hashlib.sha1(fh.read()).hexdigest()
    assert before == after, "读取/校验契约不得改写快照文件"
    with io.open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    assert list(raw) == sorted(raw), "快照必须按方法名排序"
    assert all(isinstance(value, list) for value in raw.values())
    assert raw == {key: sorted(value) for key, value in raw.items()}, "参数必须排序"
    assert len(summary["version"]["forever"]) == 12
    assert summary["count"]["forever"] >= len(_CALLS)


# ---------------------------------------------------------------- 对端源码
def test_every_registered_call_exists_in_forever_source():
    surface = _provider_surface()
    if not surface:
        pytest.skip("本机没有能陪插件目录，跳过源码扫描")
    missing = sorted(name for name in _CALLS if name not in surface)
    assert not missing, "守护调了能陪没有的方法：%s" % missing


def test_snapshot_params_come_from_forever_signature():
    """快照里的参数名必须真的是对方签名里的参数（不是我们瞎写的）。"""
    surface = _provider_surface()
    if not surface:
        pytest.skip("本机没有能陪插件目录，跳过源码扫描")
    bad = []
    for name, params in amo_contract.load_surface("forever").items():
        info = surface.get(name)
        if info is None or info[1]:      # 对方有 **kwargs，签名不设限
            continue
        extra = sorted(set(params) - set(info[0]))
        if extra:
            bad.append((name, extra))
    assert not bad, "快照里出现了对方签名没有的参数：%s" % bad[:6]


def test_bridge_kwargs_are_accepted_by_forever_signature():
    """守护真正传的关键字参数，必须被能陪签名接受。"""
    surface = _provider_surface()
    if not surface:
        pytest.skip("本机没有能陪插件目录，跳过源码扫描")
    checked = 0
    bad = []
    for method, kwargs, where in _watch_bridge_calls():
        info = surface.get(method)
        if not kwargs or info is None or info[1]:
            continue
        checked += 1
        extra = sorted(kwargs - set(info[0]))
        if extra:
            bad.append("%s 调 %s 不接受 %s" % (where, method, ",".join(extra)))
    assert checked > 0, "应当至少有一个带关键字参数的 bridge.call 调用点"
    assert not bad, "关键字参数对不上：%s" % bad[:6]


def test_contract_version_is_stable_across_reads():
    first = amo_contract.surface_version("forever")
    amo_contract._cache.clear()          # 强制重读，版本指纹必须一致
    second = amo_contract.surface_version("forever")
    assert first == second and len(first) == 12
