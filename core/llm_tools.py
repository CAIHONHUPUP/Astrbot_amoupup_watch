import logging
logger = logging.getLogger("amo.watch")
# -*- coding: utf-8 -*-
"""LLM 工具逻辑（装饰器薄壳留在 main，保证 handler 归属）。"""
try:
    pass
except ImportError as exc:
    logger.debug("[WATCH] optional step failed: %r", exc)

ALIASES = {"e生保": "e生保", "医无忧": "医无忧", "金钟罩": "金钟罩", "意外险": "金钟罩",
           "重疾险": "重疾", "重疾": "重疾", "交强险": "交强险",
           "商业车险": "商车险", "车险": "商车险", "家财险": "家财险"}


async def buy_insurance_tool(plugin, insurance_type=""):
    key = ALIASES.get(str(insurance_type or "").strip())
    if key is None:
        return "支持的险种：e生保 / 医无忧 / 金钟罩（意外险）/ 重疾险 / 交强险 / 商业车险 / 家财险"
    # 车/房状态在 finance_property.state() 里；plugin 上没有 has_car/has_house 字段
    prop = getattr(plugin, "finance_property", None)
    info = {}
    if prop is not None and callable(getattr(prop, "state", None)):
        try:
            info = prop.state() or {}
        except Exception:
            info = {}
    has_car = bool(info.get("has_car", getattr(plugin, "has_car", False)))
    has_house = bool(info.get("has_house", getattr(plugin, "has_house", False)))
    if key in ("交强险", "商车险") and not has_car:
        return "她现在没有车，不用买车险。"
    if key == "家财险" and not has_house:
        return "她现在没有房，不用买家财险。"
    try:
        from ..services.insurance_engine import PRODUCTS, build_insurance
    except ImportError:
        from services.insurance_engine import PRODUCTS, build_insurance
    ins = getattr(plugin, "insurance", None) or build_insurance(plugin)
    plugin.insurance = ins
    spec = PRODUCTS[key]
    if (ins.status().get(key) or {}).get("grace"):
        return "保单在宽限期内，先补缴保费才能继续保障。"
    if int(plugin.finance.balance_cents() or 0) < spec["premium"][0]:
        await ins.poor_diary(key)
        return "保费好贵……我现在还买不起，先在日记里记一笔。"
    out = await ins.buy(key)
    return ("买好了：%s" % spec["name"]) if out.get("ok") else ("没买成（%s）" % out.get("reason"))

VALID_FIXED_TERMS = (3, 6, 12, 36)


async def deposit_money_tool(plugin, amount=0.0, account_type="current", term_months=0):
    """把闲钱存成活期/定期。account_type: current|fixed；定期期限 3/6/12/36 个月。"""
    try:
        amount_cents = int(round(float(amount or 0) * 100))
    except (TypeError, ValueError):
        return "金额格式不对，要给一个数字（元）。"
    if amount_cents <= 0:
        return "存款金额要大于 0。"
    kind = str(account_type or "current").strip().lower()
    kind = {"活期": "current", "定期": "fixed"}.get(kind, kind)
    if kind not in ("current", "fixed"):
        return "account_type 只能是 current（活期）或 fixed（定期）。"
    try:
        term = int(term_months or 0)
    except (TypeError, ValueError):
        term = 0
    if kind == "fixed" and term not in VALID_FIXED_TERMS:
        return "定期期限只能是 3 / 6 / 12 / 36 个月。"
    if kind == "current":
        term = 0
    try:
        cash = int(plugin.finance.balance_cents() or 0)
    except Exception:
        cash = 0
    if cash < amount_cents:
        return "可用余额只有 %.2f 元，存不了 %.2f 元。" % (cash / 100.0, amount_cents / 100.0)
    try:
        from ..services.bank_engine import CURRENT_RATE, FIXED_RATES, build_bank, yuan
    except ImportError:
        from services.bank_engine import CURRENT_RATE, FIXED_RATES, build_bank, yuan
    bank = getattr(plugin, "bank", None)
    if bank is None:
        bank = build_bank(plugin)
        plugin.bank = bank
    out = await bank.deposit(amount_cents, term)
    if not out.get("ok"):
        return "存款没成功（%s），可能是余额不够。" % (out.get("reason") or "unknown")
    rate = CURRENT_RATE if not term else FIXED_RATES.get(term, 0.0)
    label = "活期" if not term else "定期%d个月" % term
    return "存好了：%s 的%s（年化 %.2f%%）。现在活期 %s，定期 %s。" % (
        yuan(amount_cents), label, rate * 100.0, yuan(bank.current_cents()), yuan(bank.fixed_cents()))



