# -*- coding: utf-8 -*-
"""工资：基本月薪 / 本月奖金扣款 / 实发工资 / 调薪。

规则（2026-11-13 用户明确）：

- 基本月薪持久化在 meta（base_salary_cents），重启不重置，默认 6800 元；
- 迟到/早退 −50~100；病假/事假按日薪（基本工资 ÷ 21.75），半天一半；
- 绩效不达标 −5%~20%；全勤/超额完成 +100~500；
- 非定期调薪：升职 +10%~30%，经济不好 −5%~10%；另有 1/7 月定期调薪；
- 实发 = 基本 + 奖金 − 扣款；15 日到账；实发 ≤ 0 当月不发，记「白干一个月」；
- 涨薪/奖金 → 狂喜 + 长篇日记 + 永久记忆；降薪/扣钱 → 委屈 + 长篇日记 + 永久记忆 + 省钱模式。
"""
from __future__ import annotations

import datetime as _dt
import hashlib

try:
    from ..guard import isolated
    from ..utils.helpers import parse_day
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated
    from utils.helpers import parse_day

DEFAULT_BASE_CENTS = 680000          # 6800 元
BASE_MIN_CENTS, BASE_MAX_CENTS = 300000, 2000000   # 3000–20000 元
WORK_DAYS = 21.75                    # 月计薪天数

#: 非定期调薪幅度
RAISE_UP = (0.10, 0.30)
RAISE_DOWN = (0.05, 0.10)

#: 省钱模式持续天数
THRIFTY_DAYS = 5


def _hash_int(text, low, high):
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    span = max(1, high - low)
    return low + int(digest[:8], 16) % span


def yuan(cents):
    return "¥%.2f" % (int(cents) / 100.0)


