# -*- coding: utf-8 -*-
"""相对导入自检：把「文件搬进子包后导入没跟着改」这类错误在插件启动时就打成 ERROR。

背景：2026-11-19 线上 20 个 LLM 工具全部 ModuleNotFoundError，根因是
`core/tool_wrappers.py` 写成 `from .core.llm_tools import ...`（解析成 core.core），
而它包在 try/except 里，导入期看不出问题，只有模型真的调用工具时才炸。

这里不执行被检查的模块，只按 Python 的包解析规则把每条相对导入还原成绝对模块名，
再去插件目录里找对应文件。几十条导入只要几毫秒，可以在 initialize() 里每次启动都跑。
"""
from __future__ import annotations

import ast
import os

SUBPACKAGES = ("core", "services", "api", "ui")


def _module_of(path: str, root: str, package: str) -> str:
    rel = os.path.relpath(path, root).replace(os.sep, ".")[:-3]
    return (package + "." + rel) if package else rel


def _exists(root: str, package: str, target: str) -> bool:
    if package:
        if not (target == package or target.startswith(package + ".")):
            return True  # 插件包以外的模块不归这里管
        rest = target[len(package) + 1:] if target != package else ""
    else:
        rest = target
    if not rest:
        return True
    path = os.path.join(root, rest.replace(".", os.sep))
    return os.path.isfile(path + ".py") or os.path.isfile(os.path.join(path, "__init__.py"))


def check(root: str, package: str) -> tuple[int, list[str]]:
    """返回 (检查条数, 解析失败列表)；列表为空表示全部正常。"""
    checked = 0
    problems: list[str] = []
    for sub in SUBPACKAGES:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                try:
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read(), filename=path)
                except (OSError, SyntaxError) as exc:
                    problems.append("%s 读不了/语法错: %s" % (rel, exc))
                    continue
                module = _module_of(path, root, package)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom) or node.level <= 0:
                        continue
                    parts = module.split(".")
                    keep = len(parts) - node.level
                    head = ".".join(parts[:keep]) if keep > 0 else ""
                    target = (head + "." + node.module) if node.module else head
                    checked += 1
                    if not target or not _exists(root, package, target):
                        problems.append(
                            "%s:%d  from %s%s import %s  ->  %s 不存在"
                            % (rel, node.lineno, "." * node.level, node.module or "", "…",
                               target or "<插件包外>"))
    return checked, problems
