# -*- coding: utf-8 -*-
"""14:50 基金行情刷新 + 给 LLM 的基金事实块。"""
from __future__ import annotations

import datetime as _dt
import json

try:  # 包模式
    from ..guard import isolated
    from ..utils.helpers import is_trading_day
    from .fund_api import fetch_fund_quotes
except ImportError:  # 直接按文件加载 / 单测
    from guard import isolated
    from utils.helpers import is_trading_day
    from services.fund_api import fetch_fund_quotes

REFRESH_MINUTE = 14 * 60 + 50
CLOSE_MINUTE = 15 * 60
MAX_TRIES = 3


def _quote_from_row(row):
    row = dict(row or {})
    dwjz = int(row.get("dwjz_e4") or 0) / 10000.0
    gsz = int(row.get("gsz_e4") or 0) / 10000.0
    change = int(row.get("gszzl_bp") or 0) / 100.0
    return {
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "dwjz": dwjz,
        "gsz": gsz,
        "gszzl": change,
        "nav": dwjz,
        "estimate": gsz or dwjz,
        "change_pct": change,
        "time": str(row.get("gztime") or row.get("day") or ""),
        "source": str(row.get("source") or ""),
        "tradeable": bool(row.get("tradeable")),
        "stale": not bool(row.get("tradeable")),
    }


class FundMarket:
    def __init__(self, plugin, runtime, repo):
        self.plugin = plugin
        self.runtime = runtime
        self.repo = repo

    def _pool(self):
        try:
            from ..core.fund_pool import FundPool
        except ImportError:
            from core.fund_pool import FundPool
        return FundPool(self.plugin)

    def _codes(self, day):
        try:
            return list(self._pool().codes(day) or [])
        except Exception as exc:
            isolated("finance.fund.codes", exc)
            return []

    def _save(self, day, quotes):
        try:
            self.repo.save_fund_quotes(day, quotes)
        except Exception as exc:
            isolated("finance.fund.save", exc)

    def _update_positions(self, quotes):
        try:
            for row in self.repo.positions("fund"):
                code = str(row.get("code") or "")
                quote = (quotes or {}).get(code)
                if not quote:
                    continue
                estimate = float(quote.get("estimate") or quote.get("nav") or 0)
                if estimate <= 0:
                    continue
                self.repo.upsert_position(
                    "fund", code, quote.get("name") or row.get("name") or code,
                    row.get("qty"), int(row.get("cost_cents") or 0),
                    int(round(estimate * 100)), row.get("available"),
                    row.get("ready_day") or "")
        except Exception as exc:
            isolated("finance.fund.positions", exc)

    async def refresh(self, now=None, force=False):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        if not is_trading_day(day):
            return {"ok": True, "skipped": "holiday", "day": day}
        minutes = now.hour * 60 + now.minute
        if not force and minutes < REFRESH_MINUTE:
            return {"ok": True, "skipped": "before_14:50", "day": day}
        if not force and minutes >= CLOSE_MINUTE:
            return {"ok": True, "skipped": "after_close", "day": day}
        flag_key = "fund_refresh:" + day
        done = str(self.repo.meta_get(flag_key, "") or "")
        if done and not force:
            return {"ok": True, "skipped": "done", "day": day, "flag": done}
        codes = self._codes(day)
        if not codes:
            return {"ok": False, "skipped": "empty_pool", "day": day}
        try:
            quotes = await self.runtime.run_io(fetch_fund_quotes, codes, timeout=30) or {}
        except Exception as exc:
            isolated("finance.fund.fetch", exc)
            quotes = {}
        if quotes:
            self._save(day, quotes)
            self._update_positions(quotes)
            try:
                self._pool().observe(quotes, day)
            except Exception as exc:
                isolated("finance.fund.observe", exc)
            try:
                self.repo.meta_set("fund_quotes", json.dumps(quotes, ensure_ascii=False))
            except Exception as exc:
                isolated("finance.fund.meta", exc)
            tradeable = sum(1 for q in quotes.values() if q.get("tradeable"))
            self.repo.meta_set(flag_key, "ok:%d/%d/tradeable=%d" % (len(quotes), len(codes), tradeable))
            return {"ok": True, "day": day, "quotes": len(quotes), "codes": len(codes),
                    "tradeable": tradeable}
        tries = int(self.repo.meta_get("fund_refresh_try:" + day, 0) or 0) + 1
        self.repo.meta_set("fund_refresh_try:" + day, tries)
        if tries >= MAX_TRIES or minutes >= CLOSE_MINUTE:
            self.repo.meta_set(flag_key, "failed:%d" % tries)
        return {"ok": False, "skipped": "no_quotes", "day": day, "tries": tries}

    def latest(self, day=None):
        day = day or _dt.date.today().isoformat()
        out = {}
        try:
            rows = self.repo.fund_quotes(day)
        except Exception as exc:
            isolated("finance.fund.read", exc)
            rows = []
        for row in rows:
            quote = _quote_from_row(row)
            if quote.get("code"):
                out[quote["code"]] = quote
        if out:
            return out
        try:
            raw = self.repo.meta_get("fund_quotes")
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                for code, item in (data or {}).items():
                    if isinstance(item, dict):
                        item = dict(item)
                        item.setdefault("code", str(code))
                        item["tradeable"] = bool(item.get("tradeable"))
                        out[str(code)] = item
        except Exception as exc:
            isolated("finance.fund.legacy", exc)
        return out

    def facts(self, day, cash_cents, quotes=None):
        quotes = quotes if quotes is not None else self.latest(day)
        lines = []
        ordered = sorted(quotes.items(),
                         key=lambda kv: -abs(float((kv[1] or {}).get("change_pct") or 0)))
        for code, quote in ordered:
            quote = quote or {}
            if quote.get("tradeable"):
                line = "%s %s | 昨日净值 %.4f | 今日估值 %.4f | 估算涨跌幅 %+.2f%%" % (
                    code, quote.get("name") or "", float(quote.get("nav") or 0),
                    float(quote.get("estimate") or 0), float(quote.get("change_pct") or 0))
            else:
                line = ("%s %s | 最近净值 %.4f（%s） | 今日估值 — | 最近涨跌幅 %+.2f%%"
                        "（今日估值不可用，今天不申购/赎回）") % (
                    code, quote.get("name") or "", float(quote.get("nav") or 0),
                    quote.get("time") or "未知日期", float(quote.get("change_pct") or 0))
            lines.append(line)
        if not lines:
            lines.append("今天没有拉到基金行情，无法申购/赎回，今天先不动。")
        return ("现在是14:50，距离基金申购截止还有10分钟。今日基金行情：\n%s\n"
                "你当前余额是 %.2f 元。你可以调用 buy_fund 或 sell_fund 自主决定是否交易。不买也可以。"
                % ("\n".join(lines), int(cash_cents or 0) / 100.0))


async def fund_market_tick(plugin):
    repo = getattr(plugin, "finance_repo", None)
    runtime = getattr(plugin, "runtime", None)
    if repo is None or runtime is None:
        return {"skipped": "not_ready"}
    market = getattr(plugin, "fund_market", None)
    if market is None:
        market = FundMarket(plugin, runtime, repo)
        plugin.fund_market = market
    return await market.refresh()
