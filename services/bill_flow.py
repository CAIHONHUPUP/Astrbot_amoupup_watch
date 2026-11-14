# -*- coding: utf-8 -*-
"""账单自主结算核心：到期事件、支付、逾期三档；纯逻辑，不直接接发送。"""
from __future__ import annotations

import datetime as _dt
import json

try:
    from ..guard import isolated
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated

PENALTY_RATE = 0.05
STAGE_HOURS = (24, 72, 168)


def _repo(plugin):
    return getattr(plugin, "finance_repo", None)


def _finance(plugin):
    return getattr(plugin, "finance", None)


def holidays(plugin):
    raw = ""
    try:
        raw = str(getattr(plugin, "cfg_str", lambda *a, **k: "" )("bill_holidays", "") or "")
    except Exception:
        raw = ""
    return {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}


def is_workday(day, holiday_set=None):
    if day.weekday() >= 5:
        return False
    holiday_set = holiday_set or set()
    return day.isoformat() not in holiday_set and day.strftime("%m-%d") not in holiday_set


def due_day(now, holiday_set=None):
    day = now.replace(day=1).date()
    for _ in range(14):
        if is_workday(day, holiday_set):
            return day
        day += _dt.timedelta(days=1)
    return day


def _amo_owns_home(plugin):
    """她是否已经住进自己买的房（amo 房屋系统为准）；住自有房就不再产生房租。"""
    link = _finance(plugin)
    getter = getattr(link, "_amo", None)
    if not callable(getter):
        return False
    try:
        amo = getter()
        housing = getattr(amo, "housing", None) if amo is not None else None
        if housing is None:
            return False
        return str(housing.current().get("kind") or "") == "own"
    except Exception:
        return False


def bill_plan(plugin, period):
    life = getattr(plugin, "finance_life", None)
    prop = getattr(plugin, "finance_property", None)
    rows = []
    if prop is not None:
        try:
            loan = prop.loan("house") or {}
            if int(loan.get("balance_cents") or 0) > 0:
                rows.append({"kind": "mortgage", "name": "房贷",
                             "amount_cents": min(int(loan.get("monthly_cents") or 0),
                                                 int(loan.get("balance_cents") or 0))})
        except Exception:
            pass
    if life is not None:
        try:
            if not life._has("house") and not _amo_owns_home(plugin):
                rows.append({"kind": "rent", "name": "房租", "amount_cents": int(life.rent_cents(period))})
        except Exception:
            pass
        try:
            rows.append({"kind": "utilities", "name": "水电燃气宽带", "amount_cents": int(life.utility_cents(period))})
        except Exception:
            pass
    return [row for row in rows if row["amount_cents"] > 0]


def _key(period):
    return "bill:due:" + str(period)


def load_due(plugin, period):
    repo = _repo(plugin)
    try:
        raw = repo.meta_get(_key(period), [])
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        return list(raw or [])
    except Exception:
        return []


def save_due(plugin, period, rows):
    try:
        _repo(plugin).meta_set(_key(period), json.dumps(rows, ensure_ascii=False))
    except Exception:
        pass


FROZEN_KEY = "bill:assets_frozen"


def frozen_state(plugin):
    """本月是否被银行冻结资产（逾期 7 天档）。"""
    try:
        raw = _repo(plugin).meta_get(FROZEN_KEY, {})
    except Exception:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def is_frozen(plugin):
    return bool(frozen_state(plugin).get("period"))


def freeze_assets(plugin, period, rows, now):
    """冻结定期/股票/基金：只进不出，直到欠款补齐。"""
    total = sum(int(row.get("amount_cents") or 0) for row in unpaid(rows))
    state = {"period": str(period), "debt_cents": total, "since": float(now.timestamp())}
    try:
        _repo(plugin).meta_set(FROZEN_KEY, json.dumps(state, ensure_ascii=False))
    except Exception:
        pass
    return state


def clear_freeze(plugin):
    try:
        _repo(plugin).meta_set(FROZEN_KEY, "")
    except Exception:
        pass


async def _bank_of(plugin):
    bank = getattr(plugin, "bank", None)
    if bank is not None:
        return bank
    builder = getattr(plugin, "_bank", None)
    if callable(builder):
        try:
            return await builder()
        except Exception:
            return None
    return None


async def _raise_from_fixed(plugin, need_cents, now):
    """3 天档：把定期转成活期，拿到真实现金。"""
    if need_cents <= 0:
        return 0
    bank = await _bank_of(plugin)
    if bank is None:
        return 0
    raised = 0
    for _ in range(4):
        if raised >= need_cents:
            break
        try:
            out = await bank.withdraw(int(need_cents - raised), 0, now)
        except TypeError:
            try:
                out = await bank.withdraw(int(need_cents - raised), 0)
            except Exception:
                break
        except Exception:
            break
        if not out or not out.get("ok"):
            break
        raised += int(out.get("amount") or 0)
    return raised


