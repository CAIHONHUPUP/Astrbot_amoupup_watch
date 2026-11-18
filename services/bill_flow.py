# -*- coding: utf-8 -*-
"""账单自主结算核心：到期事件、支付、逾期三档；纯逻辑，不直接接发送。"""
from __future__ import annotations

import datetime as _dt
import json
import time as _time

try:
    from ..guard import isolated
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated

try:
    from ..utils.helpers import HOLIDAYS_2026
    from ..utils.money import bill_idem, fmt_amount, idem
except ImportError:  # 离线测试 / 顶层加载
    from utils.helpers import HOLIDAYS_2026
    from utils.money import bill_idem, fmt_amount, idem

PENALTY_RATE = 0.05
STAGE_HOURS = (24, 72, 168)


def _repo(plugin):
    return getattr(plugin, "finance_repo", None)


def _finance(plugin):
    return getattr(plugin, "finance", None)


def holidays(plugin):
    """法定节假日 = 内置国家节假日库 + 配置里的额外日期。"""
    raw = ""
    try:
        raw = str(getattr(plugin, "cfg_str", lambda *a, **k: "" )("bill_holidays", "") or "")
    except Exception:
        raw = ""
    configured = {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}
    return set(HOLIDAYS_2026) | configured


def is_workday(day, holiday_set=None):
    if day.weekday() >= 5:
        return False
    if holiday_set is None:
        holiday_set = HOLIDAYS_2026
    return day.isoformat() not in holiday_set and day.strftime("%m-%d") not in holiday_set


def due_day(now, holiday_set=None):
    """账单日：1 号遇周末/节假日顺延到下一个工作日。"""
    base = now.date() if hasattr(now, "date") else now
    day = base.replace(day=1)
    for _ in range(30):
        if is_workday(day, holiday_set):
            return day
        day += _dt.timedelta(days=1)
    return day


def salary_pay_day(now, holiday_set=None):
    """发薪日：15 号遇周末/节假日顺延到下一个工作日（周一）。"""
    base = now.date() if hasattr(now, "date") else now
    day = base.replace(day=15)
    for _ in range(30):
        if is_workday(day, holiday_set):
            return day
        day += _dt.timedelta(days=1)
    return day


