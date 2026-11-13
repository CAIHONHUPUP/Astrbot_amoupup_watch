# -*- coding: utf-8 -*-
"""买房 / 买车：价格、首付、等额本息、固定资产与贷款月供。

规则（2026-11-13 用户明确）：

- 首付 30%，只在她**现金够付首付**时才买，绝不透支；
- 房贷 10/20/30 年（年化 3.5%–4.5%），车贷 3/5 年（年化 3.5%–5.5%），都按等额本息；
- **没房不产生物业费/房贷，买了房不再产生房租；没车不产生车贷/养车费**；
- 每月 1 日扣款顺序：房贷/车贷 → 房租（如有）→ 物业费 → 水电燃气 → 养车费用；
- 余额不足：先预警再进「资金低于 5%」的二选一流程，**不自动停扣、不没收资产**；
- 买房 / 买车 / 每次还贷：永久层记忆 + 一篇长篇日记。

本模块只决定「该扣多少、记什么」，钱一律走 FinanceLink → amo.wallet。
"""
from __future__ import annotations

import datetime as _dt
import hashlib

try:
    from ..guard import isolated
    from ..utils.helpers import (car_price_cents as base_car_price, city_factor,
                                 house_price_cents as base_house_price)
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated
    from utils.helpers import (car_price_cents as base_car_price, city_factor,
                              house_price_cents as base_house_price)
from .city import current_city, factor_of

#: 房子按 80 平算（用户给的例子）
HOUSE_AREA_M2 = 80

#: 价格区间（分）：房子 70–120 万，车 10–18 万
HOUSE_PRICE_RANGE = (70000000, 120000000)
CAR_PRICE_RANGE = (10000000, 18000000)

#: 首付比例（房、车同 30%）
DOWN_PCT = 0.30

#: 年化利率区间
HOUSE_RATE_RANGE = (0.035, 0.045)
CAR_RATE_RANGE = (0.035, 0.055)

#: 期数
HOUSE_TERMS = (10, 20, 30)
CAR_TERMS = (3, 5)
DEFAULT_HOUSE_TERM = 30
DEFAULT_CAR_TERM = 5

#: 物业费 1.5–3 元/平米/月（分）
PROPERTY_FEE_PER_M2 = (150, 300)

#: 养车：油费 300–600 / 停车 200–400 / 保险保养均摊 200–300（分）
CAR_FUEL_RANGE = (30000, 60000)
CAR_PARK_RANGE = (20000, 40000)
CAR_MAINT_RANGE = (20000, 30000)


def _hash_int(text, low, high):
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    span = max(1, high - low)
    return low + int(digest[:8], 16) % span


def monthly_payment(principal_cents, annual_rate, months):
    """等额本息月供（分）。"""
    principal = max(0, int(principal_cents))
    months = max(1, int(months))
    rate = float(annual_rate) / 12.0
    if rate <= 0:
        return int(round(principal / months))
    factor = (1 + rate) ** months
    return int(round(principal * rate * factor / (factor - 1)))


def amortize(principal_cents, annual_rate, months):
    """按等额本息摊开：返回 (月供, 总利息, 每期本金/利息列表)。"""
    principal = max(0, int(principal_cents))
    months = max(1, int(months))
    monthly_rate = float(annual_rate) / 12.0
    payment = monthly_payment(principal, annual_rate, months)
    balance = principal
    rows = []
    total_interest = 0
    for seq in range(1, months + 1):
        interest = int(round(balance * monthly_rate))
        principal_part = payment - interest
        if seq == months or principal_part >= balance:
            principal_part = balance
            pay = principal_part + interest
        else:
            pay = payment
        balance -= principal_part
        total_interest += interest
        rows.append({"seq": seq, "payment": pay, "principal": principal_part, "interest": interest})
        if balance <= 0:
            break
    return payment, total_interest, rows


