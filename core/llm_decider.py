# -*- coding: utf-8 -*-
"""LLM 决策：把真实持仓/行情/情绪交给模型，由它输出买卖与理由；失败回退规则引擎。

只输出 JSON：
{"action":"buy|sell|hold","code":"600519","qty":100,"reason":"..."}
"""
from __future__ import annotations

import json
import re

try:
    from ..guard import isolated
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated

SYSTEM = (
    "你是她的理财决策脑。只根据给定的真实持仓、行情、现金、情绪做决定，"
    "不要编造行情，不要假设用户替她下单。"
    "允许小赚就卖、大赚才卖、亏了死扛、亏了割肉、空仓观望、追高、抄底，风格不必固定。"
    "你还可以决定把闲钱存银行：活期灵活、定期利息高。"
    "倾向：可用余额连续几天超过 10000 且近期不打算重仓，就存定期赚利息；"
    "最近在观望股市、准备随时加仓，就留活期；"
    "总资产不足 10000 或刚经历破产危机，存活期，或只存 500 元 3 个月定期当心理安慰。"
    "老公建议只作参考，你可以同意，也可以为了保留子弹而拒绝。"
    "只输出一行 JSON，不要解释，不要 markdown。"
)

PROMPT = """现在：{now}
现金：{cash} 元
【银行存款】活期 {current} 元；定期 {fixed} 元
利率：活期年化 0.2%；定期 3 个月 1.3% / 6 个月 1.5% / 12 个月 1.7% / 36 个月 2.3%
持仓：{positions}
候选行情：{quotes}
情绪：{mood}
投资上限：不超过存款 50%（存款=现金+持仓市值）
规则：A 股买入 100 股整数倍；不能透支；买入价不能超过涨停。

请给出这一次的决定，JSON 格式（三选一）：
买卖/观望：{{"action":"buy|sell|hold","code":"","qty":0,"reason":"一句中文理由"}}
存钱：{{"action":"deposit","account_type":"current|fixed","term_months":0,"amount":0,"reason":"一句中文理由"}}"""


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
    reason = str(data.get("reason") or "").strip()[:60] or "她想了想，决定先不动"
    if action == "deposit":
        try:
            amount_cents = int(round(float(data.get("amount") or 0) * 100))
        except (TypeError, ValueError):
            return None
        if amount_cents <= 0:
            return None
        kind = str(data.get("account_type") or "current").strip().lower()
        kind = {"活期": "current", "定期": "fixed"}.get(kind, kind)
        if kind not in ("current", "fixed"):
            return None
        try:
            term = int(data.get("term_months") or 0)
        except (TypeError, ValueError):
            return None
        if kind == "fixed" and term not in (3, 6, 12, 36):
            return None
        if kind == "current":
            term = 0
        return {"action": "deposit", "account_type": kind, "term_months": term,
                "amount_cents": amount_cents, "code": "", "qty": 0, "reason": reason}
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


async def llm_decide(plugin, cash_cents, positions, quotes, mood_word, allowed_codes=None, news_text="", memory_text=""):
    provider = await _provider(plugin)
    if provider is None:
        return None
    pos_text = "; ".join(
        "%s(%s) %s股 盈亏%.1f%%" % (p.get("name"), p.get("code"), p.get("qty"), p.get("pnl_pct", 0))
        for p in positions) or "空仓"
    quote_text = "; ".join(
        "%s %s 现价%.2f" % (code, q.get("name", ""), float(q.get("price") or 0))
        for code, q in (quotes or {}).items()) or "无"
    try:
        bank = getattr(plugin, "bank", None)
        current_cents = int(bank.current_cents()) if bank else 0
        fixed_cents = int(bank.fixed_cents()) if bank else 0
    except Exception:
        current_cents, fixed_cents = 0, 0
    prompt = PROMPT.format(
        now=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        cash="%.2f" % (int(cash_cents) / 100.0),
        current="%.2f" % (current_cents / 100.0),
        fixed="%.2f" % (fixed_cents / 100.0),
        positions=pos_text, quotes=quote_text, mood=mood_word or "平静",
        )
    if news_text:
        prompt += "\n新闻（参考，可忽略，可写进理由）：\n" + str(news_text)[:1200]
    if memory_text:
        prompt += "\n相关记忆（最多18条）：\n" + str(memory_text)[:1500]
    try:
        response = await provider.text_chat(prompt=prompt, system_prompt=SYSTEM, contexts=[])
        text = str(getattr(response, "completion_text", "") or "")
    except Exception as exc:
        isolated("finance.llm.chat", exc)
        return None
    allowed = set(allowed_codes) if allowed_codes else ({p.get("code") for p in positions} | set(quotes or {}))
    return parse_decision(text, allowed)