def deferral_note(plugin, now=None):
    """月底最后一天，如果下月 1 号不是工作日，给聊天注入一句“还能多捂两天钱”。"""
    now = now or _dt.datetime.now()
    base = now.date() if hasattr(now, "date") else now
    if (base + _dt.timedelta(days=1)).month == base.month:
        return ""
    next_first = (base.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    due = due_day(next_first, holidays(plugin))
    if due.day <= 1:
        return ""
    name = "账单"
    try:
        rows = bill_plan(plugin, next_first.strftime("%Y-%m"))
        if rows:
            name = str(rows[0].get("name") or "账单")
    except Exception as exc:
        isolated("bill_flow.deferral_note", exc)
    return ("【账单顺延】明天一号，但刚好是周末/放假，%s要等到 %d 月 %d 日才扣，我还能多捂两天钱。"
            % (name, due.month, due.day))


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
        except Exception as exc:
            isolated("bill_flow.bill_plan", exc)
    if life is not None:
        try:
            if not life._has("house") and not _amo_owns_home(plugin):
                rows.append({"kind": "rent", "name": "房租", "amount_cents": int(life.rent_cents(period))})
        except Exception as exc:
            isolated("bill_flow.bill_plan.2", exc)
        try:
            for item in life.utility_plan(period):
                rows.append({"kind": str(item.get("key") or ""),
                             "name": str(item.get("name") or ""),
                             "amount_cents": int(item.get("amount_cents") or 0)})
        except Exception as exc:
            isolated("bill_flow.bill_plan.3", exc)
    return [row for row in rows if row["amount_cents"] > 0]


def _key(period):
    return "bill:due:" + str(period)


#: 水电燃气宽带按单项立项，每条账单只对应一个 settlements 键。
UTILITY_KEYS = ("water", "electric", "gas", "broadband")
SETTLE_KEYS = {"rent": ("rent",),
               "water": ("water",),
               "electric": ("electric",),
               "gas": ("gas",),
               "broadband": ("broadband",),
               # 迁移前的旧聚合单：整张水电都算交过
               "utilities": UTILITY_KEYS}
#: 房贷走 loans 的 settlement（house），和自动代扣共用同一个幂等键。
MORTGAGE_KEYS = ("house", "mortgage")
_UTILITY_ALIASES = ("utilities", "水电", "水电费", "水电燃气", "水电燃气宽带")


def settlement_keys(kind):
    """一个账单 kind 对应的 settlements 键。"""
    text = str(kind or "")
    if text == "mortgage":
        return MORTGAGE_KEYS
    return SETTLE_KEYS.get(text, ())


def already_settled(plugin, period, kind):
    """本期这笔账是否已经自动扣过（两条入口共用的幂等判重）。"""
    repo = _repo(plugin)
    if repo is None:
        return False
    for key in settlement_keys(kind):
        try:
            if repo.settled(period, key):
                return True
        except Exception as exc:
            isolated("bill_flow.settled", exc)
    return False


def wanted_kinds(kind):
    """把「水电」「utilities」这类说法归一成一组单项 kind。"""
    text = str(kind or "").strip()
    if not text:
        return ()
    if text in _UTILITY_ALIASES:
        return UTILITY_KEYS
    return (text,)


def mark_paid_settled(plugin, period, kind):
    """账单交完后同步结算键；失败只记日志，不影响这次扣款结果。"""
    repo = _repo(plugin)
    if repo is None:
        return
    for key in SETTLE_KEYS.get(str(kind or ""), ()):
        try:
            repo.mark_settled(period, key, "bill_paid")
        except Exception as exc:
            isolated("bill_flow.mark_settled", exc)


def _charged_already(plugin, period, name, amount, account="cash"):
    """同一期同一项是否已经在流水里扣过（哪怕幂等键不同）。

    只读 wallet.ledger：用于「自动代扣已经扣了、结算键丢了，主入口又扣一次」这种
    重试/双入口重复扣。命中就只收口、绝不再动钱。
    """
    finance = _finance(plugin)
    reader = getattr(finance, "ledger", None)
    if not callable(reader) or int(amount or 0) <= 0:
        return False
    try:
        rows = list(reader(300) or [])
    except Exception as exc:
        isolated("bill_flow.ledger", exc)
        return False
    label = str(name or "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("account") or "cash") != str(account):
            continue
        if str(row.get("reason") or "") != label:
            continue
        try:
            if int(row.get("delta_cents") or 0) != -int(amount):
                continue
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts and _dt.datetime.fromtimestamp(ts).strftime("%Y-%m") != str(period):
            continue
        return True
    return False


def load_due(plugin, period):
    repo = _repo(plugin)
    try:
        raw = repo.meta_get(_key(period), [])
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        rows = [row for row in (raw or []) if isinstance(row, dict)]
    except Exception:
        return []
    rows = _migrate_legacy_utilities(plugin, period, rows)
    return _reconcile_paid_rows(plugin, period, rows)


def _migrate_legacy_utilities(plugin, period, rows):
    """旧版把水电燃气宽带写成一张聚合单：按单项拆开，幂等、不丢老数据。"""
    if not any(str(row.get("kind") or "") == "utilities" for row in rows):
        return rows
    life = getattr(plugin, "finance_life", None)
    plan = ()
    if life is not None and hasattr(life, "utility_plan"):
        try:
            plan = tuple(life.utility_plan(period))
        except Exception as exc:
            isolated("bill_flow.migrate_plan", exc)
            plan = ()
    by_key = {str(item.get("key") or ""): item for item in plan if isinstance(item, dict)}
    if not by_key:
        return rows  # 没有生活模块：保留旧聚合行，别把欠款丢掉
    repo = _repo(plugin)
    out = []
    for row in rows:
        if str(row.get("kind") or "") != "utilities":
            out.append(row)
            continue
        if str(row.get("status") or "") == "paid":
            # 旧聚合单已经交过钱：四项都记成已交，避免自动代扣再收一遍
            for key in UTILITY_KEYS:
                try:
                    repo.mark_settled(period, key, "legacy_bill_paid")
                except Exception as exc:
                    isolated("bill_flow.migrate_settled", exc)
            continue
        for key in UTILITY_KEYS:
            if already_settled(plugin, period, key):
                continue
            item = by_key.get(key) or {}
            out.append({"kind": key, "name": str(item.get("name") or key),
                        "amount_cents": int(item.get("amount_cents") or 0),
                        "created_ts": row.get("created_ts"),
                        "status": "due", "penalty": int(row.get("penalty") or 0)})
    save_due(plugin, period, out)
    return out


def _reconcile_paid_rows(plugin, period, rows):
    """已经自动扣过的账不能再当成欠费：按 settlements 收回状态，防止二次扣款。"""
    changed = False
    for row in rows:
        if str(row.get("status") or "") != "due":
            continue
        if already_settled(plugin, period, row.get("kind")):
            row["status"] = "paid"
            row["payer"] = "auto"
            row["paid_ts"] = row.get("paid_ts") or _time.time()
            changed = True
    if changed:
        save_due(plugin, period, rows)
    return rows


def save_due(plugin, period, rows):
    try:
        _repo(plugin).meta_set(_key(period), json.dumps(rows, ensure_ascii=False))
    except Exception as exc:
        isolated("bill_flow.save_due", exc)


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
    except Exception as exc:
        isolated("bill_flow.freeze_assets", exc)
    return state


def clear_freeze(plugin):
    try:
        _repo(plugin).meta_set(FROZEN_KEY, "")
    except Exception as exc:
        isolated("bill_flow.clear_freeze", exc)


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
                                     idem=idem("bill:auto-sell", period, pos.get("code")))
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
    touched = 0
    for row in pending:
        kind = str(row.get("kind") or "")
        if already_settled(plugin, period, kind):
            # 自动代扣已经交过这一项：只把账单行收口，绝不再扣一次
            row["status"] = "paid"
            row["paid_ts"] = now.timestamp()
            row["payer"] = "auto"
            touched += 1
            continue
        if kind == "mortgage":
            # 房贷和自动代扣共用 settle_loan：同一幂等键、同一次本金摊销
            out = await _settle_mortgage(plugin, now, period)
            if str((out or {}).get("status") or "") != "paid":
                break
            row["status"] = "paid"
            row["paid_ts"] = now.timestamp()
            row["payer"] = "auto"
            paid += 1
            touched += 1
            continue
        amount = int(row.get("amount_cents") or 0)
        if int(finance.balance_cents() or 0) < amount:
            break
        try:
            result = await finance.expense(
                amount, str(row.get("name")),
                idem("bill:auto", period, kind), necessary=True)
        except Exception:
            break
        if not isinstance(result, dict) or not result.get("ok"):
            break
        row["status"] = "paid"
        row["paid_ts"] = now.timestamp()
        row["payer"] = "auto"
        mark_paid_settled(plugin, period, kind)
        paid += 1
        touched += 1
    if touched:
        save_due(plugin, period, rows)
    if paid:
        try:
            finance.small_diary("银行自动处置了定期/股票，替我交了账单，心里空落落的。")
            await finance.permanent_memory(
                "逾期 3 天，银行自动处置资产交了 %d 笔账单。" % paid,
                idem("bill:auto", period, "mem"))
        except Exception as exc:
            isolated("bill_flow.force_settle", exc)
    return {"paid": paid, "raised": raised}


def create_due(plugin, now=None):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    holiday_set = holidays(plugin)
    due = due_day(now, holiday_set)
    if now.date() < due or (now.date() == due and int(now.hour) < 9):
        return []
    rows = load_due(plugin, period)
    if rows:
        return rows
    rows = []
    for row in bill_plan(plugin, period):
        kind = str(row.get("kind") or "")
        if kind and already_settled(plugin, period, kind):
            continue  # 任何一项已经交过（自动代扣/手动），都不再重复立项
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


async def _settle_mortgage(plugin, now, period):
    """房贷代缴入口：复用 loans 的结算（同一幂等键 + 本金摊销），绝不只扣钱。"""
    prop = getattr(plugin, "finance_property", None)
    if prop is None or not hasattr(prop, "settle_loan"):
        return {"ok": False, "status": "unavailable"}
    try:
        return await prop.settle_loan("house", now, period)
    except Exception as exc:
        isolated("bill_flow.mortgage", exc)
        return {"ok": False, "status": "error"}


async def pay(plugin, kind="", now=None, payer="her"):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    holiday_set = holidays(plugin)
    due = due_day(now, holiday_set)
    if now.date() < due:
        return {"ok": False, "message": "还没到扣款日，等 %d 月 %d 日再交吧。" % (due.month, due.day)}
    if not is_workday(now.date(), holiday_set) or int(now.hour) < 9:
        return {"ok": False, "message": "今天休息，银行不办理，等下一个工作日再交吧。"}
    rows = load_due(plugin, period)
    wanted = wanted_kinds(kind)
    target = None
    for row in rows:
        if row.get("status") != "due":
            continue
        row_kind = str(row.get("kind") or "")
        if not wanted or row_kind in wanted or str(row.get("name") or "") == str(kind or ""):
            target = row
            break
    if target is None:
        for row in rows:
            row_kind = str(row.get("kind") or "")
            if row.get("status") == "paid" and (
                    (wanted and row_kind in wanted) or str(row.get("name") or "") == str(kind or "")):
                return {"ok": False, "already_settled": True,
                        "message": "这笔账单已经交过了（自动代扣或手动），不用再交一遍。"}
        return {"ok": False, "message": "这个月没有要交的这笔账呀。"}
    bill_kind = str(target.get("kind") or "")
    if bill_kind == "mortgage":
        out = await _settle_mortgage(plugin, now, period)
        status = str((out or {}).get("status") or "")
        if status == "paid":
            target["status"] = "paid"
            target["paid_ts"] = now.timestamp()
            target["payer"] = payer
            save_due(plugin, period, rows)
            if not unpaid(rows):
                clear_freeze(plugin)
            return {"ok": True, "message": "交上了：房贷 %s 元（本金 %s，利息 %s）" % (
                fmt_amount(int(out.get("amount") or 0)), fmt_amount(int(out.get("principal") or 0)),
                fmt_amount(int(out.get("interest") or 0)))}
        if status == "settled":
            target["status"] = "paid"
            target["paid_ts"] = now.timestamp()
            target["payer"] = "auto"
            save_due(plugin, period, rows)
            if not unpaid(rows):
                clear_freeze(plugin)
            return {"ok": False, "already_settled": True,
                    "message": "这个月的房贷已经自动扣过了，不用手动交。"}
        return {"ok": False, "message": "钱不够，房贷没交上。"}
    finance = _finance(plugin)
    if finance is None:
        return {"ok": False, "message": "账本没连上。"}
    if already_settled(plugin, period, bill_kind):
        target["status"] = "paid"
        target["paid_ts"] = now.timestamp()
        target["payer"] = "auto"
        save_due(plugin, period, rows)
        return {"ok": False, "already_settled": True,
                "message": "%s 已经自动扣过了，不用手动交。" % (target.get("name") or "这笔账")}
    amount = int(target.get("amount_cents") or 0)
    if int(getattr(finance, "balance_cents", lambda: 0)() or 0) < amount:
        return {"ok": False, "message": "钱不够，交不上。"}
    if _charged_already(plugin, period, target.get("name"), amount):
        # 自动代扣/手动交这两个入口历史上一度各用一套幂等键：钱包不会认重，
        # 重试就会真扣第二遍。先按流水对一次账，命中也只收口不动钱。
        target["status"] = "paid"
        target["paid_ts"] = now.timestamp()
        target["payer"] = "auto"
        save_due(plugin, period, rows)
        mark_paid_settled(plugin, period, bill_kind)
        return {"ok": False, "already_settled": True,
                "message": "%s 这个月已经扣过了，不用再交。" % (target.get("name") or "这笔账")}
    result = await finance.expense(amount, str(target.get("name")),
                                    bill_idem(period, bill_kind), necessary=True)
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "message": "扣款失败。"}
    target["status"] = "paid"
    target["paid_ts"] = now.timestamp()
    target["payer"] = payer
    save_due(plugin, period, rows)
    mark_paid_settled(plugin, period, bill_kind)
    if not unpaid(rows):
        clear_freeze(plugin)
    thanks = "老公帮我交的" if payer == "husband" else "我自己交的"
    try:
        await finance.permanent_memory("%s 交了 %s 元%s，%s。" % (
            now.strftime("%Y-%m-%d"), fmt_amount(amount), target.get("name"), thanks),
            idem("bill:paid", period, bill_kind, "mem"))
    except Exception as exc:
        isolated("bill_flow.pay", exc)
    try:
        finance.small_diary("今天交了%s，钱包又空了，%s。" % (target.get("name"), "谢谢老公救急" if payer == "husband" else "心疼"))
    except Exception as exc:
        isolated("bill_flow.pay.2", exc)
    return {"ok": True, "message": "交上了：%s %s 元" % (target.get("name"), fmt_amount(amount))}


