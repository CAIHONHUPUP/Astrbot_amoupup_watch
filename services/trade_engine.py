# -*- coding: utf-8 -*-
"""A 股 / 场外基金交易规则与执行。

- 股票：9:30–11:30、13:00–15:00；周末/节假日休市；T+1；100 股整数倍；
  佣金 0.025%（最低 5 元）+ 过户费 0.001%（双向），卖出印花税 0.05%；涨跌幅主板 10%、创业板/科创板 20%。
- 基金：15:00 前按当日净值，15:00 后顺延；T+1 确认；申购 0.12%，赎回 0.5%。
- 钱全部走 amo.wallet（余额唯一权威），本模块只记持仓与成交。
"""
from __future__ import annotations

import datetime as _dt
import time

try:
    from ..guard import isolated
    from ..utils.helpers import fund_cutoff, in_session, is_trading_day, next_trading_day
except ImportError:  # 按文件/顶层包加载时
    from guard import isolated
    from utils.helpers import fund_cutoff, in_session, is_trading_day, next_trading_day
from .market_api import fetch_quotes

COMMISSION_RATE = 0.00025
MIN_COMMISSION_CENTS = 500
TRANSFER_RATE = 0.00001
STAMP_RATE = 0.0005
FUND_BUY_RATE = 0.0012
FUND_SELL_RATE = 0.005
LOT = 100


def _fee_buy(amount_cents: int) -> int:
    commission = max(MIN_COMMISSION_CENTS, int(round(amount_cents * COMMISSION_RATE)))
    transfer = int(round(amount_cents * TRANSFER_RATE))
    return commission + transfer


def _fee_sell(amount_cents: int) -> int:
    commission = max(MIN_COMMISSION_CENTS, int(round(amount_cents * COMMISSION_RATE)))
    transfer = int(round(amount_cents * TRANSFER_RATE))
    stamp = int(round(amount_cents * STAMP_RATE))
    return commission + transfer + stamp


def limit_ratio(code: str) -> float:
    return 0.20 if str(code).startswith(("300", "301", "688")) else 0.10