class FinanceProperty:
    """房产 / 车辆资产与贷款。"""

    def __init__(self, plugin, repo, finance, life=None):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.life = life

    # ---------- 价格与利率（首次写入 meta，之后固定） ----------
    def _cfg_int(self, key, default):
        try:
            value = getattr(self.plugin, "config", {}).get(key, default)
            return int(value)
        except Exception:
            return int(default)

    def price_cents(self, kind):
        """按城市算房价/车价；城市换了会重算（meta 里记着上次是按哪个城市算的）。"""
        key = "price:%s" % kind
        low, high = HOUSE_PRICE_RANGE if kind == "house" else CAR_PRICE_RANGE
        city = current_city(getattr(self, "plugin", None))
        marker = str(city or "-")
        raw = self.repo.meta_get(key)
        stored_city = self.repo.meta_get("price_city:%s" % kind, "")
        if raw and str(stored_city or "") == marker:
            try:
                stored = int(raw)
                if low <= stored <= high:
                    return stored
            except Exception:
                pass
        if kind == "house":
            value = base_house_price(city)
            value = self._cfg_int("finance_house_price_cents", value)
        else:
            value = base_car_price(city)
            value = self._cfg_int("finance_car_price_cents", value)
        self.repo.meta_set(key, value)
        self.repo.meta_set("price_city:%s" % kind, marker)
        return value

    def rate(self, kind):
        key = "rate:%s" % kind
        raw = self.repo.meta_get(key)
        if raw:
            try:
                return float(raw)
            except Exception:
                pass
        low, high = HOUSE_RATE_RANGE if kind == "house" else CAR_RATE_RANGE
        value = round(low + (high - low) * (_hash_int("rate:" + kind, 0, 1000) / 1000.0), 4)
        self.repo.meta_set(key, value)
        return value

    def term_months(self, kind):
        years = DEFAULT_HOUSE_TERM if kind == "house" else DEFAULT_CAR_TERM
        return int(years) * 12, int(years)

    def down_cents(self, kind):
        return int(round(self.price_cents(kind) * DOWN_PCT))

    def loan_kind(self, kind):
        return "mortgage" if kind == "house" else "car_loan"

    # ---------- 当前状态 ----------
    def asset(self, kind):
        try:
            return self.repo.asset(kind)
        except Exception as exc:
            isolated("property.asset", exc)
            return None

    def loan(self, kind):
        try:
            return self.repo.loan(self.loan_kind(kind))
        except Exception as exc:
            isolated("property.loan", exc)
            return None

    def property_fee_cents(self, period):
        if not self.asset("house"):
            return 0
        per_m2 = _hash_int("property:" + str(period), *PROPERTY_FEE_PER_M2)
        return int(per_m2 * HOUSE_AREA_M2)

    def car_cost_cents(self, period):
        if not self.asset("car"):
            return 0
        factor = factor_of(current_city(getattr(self, "plugin", None)))
        fuel = _hash_int("fuel:" + str(period), *CAR_FUEL_RANGE)
        park = _hash_int("park:" + str(period), *CAR_PARK_RANGE)
        maint = _hash_int("maint:" + str(period), *CAR_MAINT_RANGE)
        return int(round((fuel + park + maint) * factor))

    def state(self):
        house = self.asset("house")
        car = self.asset("car")
        mortgage = self.loan("house")
        car_loan = self.loan("car")
        today = _dt.date.today()
        period = today.strftime("%Y-%m")
        return {
            "has_house": bool(house),
            "has_car": bool(car),
            "house_value": int((house or {}).get("value_cents") or 0) / 100.0,
            "car_value": int((car or {}).get("value_cents") or 0) / 100.0,
            "mortgage": int((mortgage or {}).get("monthly_cents") or 0) / 100.0,
            "car_loan": int((car_loan or {}).get("monthly_cents") or 0) / 100.0,
            "property_fee": self.property_fee_cents(period) / 100.0,
            "car_cost": self.car_cost_cents(period) / 100.0,
            "house_price": self.price_cents("house") / 100.0,
            "car_price": self.price_cents("car") / 100.0,
            "house_down": self.down_cents("house") / 100.0,
            "car_down": self.down_cents("car") / 100.0,
            "down_pct": int(round(DOWN_PCT * 100)),
            "house_rate": self.rate("house"),
            "car_rate": self.rate("car"),
            "house_years": list(HOUSE_TERMS),
            "car_years": list(CAR_TERMS),
            "mortgage_balance": int((mortgage or {}).get("balance_cents") or 0) / 100.0,
            "car_loan_balance": int((car_loan or {}).get("balance_cents") or 0) / 100.0,
        }

    def afford(self, kind):
        cash = int(self.finance.balance_cents() or 0)
        need = self.down_cents(kind)
        return {"cash": cash, "need": need, "ok": cash >= need, "short": max(0, need - cash)}

    # ---------- 买 ----------
    async def buy(self, kind, now=None, reason=""):
        kind = "house" if str(kind) in ("house", "房", "买房") else "car"
        if self.asset(kind):
            return {"ok": False, "reason": "already_owned", "kind": kind}
        check = self.afford(kind)
        if not check["ok"]:
            return {"ok": False, "reason": "insufficient", "kind": kind,
                    "need": check["need"] / 100.0, "short": check["short"] / 100.0}
        day = (now or _dt.date.today()).isoformat()
        price = self.price_cents(kind)
        down = self.down_cents(kind)
        principal = price - down
        rate = self.rate(kind)
        months, years = self.term_months(kind)
        monthly = monthly_payment(principal, rate, months)
        label = "房子（%d 平）" % HOUSE_AREA_M2 if kind == "house" else "车"
        paid = await self.finance.expense(
            down, "买房首付" if kind == "house" else "买车首付",
            "property:%s:%s" % (kind, day), necessary=True)
        if not paid.get("ok"):
            return {"ok": False, "reason": "debit_failed", "kind": kind}
        loan_id = self.repo.add_loan("property:%s:%s" % (kind, day), self.loan_kind(kind),
                                     principal, principal, rate, months, monthly)
        self.repo.add_asset(kind, label, price)
        self.repo.meta_set("buy:%s" % kind, day)
        mood = ""
        try:
            mood = str(self.finance.mood().get("word") or "")
        except Exception:
            mood = ""
        text = self._buy_text(kind, price, down, monthly, years, rate, mood)
        await self._remember(text, "buy:%s:%s" % (kind, day))
        self._long_diary(text)
        return {"ok": True, "kind": kind, "price": price / 100.0, "down": down / 100.0,
                "principal": principal / 100.0, "rate": rate, "months": months, "years": years,
                "monthly": monthly / 100.0, "loan_id": loan_id}

    @staticmethod
    def _buy_text(kind, price, down, monthly, years, rate, mood):
        if kind == "house":
            return ("今天去签了购房合同，%s 的房子，首付 %s，剩下 %s 万做了 %d 年房贷，"
                    "月供 %s。终于在这个城市有自己的小窝了，再也不用给房东交租，%s。"
                    % (("%.0f 万" % (price / 1000000.0)), ("%.2f 万" % (down / 1000000.0)),
                       ("%.1f" % ((price - down) / 1000000.0)), years,
                       ("%.0f 元" % (monthly / 100.0)), mood or "心里又松又紧"))
        return ("今天把车提回来了，车价 %s，首付 %s，贷款 %d 年，月供 %s。"
                "以后上班不用挤地铁了，%s。" % (("%.1f 万" % (price / 1000000.0)),
                                            ("%.2f 万" % (down / 1000000.0)), years,
                                            ("%.0f 元" % (monthly / 100.0)), mood or "有点小得意"))

    async def _remember(self, text, idem):
        try:
            await self.finance.permanent_memory(str(text), str(idem))
            return True
        except Exception as exc:
            isolated("property.memory", exc)
            return False

    def _long_diary(self, text):
        try:
            return bool(self.finance.long_diary(str(text)))
        except Exception as exc:
            isolated("property.diary", exc)
            return False

    # ---------- 每月 1 日：还贷 / 物业费 / 养车 ----------
    async def settle_loans(self, now, period):
        done = []
        for kind in ("house", "car"):
            loan = self.loan(kind)
            if not loan:
                continue
            balance = int(loan.get("balance_cents") or 0)
            if balance <= 0:
                continue
            key = self.loan_kind(kind)
            if self.repo.settled(period, key):
                continue
            rate = float(loan.get("rate") or 0)
            interest = int(round(balance * rate / 12.0))
            due = int(loan.get("monthly_cents") or 0)
            amount = min(due, balance + interest)
            name = "房贷" if kind == "house" else "车贷"
            result = await self.finance.expense(amount, name, "%s:%s" % (key, period), necessary=True)
            if not result.get("ok"):
                await self._warn_short(name, amount, period)
                return {"ok": False, "reason": "insufficient", "kind": kind}
            principal_part = max(0, amount - interest)
            new_balance = max(0, balance - principal_part)
            self.repo.update_loan(int(loan.get("id") or 0), new_balance,
                                  "closed" if new_balance <= 0 else "")
            self.repo.mark_settled(period, key, str({"amount": amount, "interest": interest,
                                                     "balance": new_balance}))
            mood = ""
            try:
                mood = str(self.finance.mood().get("word") or "")
            except Exception:
                mood = ""
            text = ("今天还了%s %s，其中利息 %s，本金还剩 %s。%s"
                    % (name, ("%.2f 元" % (amount / 100.0)), ("%.2f 元" % (interest / 100.0)),
                       ("%.2f 万" % (new_balance / 1000000.0)), mood or "日子得慢慢过"))
            await self._remember(text, "repay:%s:%s" % (key, period))
            self._long_diary(text)
            done.append({"kind": kind, "amount": amount, "balance": new_balance})
        return {"ok": True, "done": done}

    async def _warn_short(self, name, amount, period):
        key = "warned:%s:%s" % (name, period)
        cash = int(self.finance.balance_cents() or 0)
        need = max(0, int(amount) - cash)
        try:
            await self.finance.low_balance(need, "propwarn:%s:%s" % (name, period))
        except Exception as exc:
            isolated("property.warn", exc)
        text = ("这个月的%s %s 元有点拿不出来，还差 %s，%s。"
                % (name, ("%.2f" % (amount / 100.0)), ("%.2f 元" % (need / 100.0)),
                   "别慌，先想想办法"))
        try:
            await self.finance.remember_event("预警", int(need), text, key)
        except Exception as exc:
            isolated("property.warn_memory", exc)
        self._long_diary(text)

    async def charge_property_fee(self, now, period):
        fee = self.property_fee_cents(period)
        if fee <= 0 or self.repo.settled(period, "property_fee"):
            return "skip"
        result = await self.finance.expense(fee, "物业费", "property_fee:" + period, necessary=True)
        if not result.get("ok"):
            return "fail"
        self.repo.mark_settled(period, "property_fee", str(fee))
        return "ok"

    async def charge_car_cost(self, now, period):
        cost = self.car_cost_cents(period)
        if cost <= 0 or self.repo.settled(period, "car_cost"):
            return "skip"
        result = await self.finance.expense(cost, "养车费用", "car_cost:" + period, necessary=True)
        if not result.get("ok"):
            return "fail"
        self.repo.mark_settled(period, "car_cost", str(cost))
        return "ok"

    # ---------- 自主决定 ----------
    async def maybe_buy(self, now=None):
        """够首付就买：先房后车；每个 tick 只买一样。"""
        now = now or _dt.date.today()
        if not self.asset("house") and self.afford("house")["ok"]:
            return await self.buy("house", now, reason="autonomous")
        if not self.asset("car") and self.afford("car")["ok"]:
            return await self.buy("car", now, reason="autonomous")
        return {"ok": False, "reason": "skip"}
