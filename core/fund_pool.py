# -*- coding: utf-8 -*-
"""场外基金候选池：固定锚点 + 用户推荐 + 14:50 观察池。"""
from __future__ import annotations

import json
import logging
logger = logging.getLogger("amo.watch")

BASE_FUNDS = {
    "110020": "易方达沪深300ETF联接A",
    "000478": "建信中证500指数增强A",
    "005827": "易方达蓝筹精选混合",
    "161725": "招商中证白酒指数",
    "003096": "中欧医疗健康混合A",
    "001156": "申万菱信新能源汽车",
    "001595": "天弘中证银行ETF联接A",
    "000961": "天弘沪深300ETF联接A",
}
_NAME_CODES = {name: code for code, name in BASE_FUNDS.items()}
_ALIASES = {"白酒": "161725", "沪深300": "110020", "中证500": "000478",
            "新能源": "001156", "医药": "003096", "银行": "001595",
            "蓝筹": "005827"}


class FundPool:
    def __init__(self, plugin):
        self.plugin = plugin
        self.repo = getattr(plugin, "finance_repo", None)

    def recommended(self):
        try:
            raw = self.repo.meta_get("fund_recommend", []) if self.repo else []
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = []
            return [str(x) for x in (raw if isinstance(raw, list) else [])]
        except Exception:
            return []

    def recommend(self, text):
        import re
        codes = re.findall(r"(?<!\d)(\d{6})(?!\d)", str(text or ""))
        for name, code in _NAME_CODES.items():
            if name in str(text or ""):
                codes.append(code)
        for name, code in _ALIASES.items():
            if name in str(text or ""):
                codes.append(code)
        found = [c for c in dict.fromkeys(codes) if c in BASE_FUNDS or len(c) == 6]
        if not found:
            return []
        old = self.recommended()
        merged = list(dict.fromkeys(old + found))
        try:
            if self.repo:
                self.repo.meta_set("fund_recommend", json.dumps(merged, ensure_ascii=False))
        except Exception as exc:
            logger.debug("[WATCH] fund recommend save failed: %r", exc)
        return found

    def observe(self, quotes, day):
        rows = sorted((quotes or {}).values(), key=lambda x: abs(float(x.get("change_pct") or 0)), reverse=True)
        codes = [str(x.get("code")) for x in rows[:5] if x.get("code")]
        try:
            if self.repo:
                self.repo.meta_set("fund_observe:" + str(day), json.dumps(codes, ensure_ascii=False))
        except Exception as exc:
            logger.debug("[WATCH] fund observe save failed: %r", exc)
        return codes

    def codes(self, day=""):
        observed = []
        try:
            raw = self.repo.meta_get("fund_observe:" + str(day), []) if self.repo else []
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = []
            observed = list(raw if isinstance(raw, list) else [])
        except Exception:
            observed = []
        return list(dict.fromkeys(list(BASE_FUNDS) + self.recommended() + observed))
