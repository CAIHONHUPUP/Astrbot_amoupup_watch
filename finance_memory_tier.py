# -*- coding: utf-8 -*-
"""四层记忆：永久 / 长期(90天) / 短期(7天·覆盖) / 临时(3天·合并)。

- 写入：先查 memory_index（守护自己的库）做幂等，再调 amo.finance_remember；
- 检索：按层限流（永久全量、长期30天、短期/临时24小时），总条数 15–20；
- 压缩：长期层超过 5000 条时，把 90 天前明细按月汇总，原明细标记 archived。
"""
from __future__ import annotations

import json
import time

try:
    from .guard import isolated
except ImportError:  # 直接按文件加载时
    from guard import isolated

TIERS = ("permanent", "long", "short", "news")
TTL_DAYS = {"permanent": 0, "long": 90, "short": 7, "news": 3}
RETRIEVE_WINDOW = {"permanent": 0, "long": 30, "short": 1, "news": 1}
MAX_ITEMS = 18
LONG_COMPRESS_AT = 5000


class TieredMemory:
    def __init__(self, plugin, repo, memory):
        self.plugin = plugin
        self.repo = repo
        self.memory = memory

    # ---------- 写 ----------
    def _exists(self, idem):
        rows = self.repo._rows("SELECT 1 FROM memory_index WHERE idem=?", (str(idem),))
        return bool(rows)

    def _index(self, idem, tier, content, related, ttl_days):
        now = time.time()
        self.repo.store.write(
            "INSERT OR IGNORE INTO memory_index(idem, tier, content, related, created, expires_at, status) "
            "VALUES(?,?,?,?,?,?, 'active')",
            (str(idem), str(tier), str(content), str(related), now,
             now + (ttl_days * 86400 if ttl_days else 0)),
        )

    async def remember(self, tier, content, idem, related=""):
        if tier not in TIERS:
            tier = "long"
        text = str(content or "").strip()
        if not text or self._exists(idem):
            return False
        ttl = TTL_DAYS[tier]
        self._index(idem, tier, text, related, ttl)
        try:
            ok = await self.memory.remember(text, kind="finance_" + tier, idem=idem)
        except Exception as exc:
            isolated("finance.tier.write", exc)
            ok = False
        if not ok:
            self.repo.store.write("DELETE FROM memory_index WHERE idem=?", (str(idem),))
        return bool(ok)

    async def overwrite_short(self, related, content, idem):
        """短期层覆盖式：同一持仓只留最新一条。"""
        rows = self.repo._rows(
            "SELECT idem FROM memory_index WHERE tier='short' AND related=? AND status='active'",
            (str(related),))
        for row in rows:
            self.repo.store.write("UPDATE memory_index SET status='archived' WHERE idem=?", (row["idem"],))
        return await self.remember("short", content, idem, related=related)

    # ---------- 读 ----------
    def retrieve(self, related_codes=None, limit=MAX_ITEMS):
        now = time.time()
        codes = set(str(c) for c in (related_codes or []))
        picked = []
        for tier in TIERS:
            window = RETRIEVE_WINDOW[tier]
            rows = self.repo._rows(
                "SELECT idem, tier, content, related, created FROM memory_index "
                "WHERE tier=? AND status='active' AND (expires_at=0 OR expires_at>?) "
                "ORDER BY created DESC LIMIT 60", (tier, now))
            for row in rows:
                if window and now - float(row["created"]) > window * 86400:
                    continue
                if tier in ("long", "short", "news") and codes:
                    if row["related"] and row["related"] not in codes:
                        continue
                picked.append(row)
                if len(picked) >= limit:
                    break
            if len(picked) >= limit:
                break
        return picked

    def context_text(self, related_codes=None, limit=MAX_ITEMS):
        rows = self.retrieve(related_codes, limit)
        return "\n".join("- " + str(r["content"]) for r in rows)

    # ---------- 压缩 ----------
    def compress_long(self, now=None):
        now = now or time.time()
        count = self.repo._rows(
            "SELECT COUNT(*) AS c FROM memory_index WHERE tier='long' AND status='active'")[0]["c"]
        if int(count) <= LONG_COMPRESS_AT:
            return 0
        cutoff = now - 90 * 86400
        rows = self.repo._rows(
            "SELECT * FROM memory_index WHERE tier='long' AND status='active' AND created<? ORDER BY created",
            (cutoff,))
        buckets = {}
        for row in rows:
            month = time.strftime("%Y-%m", time.localtime(float(row["created"])))
            buckets.setdefault(month, []).append(row["content"])
        for month, contents in buckets.items():
            summary = "%s 长期记忆汇总：共%d条，示例：%s" % (month, len(contents), contents[0][:40])
            self.repo.store.write(
                "INSERT OR IGNORE INTO memory_index(idem, tier, content, related, created, expires_at, status) "
                "VALUES(?,?,?,?,?,0,'active')",
                ("compress:" + month, "long", summary, "", now),
            )
            for row in rows:
                if time.strftime("%Y-%m", time.localtime(float(row["created"]))) == month:
                    self.repo.store.write("UPDATE memory_index SET status='archived' WHERE idem=?", (row["idem"],))
        return len(buckets)
