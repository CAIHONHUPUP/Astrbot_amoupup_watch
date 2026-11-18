# -*- coding: utf-8 -*-
"""数据层：只碰自己的 SQLite，绝不读写 amo 的任何文件。"""
from .db import Store
from .ledger import FinanceRepo

from typing import Any

_MemoryIndex: Any = None
try:
    from .memory import MemoryIndex as _MemoryIndex
except ImportError:  # 可选模块，缺了也不影响主流程
    _MemoryIndex = None

MemoryIndex: Any = _MemoryIndex

__all__ = ["Store", "FinanceRepo", "MemoryIndex"]
