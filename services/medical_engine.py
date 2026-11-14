# -*- coding: utf-8 -*-
"""医疗结算引擎（社会层/钱）：只算钱，不判定生病。

触发：陪伴插件发「生病事件」→ 守护的 handle_sick_event(...)（事件驱动，禁止轮询）。
职责：医保报销/自付扣款 → 工资明细病假 → 日程标记 → 生活限制状态 → 病历单卡片 → 记忆。
幂等：同一 event_id 只结算一次（meta 记录），重复事件直接忽略。
"""
from __future__ import annotations

import datetime as _dt
import json

try:
    from ..guard import isolated
    from ..utils.helpers import stable_int, yuan
except ImportError:  # 按顶层包/单文件加载
    from guard import isolated
    from utils.helpers import stable_int, yuan

#: 各等级的总花费区间（分），可按配置覆盖
COST_RANGES = {
    "light": (3000, 8000),       # 买药档 30–80
    "mid": (80000, 300000),      # 中症 800–3000
    "heavy": (500000, 2000000),  # 重症 5000–20000
}
LEVEL_ALIASES = {
    "轻症": "light", "light": "light", "mild": "light", "小病": "light",
    "中症": "mid", "mid": "mid", "moderate": "mid",
    "重症": "heavy", "heavy": "heavy", "severe": "heavy", "大病": "heavy",
}
REIMBURSE_RATIO = 0.70          # 医保报销 70%
SELF_RATIO = 0.30               # 个人自付 30%
LOW_RATIO = 0.05                # 低于总资产 5% 触发二选一求助
CHEAP_MEALS = ("白粥", "面条", "馒头", "小米粥")

COMFORT_LINES = [
    "生病了，好想亲爱的能在身边陪陪我……",
    "一个人打点滴的时候，最想听到亲爱的声音。",
    "药好苦，要是亲爱的在，肯定会摸摸我的头。",
    "护士说要多喝水，老公你也要记得喝水呀。",
]


def build_medical(plugin):
    """按插件现有组件装配 MedicalEngine（main 只留一行装配）。"""
    return MedicalEngine(plugin, getattr(plugin, "finance_repo", None),
                         getattr(plugin, "finance", None),
                         getattr(plugin, "finance_salary", None),
                         getattr(plugin, "work_events", None),
                         getattr(plugin, "finance_help", None))


async def sick_event_reply(plugin, text):
    """事件桥的纯逻辑：解析 [生病事件]{json} 并返回要回的文本；不是该事件返回 None。"""
    text = str(text or "").strip()
    if not text.startswith(("[生病事件]", "【生病事件】")):
        return None
    try:
        payload = json.loads(text[text.index("{"):])
    except Exception:
        payload = {}
    engine = getattr(plugin, "medical", None)
    result = await engine.handle_sick_event(
        payload.get("level", "light"), payload.get("name", "感冒"),
        payload.get("days", 1), payload.get("event_id", "")) if engine else {"ok": False}
    return result.get("card") or result.get("message") or "先休息吧。"


