# -*- coding: utf-8 -*-
"""跨插件契约快照：watch 调 amo / 能记 的每个方法，都必须在对方源码里真实存在。

过去踩过的坑：守护调用 `data.nightly_memory_candidates`，能记里根本没有这个方法，
`hasattr` 又把「契约写错」吞成「插件没装」，整条链路空跑几个月没人发现。

做法（纯 AST，不 import 对方插件，装不装对方都能跑）：
1. 扫本仓库里所有 `amo.xxx(...)` / `data.xxx(...)` 调用，拿到方法名和关键字参数；
2. 扫对方仓库的公开函数/方法，得到「方法名 -> 允许的参数名」；
3. 断言方法存在、关键字参数被接受；
4. 把用到的「方法 -> 参数」写成快照，对方改签名时这里会红，必须显式更新。

对方插件不在同一台机器上（单独装包）时自动跳过。
更新快照：
    python tests/test_cross_plugin_contract.py --update
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.dirname(ROOT)
CONTRACT_DIR = os.path.join(ROOT, "tests", "contracts")
SKIP_DIRS = {"__pycache__", ".git", "tests", "tools", "_test_tmp", ".test-tmp",
             "dist-publish", "data", "views"}

#: 容器/字符串的内置方法：变量名 `data` 在本仓库里也常是普通 dict，别当成插件接口
BUILTIN_METHODS = frozenset((
    "get", "setdefault", "items", "keys", "values", "update", "pop", "popitem", "copy",
    "clear", "append", "extend", "insert", "remove", "discard", "add", "index", "count",
    "sort", "reverse", "format", "strip", "lstrip", "rstrip", "split", "join", "lower",
    "upper", "replace", "startswith", "endswith", "encode", "decode", "read", "write",
))

#: 本仓库里代表「对方插件实例」的变量名 -> 对方插件目录名 / 只看哪个类（None=全文）
PROVIDERS = {
    "Astrbot_amoupup_forever": {"vars": ("amo",), "cls": None},
    "Astrbot_amoupup_data": {"vars": ("data", "mem"), "cls": "MemoryAPI"},
}


def _py_files(root):
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _chain(node):
    """把 a.b.c 拆成 ("a", ["b", "c"])；不是属性链返回 (None, [])。"""
    attrs = []
    cur = node
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        attrs.reverse()
        return cur.id, attrs
    if isinstance(cur, ast.Call):  # self._amo().wallet.xxx 这类，取不到根就放过
        return None, []
    return None, []


def consumer_calls(root, vars_):
    """[(方法名, {关键字参数}, "文件:行", 完整链)]"""
    found = []
    for path in _py_files(root):
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
        except (SyntaxError, OSError):
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            root_name, attrs = _chain(node.func)
            if root_name is None or not attrs:
                continue
            if root_name == "self":
                if not attrs or attrs[0] not in vars_:
                    continue
                method, full = attrs[-1], "self." + ".".join(attrs)
            else:
                if root_name not in vars_:
                    continue
                method, full = attrs[-1], ".".join([root_name] + attrs)
            if method in BUILTIN_METHODS:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            found.append((method, kwargs, "%s:%d" % (rel, node.lineno), full))
    return found


def provider_surface(dir_path, cls_name=None):
    """{方法名: {"params": set, "var_kw": bool}}"""
    out = {}
    if not os.path.isdir(dir_path):
        return out
    for path in _py_files(dir_path):
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
        except (SyntaxError, OSError):
            continue
        targets = []
        if cls_name:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == cls_name:
                    targets = [n for n in node.body
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    break
        else:
            targets = [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in targets:
            params = {a.arg for a in list(fn.args.args) + list(fn.args.kwonlyargs)
                      if a.arg not in ("self", "cls")}
            entry = out.setdefault(fn.name, {"params": set(), "var_kw": False})
            entry["params"] |= params
            entry["var_kw"] = entry["var_kw"] or fn.args.kwarg is not None
    return out


def available():
    """只保留本机装着的对方插件。"""
    return {name: cfg for name, cfg in PROVIDERS.items()
            if os.path.isdir(os.path.join(PLUGINS_DIR, name)) for cfg in [cfg]}


def snapshot_path(provider):
    return os.path.join(CONTRACT_DIR, provider + "_surface.json")


def build_snapshot(provider, cfg):
    surface = provider_surface(os.path.join(PLUGINS_DIR, provider), cfg["cls"])
    used = sorted({call[0] for call in consumer_calls(ROOT, cfg["vars"]) if call[0] in surface})
    return {name: sorted(surface[name]["params"]) for name in used}


class CrossPluginContractTests(unittest.TestCase):
    def test_called_methods_exist(self):
        providers = available()
        self.assertTrue(providers, "本机没有对方插件目录，契约测试跳过")
        for name, cfg in providers.items():
            surface = provider_surface(os.path.join(PLUGINS_DIR, name), cfg["cls"])
            calls = consumer_calls(ROOT, cfg["vars"])
            missing = sorted({(call[0], call[2], call[3]) for call in calls
                              if call[0] not in surface})
            with self.subTest(provider=name):
                self.assertFalse(missing,
                                 "调用了 %s 里不存在的方法：%s" % (name, missing[:8]))

    def test_kwargs_are_accepted(self):
        providers = available()
        for name, cfg in providers.items():
            surface = provider_surface(os.path.join(PLUGINS_DIR, name), cfg["cls"])
            bad = []
            for method, kwargs, where, full in consumer_calls(ROOT, cfg["vars"]):
                info = surface.get(method)
                if info is None or info["var_kw"] or not kwargs:
                    continue
                extra = sorted(kwargs - info["params"])
                if extra:
                    bad.append("%s 调 %s(%s 不接受 %s)" % (where, method, full,
                                                          ",".join(extra)))
            with self.subTest(provider=name):
                self.assertFalse(bad, "关键字参数对不上：%s" % bad[:6])

    def test_surface_snapshot(self):
        providers = available()
        for name, cfg in providers.items():
            current = build_snapshot(name, cfg)
            path = snapshot_path(name)
            with self.subTest(provider=name):
                if not os.path.exists(path):
                    self.fail("缺少契约快照 %s，跑 python tests/test_cross_plugin_contract.py --update"
                              % os.path.basename(path))
                with io.open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                if current != saved:
                    diff = {k: (saved.get(k), current.get(k))
                            for k in set(current) | set(saved) if saved.get(k) != current.get(k)}
                    self.fail("对方接口表面变了（%s）：%s；确认没影响后更新快照" % (name, diff))


if __name__ == "__main__":
    if "--update" in sys.argv:
        os.makedirs(CONTRACT_DIR, exist_ok=True)
        for provider, cfg in available().items():
            payload = build_snapshot(provider, cfg)
            with io.open(snapshot_path(provider), "w", encoding="utf-8", newline="") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            print("snapshot:", provider, len(payload), "methods")
    else:
        unittest.main()
