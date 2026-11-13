# -*- coding: utf-8 -*-
"""数据层：只碰自己的库（plugin_data/Astrbot_amoupup_watch），不读 amo 任何文件。

- db: 连接 / 路径守卫 / schema 迁移
- ledger: 资产、贷款、流水、结算（余额权威仍在 amo.wallet）
- memory: 记忆索引（真正的记忆写 amo，这里只做幂等与分层 TTL）
"""
from __future__ import annotations

from .db import SCHEMA, SCHEMA_VERSION, Store  # noqa: F401
from .ledger import FinanceRepo  # noqa: F401
from .memory import MemoryIndex  # noqa: F401

__all__ = ["Store", "FinanceRepo", "MemoryIndex", "SCHEMA", "SCHEMA_VERSION"]