class Trading:
    def __init__(self, plugin, runtime, repo, finance):
        self.plugin = plugin
        self.runtime = runtime
        self.repo = repo
        self.finance = finance

    # ---------- 公共 ----------
    def _now(self):
        return _dt.datetime.now()

    def _wallet(self):
        amo = self.finance._amo()
        return getattr(amo, "wallet", None) if amo is not None else None

    def _amo(self):
        return self.finance._amo()

    def _quote(self, code, price=None):
        if price:
            return {"code": str(code), "name": "", "price": float(price), "prev_close": float(price)}
        rows = fetch_quotes([code])
        return rows.get(str(code))

    def _day(self):
        return self._now().date().isoformat()

    def _suspended(self, quote):
        return not quote or float(quote.get("price") or 0) <= 0

    def _one_word(self, quote, up=True):
        """一字板：开=高=低=现价，且贴在涨/跌停价上。"""
        try:
            o = float(quote.get("open") or 0)
            h = float(quote.get("high") or 0)
            l = float(quote.get("low") or 0)
            price = float(quote.get("price") or 0)
            prev = float(quote.get("prev_close") or 0)
        except (TypeError, ValueError):
            return False
        if not (o and h and l and prev and price):
            return False
        if abs(o - h) > 1e-6 or abs(h - l) > 1e-6 or abs(o - price) > 1e-6:
            return False
        ratio = 0.20 if str(quote.get("code") or "").startswith(("300", "301", "688")) else 0.10
        target = prev * (1 + ratio) if up else prev * (1 - ratio)
        return abs(price - target) <= max(0.011, prev * 0.005)

    def _ready_day(self, kind, now):
        day = now.date().isoformat()
        if str(kind) == "fund":
            return next_trading_day(next_trading_day(day))
        return next_trading_day(day)

    def _invested_cents(self):
        total = 0
        try:
            for row in self.repo.positions():
                total += int(row.get("last_cents") or 0) * float(row.get("qty") or 0)
        except Exception:
            return 0
        return int(round(total))

    def _cap_ok(self, wallet, add_cents):
        """投资不超过存款的 50%（存款 = 现金 + 已投入）。"""
        invested = self._invested_cents()
        deposit = int(wallet.balance()) + invested
        return (invested + int(add_cents)) <= deposit * 0.5 + 1

    # ---------- 股票 ----------
    async def buy(self, code, qty, price=None, idem=""):
        code, qty = str(code).strip(), int(float(qty or 0))
        idem = str(idem or ("buy:%s:%s:%d" % (code, self._now().strftime("%Y%m%d%H%M%S"), qty)))
        if qty <= 0 or qty % LOT:
            return {"ok": False, "message": "A 股买入必须是 100 股的整数倍"}
        if not in_session(self._now()):
            return {"ok": False, "message": "现在不是 A 股交易时段（9:30–11:30 / 13:00–15:00）"}
        quote = await self.runtime.run_io(self._quote, code, price, timeout=10)
        if not quote or not quote.get("price"):
            return {"ok": False, "message": "看不到行情，今天不交易"}
        if self._suspended(quote):
            return {"ok": False, "message": "停牌，无法成交"}
        if self._one_word(quote, up=True) and not price:
            return {"ok": False, "message": "一字涨停，买不进"}
        price = float(quote["price"])
        prev = float(quote.get("prev_close") or price)
        ratio = limit_ratio(code)
        if price > prev * (1 + ratio) or price < prev * (1 - ratio):
            return {"ok": False, "message": "超出涨跌幅限制，不能成交"}
        price_cents = int(round(price * 100))
        amount = price_cents * qty
        fee = _fee_buy(amount)
        wallet = self._wallet()
        if wallet is None:
            return {"ok": False, "message": "钱包未就绪"}
        if not self._cap_ok(wallet, amount):
            return {"ok": False, "message": "投资不能超过存款的 50%"}
        if not wallet.debit(amount + fee, "买入 %s" % code, idem):
            return {"ok": False, "message": "余额不足，买不起"}
        pos = self.repo.position("stock", code) or {"qty": 0, "cost_cents": 0, "available": 0}
        total_qty = float(pos["qty"]) + qty
        avg_cost = int(round((float(pos["cost_cents"]) * float(pos["qty"]) + amount + fee) / total_qty))
        self.repo.upsert_position("stock", code, quote.get("name", ""), total_qty,
                                  avg_cost, price_cents, float(pos["available"]),
                                  self._ready_day("stock", self._now()))
        self.repo.add_trade(idem, "buy", "stock", code, quote.get("name", ""), qty,
                            price_cents, amount, fee, 0, "T+1，明日可卖")
        await self.finance.record_trade("buy", "stock", quote.get("name", ""), code, price,
                                        qty, amount, fee, holding_qty=int(total_qty), idem=idem)
        return {"ok": True, "message": "买入 %s %d 股，成交价 %.2f，费用 %.2f" % (
            quote.get("name", code), qty, price, fee / 100.0),
            "position": {"qty": total_qty, "cost": avg_cost / 100.0},
            "next_day": next_trading_day(self._day())}

    async def sell(self, code, qty, price=None, idem=""):
        code, qty = str(code).strip(), int(float(qty or 0))
        if qty <= 0 or qty % LOT:
            return {"ok": False, "message": "A 股卖出必须是 100 股的整数倍"}
        if not in_session(self._now()):
            return {"ok": False, "message": "现在不是 A 股交易时段"}
        pos = self.repo.position("stock", code)
        if not pos or float(pos["available"]) < qty:
            return {"ok": False, "message": "可卖数量不足（T+1，今天买的明天才能卖）"}
        quote = await self.runtime.run_io(self._quote, code, price, timeout=10)
        if not quote or not quote.get("price"):
            return {"ok": False, "message": "看不到行情，今天不交易"}
        if self._suspended(quote):
            return {"ok": False, "message": "停牌，无法卖出"}
        if self._one_word(quote, up=False) and not price:
            return {"ok": False, "message": "一字跌停，卖不出"}
        price = float(quote["price"])
        price_cents = int(round(price * 100))
        amount = price_cents * qty
        fee = _fee_sell(amount)
        avg = float(pos["cost_cents"]) / 100.0
        pnl_cents = int(round((price - avg) * qty * 100)) - fee
        idem = str(idem or ("sell:%s:%s:%d" % (code, self._now().strftime("%Y%m%d%H%M%S"), qty)))
        wallet = self._wallet()
        if wallet is None:
            return {"ok": False, "message": "钱包未就绪"}
        if not wallet.credit(amount - fee, "卖出 %s" % code, idem):
            return {"ok": False, "message": "入账失败（可能重复提交）"}
        # 当天可买、T+1 才可取
        self.repo.add_pending(idem + ":w", "withdrawable", amount - fee,
                              next_trading_day(self._day()))
        left = float(pos["qty"]) - qty
        if left <= 0:
            self.repo.delete_position("stock", code)
        else:
            self.repo.upsert_position("stock", code, pos["name"], left, int(pos["cost_cents"]),
                                      price_cents, max(0.0, float(pos["available"]) - qty))
        ratio = (price - avg) / avg if avg else 0.0
        self.repo.add_trade(idem, "sell", "stock", code, pos["name"], qty, price_cents,
                            amount, fee, pnl_cents, "T+1")
        await self.finance.trade_pnl(pnl_cents, ratio, pos["name"] or code, idem)
        return {"ok": True, "message": "卖出 %s %d 股，成交价 %.2f，实现盈亏 %.2f" % (
            pos["name"] or code, qty, price, pnl_cents / 100.0),
            "pnl": pnl_cents / 100.0, "pnl_pct": ratio * 100}

    # ---------- 基金 ----------
    async def subscribe(self, code, amount_cents, nav=None, idem=""):
        code = str(code).strip()
        amount = int(float(amount_cents or 0))
        if amount <= 0:
            return {"ok": False, "message": "申购金额必须大于 0"}
        wallet = self._wallet()
        if wallet is None:
            return {"ok": False, "message": "钱包未就绪"}
        nav = float(nav or 1.0)
        fee = int(round(amount * FUND_BUY_RATE))
        shares = round((amount - fee) / (nav * 100.0), 2)
        idem = str(idem or ("fund_buy:%s:%s" % (code, self._now().strftime("%Y%m%d%H%M%S"))))
        if not self._cap_ok(wallet, amount):
            return {"ok": False, "message": "投资不能超过存款的 50%"}
        if not wallet.debit(amount, "申购基金 %s" % code, idem):
            return {"ok": False, "message": "余额不足"}
        pos = self.repo.position("fund", code) or {"qty": 0, "cost_cents": 0, "available": 0}
        total = float(pos["qty"]) + shares
        avg = int(round((float(pos["cost_cents"]) * float(pos["qty"]) + amount) / total))
        self.repo.upsert_position("fund", code, "", total, avg, int(round(nav * 100)),
                                  float(pos["available"]), self._ready_day("fund", self._now()))
        self.repo.add_trade(idem, "subscribe", "fund", code, "", shares, int(round(nav * 100)),
                            amount, fee, 0, "T+1 确认" if fund_cutoff(self._now()) else "顺延下一交易日")
        await self.finance.record_trade("buy", "fund", code, code, nav, shares, amount - fee, fee,
                                        holding_qty=int(total), idem=idem)
        return {"ok": True, "message": "申购 %s，金额 %.2f，份额 %.2f" % (code, amount / 100.0, shares),
                "shares": shares}

    async def redeem(self, code, shares, nav=None, idem=""):
        code = str(code).strip()
        shares = round(float(shares or 0), 2)
        if shares <= 0:
            return {"ok": False, "message": "赎回份额必须大于 0"}
        pos = self.repo.position("fund", code)
        if not pos or float(pos["available"]) < shares:
            return {"ok": False, "message": "可赎回份额不足（T+1 确认后才能赎回）"}
        nav = float(nav or (float(pos["last_cents"]) / 100.0))
        amount = int(round(nav * shares * 100))
        fee = int(round(amount * FUND_SELL_RATE))
        idem = str(idem or ("fund_sell:%s:%s" % (code, self._now().strftime("%Y%m%d%H%M%S"))))
        wallet = self._wallet()
        if wallet is None:
            return {"ok": False, "message": "钱包未就绪"}
        due = (self._now().date() + __import__("datetime").timedelta(days=7)).isoformat()
        self.repo.add_pending(idem, "cash_withdrawable", amount - fee, due)
        avg = float(pos["cost_cents"]) / 100.0
        pnl = int(round((nav - avg) * shares * 100)) - fee
        left = float(pos["qty"]) - shares
        if left <= 0.01:
            self.repo.delete_position("fund", code)
        else:
            self.repo.upsert_position("fund", code, pos["name"], left, int(pos["cost_cents"]),
                                      int(round(nav * 100)), max(0.0, float(pos["available"]) - shares))
        self.repo.add_trade(idem, "redeem", "fund", code, pos["name"], shares,
                            int(round(nav * 100)), amount, fee, pnl, "赎回")
        await self.finance.trade_pnl(pnl, (nav - avg) / avg if avg else 0.0, code, idem)
        return {"ok": True, "message": "赎回 %s %.2f 份，实现盈亏 %.2f" % (code, shares, pnl / 100.0),
                "pnl": pnl / 100.0}

    # ---------- T+1 可用份额 ----------
    def refresh_available(self):
        """T+1（股票）/ T+2（基金）到期后把可用数量放出来。"""
        today = self._day()
        for row in self.repo.positions():
            ready = str(row.get("ready_day") or "")
            qty = float(row.get("qty") or 0)
            if float(row.get("available") or 0) >= qty:
                continue
            if not ready or ready <= today:
                self.repo.upsert_position(row.get("kind"), row.get("code"), row.get("name"),
                                          qty, int(row.get("cost_cents") or 0),
                                          int(row.get("last_cents") or 0), qty, ready)

    async def apply_dividend(self, code, cash_per_share_cents=0, bonus_ratio=0.0, idem=""):
        """现金分红 + 送转除权：现金入账，股数/成本联动。"""
        pos = self.repo.position("stock", code)
        if not pos:
            return {"ok": False, "message": "没有这只持仓"}
        qty = float(pos["qty"])
        cash = int(round(float(cash_per_share_cents) * qty))
        ratio = float(bonus_ratio or 0)
        new_qty = qty * (1 + ratio)
        new_cost = int(round(float(pos["cost_cents"]) / (1 + ratio))) if ratio else int(pos["cost_cents"])
        self.repo.upsert_position("stock", code, pos.get("name"), new_qty, new_cost,
                                  int(pos.get("last_cents") or 0), new_qty,
                                  str(pos.get("ready_day") or ""))
        if cash > 0:
            self._wallet().credit(cash, "分红 %s" % code, idem or ("dividend:%s:%s" % (code, self._day())))
        await self.finance.remember_event("分红", cash, "%s 分红 %.2f 元，送转后持有 %.0f 股" % (
            pos.get("name") or code, cash / 100.0, new_qty), idem or "dividend")
        return {"ok": True, "cash_cents": cash, "qty": new_qty}

    async def force_close(self, code, reason="退市", price=None, idem=""):
        """退市强制平仓：不计交易时段，按最后价卖出。"""
        pos = self.repo.position("stock", code)
        if not pos:
            return {"ok": False, "message": "没有这只持仓"}
        price = float(price or (int(pos.get("last_cents") or 0) / 100.0) or 0)
        qty = int(float(pos["qty"]))
        if price <= 0:
            return {"ok": False, "message": "没有可用的最后价"}
        amount = int(round(price * 100)) * qty
        self._wallet().credit(amount, "退市平仓 %s" % code, idem or ("delist:%s:%s" % (code, self._day())))
        self.repo.delete_position("stock", code)
        self.repo.add_trade(idem or ("delist:%s" % code), "sell", "stock", code, pos.get("name"),
                            qty, int(round(price * 100)), amount, 0, 0, reason)
        await self.finance.remember_event("退市", amount, "%s 退市，按 %.2f 元强制平仓 %d 股" % (
            pos.get("name") or code, price, qty), idem or "delist")
        return {"ok": True, "amount_cents": amount}