async def _raise_from_stocks(plugin, need_cents, period, now):
    """3 天档：交易时段内按最后价卖出可用股票（真实现金入账）。"""
    trading = getattr(plugin, "trading", None)
    repo = _repo(plugin)
    if trading is None or repo is None or need_cents <= 0:
        return 0
    raised = 0
    try:
        positions = repo.positions("stock")
    except Exception:
        return 0
    for pos in positions:
        if raised >= need_cents:
            break
        try:
            qty = int(float(pos.get("available") or 0)) // 100 * 100
            price_cents = int(pos.get("last_cents") or 0)
        except Exception:
            continue
        if qty <= 0 or price_cents <= 0:
            continue
        try:
            out = await trading.sell(str(pos.get("code") or ""), qty, price_cents / 100.0,
                                     idem="bill:auto-sell:%s:%s" % (period, pos.get("code")))
        except Exception:
            continue
        if out and out.get("ok"):
            raised += price_cents * qty
    return raised


async def force_settle(plugin, rows, period, now):
    """3 天档：真的把钱弄出来（定期→股票），能交的先交掉。"""
    pending = unpaid(rows)
    finance = _finance(plugin)
    if not pending or finance is None:
        return {"paid": 0, "raised": 0}
    need = sum(int(row.get("amount_cents") or 0) for row in pending)
    balance = int(getattr(finance, "balance_cents", lambda: 0)() or 0)
    raised = 0
    if balance < need:
        raised += await _raise_from_fixed(plugin, need - balance, now)
        balance = int(finance.balance_cents() or 0)
    if balance < need:
        raised += await _raise_from_stocks(plugin, need - balance, period, now)
    paid = 0
    for row in pending:
        amount = int(row.get("amount_cents") or 0)
        if int(finance.balance_cents() or 0) < amount:
            break
        try:
            result = await finance.expense(
                amount, str(row.get("name")),
                "bill:auto:%s:%s" % (period, row.get("kind")), necessary=True)
        except Exception:
            break
        if not isinstance(result, dict) or not result.get("ok"):
            break
        row["status"] = "paid"
        row["paid_ts"] = now.timestamp()
        row["payer"] = "auto"
        paid += 1
    if paid:
        save_due(plugin, period, rows)
        try:
            finance.small_diary("银行自动处置了定期/股票，替我交了账单，心里空落落的。")
            await finance.permanent_memory(
                "逾期 3 天，银行自动处置资产交了 %d 笔账单。" % paid,
                "bill:auto:%s:mem" % period)
        except Exception:
            pass
    return {"paid": paid, "raised": raised}


def create_due(plugin, now=None):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    rows = load_due(plugin, period)
    if rows:
        return rows
    repo = _repo(plugin)

    def _settled(key):
        try:
            return bool(repo.settled(period, key))
        except Exception:
            return False

    rows = []
    for row in bill_plan(plugin, period):
        kind = str(row.get("kind") or "")
        if kind == "rent" and _settled("rent"):
            continue
        if kind == "utilities" and any(_settled(k) for k in ("water", "electric", "gas", "broadband")):
            continue
        if kind == "mortgage" and (_settled("mortgage") or _settled("house")):
            continue
        item = dict(row)
        item.update({"created_ts": now.timestamp(), "status": "due", "penalty": 0})
        rows.append(item)
    save_due(plugin, period, rows)
    return rows


def unpaid(rows):
    return [row for row in rows if row.get("status") == "due"]


def stage_of(rows, now_ts_value):
    pending = unpaid(rows)
    if not pending:
        return 0
    hours = (float(now_ts_value) - float(pending[0].get("created_ts") or 0)) / 3600.0
    stage = 0
    for index, mark in enumerate(STAGE_HOURS):
        if hours >= mark:
            stage = index + 1
    return stage


async def pay(plugin, kind="", now=None, payer="her"):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    rows = load_due(plugin, period)
    target = None
    for row in rows:
        if row.get("status") == "due" and (not kind or kind in (row.get("kind"), row.get("name"))):
            target = row
            break
    if target is None:
        return {"ok": False, "message": "这个月没有要交的这笔账呀。"}
    finance = _finance(plugin)
    if finance is None:
        return {"ok": False, "message": "账本没连上。"}
    amount = int(target.get("amount_cents") or 0)
    if int(getattr(finance, "balance_cents", lambda: 0)() or 0) < amount:
        return {"ok": False, "message": "钱不够，交不上。"}
    result = await finance.expense(amount, str(target.get("name")), "bill:%s:%s" % (period, target.get("kind")), necessary=True)
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "message": "扣款失败。"}
    target["status"] = "paid"
    target["paid_ts"] = now.timestamp()
    target["payer"] = payer
    save_due(plugin, period, rows)
    if not unpaid(rows):
        clear_freeze(plugin)
    thanks = "老公帮我交的" if payer == "husband" else "我自己交的"
    try:
        await finance.permanent_memory("%s 交了 %s 元%s，%s。" % (
            now.strftime("%Y-%m-%d"), amount / 100.0, target.get("name"), thanks),
            "bill:paid:%s:%s:mem" % (period, target.get("kind")))
    except Exception:
        pass
    try:
        finance.small_diary("今天交了%s，钱包又空了，%s。" % (target.get("name"), "谢谢老公救急" if payer == "husband" else "心疼"))
    except Exception:
        pass
    return {"ok": True, "message": "交上了：%s %s 元" % (target.get("name"), amount / 100.0)}


