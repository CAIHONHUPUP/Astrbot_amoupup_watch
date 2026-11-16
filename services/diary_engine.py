# -*- coding: utf-8 -*-
"""小日记：把真实事件交给 LLM 写成人话，失败回退事实陈述。"""
from __future__ import annotations

import json
import logging
logger = logging.getLogger("amo.watch")


FACT_RULES = (
    "【事实分级·最高优先级】\n"
    "【绝对事实】只写下面给出的真实事件；【内心活动】可以写想念/委屈/孤独；\n"
    "【严禁虚构】老公打电话、抱着我、摸我的头、给我买药、来看我，一律不能写；\n"
    "【线上/线下】老公今天只在飞书/微信上聊天，没有出现在你身边；写聊天只能写「在微信上聊了几句」；\n"
    "【思念合法写法】「一个人在出租屋里，突然好想他。要是他在身边抱抱我就好了……算了，不打扰他了。」\n"
    "【称呼】只能叫「老公」「亲爱的」或「他」。\n"
)


def build_prompt(hint, mood="", long=False):
    length = "200-300 字的长日记" if long else "30-50 字的小日记"
    return (
        FACT_RULES
        + "今天发生的真实事件（只能写这些，绝不允许编造没发生的消费或经历）：\n"
        "- %s\n当前情绪：%s\n"
        "请用第一人称写一篇%s，像普通打工人/他的女朋友随手记的，口语、自然、有细节。"
        "严禁出现“心情+5”“好感+3”这类数值，严禁写没发生的事；"
        "严禁把账本数字抄进日记（基本工资/奖金/扣款/实发/余额的具体金额一律不写），"
        "只说“发工资了”“看到余额多了一点”这种感受；"
        "如果事件很少，就写“今天什么都没干，宅了一天，无聊”这种短句，不要硬凑。"
        % (str(hint or "").strip() or "平平无奇的一天", str(mood or "平静"), length)
    )


SYSTEM = (
    "你是她的日记代笔。只根据给出的真实事件写日记，不补充任何未提供的地点、人物、消费或经历；"
    "语气符合她的人设，自然口语，不要模板腔，不要数值。"
)


async def rewrite(plugin, hint, mood="", long=False):
    amo = None
    finance = getattr(plugin, "finance", None)
    if finance is not None and hasattr(finance, "_amo"):
        amo = finance._amo()
    if amo is None or not hasattr(amo, "llm"):
        return str(hint or "")
    try:
        umos = amo._owner_umos() if hasattr(amo, "_owner_umos") else []
        umo = umos[0] if umos else ""
        text = await amo.llm.chat_raw(umo, SYSTEM, build_prompt(hint, mood, long))
    except Exception:
        return str(hint or "")
    text = str(text or "").strip().strip('"').strip("“”")
    if not text:
        return str(hint or "")
    return text[:400]


FUND_SYSTEM = (
    "你是她的日记代笔。只根据给定的结构化交易事实、当前情绪、当前余额写日记；"
    "不套模板、不写固定句子、不编造事实；她可以后悔、兴奋、无所谓、只写一句，完全由你决定。"
    "不要复述 JSON，不要出现数值加成，不要写没有发生的消费或经历。"
)


def build_fund_prompt(fact, mood="", balance_yuan=0.0):
    """结构化事实 + 情绪 + 余额 → 给 LLM 的日记素材；不含任何示例句子。"""
    return (
        "结构化交易事实（JSON）：%s\n当前情绪：%s\n当前可用余额：%.2f 元\n"
        "请用她的第一人称写今天的小日记；写什么、写多长、什么态度都由你决定。"
        % (json.dumps(fact or {}, ensure_ascii=False, sort_keys=True),
           str(mood or "平静"), float(balance_yuan or 0.0))
    )


async def rewrite_fund_fact(bridge, amo, fact):
    """让 LLM 基于事实自己写；没有 LLM 就什么也不写，绝不回退到模板句。"""
    if amo is None or getattr(amo, "llm", None) is None:
        return ""
    try:
        umos = amo._owner_umos() if hasattr(amo, "_owner_umos") else []
        umo = umos[0] if umos else ""
        mood = ""
        try:
            mood = str((bridge.mood() or {}).get("word") or "")
        except Exception as exc:
            logger.debug("[WATCH] optional step failed: %r", exc)
        balance = 0.0
        try:
            balance = float(bridge.balance_cents() or 0) / 100.0
        except Exception as exc:
            logger.debug("[WATCH] optional step failed: %r", exc)
        text = await amo.llm.chat_raw(umo, FUND_SYSTEM, build_fund_prompt(fact, mood, balance))
    except Exception as exc:
        logger.debug("[WATCH] optional step failed: %r", exc)
        return ""
    text = str(text or "").strip().strip('"').strip("“”")
    if not text:
        return ""
    try:
        bridge._small_diary(amo, text)
    except Exception as exc:
        logger.debug("[WATCH] optional step failed: %r", exc)
        return ""
    return text[:400]


OUTAGE_SYSTEM = (
    "你是她的日记代笔。只根据给定的结构化突发事实、当前情绪、当前余额写日记；"
    "不套模板、不写固定句子、不编造事实；她可以庆幸、生气、无语、只写一句，完全由你决定。"
    "不要复述 JSON，不要出现数值加成，不要写没有发生的消费或经历。"
)


def build_outage_prompt(fact, mood="", balance_yuan=0.0):
    """突发事实 + 情绪 + 余额 → 给 LLM 的日记素材；不含任何示例句子。"""
    return (
        "结构化突发事实（JSON）：%s\n当前情绪：%s\n当前可用余额：%.2f 元\n"
        "请用她的第一人称写今天的小日记；写什么、写多长、什么态度都由你决定。"
        % (json.dumps(fact or {}, ensure_ascii=False, sort_keys=True),
           str(mood or "平静"), float(balance_yuan or 0.0))
    )


async def rewrite_fund_outage(bridge, amo, fact):
    """让 LLM 基于突发事实自己写；没有 LLM 就什么也不写。"""
    if amo is None or getattr(amo, "llm", None) is None:
        return ""
    try:
        umos = amo._owner_umos() if hasattr(amo, "_owner_umos") else []
        umo = umos[0] if umos else ""
        mood = ""
        try:
            mood = str((bridge.mood() or {}).get("word") or "")
        except Exception as exc:
            logger.debug("[WATCH] optional step failed: %r", exc)
        balance = 0.0
        try:
            balance = float(bridge.balance_cents() or 0) / 100.0
        except Exception as exc:
            logger.debug("[WATCH] optional step failed: %r", exc)
        text = await amo.llm.chat_raw(umo, OUTAGE_SYSTEM, build_outage_prompt(fact, mood, balance))
    except Exception as exc:
        logger.debug("[WATCH] optional step failed: %r", exc)
        return ""
    text = str(text or "").strip().strip('"').strip("“”")
    if not text:
        return ""
    try:
        bridge._small_diary(amo, text)
    except Exception as exc:
        logger.debug("[WATCH] optional step failed: %r", exc)
        return ""
    return text[:400]


async def rewrite_and_save(bridge, amo, hint):
    try:
        text = await rewrite(bridge, hint)
        bridge._small_diary(amo, text)
    except Exception:
        try:
            bridge._small_diary(amo, str(hint or ""))
        except Exception as exc:
            logger.debug("[WATCH] optional step failed: %r", exc)
