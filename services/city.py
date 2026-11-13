# -*- coding: utf-8 -*-
"""她所在的城市：优先插件配置，其次 amo 的日程城市，最后为空（按三线算）。

城市决定：物价系数（一线 1.5 / 二线 1.0 / 三线 0.75）、房租、房价、饭钱。
"""
from __future__ import annotations

try:
    from ..guard import isolated
    from ..utils.helpers import city_factor, city_tier, normalize_city
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated
    from utils.helpers import city_factor, city_tier, normalize_city


def current_city(plugin):
    """返回城市名（可能是空串，空串按二线基准算）。

    优先级：/城市 指令设置 > 插件配置 city > amo 的日程城市。
    """
    override = str(getattr(plugin, "city_override", "") or "").strip()
    if override:
        return override
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


def city_view(plugin):
    name = current_city(plugin)
    return {"city": normalize_city(name),
            "tier": city_tier(name) if str(name or "").strip() else "未设置",
            "factor": factor_of(name)}