class FinanceSalary:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    # ---------- 基本月薪（持久化） ----------
    def base_cents(self):
        """读 base_salary_cents；老数据回退到 salary_cents；都没有就 6800。"""
        for key in ("base_salary_cents", "salary_cents"):
            raw = self.repo.meta_get(key)
            if raw:
                try:
                    return max(BASE_MIN_CENTS, min(BASE_MAX_CENTS, int(raw)))
                except Exception:
                    continue
        return DEFAULT_BASE_CENTS

    def set_base(self, cents, reason=""):
        value = max(BASE_MIN_CENTS, min(BASE_MAX_CENTS, int(cents)))
        self.repo.meta_set("base_salary_cents", value)
        self.repo.meta_set("salary_cents", value)   # 兼容老读法
        return value

    def day_rate_cents(self):
        return int(round(self.base_cents() / WORK_DAYS))

    # ---------- 本月明细 ----------
    def items(self, period):
        try:
            return self.repo.salary_items(period)
        except Exception as exc:
            isolated("salary.items", exc)
            return []

    def add_item(self, period, kind, label, amount_cents):
        amount = int(amount_cents)
        if str(kind) == "deduction":
            amount = -abs(amount)      # 扣款一律记成负数，调用方传正负都对
        elif str(kind) == "bonus":
            amount = abs(amount)
        return self.repo.add_salary_item(period, kind, label, amount)

    def summary(self, period):
        base = self.base_cents()
        rows = self.items(period)
        bonus = sum(int(r.get("amount_cents") or 0) for r in rows if int(r.get("amount_cents") or 0) > 0)
        deduction = -sum(int(r.get("amount_cents") or 0) for r in rows if int(r.get("amount_cents") or 0) < 0)
        return {
            "period": str(period),
            "base_cents": base,
            "bonus_cents": bonus,
            "deduction_cents": deduction,
            "net_cents": base + bonus - deduction,
            "items": [{"kind": r.get("kind"), "label": r.get("label"),
                       "amount_cents": int(r.get("amount_cents") or 0)} for r in rows],
        }

    # ---------- 每月可能发生的事（幂等：每期只掷一次） ----------
    async def roll_month(self, now, period):
        flag = "salary_rolled:" + str(period)
        if self.repo.meta_get(flag):
            return {"skipped": True}
        base = self.base_cents()
        made = []
        roll = _hash_int("late:" + period, 0, 100)
        late = roll < 25
        if late:
            amount = -_hash_int("late-amt:" + period, 5000, 10000)   # 50–100 元
            self.add_item(period, "deduction", "迟到/早退", amount)
            made.append(("迟到/早退", amount))
        sick = _hash_int("leave:" + period, 0, 100) < 20
        if sick:
            half = _hash_int("leave-half:" + period, 0, 2) == 0
            rate = int(round(base / WORK_DAYS))
            amount = -int(round(rate * (0.5 if half else 1.0)))
            label = "病假半天" if half else "事假一天"
            self.add_item(period, "deduction", label, amount)
            made.append((label, amount))
        perf = _hash_int("perf:" + period, 0, 100) < 15
        if perf:
            pct = _hash_int("perf-pct:" + period, 5, 20) / 100.0
            amount = -int(round(base * pct))
            self.add_item(period, "deduction", "绩效不达标(%d%%)" % int(pct * 100), amount)
            made.append(("绩效不达标", amount))
        if not late and not sick and _hash_int("full:" + period, 0, 100) < 30:
            amount = _hash_int("full-amt:" + period, 10000, 50000)   # 100–500 元
            self.add_item(period, "bonus", "全勤奖", amount)
            made.append(("全勤奖", amount))
        self.repo.meta_set(flag, "1")
        return {"rolled": made}

    # ---------- 调薪 ----------
    async def log_adjust(self, old_cents, new_cents, pct, now=None, reason=""):
        """调薪落记忆/长日记/心情（定期和非定期调薪共用）。"""
        now = now or _dt.date.today()
        up = int(new_cents) > int(old_cents)
        word = "狂喜、超级开心" if up else "委屈、难过、想哭"
        text = ("今天公司%s，月薪从 %s 变成 %s（%+.0f%%），%s。"
                % ("涨薪了" if up else "降薪了", yuan(old_cents), yuan(new_cents),
                   float(pct) * 100, word))
        await self._remember(text, "salary:adjust:%s:%s" % (now.isoformat(), int(new_cents)))
        self.finance.long_diary(text)
        self.finance.nudge_mood({"开心": 12, "兴奋": 8} if up else {"委屈": 12, "难过": 8},
                                "salary:adjust:%s" % now.isoformat())
        if not up:
            self.start_thrifty(now)
        return {"up": up, "old": old_cents, "new": new_cents}

    async def raise_or_cut(self, pct, now=None, reason=""):
        now = now or _dt.date.today()
        old = self.base_cents()
        new = max(BASE_MIN_CENTS, min(BASE_MAX_CENTS, int(round(old * (1 + float(pct))))))
        if new == old:
            return {"ok": False, "reason": "unchanged"}
        self.set_base(new)
        self.repo.meta_set("salary_adjust:%s" % now.strftime("%Y-%m"), reason or "manual")
        await self.log_adjust(old, new, pct, now, reason)
        return {"ok": True, "old": old, "new": new, "pct": float(pct), "up": new > old}

    async def maybe_adjust(self, now):
        """非定期调薪：每月最多掷一次，升职 12% / 降薪 6%。"""
        month = now.strftime("%Y-%m")
        flag = "salary_adjust_rolled:" + month
        if self.repo.meta_get(flag):
            return None
        self.repo.meta_set(flag, "1")
        roll = _hash_int("adjust:" + month, 0, 100)
        if roll < 12:
            pct = _hash_int("up:" + month, int(RAISE_UP[0] * 100), int(RAISE_UP[1] * 100) + 1) / 100.0
            return await self.raise_or_cut(pct, now, reason="promotion")
        if roll < 18:
            pct = -_hash_int("down:" + month, int(RAISE_DOWN[0] * 100), int(RAISE_DOWN[1] * 100) + 1) / 100.0
            return await self.raise_or_cut(pct, now, reason="downturn")
        return None

    # ---------- 请假（事假/病假）全链路 ----------
    @staticmethod
    def _leave_label(kind, days):
        half = float(days or 1.0) < 1.0
        prefix = "病假" if str(kind) in ("病假", "sick") else "事假"
        return prefix + ("半天" if half else "一天")

    async def apply_leave(self, now=None, kind="事假", days=1.0, reason="", idem="", day=None):
        """请假：扣款明细 → amo 日程 → 长期记忆 → 小日记 → 情绪 → 卡片。

        day 支持 "2026-11-14" / "明天" / "tomorrow"，不传就是今天。
        """
        target = str(day or "").strip().lower()
        if target in ("明天", "tomorrow", "next"):
            now = (now or _dt.date.today()) + _dt.timedelta(days=1)
        elif target in ("后天",):
            now = (now or _dt.date.today()) + _dt.timedelta(days=2)
        elif target:
            now = parse_day(target) or now or _dt.date.today()
        else:
            now = now or _dt.date.today()
        period = now.strftime("%Y-%m")
        rate = int(round(self.base_cents() / WORK_DAYS))
        amount = -int(round(rate * float(days or 1.0)))
        label = self._leave_label(kind, days)
        self.add_item(period, "deduction", label, amount)
        day = now.isoformat()
        key = str(idem or ("leave:%s:%s" % (day, label)))
        money = "%.2f" % (abs(amount) / 100.0)
        # 1) amo 日程：当天一条整天事项
        try:
            self.finance.add_schedule(day, label + "休息（全天）", detail=label)
        except Exception as exc:
            isolated("salary.leave.schedule", exc)
        # 2) 长期记忆（以后聊到工作和钱能想起来）
        text = "%s请了%s，被扣款%s元。" % (now.strftime("%Y年%m月%d日"), label, money)
        await self._remember(text, key)
        # 3) 小日记（只写感受）
        self.finance.small_diary("今天请假了，虽然休息了一天，但是想到被扣了%s元，好心痛，呜呜。" % money)
        # 4) 情绪：本来余额就紧张的话直接崩溃档
        low = False
        try:
            low = bool(self.plugin.finance_help.status().get("low"))
        except Exception:
            low = False
        if low:
            self.finance.nudge_mood({"委屈": 12, "郁闷": 8, "焦虑": 6}, key)
            try:
                await self.finance.low_balance(0, key + ":low")
            except Exception as exc:
                isolated("salary.leave.help", exc)
        else:
            self.finance.nudge_mood({"委屈": 8, "郁闷": 5}, key)
        summary = self.summary(period)
        card = "%s · 扣款\n-¥%s\n本月实发还剩 ¥%.2f" % (
            label, money, summary["net_cents"] / 100.0)
        return {"ok": True, "label": label, "amount_cents": amount,
                "net_cents": summary["net_cents"], "low": low, "day": day,
                "card": card, "memory": text}

    # ---------- 省钱模式 ----------
    def start_thrifty(self, now=None):
        now = now or _dt.date.today()
        until = (now + _dt.timedelta(days=THRIFTY_DAYS)).isoformat()
        self.repo.meta_set("thrifty_until", until)
        return until

    def thrifty_until(self):
        return str(self.repo.meta_get("thrifty_until") or "")

    def thrifty_active(self, day=None):
        day = day or _dt.date.today()
        until = self.thrifty_until()
        return bool(until) and day.isoformat() <= until

    # ---------- 结算 ----------
    async def settle(self, now, period):
        # 全勤奖：当月没有任何迟到/早退/事假/病假，发薪日补一笔
        engine = getattr(self.plugin, "work_events", None)
        if engine is not None:
            try:
                await engine.grant_full_attendance(period, day=now)
            except Exception as exc:
                isolated("salary.full_attendance", exc)
        summary = self.summary(period)
        net = int(summary["net_cents"])
        if net <= 0:
            text = "这个月白干了，基本工资 %s，扣完还倒欠，实发 %s，只能等下个月。" % (
                yuan(summary["base_cents"]), yuan(net))
            await self._remember(text, "salary:zero:%s" % period)
            self.finance.long_diary(text)
            self.repo.mark_settled(period, "salary", str({"net": net, "zero": True}))
            return {"ok": True, "paid": 0, "zero": True, "summary": summary}
        result = await self.finance.income(net, "工资", "salary:" + period, salary=True)
        if not result.get("ok"):
            return {"ok": False, "reason": "wallet", "summary": summary}
        detail = self._detail_text(summary)
        word = "狂喜、超级开心" if summary["bonus_cents"] > 0 else (
            "委屈、难过、想哭" if summary["deduction_cents"] > 0 else "平静")
        text = "今天发工资了！%s实发 %s。%s" % (detail, yuan(net), word)
        await self._remember(text, "salary:paid:%s" % period)
        self.finance.long_diary(text)
        if summary["deduction_cents"] > 0:
            self.finance.nudge_mood({"委屈": 10, "难过": 6}, "salary:cut:%s" % period)
            self.start_thrifty(now)
        elif summary["bonus_cents"] > 0:
            self.finance.nudge_mood({"开心": 10, "兴奋": 6}, "salary:bonus:%s" % period)
        self.repo.mark_settled(period, "salary", str({"net": net}))
        return {"ok": True, "paid": net, "zero": False, "summary": summary}

    @staticmethod
    def _detail_text(summary):
        parts = ["基本 %s" % yuan(summary["base_cents"])]
        if summary["bonus_cents"]:
            parts.append("奖金 %s" % yuan(summary["bonus_cents"]))
        if summary["deduction_cents"]:
            parts.append("扣款 %s" % yuan(summary["deduction_cents"]))
        return "，".join(parts) + "，"

    async def _remember(self, text, idem):
        try:
            await self.finance.permanent_memory(str(text), str(idem))
            return True
        except Exception as exc:
            isolated("salary.memory", exc)
            return False
