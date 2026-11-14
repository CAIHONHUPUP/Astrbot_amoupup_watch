# -*- coding: utf-8 -*-
"""职场事件引擎：迟到、病假、全勤、项目奖金、周末加班。

每次事件必须走完整链路（少一步都算失败）：
  工资明细/余额 → 当日职场状态 → amo 日程 → 长期记忆 → 小日记 → 情绪。

数值规则（2026-11-13 用户明确）：
- 迟到/早退：-50 元
- 病假一天：日薪（月薪 ÷ 21.75）
- 全勤奖：当月无迟到/早退/事假/病假，发薪日 +300 元
- 项目超额完成：+500~1000 元
- 周末加班：双倍日薪（月薪 ÷ 21.75 × 2）
"""
from __future__ import annotations

import datetime as _dt
import json

try:
    from ..guard import isolated
    from ..utils.helpers import is_holiday, is_trading_day, parse_day, stable_int, yuan
except ImportError:  # 直接按文件加载时
    from guard import isolated
    from utils.helpers import is_holiday, is_trading_day, parse_day, stable_int, yuan

KIND_LATE = "late"
KIND_SICK = "sick"
KIND_FULL = "full_attendance"
KIND_PROJECT = "project"
KIND_OVERTIME = "overtime"

#: 事件 → 展示名
LABELS = {
    KIND_LATE: "迟到/早退",
    KIND_SICK: "病假一天",
    KIND_FULL: "全勤奖",
    KIND_PROJECT: "项目超额完成",
    KIND_OVERTIME: "周末加班",
}

#: 这个月出现这些标签就不算全勤
ABSENCE_LABELS = ("迟到", "早退", "事假", "病假")

PROJECT_RANGE = (20000, 50000)    # 200–500 元（2026-11-13 用户明确）
FULL_BONUS_CENTS = 30000          # 300 元


