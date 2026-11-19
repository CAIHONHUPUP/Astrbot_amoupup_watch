# -*- coding: utf-8 -*-
"""生产命名空间导入守卫。

AstrBot 按 ``data.plugins.<插件名>.main`` 加载插件，所以 ``core/`` 里的模块
``__package__`` 是 ``...<插件名>.core``：

* 同包模块写 ``from .llm_tools import x``；
* 兄弟子包写 ``from ..services.date_engine import y``；
* 写成 ``from .core.llm_tools`` / ``from .services.date_engine``（或兜底的
  ``from core.xxx`` / ``from services.xxx``）在线上都会解析到不存在的路径。

2026-11-19 线上 20 个 LLM 工具就是因为这个全部 ``ModuleNotFoundError``，而单测把
``core`` 当成顶层包、兜底那句恰好能过，所以一直是绿的。这里按 AstrBot 的命名空间
真导入一次，并静态拦掉「一级相对导入越界」。
"""
import ast
import importlib
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("WATCH_PLUGIN_ROOT") or os.path.dirname(HERE)
SIBLINGS = ("core", "services", "api", "ui")
ALIAS = "dsh_watch_probe.plugins.Astrbot_amoupup_watch"

# 混入里必须存在的关键工具（覆盖 it 里最容易坏的几条链路）
REQUIRED_TOOLS = (
    "propose_date_tool", "make_date_tool", "cancel_date_tool", "attend_date_tool",
    "confirm_arrival", "buy_insurance", "buy_gift", "deposit_money",
    "buy_stock", "sell_stock", "buy_fund", "sell_fund",
    "accept_social_event", "reject_social_event", "send_message_to_user",
    "skip_fund_trade", "reallocate_funds", "rent_out_property", "sell_property",
)


def _py_files(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", "tests", "data"))
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _mount(alias):
    parts = alias.split(".")
    for i in range(1, len(parts) + 1):
        name = ".".join(parts[:i])
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [ROOT]
            sys.modules[name] = module


class RelativeImportBoundaryTests(unittest.TestCase):
    def test_no_one_dot_import_escapes_own_package(self):
        bad = []
        for sub in SIBLINGS:
            base = os.path.join(ROOT, sub)
            if not os.path.isdir(base):
                continue
            for path in _py_files(base):
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.level == 1:
                        head = (node.module or "").split(".")[0]
                        if head in SIBLINGS:
                            bad.append("%s:%d  from .%s"
                                       % (os.path.relpath(path, ROOT), node.lineno, node.module))
        self.assertEqual(bad, [],
                         "子包里的一级相对导入越界（同包用 .x，兄弟包用 ..x）：\n" + "\n".join(bad))


class ProductionNamespaceTests(unittest.TestCase):
    def setUp(self):
        _mount(ALIAS)

    def tearDown(self):
        for name in [n for n in list(sys.modules) if n.startswith("dsh_watch_probe")]:
            del sys.modules[name]

    def test_tool_wrappers_import_in_astrbot_namespace(self):
        module = importlib.import_module(ALIAS + ".core.tool_wrappers")
        mixin = module.ToolWrappersMixin
        names = [n for n, value in vars(mixin).items()
                 if callable(value) and not n.startswith("_")]
        self.assertGreaterEqual(len(names), 19, names)
        for required in REQUIRED_TOOLS:
            self.assertIn(required, names)
        self.assertTrue(callable(getattr(module, "modify_date_tool", None)))

    def test_llm_tools_and_sibling_packages_resolve(self):
        importlib.import_module(ALIAS + ".core.llm_tools")
        for target in ("core.fund_actions", "services.date_engine", "core.llm_decider"):
            spec = importlib.util.find_spec(ALIAS + "." + target)
            self.assertIsNotNone(spec, "包路径解析失败：%s" % target)


if __name__ == "__main__":
    unittest.main()