async def overdue_tick(plugin, now=None):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    rows = load_due(plugin, period)
    pending = unpaid(rows)
    if not pending:
        return {"ok": True, "stage": 0}
    stage = stage_of(rows, now.timestamp())
    finance = _finance(plugin)
    if stage >= 1:
        for row in pending:
            if row.get("penalty"):
                continue
            fee = max(1, int(round(int(row.get("amount_cents") or 0) * PENALTY_RATE)))
            if finance is None:
                continue
            try:
                paid = await finance.expense(fee, "%s滞纳金" % row.get("name"),
                                             "bill:penalty:%s:%s" % (period, row.get("kind")), necessary=True)
                if not (isinstance(paid, dict) and paid.get("ok")):
                    # 余额不够时 expense 返回 ok=False；这时不能回写 penalty，
                    # 否则下一轮直接 continue，账没扣、记忆却说扣了，永远补不回来。
                    continue
                row["penalty"] = fee
                finance.nudge_mood({"委屈": 30, "郁闷": 25, "开心": -25}, "bill:overdue:%s" % period)
                await finance.permanent_memory(
                    "因为没交%s逾期了，被银行扣了 %s 元滞纳金，天哪我好没用。" % (row.get("name"), fee / 100.0),
                    "bill:overdue:%s:%s:mem" % (period, row.get("kind")))
                finance.small_diary("忘了交%s，被扣了滞纳金，呜呜。" % row.get("name"))
            except Exception as exc:
                isolated("bill.penalty", exc)
    if stage >= 2:
        settled = {}
        try:
            settled = await force_settle(plugin, rows, period, now)
        except Exception:
            settled = {}
        pending = unpaid(rows)
        for row in pending:
            row["forced"] = True
        save_due(plugin, period, rows)
        if finance is not None and pending:
            try:
                finance.nudge_mood({"焦虑": 20, "郁闷": 15}, "bill:forced:%s" % period)
                if not settled.get("paid"):
                    finance.small_diary("银行催我处置资产补账单，我不敢看账户了。")
            except Exception:
                pass
    if stage >= 3 and finance is not None:
        pending = unpaid(rows)
        for row in pending:
            fee = int(row.get("penalty") or 0)
            if fee and not row.get("final_fee"):
                try:
                    paid = await finance.expense(fee, "%s最终违约金" % row.get("name"),
                                                 "bill:final:%s:%s" % (period, row.get("kind")), necessary=True)
                    if isinstance(paid, dict) and paid.get("ok"):
                        row["final_fee"] = True
                except Exception as exc:
                    isolated("bill.final_fee", exc)
            row["frozen"] = True
        if pending:
            freeze_assets(plugin, period, rows, now)
        save_due(plugin, period, rows)
        try:
            finance.nudge_mood({"委屈": 50, "郁闷": 40, "开心": -40, "焦虑": 30}, "bill:final:%s" % period)
            finance.small_diary("银行把我的账户冻结了，定期和股票都动不了……")
        except Exception:
            pass
    save_due(plugin, period, rows)
    return {"ok": True, "stage": stage}


def prompt_line(plugin, now=None):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    rows = load_due(plugin, period)
    pending = unpaid(rows)
    if not pending:
        return ""
    names = "、".join(str(row.get("name")) for row in pending)
    total = sum(int(row.get("amount_cents") or 0) for row in pending) / 100.0
    line = ("【银行提醒】%s 到期，共 %.2f 元，请及时还款。"
            "如果余额不够，先把定期/基金转回活期再交；决定交就必须立刻调用 pay_bill，否则视为未交。") % (names, total)
    if is_frozen(plugin):
        line += ("【账户冻结】银行已经冻结了你的定期/股票/基金，这些钱现在动不了；"
                 "只能等老公转账或还钱，欠款补上后才会解冻。")
    elif any(row.get("forced") for row in pending):
        line += ("【强制执行】银行要求你变卖资产补上欠款，请调用 sell_fund / sell_stock / withdraw_fixed "
                 "把钱转到可用余额，再调用 pay_bill；不处理会继续滞纳金，直到资产被冻结。")
    return line


def survival_line(plugin):
    finance = _finance(plugin)
    try:
        balance = int(finance.balance_cents() or 0)
    except Exception:
        return ""
    if balance < 50000:
        return "你刚交完大额账单，现在只剩 %.2f 元，接下来几天除了吃饭不能买非必需品。" % (balance / 100.0)
    return ""