class WorkEventEngine:
    def __init__(self, plugin, repo, finance, salary):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.salary = salary

    # ---------- 基础数值 ----------
    def daily_rate_cents(self):
        if self.salary is None:
            return 0
        return int(round(self.salary.base_cents() / 21.75))

    @staticmethod
    def day_of(day=None):
        if day in (None, ""):
            return _dt.date.today()
        parsed = parse_day(day) if not hasattr(day, "isoformat") else day
        return parsed or _dt.date.today()

    # ---------- 当日职场状态 ----------
    @staticmethod
    def _status_key(day):
        return "work_day:" + str(day)

    def today_events(self, day=None):
        key = self._status_key(self.day_of(day))
        raw = self.repo.meta_get(key)
        if not raw:
            return []
        try:
            rows = json.loads(raw)
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    def _push_status(self, day, item):
        rows = self.today_events(day)
        rows.append(item)
        self.repo.meta_set(self._status_key(day), json.dumps(rows, ensure_ascii=False))
        return rows

    def prompt_line(self, day=None):
        """注入 LLM 的一句话：她今天在职场经历了什么。"""
        rows = self.today_events(day)
        if not rows:
            return ""
        parts = []
        for row in rows:
            amount = int(row.get("amount_cents") or 0)
            sign = "+" if amount >= 0 else "-"
            parts.append("%s %s%s" % (row.get("label"), sign, yuan(abs(amount))))
        mood = next((r.get("mood_word") for r in rows if r.get("mood_word")), "")
        mood = mood or ("开心" if any(int(r.get("amount_cents") or 0) > 0 for r in rows) else "低落")
        return ("【今日职场】" + "；".join(parts) + "。她今天心情%s，"
                "聊天时可以主动吐槽或炫耀，但不要把数字念成流水账。" % mood)

    # ---------- 统一链路 ----------
    async def fire(self, kind, day=None, amount_cents=None, reason="", source="user"):
        kind = str(kind or "").strip()
        if kind not in LABELS:
            return {"ok": False, "reason": "unknown_kind", "kind": kind}
        target = self.day_of(day)
        day_text = target.isoformat()
        period = target.strftime("%Y-%m")
        rate = self.daily_rate_cents()
        plan = self._plan(kind, rate, target, amount_cents)
        label = plan["label"]
        amount = int(plan["amount_cents"])
        item_kind = "bonus" if amount >= 0 else "deduction"
        if self.salary is None:
            return {"ok": False, "reason": "salary_missing"}
        self.salary.add_item(period, item_kind, label, amount)
        key = "work:%s:%s:%s" % (kind, day_text, abs(amount))
        sign_text = ("+" if amount >= 0 else "-") + yuan(abs(amount))
        # 日程：活动名带钱，详情写成一句人话，方便 hourly/每日日记直接引用
        try:
            self.finance.add_schedule(
                day_text, "%s（%s）" % (label, sign_text),
                detail="【职场】%s %s，%s" % (label, sign_text, plan["diary"]),
                category="work")
        except Exception as exc:
            isolated("work.schedule", exc)
        # 长期记忆
        text = "%s %s：%s%s。%s" % (day_text, plan["activity"], label,
                                    ("+" if amount >= 0 else "-") + yuan(abs(amount)),
                                    plan["diary"])
        try:
            await self.finance.permanent_memory(text, key)
        except Exception as exc:
            isolated("work.memory", exc)
        # 小日记：带【职场】前缀，保证每日总结一定能扫到
        try:
            self.finance.small_diary("【职场】" + plan["diary"])
        except Exception as exc:
            isolated("work.diary", exc)
        # 情绪
        try:
            self.finance.nudge_mood(plan["mood"], key)
        except Exception as exc:
            isolated("work.mood", exc)
        # 当日状态（供 UI / prompt 使用）
        self._push_status(day_text, {"kind": kind, "label": label,
                                     "amount_cents": amount, "at": _dt.datetime.now().strftime("%H:%M"),
                                     "mood_word": plan.get("mood_word", ""), "source": source})
        # 奖金可能引发冲动消费
        if kind == KIND_PROJECT and stable_int("impulse:" + key, 0, 100) < 35:
            try:
                self._impulse(day_text, key)
            except Exception as exc:
                isolated("work.impulse", exc)
        return {"ok": True, "kind": kind, "label": label, "amount_cents": amount,
                "day": day_text, "mood": plan["mood"], "diary": plan["diary"],
                "card": "[%s] %s%s" % (plan.get("card_label") or label,
                                       "+" if amount >= 0 else "-", yuan(abs(amount))),
                "net_cents": self.salary.summary(period)["net_cents"]}

    async def _impulse(self, day, key):
        exp = getattr(self.plugin, "finance_expense", None)
        if exp is None:
            return None
        amount = 1500 if stable_int(key + ":pick", 0, 2) == 0 else 5900
        label = "奶茶" if amount == 1500 else "盲盒"
        return await exp.spend("impulse", label, amount, day=day, idem=key + ":impulse")

    def _plan(self, kind, rate, day, amount_cents):
        if kind == KIND_LATE:
            return {"label": LABELS[kind], "card_label": "迟到", "mood_word": "低落",
                    "amount_cents": -5000,
                    "schedule": "上班迟到", "activity": "迟到",
                    "diary": "早上又迟到，被扣了50块，气死我了，打工好难呜呜。",
                    "mood": {"郁闷": 8, "委屈": 5}}
        if kind == KIND_SICK:
            return {"label": LABELS[kind], "mood_word": "低落", "amount_cents": -rate,
                    "schedule": "病假休息", "activity": "病假",
                    "diary": "生病请假还得被扣钱，难受，这破公司。",
                    "mood": {"疲惫": 8, "委屈": 6}}
        if kind == KIND_PROJECT:
            amount = int(amount_cents) if amount_cents else stable_int("project:" + day.isoformat(), *PROJECT_RANGE)
            return {"label": LABELS[kind], "mood_word": "狂喜", "amount_cents": abs(amount),
                    "schedule": "项目进度超额", "activity": "项目奖励",
                    "diary": "项目拿奖金了！多了%s块，今晚去吃顿好的！" % ("%.0f" % (abs(amount) / 100.0)),
                    "mood": {"开心": 12, "兴奋": 8}}
        if kind == KIND_OVERTIME:
            holiday = is_holiday(day)
            amount = int(amount_cents) if amount_cents else rate * (3 if holiday else 2)
            label = "节假日加班" if holiday else ("周末加班" if day.weekday() >= 5 else "加班")
            return {"label": label, "amount_cents": abs(amount),
                    "schedule": "周末加班", "activity": "加班",
                    "diary": "周末还在加班，累死了……不过加班费到手，值了。",
                    "mood": {"疲惫": 5, "开心": 6}}
        # 全勤奖：由结算时判定，这里只负责记账
        amount = int(amount_cents) if amount_cents else FULL_BONUS_CENTS
        return {"label": LABELS[kind], "amount_cents": abs(amount),
                "schedule": "本月全勤", "activity": "全勤",
                "diary": "这个月一天没请假也没迟到，全勤奖到手！",
                "mood": {"开心": 10, "兴奋": 5}}

    # ---------- 每日职场小结（写进小时小日记，供每日总结引用） ----------
    async def daily_recap(self, now=None):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        flag = "work_recap:" + day
        if self.repo.meta_get(flag):
            return None
        rows = self.today_events(day)
        if not rows:
            self.repo.meta_set(flag, "empty")
            return None
        if now.hour < 21:
            return None
        parts = []
        net = 0
        for row in rows:
            amount = int(row.get("amount_cents") or 0)
            net += amount
            parts.append("%s%s" % (row.get("label"), ("+" if amount >= 0 else "-") + yuan(abs(amount))))
        mood = "开心" if net > 0 else "郁闷"
        text = "今天职场：%s，一共%s%s。%s" % (
            "、".join(parts), "+" if net >= 0 else "-", yuan(abs(net)),
            "有点小得意，今晚可以放松一下。" if net > 0 else "唉，钱不好挣，心情有点堵。")
        try:
            self.finance.small_diary("【职场小结】" + text)
        except Exception as exc:
            isolated("work.recap.diary", exc)
        try:
            await self.finance.long_memory(text, "work_recap:" + day)
        except Exception as exc:
            isolated("work.recap.memory", exc)
        self.repo.meta_set(flag, mood)
        return {"ok": True, "text": text, "net": net}

    # ---------- 全勤判定 ----------
    def has_absence(self, period):
        try:
            items = self.salary.items(period) if self.salary else []
        except Exception:
            items = []
        for item in items:
            label = str(item.get("label") or "")
            if any(bad in label for bad in ABSENCE_LABELS):
                return True
        return False

    async def grant_full_attendance(self, period, day=None):
        if self.has_absence(period):
            return None
        if any(str(i.get("label")) == LABELS[KIND_FULL] for i in (self.salary.items(period) if self.salary else [])):
            return None
        return await self.fire(KIND_FULL, day=day, source="auto")

    # ---------- 每日自主概率 ----------
    async def daily_random(self, now=None):
        now = now or _dt.datetime.now()
        day = now.date()
        flag = "work_random:" + day.isoformat()
        if self.repo.meta_get(flag):
            return None
        if not is_trading_day(day):
            self.repo.meta_set(flag, "off")
            return None
        # 只在早上掷（06:30–11:00），像真实的上班路上/工位前
        if now.hour < 6 or now.hour >= 11:
            return None
        self.repo.meta_set(flag, "1")
        roll = stable_int("late:" + day.isoformat(), 0, 100)
        if roll < 10:
            return await self.fire(KIND_LATE, day=day, source="auto")
        if roll < 15:
            return await self.fire(KIND_PROJECT, day=day, source="auto")
        return None