async def buy_gift_tool(plugin, target_name="", gift_name="", price=0.0):
    """给 NPC 买礼物：target_name/gift_name/price（元），校验余额后扣款并记忆。"""
    target = str(target_name or "").strip() or "朋友"
    gift = str(gift_name or "").strip()
    if not gift:
        return "要买什么礼物呀？"
    try:
        cents = int(round(float(price or 0) * 100))
    except (TypeError, ValueError):
        return "价格不太对。"
    if cents <= 0:
        return "礼物价格要大于 0。"
    finance = getattr(plugin, "finance", None)
    if finance is None:
        return "钱包未就绪。"
    cash = int(finance.balance_cents() or 0)
    if cents > cash:
        return "可用余额只有 %.2f 元，买不起 %s。" % (cash / 100.0, gift)
    try:
        res = await finance.expense(cents, "礼物：%s" % gift,
                                    "gift:%s:%s" % (target, cents), necessary=True)
    except Exception:
        res = {"ok": False}
    if not isinstance(res, dict) or not res.get("ok"):
        return "礼物没买成，钱包好像不够。"
    try:
        finance.small_diary("给%s买了个%s，希望TA喜欢！" % (target, gift))
        await finance.permanent_memory("给%s买了个%s，花了 %d 元。" % (target, gift, cents // 100),
                                       "gift:%s:%s:mem" % (target, cents))
    except Exception as exc:
        logger.debug("[WATCH] optional step failed: %r", exc)
    return "买好啦：%s 的%s，花了 %d 元。" % (target, gift, cents // 100)


def _housing_of(plugin):
    finance = getattr(plugin, "finance", None)
    amo = finance._amo() if finance is not None and hasattr(finance, "_amo") else None
    return getattr(amo, "housing", None) if amo is not None else None


def _pick_home(housing, ref="", only_idle=False):
    raw = getattr(housing, "homes", [])
    homes = list(raw() if callable(raw) else raw or [])
    ref = str(ref or "").strip()
    if ref:
        for home in homes:
            if str(home.get("id")) == ref or str(home.get("name") or "").strip() == ref:
                return home
        return None
    current = str((housing.current() or {}).get("id") or "")
    for home in homes:
        if home.get("kind") != "own":
            continue
        if only_idle and (home.get("id") == current or home.get("status") == "rented_out"):
            continue
        return home
    return None


async def rent_out_property_tool(plugin, property_name="", rent=0.0):
    """她自主决定把闲置自有房出租；不传名字时挑一套空置的。"""
    housing = _housing_of(plugin)
    if housing is None:
        return "房屋模块未就绪。"
    home = _pick_home(housing, property_name, only_idle=True)
    if home is None:
        return "没有可出租的闲置自有房产。"
    try:
        cents = int(round(float(rent or 0) * 100))
    except (TypeError, ValueError):
        cents = 0
    cents = cents or int(home.get("rent_cents") or 150000)
    ok, message = housing.rent_out(home.get("id"), cents)
    return message if ok else ("没租出去：%s" % message)


async def sell_property_tool(plugin, property_name=""):
    """她自主决定出售闲置房产；不传名字时只挑非当前居住的自有房。"""
    housing = _housing_of(plugin)
    if housing is None:
        return "房屋模块未就绪。"
    home = _pick_home(housing, property_name, only_idle=True)
    if home is None:
        return "没有可出售的闲置自有房产；如果真要卖当前住处，请说出具体房产名。"
    ok, message = housing.sell_home(home.get("id"))
    return message if ok else ("没卖成：%s" % message)


async def propose_date_tool(plugin, activity="", time=""):
    """答应老公的约会提议：写进日程，返回开心的回复。"""
    activity = str(activity or "").strip() or "吃火锅"
    raw = str(time or "").strip()
    mapping = {"中午": "12:00", "晚上": "18:30", "早上": "08:00", "下午": "15:00"}
    when = mapping.get(raw, raw)
    try:
        from .services.date_engine import make_date
    except ImportError:
        from services.date_engine import make_date
    out = await make_date(plugin, activity, when)
    if not out.get("ok"):
        return out.get("message") or "我们不是已经约好了吗？我记着呢！"

    # 只用口语说时间，绝不把 11-16 18:27 这种系统格式糊到聊天里
    return out.get("message") or "好呀好呀！那说定啦，晚点见~"


async def confirm_arrival_tool(plugin, activity_name=""):
    """等待赴约时确认老公到达：立刻进入 dating，并说见面的话。"""
    try:
        from .services.date_engine import active_date, attend_date, find_pending
    except ImportError:
        from services.date_engine import active_date, attend_date, find_pending
    if active_date(plugin):
        return "（系统）你们已经在约会中了。不要复述这句提示，按【约会开场】的事实自己接话。"
    pending = find_pending(plugin, activity_name)
    if pending is None:
        return "现在没有在等你赴约呀。"
    out = await attend_date(plugin, pending.get("title"))
    if not out.get("ok"):
        return out.get("message") or "约会没开始成……"
    return ("（系统）约会已经开始，状态已切到 dating。"
            "请按【约会开场】里的事件/环境/穿搭/状态，自己写见面第一句，"
            "动作必须按现场自由设计，禁止套用固定台词或固定动作。")


async def accept_social_event_tool(plugin, npc="", place="", amount=0.0, note="", start="", end="",
                                    about_user=""):
    """接受邀约：真实扣款、占日程、刷新此刻动作、写记忆/日记/流水。

    借钱时金额不能超过余额，超出会被拒；可以少借一点（自己传 amount）。
    """
    engine = getattr(plugin, "social", None)
    if engine is None:
        return "（系统）现在没有待回的邀约，不要编造答应过谁。"
    try:
        amount_cents = int(round(float(amount or 0) * 100))
    except (TypeError, ValueError):
        amount_cents = 0
    out = await engine.accept_invite(npc=str(npc or ""), place=str(place or ""),
                                     amount_cents=amount_cents, note=str(note or ""),
                                     start=str(start or ""), end=str(end or ""),
                                     about_user=str(about_user or ""))
    if not out.get("ok"):
        return ("（系统）没去成：%s。按这个事实说话，不要假装已经出门。"
                % (out.get("message") or out.get("reason") or "失败"))
    try:
        from ..utils.helpers import yuan
    except ImportError:
        from utils.helpers import yuan
    return ("（系统）已按她的决定结算：%s，花了 %s。日程、情绪、记忆都已更新；"
            "按这个事实自己说话，不要复述本提示。" % (out.get("note") or "去了",
                                              yuan(int(out.get("cost") or 0))))


async def _trade_tool(plugin, event, action, code, qty=0, price=0.0, amount=0.0, nav=0.0):
    trading = getattr(plugin, "trading", None)
    if trading is None:
        return "（系统）交易模块未就绪，这次不能下单。"
    import time as _time
    code = str(code or "").strip()
    if not code:
        return "（系统）缺少股票/基金代码。"
    stamp = _time.strftime("%Y%m%d%H%M%S")
    tag = "tool:%s:%s:%s" % (action, code, stamp)
    try:
        if action == "buy_stock":
            out = await trading.buy(code, int(float(qty or 0)), float(price or 0) or None, idem=tag)
        elif action == "sell_stock":
            out = await trading.sell(code, int(float(qty or 0)), float(price or 0) or None, idem=tag)
        elif action == "buy_fund":
            out = await trading.subscribe(code, int(round(float(amount or 0) * 100)),
                                          float(nav or 0) or None, idem=tag)
        elif action == "sell_fund":
            out = await trading.redeem(code, float(qty or 0), float(nav or 0) or None, idem=tag)
        else:
            return "（系统）未知交易动作。"
    except Exception as exc:
        return "（系统）交易失败：%s" % exc
    if not isinstance(out, dict) or not out.get("ok"):
        return "（系统）交易被拒绝：%s" % ((out or {}).get("message") or "失败")
    return "（系统）交易成功：%s" % (out.get("message") or "已成交")


async def buy_stock_tool(plugin, event=None, code="", qty=100, price=0.0):
    return await _trade_tool(plugin, event, "buy_stock", code, qty=qty, price=price)


async def sell_stock_tool(plugin, event=None, code="", qty=100, price=0.0):
    return await _trade_tool(plugin, event, "sell_stock", code, qty=qty, price=price)


async def buy_fund_tool(plugin, event=None, code="", amount=0.0, nav=0.0):
    return await _trade_tool(plugin, event, "buy_fund", code, amount=amount, nav=nav)


async def sell_fund_tool(plugin, event=None, code="", shares=0.0, nav=0.0):
    return await _trade_tool(plugin, event, "sell_fund", code, qty=shares, nav=nav)


async def reject_social_event_tool(plugin, reason="", note=""):
    """婉拒邀约：记下真实理由，不动钱、不占日程。"""
    engine = getattr(plugin, "social", None)
    if engine is None:
        return "（系统）现在没有待回的邀约。"
    out = await engine.reject_invite(reason=str(reason or ""), note=str(note or ""))
    if not out.get("ok"):
        return "（系统）没拒绝成：%s" % (out.get("message") or out.get("reason") or "失败")
    return ("（系统）已经婉拒了（理由：%s）。接下来照实说，不要一边拒绝一边答应。"
            % (out.get("reason") or out.get("note") or "不方便"))
