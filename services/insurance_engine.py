# -*- coding: utf-8 -*-
"""商业保险：百万医疗/意外/重疾，年缴、断缴失效、二次报销与重疾赔付。"""
from __future__ import annotations
import datetime as _dt, json

try:
    from ..utils.helpers import stable_int, yuan
except ImportError:
    from utils.helpers import stable_int, yuan

PRODUCTS = {
    "e生保": {"name": "平安e生保", "premium": (17400, 40000), "kind": "medical", "deductible": 1000000},
    "医无忧": {"name": "平安医无忧", "premium": (22600, 53400), "kind": "medical"},
    "金钟罩": {"name": "平安金钟罩", "premium": (18800, 18800), "kind": "accident", "deductible": 0},
    "重疾": {"name": "平安一年期重疾险", "premium": (5800, 15000), "kind": "critical", "payout": 10000000},
    "交强险": {"name": "交强险", "premium": (95000, 95000), "kind": "car", "required": "car"},
    "商车险": {"name": "平安商业车险", "premium": (200000, 400000), "kind": "car"},
    "家财险": {"name": "平安家庭财产险", "premium": (10000, 20000), "kind": "house", "required": "house"},
}
class InsuranceEngine:
    def __init__(self, plugin, repo, finance):
        self.plugin, self.repo, self.finance = plugin, repo, finance

    def _data(self):
        try:
            return json.loads(self.repo.meta_get("insurance") or "{}") or {}
        except Exception:
            return {}

    def _save(self, d):
        self.repo.meta_set("insurance", json.dumps(d, ensure_ascii=False))

    def status(self):
        return self._data()

    def active(self, key):
        row = self._data().get(key) or {}
        return bool(row.get("active"))

    def prompt_line(self):
        d = self._data()
        parts = []
        for key, spec in PRODUCTS.items():
            row = d.get(key) or {}
            parts.append("%s（%s）" % (spec["name"], "生效中" if row.get("active") else "未购买"))
        return "【商业保险】" + "；".join(parts)

    async def buy(self, key, now=None):
        spec = PRODUCTS.get(key)
        if not spec:
            return {"ok": False, "reason": "unknown"}
        now = now or _dt.datetime.now()
        year = now.year
        idem = "insurance:%d:%s" % (year, key)
        if (self._data().get(key) or {}).get("year") == year and self.active(key):
            return {"ok": False, "reason": "already"}
        premium = stable_int("premium:%s:%d" % (key, year), *spec["premium"])
        res = await self.finance.expense(premium, "保费：%s" % spec["name"], idem, necessary=True)
        if not isinstance(res, dict) or not res.get("ok"):
            d = self._data()
            d[key] = {"active": False, "grace": True,
                      "grace_until": (now + _dt.timedelta(days=30)).date().isoformat(),
                      "year": year, "lapsed": True}
            self._save(d)
            self.finance.nudge_mood({"委屈": 8}, idem + ":lapse")
            self.finance.small_diary("完蛋了，卡里没钱，%s保费没扣成功，保险失效了……" % spec["name"])
            return {"ok": False, "reason": "insufficient", "lapsed": True}
        d = self._data()
        d[key] = {"active": True, "year": year, "premium": premium}
        self._save(d)
        self.finance.nudge_mood({"委屈": 5}, idem + ":mood")
        self.finance.small_diary("今天又到了交保费的日子，一下子扣了 %s，好心痛，不过就当买个平安吧。" % yuan(premium))
        return {"ok": True, "premium": premium, "name": spec["name"]}

    def grace_until(self, key):
        row = self._data().get(key) or {}
        return str(row.get("grace_until") or "")

    async def repay(self, key, now=None):
        now = now or _dt.datetime.now()
        row = self._data().get(key) or {}
        if not row.get("grace"):
            return {"ok": False, "reason": "not_grace"}
        spec = PRODUCTS.get(key) or {}
        premium = stable_int("premium:%s:%d" % (key, now.year), *(spec.get("premium") or (10000, 10000)))
        res = await self.finance.expense(premium, "补缴保费：%s" % spec.get("name", key), "insurance:repay:%d:%s" % (now.year, key), necessary=True)
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "reason": "insufficient"}
        d = self._data()
        d[key] = {"active": True, "year": now.year, "premium": premium}
        self._save(d)
        return {"ok": True, "premium": premium}

    async def expire_tick(self, now=None):
        now = now or _dt.datetime.now()
        d = self._data()
        changed = False
        for key, row in list(d.items()):
            if row.get("grace") and str(row.get("grace_until") or "") < now.date().isoformat():
                row["grace"] = False
                row["expired"] = True
                changed = True
        if changed:
            self._save(d)
        return changed

    def claim(self, level, self_pay_cents, accident=False):
        extra, payout, notes = 0, 0, []
        d = self._data()
        def ok(k):
            row = d.get(k) or {}
            if row.get("grace"):
                notes.append("%s 宽限期内，请先补缴保费才能理赔" % (PRODUCTS.get(k, {}).get("name") or k))
                return False
            return bool(row.get("active"))
        if ok("e生保") and self_pay_cents > 1000000:
            extra += self_pay_cents - 1000000
            notes.append("e生保报销 %s" % yuan(self_pay_cents - 1000000))
        elif ok("医无忧"):
            low = min(self_pay_cents, 1000000)
            high = max(0, self_pay_cents - 1000000)
            extra += int(low * 0.3) + high
            notes.append("医无忧报销 %s" % yuan(int(low * 0.3) + high))
        if accident and ok("金钟罩"):
            extra = max(extra, self_pay_cents)
            notes.append("金钟罩全额报销")
        if level == "heavy" and ok("重疾"):
            payout = 10000000
            notes.append("重疾险赔付 %s" % yuan(payout))
        return min(extra, self_pay_cents), payout, "；".join(notes)

    async def car_claim(self, cost_cents, has_car=True):
        if not has_car:
            return {"covered": False, "reason": "no_car"}
        if self.active("商车险"):
            return {"covered": True, "note": "平安商业车险全赔 %s" % yuan(cost_cents)}
        return {"covered": False, "reason": "no_policy", "note": "自掏腰包 %s" % yuan(cost_cents)}

    async def house_claim(self, cost_cents, has_house=True):
        if not has_house:
            return {"covered": False, "reason": "no_house"}
        if self.active("家财险"):
            return {"covered": True, "note": "家财险赔付 %s" % yuan(cost_cents)}
        return {"covered": False, "reason": "no_policy", "note": "强制自费维修 %s" % yuan(cost_cents)}


    def decision_hint(self, health=80, news="", idle_cents=0, has_car=False, has_house=False):
        d = self._data()
        def ok(k): return bool((d.get(k) or {}).get("active"))
        tips = []
        if health < 60 or any(w in str(news) for w in ("猝死", "重疾", "癌", "流感")):
            if not (ok("e生保") or ok("医无忧")):
                tips.append("你最近身体/新闻让你有点慌，考虑买 e生保 或 医无忧。")
            if not ok("重疾"):
                tips.append("也可以看看一年期重疾险。")
        if has_car and not (ok("交强险") and ok("商车险")):
            tips.append("你有车但车险不全：交强险是强制的，商业车险能赔修车费。")
        if has_house and not ok("家财险"):
            tips.append("你有房，考虑家财险，漏水失窃能赔。")
        if idle_cents > 500000 and not any(tips):
            tips.append("手里闲钱较多，可以配置一点保障。")
        if not tips:
            return ""
        return "【保险购买建议】" + " ".join(tips) + " 如果保费太贵或钱不够，不要买，只在日记里写下纠结。"

    async def poor_diary(self, key="e生保"):
        spec = PRODUCTS.get(key) or {}
        self.finance.small_diary("今天看到新闻说年轻人患癌概率变高了，我想买份%s，可是卡里只剩一点钱，连保费都不够，呜呜呜。老公，你能帮帮我吗……" % spec.get("name", key))


def build_insurance(plugin):
    return InsuranceEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None))
