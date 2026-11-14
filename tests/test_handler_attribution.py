# -*- coding: utf-8 -*-
"""handler 归属护栏：注册语句必须在插件类体里（含类体内的 if 块）。

挪到类外/模块级会让 handler_module_path 变成 services.*，
AstrBot 派发时静默跳过（amo 2026-11-10 踩过这个坑）。
"""
import ast
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


def _cls():
    tree = ast.parse(io.open(MAIN, encoding="utf-8").read())
    return next(n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "WatchPlugin")


def _statements(nodes):
    """类体层语句（递归展开 if/else），跳过方法/嵌套类内部。"""
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield node
        if isinstance(node, ast.If):
            yield from _statements(node.body)
            yield from _statements(node.orelse)


def test_command_handlers_bound_inside_plugin_class():
    bound = []
    for node in _statements(_cls().body):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        dumped = ast.dump(node.value)
        if "filter" in dumped and "command" in dumped:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.append(target.id)
    # 同一个方法可挂多条指令（social_cmd 一条方法接 7 个命令），所以数“注册次数”
    assert len(bound) >= 8, "类体内只找到 %d 条指令注册：%s" % (len(bound), bound)


def test_llm_tool_bound_inside_plugin_class():
    found = 0
    for node in _statements(_cls().body):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if "llm_tool" in ast.dump(node.value):
                found += 1
    assert found >= 1, "类体里没有 llm_tool 注册（装饰器可能被挪出类体）"
