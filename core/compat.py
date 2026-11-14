# -*- coding: utf-8 -*-
"""双路径导入：包内相对导入失败时回退到顶层绝对导入。

main.py 里几十条
    try:
        from .a import X
    except ImportError:
        from a import X
统一收口到这里，少一层缩进和重复。
"""
from __future__ import annotations

import importlib


def load(root, module, *names):
    """root=调用方的 __package__；先按包内相对导入，失败退回顶层。"""
    mod = None
    if root:
        try:
            mod = importlib.import_module("." + module, root)
        except Exception:
            mod = None
    if mod is None:
        mod = importlib.import_module(module)
    if not names:
        return mod
    values = tuple(getattr(mod, name) for name in names)
    return values[0] if len(values) == 1 else values