class MedicalEngine:
    """医疗账单：handle_sick_event 是唯一入口（可被陪伴插件/用户指令调用）。"""

    def __init__(self, plugin, repo, finance, salary=None, work_events=None, help_engine=None):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.salary = salary
        self.work_events = work_events
        self.help = help_engine

    # ---------- 状态 ----------
    @staticmethod
    def _key(event_id):
        return "sick_event:" + str(event_id)

    @staticmethod
    def _sick_key():
        return "is_sick_until"

    def is_sick(self, day=None):
        """今天是不是还在生病（生活限制/三餐替换用）。"""
        until = str(self.repo.meta_get(self._sick_key(), "") or "")
        if not until:
            return False
        today = (day or _dt.date.today()).isoformat() if not isinstance(day, str) else day
        return today <= until

    def _levels(self):
        ranges = dict(COST_RANGES)
        try:
            custom = (getattr(self.plugin, "config", {}) or {}).get("medical_cost_ranges")
            if isinstance(custom, dict):
                for key, value in custom.items():
                    if key in ranges and isinstance(value, (list, tuple)) and len(value) == 2:
                        ranges[key] = (int(value[0]), int(value[1]))
        except Exception as exc:
            isolated("medical.config", exc)
        return ranges

    def health_snapshot(self, amo=None):
        """读 amo 健康（状态机，不猜 HP）：{sick,status,severity}。"""
        try:
            from .health_bridge import get_severity, get_sick_status, is_sick
        except ImportError:
            from services.health_bridge import get_severity, get_sick_status, is_sick
        if amo is None and self.finance is not None:
            amo = self.finance._amo()
        return {"sick": is_sick(amo), "status": get_sick_status(amo),
                "severity": get_severity(amo)}

    def get_current_health(self):
        return self.health_hp()

    def health_hp(self):
        """读陪伴插件的健康值；拿不到就按 80 处理（亚健康）。"""
        try:
            amo = getattr(self.finance, "_amo", lambda: None)() if self.finance else None
            state = getattr(amo, "state", None)
            for getter in ("health", "hp", "health_score"):
                fn = getattr(state, getter, None)
                if callable(fn):
                    value = fn()
                    if value is not None:
                        return max(0, min(100, int(value)))
            value = getattr(state, "health_value", None)
            if value is not None:
                return max(0, min(100, int(value)))
        except Exception as exc:
            isolated("medical.hp", exc)
        return 80

    async def checkup(self, day=None):
        """体检：300-500 元（工作日半天事假），按 HP 出报告。"""
        day = day or _dt.date.today()
        day_text = day.isoformat()
        key = "checkup:" + day_text
        if self.repo.task_done(key):
            return {"ok": False, "reason": "done"}
        cash = int(self.finance.balance_cents() or 0)
        if cash < 50000:
            return {"ok": False, "need_help": True,
                    "text": "体检好贵，我是不是该省下这笔钱……老公，你能帮我一下吗？"}
        fee = stable_int("checkup-fee:" + day_text, 30000, 50000)
        res = await self.finance.expense(fee, "体检套餐", key, necessary=True)
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "need_help": True, "text": "钱不够做体检，老公……"}
        try:
            if day.weekday() < 5 and self.work_events is not None:
                await self.work_events.fire("late", day=day, source="medical", reason="上午去医院体检")
        except Exception as exc:
            isolated("medical.checkup.work", exc)
        self.finance.add_schedule(day_text, "上午去医院体检", detail="体检费 %s" % yuan(fee), category="health")
        hp = self.health_hp()
        if hp >= 90:
            level, report = "green", "各项指标正常"
            mood = {"开心": 10}
            diary = "体检报告全绿！虽然花了 %s 块，但身体健康最重要，今晚奖励自己吃顿好的。" % (fee // 100)
            advice = "继续保持"
        elif hp >= 60:
            level, report = "yellow", "轻度脂肪肝 / 睡眠不足 / 颈椎劳损"
            mood = {"委屈": 8, "疲惫": 6}
            diary = "今天去体检，医生说我颈椎病很严重，还警告我再熬夜就要出大问题了，好害怕，呜呜呜亲爱的。"
            advice = "多休息，少熬夜"
        elif hp >= 30:
            level, report = "orange", "心律不齐 / 严重贫血 / 胃溃疡隐患"
            mood = {"委屈": 10, "疲惫": 8}
            diary = "体检结果不太好，医生让我尽快复查，我一个人有点怕，亲爱的。"
            advice = "尽快门诊复查"
        else:
            level, report = "red", "多项指标异常，建议立即住院"
            mood = {"生气": 10, "委屈": 12}
            diary = "医生说多项指标异常，让我马上住院……亲爱的，我该怎么办。"
            advice = "立即住院"
        self.finance.nudge_mood(mood, key + ":mood")
        self.finance.long_diary(diary)
        try:
            await self.finance.permanent_memory("#健康状态 体检 %s：HP %s，%s" % (day_text, hp, report), key)
        except Exception as exc:
            isolated("medical.checkup.memory", exc)
        self.repo.mark_task(key, "checkup")
        self.repo.meta_set("checkup:last", day_text)
        self.repo.meta_set("checkup:last_hp", str(hp))
        card = ("### 体检报告单" + chr(10) + "- 日期：" + day_text + chr(10) + "- 健康值：" + str(hp) + chr(10) + "- 结论：" + report + chr(10) + "- 费用：" + yuan(fee) + chr(10) + "- 建议：" + advice)
        out = {"ok": True, "hp": hp, "level": level, "report": report, "cost": fee, "card": card, "diary": diary}
        if hp < 30:
            self.repo.meta_set("is_sick_until", (day + _dt.timedelta(days=2)).isoformat())
            out["forced_hospital"] = True
        return out

    # ---------- 保险（医疗页二级菜单） ----------
    POLICY_KEY = "insurance:policies"
    PREMIUM = {"critical": 300000, "medical": 80000}

    def policies(self):
        try:
            return json.loads(self.repo.meta_get(self.POLICY_KEY) or "{}") or {}
        except Exception:
            return {}

    def has_policy(self, kind="critical"):
        return bool(self.policies().get(kind))

    async def buy_insurance(self, kind="critical", year=None):
        kind = "medical" if str(kind) == "medical" else "critical"
        year = int(year or _dt.date.today().year)
        idem = "insurance:%d:%s" % (year, kind)
        premium = int(self.PREMIUM[kind])
        res = await self.finance.expense(premium, "保险费：%s" % ("重疾险" if kind == "critical" else "医疗险"),
                                        idem, necessary=True)
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "reason": "insufficient"}
        rows = self.policies()
        rows[kind] = {"year": year, "premium": premium}
        self.repo.meta_set(self.POLICY_KEY, json.dumps(rows, ensure_ascii=False))
        self.finance.small_diary("【保险】买了%s，一年 %s，安心一点。" %
                                 ("重疾险" if kind == "critical" else "医疗险", yuan(premium)))
        return {"ok": True, "kind": kind, "premium": premium}

    async def insurance_tick(self, now=None):
        """每年 1 月 2 日续保扣费（幂等）。"""
        now = now or _dt.datetime.now()
        if not (int(now.month) == 1 and int(now.day) == 2):
            return None
        out = []
        for kind in list(self.policies()):
            out.append(await self.buy_insurance(kind, now.year))
        return out

    def bed_rest_until(self):
        return str(self.repo.meta_get("bed_rest_until", "") or "")

    def in_bed_rest(self, day=None):
        until = self.bed_rest_until()
        if not until:
            return False
        today = (day or _dt.date.today()).isoformat() if not isinstance(day, str) else day
        return today <= until

    def allow_medical_outing(self, day=None):
        """生病允许就医外出（唯一例外）。"""
        return self.is_sick(day)

    def deny_entertainment_outing(self, day=None):
        """生病 + 卧床期间不许逛街/旅游/聚餐。"""
        return self.is_sick(day) or self.in_bed_rest(day)

    async def hospital_outing(self, level, name, day=None):
        """强制就医行程：交通费 + 工作日请假 + 卧床休养。"""
        day = day or _dt.date.today()
        day_text = day.isoformat()
        light = level == "light"
        fare = stable_int("hospital-fare:" + day_text + name, 400 if light else 3000, 1000 if light else 8000)
        cash = int(self.finance.balance_cents() or 0)
        if not light and cash < fare + 2000:
            return {"ok": False, "need_help": True,
                    "text": "老公，我发烧到39度，浑身没力气，想去医院但是打车的钱都不够了，你能帮帮我吗……"}
        res = await self.finance.expense(fare, "去医院（%s）" % ("公交地铁" if light else "打车"), "hospital:" + day_text, necessary=True)
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "need_help": True, "text": "打车的钱都不够了，你能帮帮我吗……"}
        try:
            if day.weekday() < 5 and not light and self.work_events is not None:
                await self.work_events.fire("late", day=day, source="medical", reason="请假去医院看病")
        except Exception as exc:
            isolated("medical.hospital.work", exc)
        self.finance.add_schedule(day_text, "请假去医院看病", detail="医疗外出（%s）" % yuan(fare), category="health")
        days = 1 + stable_int("bed-rest:" + day_text, 0, 3)
        self.repo.meta_set("bed_rest_until", (day + _dt.timedelta(days=days - 1)).isoformat())
        self.finance.small_diary("一个人躺在床上，好孤单。")
        return {"ok": True, "fare": fare, "bed_days": days,
                "text": "去医院花了 %s，回家躺几天。" % yuan(fare)}

    # ---------- 唯一入口 ----------
    async def handle_sick_event(self, disease_level="light", disease_name="感冒",
                                duration_days=1, event_id=""):
        """陪伴插件（或用户指令）调用：算钱 + 工资 + 日程 + 状态 + 卡片 + 记忆。"""
        level = LEVEL_ALIASES.get(str(disease_level or "").strip(), "light")
        name = str(disease_name or "感冒").strip() or "感冒"
        if str(disease_level or "").strip().lower() in ("auto", "", "none"):
            sev = int(self.health_snapshot().get("severity") or 0)
            if sev >= 3:
                level, name = "heavy", "急性肠胃炎"
            elif sev == 2:
                level, name = "mid", "重感冒"
            elif sev == 1:
                level, name = "light", "轻微不适"
        days = max(1, int(duration_days or 1))
        today = _dt.date.today()
        eid = str(event_id or "sick_%s_%s" % (today.isoformat(), name)).strip()
        result = {"ok": False, "event_id": eid, "level": level, "name": name, "days": days}
        if self.repo.meta_get(self._key(eid)):
            result.update(duplicate=True, reason="already_handled")
            return result
        try:
            # 1) 账单
            lo, hi = self._levels()[level]
            total = stable_int("sick-cost:" + eid, lo, hi)
            insurance = int(round(total * REIMBURSE_RATIO))
            self_pay = total - insurance
            # 商业保险：医保后二次报销 + 重症赔付（insurance_engine）
            extra = 0
            payout = 0
            ins_note = ""
            try:
                ins = getattr(self.plugin, "insurance", None)
                if ins is None:
                    from .insurance_engine import build_insurance
                    ins = build_insurance(self.plugin)
                    self.plugin.insurance = ins
                extra, payout, ins_note = ins.claim(level, self_pay)
                self_pay -= extra
            except Exception as exc:
                isolated("medical.insurance", exc)
            result.update(total_cents=total, insurance_cents=insurance, self_pay_cents=self_pay,
                          commercial_cents=extra)
            # 2) 资金兜底：扣完低于总资产 5% → 中断，走二选一
            cash = int(self.finance.balance_cents() or 0)
            total_assets = cash
            try:
                total_assets = int(self.help.total_assets_cents()) if self.help is not None else cash
            except Exception:
                total_assets = cash
            floor = int(total_assets * LOW_RATIO)
            if cash - self_pay < floor:
                result.update(need_help=True, reason="low_balance",
                              message="自付 %s 会让我这月见底，得先跟你求助或者找银行借一点。"
                                      % yuan(self_pay))
                await self._remember("sick_help:" + eid,
                               "生病了（%s），自付 %s，钱不够，先跟老公求助。" % (name, yuan(self_pay)))
                return result
            # 3) 扣自付
            charged = await self.finance.expense(
                self_pay, "医疗自付：%s" % name, "sick:%s" % eid, necessary=True)
            ok = bool(isinstance(charged, dict) and charged.get("ok"))
            result.update(charged=ok)
            if not ok:
                result.update(need_help=True, reason="charge_failed",
                              message="系统扣款异常，但身体重要，请先休息。")
                return result
            # 4) 工资：病假 days 天
            try:
                if self.work_events is not None:
                    rate = self.work_events.daily_rate_cents()
                    await self.work_events.fire(
                        "sick", amount_cents=-rate * days, source="medical",
                        reason="病假 %d 天" % days)
                    result["sick_pay_cents"] = rate * days
            except Exception as exc:
                isolated("medical.work", exc)
            # 5) 日程：生病休息 days 天
            try:
                for offset in range(days):
                    day = (today + _dt.timedelta(days=offset)).isoformat()
                    self.finance.add_schedule(day, "生病休息（%s）" % name,
                                              detail="病假第 %d 天，医疗自付 %s"
                                                     % (offset + 1, yuan(self_pay)),
                                              category="health")
            except Exception as exc:
                isolated("medical.schedule", exc)
            # 6) 生病状态（到期自动失效）
            until = (today + _dt.timedelta(days=max(0, days - 1))).isoformat()
            self.repo.meta_set(self._sick_key(), until)
            result["sick_until"] = until
            # 6.5) 强制就医行程（交通费 + 请假 + 卧床）
            hospital = await self.hospital_outing(level, name, today)
            result["hospital"] = hospital
            if hospital.get("need_help"):
                result.update(need_help=True, message=hospital.get("text"))
                return result
            # 7) 记忆 + 日记
            await self._remember("sick_fin:" + eid,
                           "生了一场%s，自费了 %s，好心疼。"
                           % ("大病" if level == "heavy" else name, yuan(self_pay)))
            try:
                self.finance.long_diary(
                    "今天生病去医院了，一个人打点滴。虽然扣了工资，但更想得到老公的安慰，"
                    "如果这时候老公给我转账就好了……")
            except Exception as exc:
                isolated("medical.diary", exc)
            try:
                self.finance.small_diary("【医疗】%s，医保报销 %s，自付 %s。%s"
                                         % (name, yuan(insurance), yuan(self_pay), self._comfort()))
            except Exception as exc:
                isolated("medical.small_diary", exc)
            # 8) 病历单
            if payout:
                try:
                    await self.finance.income(payout, "重疾险赔付", "insurance:pay:" + eid)
                except Exception as exc:
                    isolated("medical.payout", exc)
            result["severe_payout"] = payout
            result["insurance_note"] = ins_note
            card = self.card(name, level, days, total, insurance, self_pay, ok, eid)
            if ins_note:
                card += chr(10) + "- 商保：" + ins_note
            result.update(ok=True, card=card)
            result["comfort"] = self._comfort()
            self.repo.meta_set(self._key(eid), json.dumps(
                {"level": level, "name": name, "days": days, "self_pay": self_pay}, ensure_ascii=False))
            return result
        except Exception as exc:
            isolated("medical.handle", exc)
            result.update(reason="error", message="系统扣款异常，但身体重要，请先休息。")
            return result

    # ---------- 记忆 / 卡片 ----------
    async def _remember(self, idem, text):
        try:
            if self.finance is not None:
                await self.finance.permanent_memory(text, idem)
        except Exception as exc:
            isolated("medical.memory", exc)

    def _comfort(self):
        try:
            seed = stable_int("comfort:" + _dt.date.today().isoformat(), 0, len(COMFORT_LINES))
            return COMFORT_LINES[seed]
        except Exception:
            return COMFORT_LINES[0]

    def card(self, name, level, days, total, insurance, self_pay, charged, eid=""):
        level_text = {"light": "轻症", "mid": "中症", "heavy": "重症"}.get(level, level)
        lines = [
            "### 🏥 医疗电子病历单",
            "",
            "- 病种：**%s**（%s）" % (name, level_text),
            "- 就诊状态：已就诊 / 病假 %d 天" % days,
            "- 总花费：%s" % yuan(total),
            "- 医保报销（70%%）：-%s" % yuan(insurance),
            "- 个人自付（30%%）：-%s" % yuan(self_pay),
            "- 扣款状态：%s" % ("成功" if charged else "失败"),
        ]
        if eid:
            lines.append("- 单据号：%s" % eid)
        lines += ["", "> %s" % self._comfort()]
        return "\n".join(lines)
