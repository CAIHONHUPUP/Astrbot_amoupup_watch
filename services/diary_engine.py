# -*- coding: utf-8 -*-
"""小日记：把真实事件交给 LLM 写成人话，失败回退事实陈述。"""
from __future__ import annotations


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


async def rewrite_and_save(bridge, amo, hint):
    try:
        text = await rewrite(bridge, hint)
        bridge._small_diary(amo, text)
    except Exception:
        try:
            bridge._small_diary(amo, str(hint or ""))
        except Exception:
            pass
