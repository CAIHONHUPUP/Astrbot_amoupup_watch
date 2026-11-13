# -*- coding: utf-8 -*-
"""记忆索引：本地只存「写过什么」和过期时间，正文在 amo。

四层 TTL 由上层（services 的 TieredMemory）决定，这里只管存取：
- exists/put/delete：幂等写入；
- expired(now)：给压缩/清理用；
- count()：给压缩阈值用（长期层超过 5000 条压缩）。
"""
from __future__ import annotations

import time


class MemoryIndex:
    def __init__(self, store):
        self.store = store

    def _rows(self, sql, params=()):
        rows = self.store.query(sql, params)
        return [dict(r) if not isinstance(r, dict) else r for r in rows]

    def exists(self, idem):
        return bool(self._rows("SELECT 1 FROM memory_index WHERE idem=?", (str(idem),)))

    def put(self, idem, tier, content, related="", ttl_days=0, now=None):
        stamp = float(now if now is not None else time.time())
        ttl = max(0, int(ttl_days or 0))
        return bool(self.store.write(
            "INSERT OR IGNORE INTO memory_index(idem, tier, content, related, created, expires_at, status) "
            "VALUES(?,?,?,?,?,?, 'active')",
            (str(idem), str(tier), str(content or ""), str(related or ""), stamp,
             stamp + ttl * 86400 if ttl else 0)))

    def delete(self, idem):
        return bool(self.store.write("DELETE FROM memory_index WHERE idem=?", (str(idem),)))

    def archive(self, idem):
        return bool(self.store.write("UPDATE memory_index SET status='archived' WHERE idem=?",
                                     (str(idem),)))

    def count(self, tier=""):
        if tier:
            rows = self._rows("SELECT COUNT(*) AS c FROM memory_index WHERE tier=? AND status='active'",
                              (str(tier),))
        else:
            rows = self._rows("SELECT COUNT(*) AS c FROM memory_index WHERE status='active'")
        return int(rows[0]["c"]) if rows else 0

    def expired(self, now=None):
        stamp = float(now if now is not None else time.time())
        return self._rows("SELECT idem FROM memory_index WHERE status='active' "
                          "AND expires_at>0 AND expires_at<?", (stamp,))
