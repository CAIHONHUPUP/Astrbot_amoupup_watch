# -*- coding: utf-8 -*-
"""数据层：只碰自己的 SQLite，绝不读写 amo 的任何文件。"""
from .db import Store
from .ledger import FinanceRepo

try:
    from .memory import MemoryIndex
except Exception:  # 可选模块
    MemoryIndex = None

__all__ = ["Store", "FinanceRepo", "MemoryIndex"]
