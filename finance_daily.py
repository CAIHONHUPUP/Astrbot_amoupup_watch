# -*- coding: utf-8 -*-
"""三餐：每天在窗口内由她自己挑时点，按单品逐笔扣款并写记忆/小日记。

早餐 7:30–9:00，午餐 11:30–13:00，晚餐 17:30–19:30。
每个时段每天只吃一次（meta 幂等）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib

try:
    from .guard import isolated
except ImportError:  # 直接按文件加载时
    from guard import isolated

WINDOWS = {
    "breakfast": ((7, 30), (9, 0)),
    "lunch": ((11, 30), (13, 0)),
    "dinner": ((17, 30), (19, 30)),
}

MENUS = {
    "breakfast": (("豆浆", 1, "肠粉", 1), ("包子", 1, "白粥", 1), ("豆浆", 1, "油条", 1)),
    "lunch": (("快餐", 1,), ("面条", 1,), ("米饭套餐", 1,)),
    "dinner": (("米饭套餐", 1,), ("自己做饭", 1,), ("面条", 1,)),
}


def _pick(day, slot, options):
    digest = hashlib.sha256(("%s:%s" % (day, slot)).encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


class FinanceDaily:
    def __init__(self, plugin, repo, finance, consume):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.consume = consume

    def _in_window(self, slot, now):
        start, end = WINDOWS[slot]
        minutes = now.hour * 60 + now.minute
        return start[0] * 60 + start[1] <= minutes < end[0] * 60 + end[1]

    async def tick(self, now=None):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        done = []
        for slot in WINDOWS:
            if not self._in_window(slot, now):
                continue
            flag = "meal:%s:%s" % (day, slot)
            if self.repo.meta_get(flag):
                continue
            items = _pick(day, slot, MENUS[slot])
            pairs = [{"item": items[i], "qty": items[i + 1]} for i in range(0, len(items), 2)]
            results = await self.consume.buy_items(pairs, idem="meal:%s:%s" % (day, slot), when=slot)
            if any(r.get("ok") for r in results):
                self.repo.meta_set(flag, "1")
            done.append({"slot": slot, "items": pairs, "ok": [r.get("ok") for r in results]})
        return done
