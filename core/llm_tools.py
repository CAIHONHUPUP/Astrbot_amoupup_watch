# -*- coding: utf-8 -*-
"""LLM 工具逻辑（装饰器薄壳留在 main，保证 handler 归属）。"""
try:
    pass
except ImportError:
    pass

ALIASES = {"e生保": "e生保", "医无忧": "医无忧", "金钟罩": "金钟罩", "意外险": "金钟罩",
           "重疾险": "重疾", "重疾": "重疾", "交强险": "交强险",
           "商业车险": "商车险", "车险": "商车险", "家财险": "家财险"}


async def buy_insurance_tool(plugin, insurance_type=""):
    key = ALIASES.get(str(insurance_type or "").strip())
    if key is None:
        return "支持的险种：e生保 / 医无忧 / 金钟罩（意外险）/ 重疾险 / 交强险 / 商业车险 / 家财险"
    if key in ("交强险", "商车险") and not getattr(plugin, "has_car", False):
        return "她现在没有车，不用买车险。"
    if key == "家财险" and not getattr(plugin, "has_house", False):
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
    except Exception:
        pass
    return "买好啦：%s 的%s，花了 %d 元。" % (target, gift, cents // 100)


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
    return "好呀好呀！我这就准备出门，%s见！" % (raw or "等会儿")
