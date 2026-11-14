# -*- coding: utf-8 -*-
"""社会关系引擎：NPC 档案 + 好感度 + 事件驱动（钱/情绪/记忆/日程）。"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
    from ..utils.helpers import stable_int, yuan
except ImportError:
    from guard import isolated
    from utils.helpers import stable_int, yuan

SEED = [
    ("柳如烟", "friend", "闺蜜", 88), ("白凝冰", "friend", "死党", 80),
    ("季博达", "work", "直属领导", 45), ("方圆", "work", "普通同事", 55),
    ("妈妈", "family", "妈妈", 95), ("爸爸", "family", "爸爸", 92),
    ("沈幼微", "edge", "远房表姐", 40),
]
GIFT_RANGE = {"work": (20000, 30000), "friend": (50000, 100000), "family": (50000, 200000)}
RED_RANGE = (50000, 200000)
DINNER_RANGE = (10000, 30000)
BORROW_RANGE = (50000, 100000)


class SocialEngine:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    # ---------- NPC ----------
    def ensure_seed(self):
        for name, circle, relation, affinity in SEED:
            if self.repo.npc_get(name) is None:
                self.repo.upsert_npc(name, circle, relation, affinity, "#社交_" + name)

    def by_circle(self, circle):
        return [n for n in self.repo.npcs() if n.get("circle") == circle]

    def adjust(self, name, delta):
        row = self.repo.npc_get(name) or {}
        self.repo.set_affinity(name, int(row.get("affinity") or 50) + int(delta))

    def relation_line(self):
        parts = []
        for row in self.repo.npcs()[:6]:
            owed = int(row.get("borrowed_from_her") or 0)
            parts.append("%s（%s，好感 %s%s）" % (row["name"], row["relation"], row["affinity"],
                                                "，欠我" + yuan(owed) if owed else ""))
        return "；".join(parts)

    # ---------- 事件 ----------
    async def gift_money(self, name=None, amount_cents=None):
        self.ensure_seed()
        npc = self.repo.npc_get(name) if name else (self.by_circle("work") or [{}])[0]
        if not npc:
            return {"ok": False, "reason": "no_npc"}
        lo, hi = GIFT_RANGE.get(npc["circle"], GIFT_RANGE["work"])
        amount = int(amount_cents or stable_int("gift:" + npc["name"] + _dt.date.today().isoformat(), lo, hi))
        if not self._afford(amount, "social.gift"):
            return self._escape("随礼")
        res = await self.finance.expense(amount, "随礼：%s结婚" % npc["name"], "social:gift:" + npc["name"] + _dt.date.today().isoformat())
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        self.adjust(npc["name"], 6)
        self.repo.set_npc_event(npc["name"], "结婚，我随了 " + yuan(amount))
        self.finance.nudge_mood({"委屈": 6}, "social:gift:" + npc["name"])
        self.finance.small_diary("今天给%s随礼，花了%s，有点肉疼。" % (npc["name"], yuan(amount)))
        self._schedule("参加%s的婚礼" % npc["name"])
        return {"ok": True, "npc": npc["name"], "amount_cents": amount,
                "text": "%s 结婚，我随了 %s。肉疼……" % (npc["name"], yuan(amount))}

    async def red_packet(self):
        self.ensure_seed()
        amount = int(stable_int("red:" + _dt.date.today().isoformat(), *RED_RANGE))
        res = await self.finance.income(amount, "妈妈发红包", "social:red:" + _dt.date.today().isoformat())
        if not (isinstance(res, dict) and res.get("ok", True)):
            return {"ok": False}
        self.adjust("妈妈", 3)
        self.finance.nudge_mood({"开心": 10, "依恋": 6}, "social:red:mood")
        self.finance.small_diary("爸爸妈妈真好，给我转了 %s，亲爱的你也要对我好一点。" % yuan(amount))
        return {"ok": True, "amount_cents": amount, "text": "妈妈给我转了 %s，感动。" % yuan(amount)}

    async def dinner(self, name=None):
        self.ensure_seed()
        npc = self.repo.npc_get(name) if name else (self.by_circle("friend") or [{}])[0]
        if not npc or int(npc.get("affinity") or 0) < 20:
            return {"ok": False, "reason": "surface_friend"}
        cash = int(self.finance.balance_cents() or 0)
        if cash < 50000:
            self.finance.nudge_mood({"委屈": 8}, "social:escape:dinner")
            self._remember("social:escape:" + _dt.date.today().isoformat(),
                           "没钱，借口加班没去聚餐。")
            return {"ok": False, "reason": "escape", "text": "借口加班，其实是没钱……好难受。"}
        amount = int(stable_int("dinner:" + _dt.date.today().isoformat(), *DINNER_RANGE))
        res = await self.finance.expense(amount, "和%s吃饭" % npc["name"], "social:dinner:" + _dt.date.today().isoformat())
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        self.adjust(npc["name"], 5)
        self.finance.small_diary("今天跟%s吃了顿火锅，花了%s，还算开心。" % (npc["name"], yuan(amount)))
        return {"ok": True, "amount_cents": amount, "text": "%s 约饭，花了 %s。" % (npc["name"], yuan(amount))}

    async def borrow_request(self, name="柳如烟", amount_cents=None, days=30):
        self.ensure_seed()
        npc = self.repo.npc_get(name)
        if not npc or int(npc.get("affinity") or 0) < 20:
            return {"ok": False, "reason": "refused"}
        amount = int(amount_cents or stable_int("borrow:" + name, *BORROW_RANGE))
        res = await self.finance.expense(amount, "借给%s" % name, "social:borrow:" + name + _dt.date.today().isoformat())
        if not (isinstance(res, dict) and res.get("ok")):
            return {"ok": False, "reason": "insufficient"}
        due = (_dt.date.today() + _dt.timedelta(days=days)).isoformat()
        self.repo.add_receivable(name, amount, due)
        self.adjust(name, 8)
        self.repo.set_npc_event(name, "借了我 " + yuan(amount))
        self.finance.small_diary("借给%s %s，希望能还。" % (name, yuan(amount)))
        return {"ok": True, "npc": name, "amount_cents": amount, "due": due}

    async def repay_tick(self, today=None):
        today = (today or _dt.date.today()).isoformat()
        for row in self.repo.receivables():
            if row.get("due_day") and str(row["due_day"]) > today:
                continue
            key = "social:repay:%s:%s" % (row["id"], today)
            if self.repo.task_done(key):
                continue
            npc, amount = row["npc"], int(row["amount_cents"])
            if stable_int(key, 0, 100) < 80:
                await self.finance.income(amount, "%s还钱" % npc, key)
                self.repo.settle_receivable(row["id"], "paid")
                self.finance.small_diary("【社交】%s 把钱还了，还算讲信用。" % npc)
            else:
                self.repo.settle_receivable(row["id"], "defaulted")
                self.finance.nudge_mood({"生气": 12, "委屈": 10}, key + ":mood")
                self.finance.small_diary("【社交】%s 借钱不还还拉黑我，气哭了。" % npc)
                await self.finance.permanent_memory("被%s骗了 %s。" % (npc, yuan(amount)), key)
            self.repo.mark_task(key, "repay")

    async def criticism(self):
        npc = (self.by_circle("work") or [{}])[0]
        self.finance.nudge_mood({"委屈": 10}, "social:criticism:" + _dt.date.today().isoformat())
        self.finance.small_diary("今天被%s批评了，委屈。" % (npc.get("name") or "领导"))
        return {"ok": True, "text": "被%s批评了，委屈……" % (npc.get("name") or "领导")}

    # ---------- 定时：3-7 天随机 + 节日红包 + 应收结算 ----------
    async def scheduled(self, now=None):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        out = {"ok": True, "events": []}
        try:
            from ..utils.helpers import is_holiday
        except ImportError:
            from utils.helpers import is_holiday
        # 节日 / 每月 1 号：爸妈红包
        red_key = "social:red:" + day
        if (is_holiday(now.date()) or int(now.day) == 1) and not self.repo.task_done(red_key):
            out["events"].append(await self.red_packet())
            self.repo.mark_task(red_key, "red_packet")
        # 平时：严格 3-7 天一次（从上一次社交日算起）
        last = str(self.repo.meta_get("social:last_day") or "")
        gap = 3 + stable_int("social-gap:" + day, 0, 5)
        ready = True
        if last:
            try:
                ready = (now.date() - _dt.date.fromisoformat(last)).days >= gap
            except Exception:
                ready = True
        roll_key = "social:roll:" + day
        if ready and not self.repo.task_done(roll_key):
            try:
                mood_word = str(self.finance.mood().get("word") or "")
                cash = int(self.finance.balance_cents() or 0)
            except Exception:
                mood_word, cash = "", 0
            low = any(word in mood_word for word in ("崩溃", "抑郁", "低落", "难过", "委屈"))
            if low or cash < 50000 or stable_int("social-gate:" + day, 0, 100) < 35:
                self.repo.meta_set("social:last_day", day)
                out["skipped"] = "state"
            else:
                pick = stable_int("social:pick:" + day, 0, 3)
                out["events"].append(await [self.gift_money, self.dinner, self.borrow_request][pick]())
                self.repo.meta_set("social:last_day", day)
                self.repo.mark_task(roll_key, "roll")
        await self.repay_tick(now)
        # 收尾：每年 3 月 1 日自动体检
        med = getattr(self.plugin, "medical", None)
        if med is not None and int(now.month) == 3 and int(now.day) == 1 and not self.repo.task_done("checkup:annual:" + day):
            try:
                out["checkup"] = await med.checkup(now.date())
                self.repo.mark_task("checkup:annual:" + day, "annual")
            except Exception as exc:
                isolated("social.checkup.tick", exc)
        # 收尾：昨天冲动消费 → 今天后悔
        regret = str(self.repo.meta_get("regret_pending") or "")
        if regret and regret < day:
            self.repo.meta_set("regret_pending", "")
            self.finance.nudge_mood({"委屈": 6}, "regret:" + regret)
            self.finance.small_diary("昨天不该买那件衣服的，这个月要吃土了。")
        return out

    # ---------- 内部 ----------
    def _afford(self, amount, scope):
        cash = int(self.finance.balance_cents() or 0)
        if cash >= amount:
            return True
        isolated(scope, RuntimeError("balance low"))
        return False

    def _escape(self, what):
        self.finance.nudge_mood({"委屈": 10}, "social:escape:" + what)
        self.finance.small_diary("【社交】没钱%s，借口加班躲过去了，好难受。" % what)
        return {"ok": False, "reason": "escape", "text": "不敢去%s，怕被看出来没钱……" % what}

    def _schedule(self, activity):
        try:
            self.finance.add_schedule(_dt.date.today().isoformat(), activity, detail="social", category="social")
        except Exception as exc:
            isolated("social.schedule", exc)

    def _remember(self, idem, text):
        try:
            self.finance.small_diary(text)
        except Exception as exc:
            isolated("social.memory", exc)


def build_social(plugin):
    return SocialEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None))
