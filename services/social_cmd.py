# -*- coding: utf-8 -*-
"""社交指令：/查关系、/劝她别借钱给X、/社交 <事件名>。"""
from __future__ import annotations

try:
    from ..guard import isolated
    from ..utils.helpers import yuan
except ImportError:
    from guard import isolated
    from utils.helpers import yuan


def _engine(plugin):
    engine = getattr(plugin, "social", None)
    if engine is None:
        try:
            from .social_engine import build_social
        except ImportError:
            from services.social_engine import build_social
        engine = build_social(plugin)
    return engine


def query_text(plugin):
    engine = _engine(plugin)
    if engine is None:
        return "社交模块未就绪"
    engine.ensure_seed()
    lines = ["【核心关系】"]
    for row in engine.repo.npcs():
        lines.append("- %s（%s，好感 %s）%s" % (
            row["name"], row["relation"], row["affinity"],
            " 最近：" + row["last_event"] if row.get("last_event") else ""))
    rows = engine.repo.receivables()
    if rows:
        lines.append("【应收】" + "；".join("%s 欠 %s" % (r["npc"], yuan(r["amount_cents"])) for r in rows))
    return "\n".join(lines)


async def persuade(plugin, name):
    engine = _engine(plugin)
    if engine is None:
        return "社交模块未就绪"
    engine.repo.set_npc_event(name, "老公不让我借钱给她")
    engine.repo.upsert_npc(name, "friend", "朋友", 50, "#社交_" + name) if engine.repo.npc_get(name) is None else None
    return "好吧，听老公的，我找个借口拒绝%s。" % name


async def social_reply(plugin, text):
    text = str(text or "").strip()
    engine = _engine(plugin)
    if engine is None:
        return ""
    if text.startswith(("/守护面板", "/她的状态", "守护面板", "她的状态")):
        from .dashboard import dashboard_text
        return dashboard_text(plugin)
    if text.startswith(("/查关系", "查关系")):
        return query_text(plugin)
    if text.startswith(("/劝她别借钱", "劝她别借钱")):
        name = text.split("借钱给")[-1].strip(" ：:") or "朋友"
        return await persuade(plugin, name)
    if text.startswith(("/督促她买保险", "督促她买保险")):
        arg = text.replace("/督促她买保险", "").replace("督促她买保险", "").strip()
        table = {"e生保": "e生保", "医无忧": "医无忧", "金钟罩": "金钟罩", "意外险": "金钟罩",
                 "重疾险": "重疾", "重疾": "重疾", "交强险": "交强险", "商业车险": "商车险",
                 "车险": "商车险", "家财险": "家财险"}
        key = table.get(arg)
        if key is None:
            return "支持的险种：e生保 / 医无忧 / 金钟罩（意外险）/ 重疾险 / 交强险 / 商业车险 / 家财险"
        if key in ("交强险", "商车险") and not getattr(plugin, "has_car", False):
            return "她现在没有车，不用买车险。"
        if key == "家财险" and not getattr(plugin, "has_house", False):
            return "她现在没有房，不用买家财险。"
        try:
            from .insurance_engine import build_insurance
        except ImportError:
            from services.insurance_engine import build_insurance
        ins = getattr(plugin, "insurance", None) or build_insurance(plugin)
        plugin.insurance = ins
        out = await ins.buy(key)
        if out.get("ok"):
            return "好吧，听老公的，给自己/车/房买个保障。"
        return "保费没扣成（%s）……" % out.get("reason")
    if text.startswith(("/让她去体检", "让她去体检")):
        medical = getattr(plugin, "medical", None)
        if medical is None:
            return "医疗模块未就绪"
        out = await medical.checkup()
        return out.get("card") or out.get("text") or "体检没做成"
    if text.startswith(("/外出", "外出")):
        try:
            from .event_engine import build_event
        except ImportError:
            from services.event_engine import build_event
        engine_e = getattr(plugin, "events", None) or build_event(plugin)
        arg = text.split(" ", 1)[-1].strip() if " " in text else ""
        table = {"父母": engine_e.visit_parents, "逛街": engine_e.mall_with_bff, "旅游": engine_e.trip}
        fn = table.get(arg)
        if fn is None:
            return "可选：/外出 父母、/外出 逛街、/外出 旅游"
        result = await fn()
        return result.get("text") or ("没去成（%s）" % result.get("reason"))
    if text.startswith(("/赞助", "赞助")):
        try:
            from .event_engine import build_event
        except ImportError:
            from services.event_engine import build_event
        engine_e = getattr(plugin, "events", None) or build_event(plugin)
        arg = text.split(" ")[-1].strip() if " " in text else "0"
        try:
            cents = int(round(float(arg) * 100))
        except Exception:
            cents = 0
        result = await engine_e.sponsor(cents)
        return result.get("text") or "金额没看懂"
    if text.startswith(("/社交", "社交")):
        arg = text.split(" ", 1)[-1].strip() if " " in text else ""
        if not arg:
            return "可用事件：随礼 / 红包 / 聚餐 / 借钱 / 批评"
        table = {"随礼": engine.gift_money, "红包": engine.red_packet,
                 "聚餐": engine.dinner, "借钱": engine.borrow_request, "批评": engine.criticism}
        fn = table.get(arg)
        if fn is None:
            return "没这个事件，可选：随礼 / 红包 / 聚餐 / 借钱 / 批评"
        result = await fn()
        return result.get("text") or ("好了" if result.get("ok") else "这次没触发（%s）" % result.get("reason"))
    return ""

def inject_request(plugin, request):
    """把【关系】一行塞进 LLM 请求（临时 part，不改 system prompt 缓存）。"""
    line = prompt_line(plugin)
    if not line or request is None:
        return
    try:
        # AstrBot 4.29 没有 TextPart（api/core 都导不出），旧写法每次注入都 ImportError，
        # 关系上下文实际从没进过模型；直接拼 system_prompt 最稳。
        request.system_prompt = str(getattr(request, "system_prompt", "") or "") + "\n" + line
    except Exception as exc:
        isolated("social.llm_inject", exc)


def prompt_line(plugin):
    """给 LLM 的一行关系上下文（on_llm_request 注入用，不改 amo 代码）。"""
    engine = _engine(plugin)
    if engine is None:
        return ""
    engine.ensure_seed()
    return "【关系】" + engine.relation_line()
