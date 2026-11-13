# -*- coding: utf-8 -*-
"""候选池：固定锚点 + 用户推荐 + 行情观察。

- 锚点池：配置里的 20–30 只 A 股，启动即加载，不依赖外部接口；
- 推荐池："建议买 600519/贵州茅台" 写入，7 天过期，上限 10 只，只入池不买入；
- 观察池：三档行情时对锚点批量拉取，按 |涨跌幅| + 成交额筛前 5–8 只，存当日 meta。
"""
from __future__ import annotations

import hashlib
import json
import re
import time

try:
    from .guard import isolated
except ImportError:  # 直接按文件加载时
    from guard import isolated

#: 平价底仓池：一手（100 股）大致 200–2000 元，适合她 2.3 万本金的体量。
#: 价格会变，这里只定“看谁”，下单前一律以腾讯实时价为准。
DEFAULT_ANCHORS = (
    "601398", "601988", "601939", "601288", "601328", "601818", "600016", "601998",
    "601668", "601390", "601766", "600028", "601857", "600019", "600050", "600905",
    "600011", "600795", "601991", "601600", "601816", "601985", "600968", "601598",
)

#: 每日轮换用的底仓池（40 只，仍全部是低价大盘，不扫全市场）
SEED_POOL = DEFAULT_ANCHORS + (
    "000725", "000100", "000001", "002027", "002415", "600606", "600170", "600325",
    "601618", "601117", "601866", "600018", "600115", "600221", "600027", "600583",
)

MAX_RECOMMEND = 10
RECOMMEND_DAYS = 7
OBSERVE_MAX = 8


def parse_recommendation(text, name_to_code=None):
    """从「建议买贵州茅台600519」里解析代码；只有名字时查锚点表。"""
    raw = str(text or "")
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    if match:
        return match.group(1)
    for name, code in (name_to_code or {}).items():
        if name and name in raw:
            return code
    return ""


def observation_pool(quotes, limit=OBSERVE_MAX):
    """按 |涨跌幅| + 成交额粗排，取前 N（只从给定列表里选，不生成新代码）。"""
    scored = []
    for code, quote in (quotes or {}).items():
        try:
            price = float(quote.get("price") or 0)
            prev = float(quote.get("prev_close") or 0)
            volume = float(quote.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or prev <= 0:
            continue
        change = abs(price - prev) / prev
        turnover = price * volume
        scored.append((change * 100 + turnover / 1e6, code))
    scored.sort(reverse=True)
    return [code for _, code in scored[:max(1, int(limit))]]


class CandidatePools:
    def __init__(self, plugin, repo):
        self.plugin = plugin
        self.repo = repo

    def anchors(self):
        raw = ""
        try:
            raw = str(getattr(self.plugin, "config", {}).get("finance_anchor_codes") or "")
        except Exception:
            raw = ""
        codes = [c.strip() for c in re.split(r"[,，\s]+", raw) if c.strip().isdigit() and len(c.strip()) == 6]
        return codes or list(DEFAULT_ANCHORS)

    def daily_anchors(self, day, count=24):
        """每天从固定底仓池里轮换一批（确定性洗牌，不扫全市场）。

        轮换结果 = 当天洗牌前 N + 昨天的观察池（最多 3 只），保证连续性。
        """
        pool = list(SEED_POOL)
        if self.plugin is not None:
            try:
                configured = str(getattr(self.plugin, "config", {}).get("finance_seed_pool") or "")
                extra = [c.strip() for c in re.split(r"[,，\s]+", configured)
                         if c.strip().isdigit() and len(c.strip()) == 6]
                if extra:
                    pool = extra
            except Exception:
                pass
        digest = hashlib.sha256(("anchors:" + str(day)).encode("utf-8")).hexdigest()
        ordered = sorted(pool, key=lambda code: hashlib.sha256((digest + code).encode()).hexdigest())
        chosen = ordered[:max(1, int(count))]
        for code in self.observation()[:3]:
            if code not in chosen:
                chosen.append(code)
        return chosen[:max(1, int(count)) + 3]

    def affordable(self, quotes, cash_cents, cap_ratio=0.5):
        """只留下“一手买得起、且不超过现金 50%”的代码。"""
        out = []
        budget = int(cash_cents) * float(cap_ratio)
        for code, quote in (quotes or {}).items():
            try:
                price = float(quote.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            need = int(round(price * 100)) * 100 + 600
            if need <= budget and need <= int(cash_cents):
                out.append(code)
        return out

    def name_map(self):
        mapping = {}
        try:
            for row in self.repo.candidates():
                if row.get("name"):
                    mapping[str(row["name"])] = str(row["code"])
            cached = self.repo.meta_get("quote_names")
            if cached:
                mapping.update(json.loads(cached))
        except Exception:
            pass
        return mapping

    def recommend(self, text):
        code = parse_recommendation(text, self.name_map())
        if not code:
            return {"ok": False, "message": "没听出是哪只票，发我 6 位代码吧"}
        rows = self.repo.candidates()
        if len(rows) >= MAX_RECOMMEND and code not in {r["code"] for r in rows}:
            oldest = rows[-1]
            self.repo.store.write("DELETE FROM candidates WHERE code=?", (oldest["code"],))
        self.repo.add_candidate(code, self.name_map().get(code, ""), "user", RECOMMEND_DAYS)
        return {"ok": True, "code": code, "message": "记下了，先放进候选池看看（不买）"}

    def remember_names(self, quotes):
        try:
            names = {str(c): str(q.get("name") or "") for c, q in (quotes or {}).items() if q.get("name")}
            if names:
                self.repo.meta_set("quote_names", json.dumps(names, ensure_ascii=False))
        except Exception:
            pass

    def observe(self, quotes, day):
        pool = observation_pool(quotes)
        try:
            self.repo.meta_set("observation_pool", json.dumps({"day": day, "codes": pool}, ensure_ascii=False))
        except Exception as exc:
            isolated("finance.candidates.observe", exc)
        return pool

    def observation(self):
        try:
            raw = self.repo.meta_get("observation_pool")
            data = json.loads(raw) if raw else {}
            return list(data.get("codes") or [])
        except Exception:
            return []

    def all_codes(self):
        codes = list(self.anchors())
        for row in self.repo.candidates():
            code = str(row.get("code") or "")
            if code and code not in codes:
                codes.append(code)
        for code in self.observation():
            if code not in codes:
                codes.append(code)
        return codes