async def overdue_tick(plugin, now=None):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    rows = load_due(plugin, period)
    pending = unpaid(rows)
    if not pending:
        return {"ok": True, "stage": 0}
    stage = stage_of(rows, now.timestamp())
    holiday_set = holidays(plugin)
    if not is_workday(now.date(), holiday_set) or int(now.hour) < 9:
        # 周末/节假日/凌晨不扣滞纳金，等工作日 9 点后再说
        return {"ok": True, "stage": stage, "deferred": True}
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
                                             idem("bill:penalty", period, row.get("kind")), necessary=True)
                if not (isinstance(paid, dict) and paid.get("ok")):
                    # 余额不够时 expense 返回 ok=False；这时不能回写 penalty，
                    # 否则下一轮直接 continue，账没扣、记忆却说扣了，永远补不回来。
                    continue
                row["penalty"] = fee
                finance.nudge_mood({"委屈": 30, "郁闷": 25, "开心": -25}, idem("bill:overdue", period))
                await finance.permanent_memory(
                    "因为没交%s逾期了，被银行扣了 %s 元滞纳金，天哪我好没用。" % (row.get("name"), fmt_amount(fee)),
                    idem("bill:overdue", period, row.get("kind"), "mem"))
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
                finance.nudge_mood({"焦虑": 20, "郁闷": 15}, idem("bill:forced", period))
                if not settled.get("paid"):
                    finance.small_diary("银行催我处置资产补账单，我不敢看账户了。")
            except Exception as exc:
                isolated("bill_flow.overdue_tick", exc)
    if stage >= 3 and finance is not None:
        pending = unpaid(rows)
        for row in pending:
            fee = int(row.get("penalty") or 0)
            if fee and not row.get("final_fee"):
                try:
                    paid = await finance.expense(fee, "%s最终违约金" % row.get("name"),
                                                 idem("bill:final", period, row.get("kind")), necessary=True)
                    if isinstance(paid, dict) and paid.get("ok"):
                        row["final_fee"] = True
                except Exception as exc:
                    isolated("bill.final_fee", exc)
            row["frozen"] = True
        if pending:
            freeze_assets(plugin, period, rows, now)
        save_due(plugin, period, rows)
        try:
            finance.nudge_mood({"委屈": 50, "郁闷": 40, "开心": -40, "焦虑": 30}, idem("bill:final", period))
            finance.small_diary("银行把我的账户冻结了，定期和股票都动不了……")
        except Exception as exc:
            isolated("bill_flow.overdue_tick.2", exc)
    save_due(plugin, period, rows)
    return {"ok": True, "stage": stage}


def prompt_line(plugin, now=None):
    now = now or _dt.datetime.now()
    period = now.strftime("%Y-%m")
    note = deferral_note(plugin, now)
    rows = load_due(plugin, period)
    pending = unpaid(rows)
    if not pending:
        return note
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
    return line + note


def survival_line(plugin):
    finance = _finance(plugin)
    try:
        balance = int(finance.balance_cents() or 0)
    except Exception:
        return ""
    if balance < 50000:
        return "你刚交完大额账单，现在只剩 %s 元，接下来几天除了吃饭不能买非必需品。" % fmt_amount(balance)
    return ""
