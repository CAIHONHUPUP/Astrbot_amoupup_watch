# -*- coding: utf-8 -*-
"""架构棘轮（2026-11-20 引入，与能陪伴同名但规则裁剪）。

两条防线：
1. **只降不升**：每个生产模块 import 了多少个插件内部模块，不许比基线多；
   减少时打印「可下调基线」。基线 tests/architecture_baseline.json。
2. **分层硬规则**：引擎模块（*_engine.py / engine.py）不许 import 入口 main，
   也不许 import 传输层 api.*（要保留请写理由并改规则）。

更新基线：python tests/test_architecture_ratchet.py --update-baseline
（只在依赖真的减少、或经评审的搬迁后更新，不许为了变绿手动调大。）
"""
import ast
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(TESTS, "architecture_baseline.json")
SKIP_DIRS = {"tests", "work", "__pycache__", "docs", "data", "tmp", ".git"}
ENTRY_MODULES = {"main"}


def _production_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            if base == ROOT and name.startswith("test_"):
                continue  # 根目录的手工测试脚本不算生产模块
            yield os.path.join(base, name)


def _module_names():
    names = set()
    for path in _production_files():
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        names.add(mod)
    return names


def _collect(names):
    fanout = {}
    for path in _production_files():
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        targets = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (node.level or 0) > 0:
                    if node.module:
                        targets.add(node.module.split(".")[0])
                    else:
                        for alias in node.names:
                            targets.add(alias.name.split(".")[0])
                else:
                    mod = (node.module or "").split(".")[0]
                    if mod in names:
                        targets.add(mod)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if mod in names:
                        targets.add(mod)
        targets.discard("")
        fanout[rel] = sorted(t for t in targets if t in names)
    return fanout


def _is_engine(rel):
    stem = os.path.basename(rel)[:-3] if rel.endswith(".py") else os.path.basename(rel)
    return stem == "engine" or stem.endswith("_engine")


def boundary_violations(fanout):
    """引擎不许 import 入口/传输层。返回 [(文件, 目标, 原因)]。"""
    found = []
    for rel, targets in sorted(fanout.items()):
        if not _is_engine(rel):
            continue
        for target in targets:
            if target in ENTRY_MODULES:
                found.append((rel, target, "引擎不得反向 import 入口层"))
            elif target == "api":
                found.append((rel, target, "引擎不得 import 传输层 api"))
    return found


def load_baseline():
    with open(BASELINE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class ArchitectureRatchetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.names = _module_names()
        cls.fanout = _collect(cls.names)

    def test_no_new_internal_imports(self):
        baseline = load_baseline()
        added = {}
        for rel, targets in self.fanout.items():
            extra = sorted(set(targets) - set(baseline.get(rel, [])))
            if extra:
                added[rel] = extra
        self.assertEqual({}, added, "新增了内部模块依赖（棘轮只降不升）：%s" % added)

    def test_removed_imports_are_reported(self):
        baseline = load_baseline()
        removed = {}
        for rel, allowed in baseline.items():
            gone = sorted(set(allowed) - set(self.fanout.get(rel, [])))
            if gone:
                removed[rel] = gone
        if removed:
            print("[architecture] 依赖已减少，可下调基线：",
                  json.dumps(removed, ensure_ascii=False))

    def test_import_boundaries(self):
        self.assertEqual([], boundary_violations(self.fanout),
                         "跨层 import 违反硬规则")

    def test_reverse_probe_is_caught(self):
        probe = {"services/daily_engine.py": ["main", "api"]}
        found = boundary_violations(probe)
        self.assertEqual(2, len(found))


def _generate_baseline():
    fanout = _collect(_module_names())
    boundary = boundary_violations(fanout)
    assert not boundary, boundary
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(fanout, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print("baseline written:", BASELINE_PATH, len(fanout), "files")


if __name__ == "__main__":
    if "--update-baseline" in sys.argv:
        _generate_baseline()
    else:
        unittest.main()
