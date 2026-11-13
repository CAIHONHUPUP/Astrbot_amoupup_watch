# -*- coding: utf-8 -*-
"""金融 ↔ 记忆插件（Astrbot_amoupup_data）联动层。

不碰表名/字段名，只调 amo 暴露的方法：
- 写入 → amo_memory_data.py HumanCorePlugin.finance_remember(content, kind, tier, idem_key)
         → 内部 _data_memory_write(...) → 记忆插件 remember(bot_id, uid, content, kind, importance, scope, subject="bot", idem_key=...)
- 检索 → amo_memory_data.py HumanCorePlugin.finance_recall(query, limit)
         → 内部 _data_memory_recall(...) → 记忆插件 recall(bot_id, uid, query, limit, scope, group_id, platform)
- 回读 → 写入后立刻 finance_recall 查一次，确认能检索到；查不到只标记 verified=False，不影响交易
"""
from __future__ import annotations

import logging

try:
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

try:
    from ..guard import isolated
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated

AMO_PLUGIN_NAME = "Astrbot_amoupup_forever"


class MemoryLink:
    def __init__(self, plugin):
        self.plugin = plugin

    def _amo(self):
        try:
            context = getattr(self.plugin, "context", None)
            getter = getattr(context, "get_registered_star", None) if context else None
            meta = getter(AMO_PLUGIN_NAME) if callable(getter) else None
            if meta is None or getattr(meta, "activated", True) is False:
                return None
            amo = getattr(meta, "star_cls", None)
            if amo is None:
                return None
            if not callable(getattr(amo, "finance_remember", None)):
                return None
            return amo
        except Exception as exc:
            isolated("finance.memory.amo", exc)
            return None

    async def remember(self, content, kind="finance", idem="", verify=True):
        """写一条长期记忆；写入后回读校验。返回 {ok, verified, reason?}。"""
        text = str(content or "").strip()
        if not text:
            return {"ok": False, "verified": False, "reason": "empty"}
        amo = self._amo()
        if amo is None:
            return {"ok": False, "verified": False, "reason": "memory 未就绪"}
        try:
            ok = bool(await amo.finance_remember(text, kind=kind, idem_key=str(idem or "")))
        except Exception as exc:
            isolated("finance.memory.write", exc)
            return {"ok": False, "verified": False, "reason": str(exc)}
        result = {"ok": ok, "verified": False}
        if ok and verify:
            try:
                rows = await amo.finance_recall(text[:24], limit=3)
                result["verified"] = bool(rows)
            except Exception as exc:
                isolated("finance.memory.verify", exc)
        if not ok:
            logger.warning("[FIN] 记忆写入失败 kind=%s idem=%s", kind, idem)
        return result

    async def recall(self, query, limit=5):
        amo = self._amo()
        if amo is None:
            return []
        try:
            rows = await amo.finance_recall(str(query or ""), limit=int(limit))
            return list(rows or [])
        except Exception as exc:
            isolated("finance.memory.recall", exc)
            return []

    async def context(self, limit=5):
        """给 LLM 的持仓/盈亏记忆摘要；检索失败返回空串（调用方用结构化数据兜底）。"""
        rows = await self.recall("当前持仓 股票 基金 最近交易 盈亏", limit=limit)
        lines = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = str(row.get("content") or row.get("text") or row.get("summary") or "").strip()
            if text:
                lines.append("- " + text[:160])
        return "\n".join(lines)
