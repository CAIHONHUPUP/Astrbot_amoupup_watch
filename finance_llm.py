# -*- coding: utf-8 -*-
"""LLM 决策：把真实持仓/行情/情绪交给模型，由它输出买卖与理由；失败回退规则引擎。

只输出 JSON：
{"action":"buy|sell|hold","code":"600519","qty":100,"reason":"..."}
"""
from __future__ import annotations

import json
import re

try:
    from .guard import isolated
except ImportError:  # 直接按文件加载时
    from guard import isolated

SYSTEM = (
    "你是她的交易决策脑。只根据给定的真实持仓、行情、现金、情绪做决定，"
    "不要编造行情，不要假设用户替她下单。"
    "允许小赚就卖、大赚才卖、亏了死扛、亏了割肉、空仓观望、追高、抄底，风格不必固定。"
    "只输出一行 JSON，不要解释，不要 markdown。"
)

PROMPT = """现在：{now}
现金：{cash} 元
持仓：{positions}
候选行情：{quotes}
情绪：{mood}
投资上限：不超过存款 50%（存款=现金+持仓市值）
规则：A 股买入 100 股整数倍；不能透支；买入价不能超过涨停。

请给出这一次的决定，JSON 格式：
{{"action":"buy|sell|hold","code":"","qty":0,"reason":"一句中文理由"}}"""


async def _provider(plugin):
    try:
        context = getattr(plugin, "context", None)
        if context is None:
            return None
        if hasattr(context, "get_using_provider_async"):
            return await context.get_using_provider_async(None)
        return context.get_using_provider(None)
    except Exception as exc:
        isolated("finance.llm.provider", exc)
        return None


def parse_decision(text, allowed_codes):
    """从模型输出里抽 JSON；不合法返回 None。"""
    raw = str(text or "")
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    action = str(data.get("action") or "").lower()
    if action not in ("buy", "sell", "hold"):
        return None
    code = str(data.get("code") or "").strip()
    if action != "hold" and code not in allowed_codes:
        return None
    try:
        qty = int(float(data.get("qty") or 0))
    except Exception:
        qty = 0
    if action == "buy" and (qty <= 0 or qty % 100):
        return None
    reason = str(data.get("reason") or "").strip()[:60] or "她想了想，决定先不动"
    return {"action": action, "code": code, "qty": qty, "reason": reason}


async def llm_decide(plugin, cash_cents, positions, quotes, mood_word, allowed_codes=None):
    provider = await _provider(plugin)
    if provider is None:
        return None
    pos_text = "; ".join(
        "%s(%s) %s股 盈亏%.1f%%" % (p.get("name"), p.get("code"), p.get("qty"), p.get("pnl_pct", 0))
        for p in positions) or "空仓"
    quote_text = "; ".join(
        "%s %s 现价%.2f" % (code, q.get("name", ""), float(q.get("price") or 0))
        for code, q in (quotes or {}).items()) or "无"
    prompt = PROMPT.format(
        now=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        cash="%.2f" % (int(cash_cents) / 100.0),
        positions=pos_text, quotes=quote_text, mood=mood_word or "平静",
        )
    try:
        response = await provider.text_chat(prompt=prompt, system_prompt=SYSTEM, contexts=[])
        text = str(getattr(response, "completion_text", "") or "")
    except Exception as exc:
        isolated("finance.llm.chat", exc)
        return None
    allowed = set(allowed_codes) if allowed_codes else ({p.get("code") for p in positions} | set(quotes or {}))
    return parse_decision(text, allowed)
