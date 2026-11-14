# -*- coding: utf-8 -*-
"""银行储蓄：活期/定期、计息、提前支取惩罚、应急取现、闲钱自动存定期。"""
from __future__ import annotations
import asyncio, calendar, datetime as _dt, json, time

try:
    from ..utils.helpers import stable_int, yuan
except ImportError:
    from utils.helpers import stable_int, yuan

CURRENT_RATE = 0.002
FIXED_RATES = {3: 0.013, 6: 0.015, 12: 0.017, 36: 0.023}
QUARTER_MONTHS = (3, 6, 9, 12)


class BankEngine:
    def __init__(self, plugin, repo, finance):
        self.plugin, self.repo, self.finance = plugin, repo, finance

    def _data(self):
        try:
            return json.loads(self.repo.meta_get("bank") or "{}") or {}
        except Exception:
            return {}

    def _save(self, data):
        self.repo.meta_set("bank", json.dumps(data, ensure_ascii=False))

    def current_cents(self):
        return int(self._data().get("current") or 0)

    def fixed_rows(self):
        return list(self._data().get("fixed") or [])

    def active_fixed_rows(self):
        """未结束的定期（status 缺省按 active，兼容老数据）。"""
        return [r for r in self.fixed_rows() if str(r.get("status") or "active") != "completed"]

    def fixed_cents(self):
        return sum(int(r.get("amount") or 0) for r in self.active_fixed_rows())

    def total_cents(self):
        return self.current_cents() + self.fixed_cents()

    async def deposit(self, amount_cents, term_months=0, now=None):
        amount = int(amount_cents or 0)
        if amount <= 0:
            return {"ok": False, "reason": "zero"}
        now = now or _dt.datetime.now()
        res = await self.finance.expense(
            amount, "存款：%s" % ("活期" if not term_months else "定期%d个月" % int(term_months)),
            "bank:dep:%s:%s:%s" % (now.date().isoformat(), amount, int(term_months or 0)),
            necessary=True)
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "reason": "insufficient"}
        data = self._data()
        if not term_months:
            data["current"] = self.current_cents() + amount
        else:
            months = int(term_months) if int(term_months) in FIXED_RATES else 12
            now = now or _dt.datetime.now()
            start = now.date()
            end = _add_months(start, months)
            data.setdefault("fixed", []).append({
                "amount": amount, "rate": FIXED_RATES[months],
                "start": start.isoformat(), "end": end.isoformat(),
                "mature_at": _midnight_ts(end),
                "months": months, "status": "active",
                "interest_cents": 0, "matured_at": ""})
        self._save(data)
        self.finance.nudge_mood({"开心": 6}, "bank:dep:mood")
        if not term_months:
            lines = ["留了 %s 在活期当备用金，以备不时之需。" % yuan(amount),
                     "把 %s 放进活期，随时能取，心里踏实。" % yuan(amount)]
        else:
            lines = ["今天把 %s 的闲钱存了%d个月定期，希望能多点利息，开心。" % (yuan(amount), int(term_months)),
                     "存了 %s 的%d个月定期，利息比活期高，想想就高兴。" % (yuan(amount), int(term_months))]
        self.finance.small_diary(lines[stable_int("bank-dep:" + str(amount), 0, len(lines))])
        return {"ok": True, "amount": amount, "term": int(term_months or 0)}

    async def withdraw(self, amount_cents, term_months=None, now=None):
        amount = int(amount_cents or 0)
        now = now or _dt.datetime.now()
        data = self._data()
        if term_months is None:
            if self.current_cents() < amount:
                return {"ok": False, "reason": "insufficient"}
            data["current"] = self.current_cents() - amount
            penalty = False
        else:
            rows = self.fixed_rows()
            active = [r for r in rows if str(r.get("status") or "active") != "completed"]
            if not active:
                return {"ok": False, "reason": "no_fixed"}
            row = active[0]
            amount = min(amount, int(row["amount"]))
            early = now.date().isoformat() < str(row.get("end") or "")
            if early:
                days = max(1, (now.date() - _dt.date.fromisoformat(row["start"])).days)
                interest = int(row["amount"] * CURRENT_RATE * days / 365)
                penalty = True
            else:
                months = int(row.get("months") or 12)
                interest = int(row["amount"] * float(row.get("rate") or 0) * months / 12)
                penalty = False
            row["amount"] = int(row["amount"]) - amount
            if row["amount"] <= 0:
                rows.remove(row)
            data["fixed"] = rows
            data["current"] = self.current_cents() + amount + interest
        self._save(data)
        await self.finance.income(
            amount, "取款：%s" % ("活期" if term_months is None else "定期"),
            "bank:wd:%s:%s:%s" % (now.date().isoformat(), amount,
                                  "cur" if term_months is None else int(term_months)))
        if penalty:
            self.finance.nudge_mood({"委屈": 10}, "bank:wd:penalty")
            self.finance.long_diary(stable_int("bank-wd:", 0, 1) and
                "呜呜呜定期还没到期就被我取出来了，利息全没了……要不是真的撑不住，我真不想动这笔钱。" or
                "存钱真的好难。好不容易攒下的定期，还是败给了现实，利息全打水漂了，我是不是特别没用。")
        return {"ok": True, "amount": amount, "penalty": penalty}

    async def interest_tick(self, now=None):
        now = now or _dt.datetime.now()
        out = []
        data = self._data()
        if int(now.month) in QUARTER_MONTHS and int(now.day) == 28:
            key = "bank:interest:%d-%d" % (now.year, now.month)
            if not self.repo.task_done(key):
                interest = (self.current_cents() * 20 * 90) // (10000 * 365)
                data["current"] = self.current_cents() + interest
                self._save(data)
                self.finance.nudge_mood({"开心": 6}, key + ":mood")
                lines = ["今天银行结息 %s，够买杯奶茶了，开心。" % yuan(interest),
                         "结息 %s 到账，虽然不多，但是白捡的呀。" % yuan(interest),
                         "收到 %s 利息，存钱还是有好处的嘛。" % yuan(interest)]
                self.finance.small_diary(lines[stable_int("bank-int:" + key, 0, len(lines))])
                self.repo.mark_task(key, "interest")
                out.append(interest)
        return out

    async def process_matured_fixed_deposits(self, now=None):
        """每日巡检：到期定期「本金+利息」转入活期。

        防重复：asyncio 锁串行 + 每单 status（completed 永不重付）+ 入账与标记同一次
        落盘（单写原子，不存在“付了没标记”的窗口）。
        防漏单：条件用 mature_at <= now，漏跑几天后重启会把积压的全结掉。
        防精度：利息全程整数分（金额×基点×月数 // 10000/12），无 float 乘法。
        防时区：mature_at 是本地时区 epoch 秒，统一和 now.timestamp() 比较。
        """
        now = now or _dt.datetime.now()
        now_ts = float(now.timestamp())
        day = now.date().isoformat()
        lock = self._settle_lock()
        async with lock:
            data = self._data()
            paid = []
            for row in data.get("fixed") or []:
                if str(row.get("status") or "active") != "active":
                    continue
                mature_ts = _row_mature_ts(row)
                if mature_ts is None or mature_ts > now_ts:
                    continue
                amount = int(row.get("amount") or 0)
                interest = _interest_cents(amount, row.get("rate"), row.get("months"))
                row["status"] = "completed"
                row["matured_at"] = now.date().isoformat()
                row["interest_cents"] = interest
                data["current"] = int(data.get("current") or 0) + amount + interest
                paid.append({"amount": amount, "interest": interest,
                             "total": amount + interest,
                             "months": int(row.get("months") or 12),
                             "end": str(row.get("end") or "")})
            if not paid:
                return []
            self._save(data)   # 入账 + completed 一次提交
        for item in paid:
            self.finance.nudge_mood({"开心": 8}, "bank:matured:mood")
            lines = ["一笔 %s 的定期到期啦，利息 %s，已经自动转回活期，心里踏实。"
                     % (yuan(item["amount"]), yuan(item["interest"])),
                     "%s 的定期到期了，收到 %s 利息，虽然不多但也是白捡的。"
                     % (yuan(item["amount"]), yuan(item["interest"])),
                     "定期到期，本金 %s 加利息 %s 都回到活期账户了，存钱真的有回报。"
                     % (yuan(item["amount"]), yuan(item["interest"]))]
            self.finance.small_diary(lines[stable_int("bank-matured:%s:%d" % (day, len(paid)), 0, len(lines))])
        self.repo.meta_set("bank:last_matured",
                           json.dumps({"day": day, "items": paid}, ensure_ascii=False))
        return paid

    async def daily_tick(self, now=None):
        """每天 00:05 之后跑一次：到期定期入账 + 季度活期结息；按日期幂等。"""
        now = now or _dt.datetime.now()
        if now.hour == 0 and now.minute < 5:
            return {"matured": [], "interest": []}
        # 幂等靠每笔定期的 status（completed 不会再付），日期只做审计记录
        matured = await self.process_matured_fixed_deposits(now)
        interest = await self.interest_tick(now)
        if matured or interest:
            self.repo.meta_set("bank:last_run", now.date().isoformat())
        return {"matured": matured, "interest": interest}

    async def auto_deposit(self, now=None):
        """连续 3 天可用余额 > 10000 → 自动存 5000 定期 1 年。"""
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        cash = int(self.finance.balance_cents() or 0)
        streak = int(self.repo.meta_get("bank:rich_streak") or 0)
        streak = streak + 1 if cash > 1000000 else 0
        self.repo.meta_set("bank:rich_streak", streak)
        key = "bank:auto:" + day
        if streak >= 3 and not self.repo.task_done(key):
            self.repo.mark_task(key, "auto")
            return await self.deposit(500000, 12, now)
        return {"ok": False, "reason": "not_yet"}

    async def emergency_liquidate(self, need_cents):
        if int(self.finance.balance_cents() or 0) >= int(need_cents):
            return {"ok": True, "from": "cash"}
        if self.current_cents() > 0:
            await self.withdraw(min(self.current_cents(), int(need_cents)), None)
            return {"ok": True, "from": "current"}
        if self.fixed_cents() > 0:
            await self.withdraw(int(need_cents), 0)
            return {"ok": True, "from": "fixed", "penalty": True}
        return {"ok": False}


    def prompt_line(self):
        fixed = self.active_fixed_rows()
        extra = ""
        if fixed:
            extra = "定期 %s（%s 到期）" % (yuan(self.fixed_cents()), fixed[0].get("end") or "—")
        return "【银行存款】活期 %s；%s" % (yuan(self.current_cents()), extra or "无定期")

    def _settle_lock(self):
        """同一引擎只建一把锁；串行化到期结算，防并发双付。"""
        lock = getattr(self, "_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._lock = lock
        return lock

    def last_matured(self):
        """最近一次到期入账的摘要（给面板提示用）。"""
        try:
            raw = self.repo.meta_get("bank:last_matured")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def assets_breakdown(self):
        return {"current_cents": self.current_cents(), "fixed_cents": self.fixed_cents(),
                "total_cents": self.total_cents()}


def build_bank(plugin):
    return BankEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None))

def _add_months(day, months):
    """本地日期加自然月（11-16 + 12 月 → 次年 11-16，月末自动收敛）。"""
    months = int(months)
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    day_num = min(day.day, calendar.monthrange(year, month)[1])
    return _dt.date(year, month, day_num)


def _midnight_ts(day):
    """本地时区当天 00:00 的 epoch 秒（全库统一本地时间）。"""
    return int(time.mktime(_dt.datetime(day.year, day.month, day.day, 0, 0, 0).timetuple()))


def _row_mature_ts(row):
    """到期时间戳：优先 mature_at（epoch 秒），老数据回退到 end 日期本地零点。"""
    ts = row.get("mature_at")
    if ts is not None:
        try:
            return int(ts)
        except Exception:
            pass
    end = str(row.get("end") or "")
    if end:
        try:
            return _midnight_ts(_dt.date.fromisoformat(end))
        except Exception:
            return None
    return None


def _interest_cents(amount_cents, rate, months):
    """整数分计息：先把年利率换成基点，全程不出现 float 乘法。"""
    try:
        basis_points = int(round(float(rate or 0) * 10000))
        months = max(1, int(months or 12))
        return (int(amount_cents) * basis_points * months) // (10000 * 12)
    except Exception:
        return 0
