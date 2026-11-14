# -*- coding: utf-8 -*-
"""她所在的城市：优先插件配置，其次 amo 的日程城市，最后为空（按三线算）。

城市决定：物价系数（一线 1.5 / 二线 1.0 / 三线 0.75）、房租、房价、饭钱。
"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
    from ..utils.helpers import city_factor, city_tier, normalize_city, stable_int
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated
    from utils.helpers import city_factor, city_tier, normalize_city, stable_int


def current_city(plugin):
    """返回城市名（可能是空串，空串按二线基准算）。

    优先级：/城市 指令设置 > 插件配置 city > amo 的日程城市。
    """
    config = getattr(plugin, "config", {}) or {}
    for key in ("city", "schedule_city"):
        value = str(config.get(key) or "").strip()
        if value:
            return value
    try:
        finance = getattr(plugin, "finance", None)
        amo = finance._amo() if finance is not None else None
        if amo is not None:
            # 1) 世界观里的城市（面板「城市 东莞」就是它）
            world = getattr(getattr(amo, "state", None), "worldview", None) or {}
            value = str(world.get("city") or "").strip()
            if value:
                return value
            # 2) 天气城市显示名（amo 配置 weather_city_name）
            getter = getattr(amo, "cfg_str", None)
            if callable(getter):
                value = str(getter("weather_city_name", "") or "").strip()
                if value:
                    return value
                value = str(getter("schedule_city", "") or "").strip()
                if value:
                    return value
            config = getattr(amo, "config", {}) or {}
            for key in ("weather_city_name", "schedule_city"):
                value = str(config.get(key) or "").strip()
                if value:
                    return value
    except Exception as exc:
        isolated("city.amo", exc)
    # amo 不可用（或它没配城市）时，才用 /城市 记录的「搬家」兜底
    override = str(getattr(plugin, "city_override", "") or "").strip()
    if override:
        return override
    return ""


def factor_of(name):
    """没设城市时按 1.0（二线基准）算，避免默认就给她打折；
    设了但不认识的城市，按三线 0.75。"""
    text = str(name or "").strip()
    return 1.0 if not text else city_factor(text)


async def move_to(plugin, name):
    """搬家：以新城市为准，同步 amo 的面板城市（世界观）与天气城市。

    - 天气：amo.config weather_city(LocationID) + weather_city_name，写完立刻刷新
    - 面板：amo.state.worldview.city（面板「城市」卡片读的就是它），并回写 amo 的 kv
    任何一步失败只记日志，不影响本插件自己的城市判定（调用方会写 meta 覆盖）。
    """
    name = str(name or "").strip()
    result = {"ok": False, "city": name, "weather": None, "world": False, "reason": ""}
    if not name:
        result["reason"] = "empty"
        return result
    amo = None
    try:
        finance = getattr(plugin, "finance", None)
        amo = finance._amo() if finance is not None else None
    except Exception as exc:
        isolated("city.move.amo", exc)
    if amo is None:
        result.update(ok=True, reason="amo_missing")
        return result
    # 1) 天气城市（自动切换）
    try:
        lookup = getattr(amo, "_city_lookup", None)
        hit = lookup(name) if callable(lookup) else None
        if isinstance(hit, dict) and hit.get("id"):
            config = getattr(amo, "config", None)
            if config is not None:
                config["weather_city"] = str(hit["id"])
                config["weather_city_name"] = str(hit.get("name") or name)
                # 日程城市一起改：amo 的 city_or_empty 优先读 schedule_city
                config["schedule_city"] = str(hit.get("name") or name)
                saver = getattr(config, "save_config", None)
                if callable(saver):
                    saver()
            result["weather"] = {"id": hit["id"], "name": hit.get("name") or name}
    except Exception as exc:
        isolated("city.move.weather", exc)
    try:
        state = getattr(amo, "state", None)
        refresh = getattr(state, "refresh_weather", None)
        if callable(refresh):
            await refresh(force=True)
    except Exception as exc:
        isolated("city.move.refresh", exc)
    # 2) 面板城市（世界观）
    try:
        state = getattr(amo, "state", None)
        world = dict(getattr(state, "worldview", {}) or {})
        world["city"] = name
        state.worldview = world
        db = getattr(amo, "db", None)
        if db is not None:
            db.kv_set("worldview", world)
        result["world"] = True
    except Exception as exc:
        isolated("city.move.world", exc)
    result["ok"] = True
    return result


def moving_fee_cents(from_city, to_city):
    """搬家费：车程/搬家公司口径，500–2000 元，跨城市等级越远越贵；同城搬为 0。"""
    a = str(from_city or "").strip()
    b = str(to_city or "").strip()
    if not b or a == b:
        return 0
    order = {"一线": 0, "二线": 1, "三线": 2, "未设置": 1}
    diff = abs(order.get(city_tier(a), 1) - order.get(city_tier(b), 1))
    base = 80000 + diff * 40000
    jitter = stable_int("move:%s>%s" % (a, b), 0, 40000)
    return max(50000, min(200000, base + jitter))


async def relocate(plugin, name):
    """用户下搬家指令：同步城市 + 扣搬家费 + 记忆/日记/日程/情绪一次做完。

    资金不够时不阻断搬家：记账为「待补搬家费」，等用户 [转账] 时结清。
    这是城市切换的唯一入口（她绝不会自己换城市）。
    """
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "reason": "empty"}
    from_city = city_view(plugin)["city"]
    plugin.city_override = name
    moved = await move_to(plugin, name)
    fee = moving_fee_cents(from_city, name)
    day = _dt.date.today().isoformat()
    finance = getattr(plugin, "finance", None)
    repo = getattr(plugin, "finance_repo", None)
    paid = False
    pending = 0
    if fee and finance is not None:
        try:
            result = await finance.expense(
                fee, "搬家：%s→%s" % (from_city or "原来", name),
                "move:%s:%s" % (day, name), necessary=True)
            paid = bool(isinstance(result, dict) and result.get("ok"))
        except Exception as exc:
            isolated("city.relocate.fee", exc)
        if not paid:
            pending = fee
            if repo is not None:
                try:
                    repo.meta_set("move_pending_fee", fee)
                    repo.meta_set("move_pending_to", name)
                except Exception as exc:
                    isolated("city.relocate.pending", exc)
    if finance is not None:
        try:
            await finance.permanent_memory(
                "今天跟着老公搬到了%s，虽然换了新环境，但有老公在就很安心。" % name,
                "move:%s:%s" % (day, name))
        except Exception as exc:
            isolated("city.relocate.memory", exc)
        try:
            finance.long_diary(
                "要离开%s了，有点不舍，但老公说去%s会更好，我听老公的！"
                % (from_city or "这个住了很久的地方", name))
        except Exception as exc:
            isolated("city.relocate.diary", exc)
        try:
            finance.add_schedule(day, "今日与老公搬家",
                                 detail="从%s搬到%s" % (from_city or "这儿", name),
                                 category="life")
        except Exception as exc:
            isolated("city.relocate.schedule", exc)
        try:
            finance.nudge_mood({"兴奋": 20, "开心": 15}, "move-mood:%s:%s" % (day, name))
        except Exception as exc:
            isolated("city.relocate.mood", exc)
    return {"ok": True, "city": name, "from_city": from_city, "fee_cents": fee,
            "paid": paid, "pending_cents": pending, "moved": moved}


def city_status_text(plugin):
    view = city_view(plugin)
    return "她现在在「%s」（%s，物价 ×%s）。想换就发：/城市 杭州" % (
        view["city"] or "未设置", view["tier"], view["factor"])


def city_reply(plugin, moved, saved=True):
    view = city_view(plugin)
    weather = ((moved or {}).get("moved") or {}).get("weather") or {}
    weather_text = ("天气已经切到%s" % weather.get("name")) if weather else "天气城市没找到，只改了账本口径"
    fee = int((moved or {}).get("fee_cents") or 0)
    if (moved or {}).get("pending_cents"):
        fee_text = "搬家费 %s 我先欠着，你转我一下（[转账]%s）就行；" % (yuan(fee), yuan(fee).replace("¥", ""))
    else:
        fee_text = ("搬家费 %s 已经付了；" % yuan(fee)) if fee else ""
    return "好，搬去%s了（%s，物价 ×%s），%s；%s房租、饭钱、房价都按这里算%s。" % (
        view["city"], view["tier"], view["factor"], weather_text, fee_text,
        "" if saved else "（这次没存上，重启会恢复）")


async def settle_pending_fee(plugin):
    """用户转账后补交搬家费；返回一句可追加到回复里的话（没有欠费就返回空串）。"""
    try:
        repo = getattr(plugin, "finance_repo", None)
        pending = int(repo.meta_get("move_pending_fee", 0) or 0) if repo is not None else 0
        finance = getattr(plugin, "finance", None)
        if not pending or finance is None:
            return ""
        settled = await finance.expense(
            pending, "补交搬家费", "move-settle:%s" % _dt.date.today().isoformat())
        if isinstance(settled, dict) and settled.get("ok"):
            repo.meta_set("move_pending_fee", 0)
            return "；搬家费 %s 已结清" % yuan(pending)
    except Exception as exc:
        isolated("city.settle_fee", exc)
    return ""


def city_view(plugin):
    name = current_city(plugin)
    return {"city": normalize_city(name),
            "tier": city_tier(name) if str(name or "").strip() else "未设置",
            "factor": factor_of(name)}
