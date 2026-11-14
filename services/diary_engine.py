# -*- coding: utf-8 -*-
"""小日记：把真实事件交给 LLM 写成人话，失败回退事实陈述。"""
from __future__ import annotations


def build_prompt(hint, mood="", long=False):
    length = "200-300 字的长日记" if long else "30-50 字的小日记"
    return (
        "今天发生的真实事件（只能写这些，绝不允许编造没发生的消费或经历）：\n"
        "- %s\n当前情绪：%s\n"
        "请用第一人称写一篇%s，像普通打工人/他的女朋友随手记的，口语、自然、有细节。"
        "严禁出现“心情+5”“好感+3”这类数值，严禁写没发生的事；"
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
