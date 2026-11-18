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
    "你也可以申购/赎回场外基金，或者今天行情太差就不买。"
    "倾向：可用余额连续几天超过 10000 且近期不打算重仓，就存定期赚利息；"
    "最近在观望股市、准备随时加仓，就留活期；"
    "总资产不足 10000 或刚经历破产危机，存活期，或只存 500 元 3 个月定期当心理安慰。"
    "老公建议只作参考，你可以同意，也可以为了保留子弹而拒绝。"
    "空仓且现金足够买 100 股时，不要连续好几天只会观望：可以小仓位试一笔；行情差/有明显利空可以继续空仓。"
    "只输出一行 JSON，不要解释，不要 markdown。"
)

PROMPT = """现在：{now}
现金：{cash} 元
【银行存款】活期 {current} 元；定期 {fixed} 元
利率：活期年化 0.2%；定期 3 个月 1.3% / 6 个月 1.5% / 12 个月 1.7% / 36 个月 2.3%
持仓：{positions}
候选行情：{quotes}
基金事实：{funds}
情绪：{mood}
投资上限：单笔不超过当前现金的 50%（例：现金 6000 元 → 单笔最多 3000 元）
规则：A 股买入 100 股整数倍；不能透支；买入价不能超过涨停。

请给出这一次的决定，JSON 格式（三选一）：
买卖/观望：{{"action":"buy|sell|hold","code":"","qty":0,"reason":"一句中文理由"}}
存钱：{{"action":"deposit","account_type":"current|fixed","term_months":0,"amount":0,"reason":"一句中文理由"}}
基金：{{"action":"buy_fund|sell_fund","code":"","amount":0,"shares":0,"reason":"一句中文理由"}}"""


OUTAGE_SYSTEM = (
    "你是她的理财决策脑。现在基金估值接口全部拿不到，你有主动权决定怎么反应。"
    "只根据给定的突发事实、当前情绪、当前余额做决定，不要编造行情；"
    "你可以抱怨、放弃、或者把钱挪作他用。只输出一行 JSON，不要解释，不要 markdown。"
)

OUTAGE_PROMPT = """{facts}
当前情绪：{mood}
可用行动（只选一个，输出一行 JSON）：
1. 主动给他发消息：{{"action":"send_message_to_user","text":"你自己想说的话"}}
2. 放弃今天买基金（顺带跟他说一句，或留给日记）：{{"action":"skip_fund_trade","reason":"你自己的理由","text":"想跟他抱怨的一句话，可留空"}}
3. 把钱挪作他用：{{"action":"reallocate_funds","target":"deposit_fixed|deposit_current|shopping|keep","amount":0,"term_months":0,"note":"你自己的话"}}
amount 单位是元；定期只能选 3/6/12/36 个月；keep 表示先留着。"""


def parse_outage_decision(text):
    """解析故障时的自主行动；不合法返回 None。"""
    raw = str(text or "")
    match = re.search(r"{.*}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "").lower()
    if action == "send_message_to_user":
        message = str(data.get("text") or data.get("message") or "").strip()
        if not message:
            return None
        return {"action": "send_message_to_user", "text": message[:200]}
    if action == "skip_fund_trade":
        return {"action": "skip_fund_trade",
                "reason": str(data.get("reason") or "").strip()[:120],
                "text": str(data.get("text") or data.get("message") or "").strip()[:200]}
    if action == "reallocate_funds":
        target = str(data.get("target") or "").strip().lower()
        if target not in ("deposit_fixed", "deposit_current", "shopping", "keep"):
            return None
        try:
            amount_cents = int(round(float(data.get("amount") or 0) * 100))
            term = int(data.get("term_months") or 0)
        except (TypeError, ValueError):
            return None
        if target in ("deposit_fixed", "deposit_current", "shopping") and amount_cents <= 0:
            return None
        if target == "deposit_fixed" and term not in (3, 6, 12, 36):
            return None
        if target != "deposit_fixed":
            term = 0
        return {"action": "reallocate_funds", "target": target,
                "amount_cents": amount_cents, "term_months": term,
                "note": str(data.get("note") or "").strip()[:120]}
    return None


async def llm_decide_outage(plugin, facts, mood_word, memory_text=""):
    """故障时把事实交给 LLM，由她决定抱怨/放弃/挪钱。"""
    provider = await _provider(plugin)
    if provider is None:
        return None
    prompt = OUTAGE_PROMPT.format(facts=str(facts or ""), mood=str(mood_word or "平静"))
    if memory_text:
        prompt += "\n相关记忆（参考，可忽略）：\n" + str(memory_text)[:1200]
    try:
        response = await provider.text_chat(prompt=prompt, system_prompt=OUTAGE_SYSTEM, contexts=[])
        text = str(getattr(response, "completion_text", "") or "")
    except Exception as exc:
        isolated("finance.outage.llm", exc)
        return None
    return parse_outage_decision(text)


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
    if action in ("buy_fund", "sell_fund"):
        code = str(data.get("code") or "").strip()
        if code not in allowed_codes:
            return None
        try:
            amount = float(data.get("amount") or 0)
            shares = float(data.get("shares") or 0)
        except (TypeError, ValueError):
            return None
        if action == "buy_fund" and amount <= 0:
            return None
        if action == "sell_fund" and shares <= 0:
            return None
        return {"action": action, "code": code, "qty": 0, "shares": shares,
                "amount_cents": int(round(amount * 100)),
                "reason": str(data.get("reason") or "").strip()[:60] or "她看了看基金"}
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


async def llm_decide(plugin, cash_cents, positions, quotes, mood_word, allowed_codes=None, news_text="", memory_text="", fund_text=""):
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
        funds=fund_text or "今天没有基金事实",
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
