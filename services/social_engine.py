# -*- coding: utf-8 -*-
"""社会关系引擎：专属 NPC 名单 + 好感/状态 + 邀约（NPC 自己发、她决定）+ 事件流水。

2026-11-15 二次封板：
1. 没有事件池——系统只给「NPC 近期状态 + 她的状态」，约什么由 LLM 决定；
2. 邀约有硬闸：全局间隔 ≥2.5h、每天最多 2 次、同一天同一个 NPC 只能约一次；
3. 常识校验：喜酒这类大事提前 ≥3 天；领导约工作日/下班后；闺蜜约周末；
4. 日程冲突：她已答应的时段不再排新邀约，接受时也会再查一次；
5. 文案里不出现精确金额和系统标签，金额只会说「两百多」。
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import re
import time
import uuid

try:
    from ..guard import isolated
    from ..utils.helpers import is_holiday, stable_int
    from ..utils.humanize import human_money, humanize_numbers
    from . import npc_roster, npc_status
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated
    from utils.helpers import is_holiday, stable_int
    from utils.humanize import human_money, humanize_numbers
    from services import npc_roster, npc_status

#: 唯一合法的 7 个核心 NPC
SEED = tuple((item["name"], item["circle"], item["relation"], item["affinity"])
             for item in npc_roster.ROSTER)

#: 只用来把 LLM 说的「约什么」落到 amo 认可的地点和场景（不是事件池）
KIND_PLACES = {
    "meal": ("楼下小馆", "restaurant"),
    "drink": ("楼下小馆", "restaurant"),
    "shopping": ("商场", "mall"),
    "ceremony": ("酒店", "hotel"),
    "work": ("公司", "office"),
    "help": ("公司", "office"),
    "borrow": ("", ""),
    "other": ("楼下小馆", "restaurant"),
}
KIND_BUDGET = {"meal": 12000, "drink": 8000, "shopping": 25000, "ceremony": 40000,
               "work": 0, "help": 0, "borrow": 60000, "other": 10000}

MIN_GAP_HOURS = 2.5          # 两次邀约之间至少隔 2.5 小时
MAX_PER_DAY = 2              # 一天最多 2 次邀约
CEREMONY_LEAD_DAYS = 3       # 喜酒/婚礼至少提前 3 天
INVITE_WINDOW = (9, 22)      # 只在 9:00-22:00 抛邀约
INVITE_TTL_HOURS = 8
RED_RANGE = (50000, 200000)
BORROW_DAYS = 30


def _minutes(text, default=0):
    try:
        hour, minute = str(text).split(":")[:2]
        return int(hour) * 60 + int(minute)
    except Exception:
        return int(default)


_DAY_WORDS = (("今天", 0), ("明天", 1), ("后天", 2), ("大后天", 3))
_PERIODS = (("上午", ("09:00", "11:30")), ("中午", ("12:00", "13:30")),
            ("下午", ("14:00", "17:00")), ("傍晚", ("17:30", "19:00")),
            ("晚上", ("19:00", "21:00")), ("夜里", ("20:00", "22:00")))


def parse_when(text, now=None):
    """「明天下午」→ (日期, 14:00, 17:00)。大模型只给自然语言时用它兜底。"""
    now = now or _dt.datetime.now()
    body = str(text or "")
    offset = None
    for word, days in _DAY_WORDS:
        if word in body:
            offset = days
            break
    if offset is None and "周末" in body:
        ahead = (5 - now.weekday()) % 7
        offset = ahead or 7
    start, end = "14:00", "17:00"
    for word, span in _PERIODS:
        if word in body:
            start, end = span
            break
    target = (now + _dt.timedelta(days=int(offset or 0))).date().isoformat()
    return target, start, end


def _day_label(day):
    """把日期翻成她聊天时会用的人话：工作日 / 周末 / 法定节假日。"""
    if is_holiday(day):
        return "法定节假日"
    return "周末" if day.weekday() >= 5 else "工作日"


def _off_day(day):
    return bool(day.weekday() >= 5 or is_holiday(day))


def _brief_status(row):
    """卡片只留最近的一件事；长期设定（出国/换工作）不跟这次邀约混在一起。"""
    text = str((row or {}).get("last_event") or (row or {}).get("status") or "最近没什么特别的事").strip()
    text = re.split(r"[。！？\n]", text, 1)[0].strip()
    return text[:42]


def _nearest_name(name, allowed):
    name = str(name or "").strip()
    if name in allowed:
        return name
    matches = difflib.get_close_matches(name, list(allowed), n=1, cutoff=0.55)
    return matches[0] if matches else ""


def _relative_label(target, now):
    delta = (target - now.date()).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return "%d月%d日" % (target.month, target.day)


def _retime_ask(text, target, now, start):
    """邀约日期/时段被常识规则修正后，把微信原话里的“明天/下午”一起改掉，避免文案穿帮。"""
    out = str(text or "")
    label = _relative_label(target, now)
    for word in ("大后天", "后天", "明天", "今天"):
        if word in out:
            out = out.replace(word, label)
    if _minutes(start) >= 18 * 60:
        for word in ("上午", "中午", "下午", "白天"):
            out = out.replace(word, "晚上")
    return out


def _ts(day, hhmm, fallback=0.0):
    try:
        return _dt.datetime.strptime("%s %s" % (day, hhmm), "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return float(fallback)


class SocialEngine:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    # ---------- NPC ----------
    def ensure_seed(self):
        if self.repo is None:
            return
        try:
            store = getattr(self.repo, "store", None)
            if store is not None and hasattr(store, "trim_events"):
                store.trim_events(2)
        except Exception as exc:
            isolated("social.seed.trim", exc)
        for name, circle, relation, affinity in SEED:
            if self.repo.npc_get(name) is None:
                self.repo.upsert_npc(name, circle, relation, affinity, "#社交_" + name)
        self._prune()
        self._clean_npc_text()
        try:
            npc_status.refresh(self.repo)
        except Exception as exc:
            isolated("social.status", exc)

    def _clean_npc_text(self):
        """把老数据里被大模型写错的名字（李博达/王博达…）按名单改回来。"""
        if self.repo is None:
            return
        try:
            rows = self.repo.npcs()
        except Exception as exc:
            isolated("social.npc_text.read", exc)
            return
        for row in rows:
            name = str((row or {}).get("name") or "")
            if name not in npc_roster.NAME_SET:
                continue
            for field, setter in (("last_event", self.repo.set_npc_event),
                                  ("status", None)):
                text = str((row or {}).get(field) or "")
                fixed = npc_roster.sanitize(text, name)
                if field == "last_event" and re.search(r"喜酒|婚礼|满月酒", text) and re.search(r"今天|明天|后天", text):
                    # 旧数据里“今天说明天喝喜酒”的穿帮动态：直接清掉，卡片回落到现在状态。
                    fixed = ""
                if fixed == text:
                    continue
                try:
                    if setter is not None:
                        setter(name, fixed)
                    else:
                        self.repo.set_npc_status(name, fixed, float(row.get("status_at") or 0))
                except Exception as exc:
                    isolated("social.npc_text.write", exc)

    def _prune(self):
        if getattr(self, "_pruned", False):
            return
        self._pruned = True
        try:
            self.repo.prune_npcs(npc_roster.NAMES)
        except Exception as exc:
            isolated("social.prune", exc)

    def by_circle(self, circle):
        if self.repo is None:
            return []
        return [n for n in self.repo.npcs() if n.get("circle") == circle]

    def adjust(self, name, delta):
        if self.repo is None or name not in npc_roster.NAME_SET:
            return
        row = self.repo.npc_get(name) or {}
        self.repo.set_affinity(name, int(row.get("affinity") or 50) + int(delta))

    def relation_line(self):
        if self.repo is None:
            return ""
        parts = []
        for row in self.repo.npcs()[:8]:
            owed = int(row.get("borrowed_from_her") or 0)
            status = str(row.get("status") or "").strip()
            parts.append("%s（%s，好感 %s%s%s）" % (
                row["name"], row["relation"], row["affinity"],
                "，" + status if status else "",
                "，欠我 " + human_money(owed) if owed else ""))
        return "【关系】" + "；".join(parts)

    # ---------- 结构化兜底（人话，不带标签、不带精确金额） ----------
    def fallback_line(self, kind, npc="", topic="", label="", place="", start="", end="",
                      amount_cents=0, status=""):
        k = str(kind or "")
        npc = str(npc or "")
        what = str(topic or label or "").strip() or (("去" + place) if place else "见面")
        if k.endswith(":invite"):
            return "%s约她%s" % (npc, what)
        if k.endswith(":accept"):
            return "她答应了%s" % npc
        if k.endswith(":reject"):
            return "她婉拒了%s" % npc
        if k.endswith(":expire"):
            return "%s的邀约她没回" % npc
        if k.endswith(":red"):
            return "%s给她发了个红包" % npc
        if k.endswith(":repay"):
            return "%s把钱还了" % npc
        if k.endswith(":default"):
            return "%s借的钱一直没还" % npc
        if k.endswith(":criticism"):
            return "%s批评了她" % npc
        return "%s那边有点动静" % (npc or "朋友")

    # ---------- 事件流水 ----------
    def record(self, kind, npc="", text="", status="", amount_cents=0, day="", extra=None,
               topic="", label="", place="", start="", end="", idem=""):
        if self.repo is None:
            return False
        clean = humanize_numbers(npc_roster.sanitize(text, npc))
        payload = dict(extra or {})
        payload["fallback"] = self.fallback_line(kind, npc=npc, topic=topic, label=label,
                                                 place=place, start=start, end=end,
                                                 amount_cents=amount_cents, status=status)
        idem = str(idem or "%s:%s:%s:%s" % (kind, npc, day or _dt.date.today().isoformat(), status))
        try:
            return bool(self.repo.add_event(kind, clean, npc=npc, status=status,
                                            amount_cents=amount_cents, day=day, extra=payload,
                                            idem=idem))
        except Exception as exc:
            isolated("social.record", exc)
            return False

    def events(self, limit=10):
        if self.repo is None:
            return []
        try:
            limit = max(1, int(limit or 10))
            # 关系动态只保留 2 天：先物理清掉更老的，再按 created_at 过滤。
            try:
                store = getattr(self.repo, "store", None)
                if store is not None and hasattr(store, "trim_events"):
                    store.trim_events(2)
            except Exception as exc:
                isolated("social.events.trim", exc)
            rows = list(self.repo.events(limit + 100) or [])
        except Exception as exc:
            isolated("social.events", exc)
            return []
        cutoff = time.time() - 2 * 86400
        # 旧版曾把 NPC 长期状态写进事件表；这里兜底过滤，绝不再冒充互动流水。
        rows = [row for row in rows
                if str(row.get("kind") or "") != "social:status"
                and float(row.get("created_at") or 0) >= cutoff][:limit]
        for row in rows:
            payload = row.get("payload") or {}
            row["display"] = humanize_numbers(
                str(payload.get("text") or payload.get("fallback") or "").strip())
        return rows

    # ---------- 待决邀约 ----------
    def pending_invite(self):
        if self.repo is None:
            return None
        raw = self.repo.meta_get("social:pending")
        if not raw:
            return None
        try:
            data = json.loads(str(raw))
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("npc"):
            return None
        if str(data.get("npc")) not in npc_roster.NAME_SET:
            return None
        return data

    def set_invite(self, invite):
        if self.repo is None or not isinstance(invite, dict):
            return False
        return bool(self.repo.meta_set("social:pending", json.dumps(invite, ensure_ascii=False)))

    def mark_resolved(self, token):
        """邀约处理完就进 resolved 列表；定时器再扫到同一 token 直接跳过。"""
        if self.repo is None or not token:
            return False
        try:
            raw = self.repo.meta_get("social:resolved", []) or []
            if isinstance(raw, str):
                raw = json.loads(raw or "[]")
            items = [str(x) for x in (raw or []) if str(x) and str(x) != str(token)]
            items.append(str(token))
            return bool(self.repo.meta_set("social:resolved", json.dumps(items[-50:])))
        except Exception as exc:
            isolated("social.mark_resolved", exc)
            return False

    def is_resolved(self, token):
        try:
            raw = self.repo.meta_get("social:resolved", []) or []
            if isinstance(raw, str):
                raw = json.loads(raw or "[]")
            return str(token) in [str(x) for x in (raw or [])]
        except Exception:
            return False

    def clear_invite(self):
        if self.repo is None:
            return False
        return bool(self.repo.meta_set("social:pending", ""))

    # ---------- 状态读数 ----------
    def _cash(self):
        try:
            return int(self.finance.balance_cents() or 0)
        except Exception:
            return 0

    def _amo(self):
        fn = getattr(self.finance, "_amo", None) if self.finance is not None else None
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:
            return None

    def busy_state(self, now=None):
        """约会中 / 睡眠中：她把手机静音了，绝不处理第三方社交。"""
        now = now or _dt.datetime.now()
        amo = self._amo()
        if amo is None:
            return ""
        try:
            date_now = amo.db.kv_get("date_now", {}) or {}
            if isinstance(date_now, dict) and float(date_now.get("until") or 0) > now.timestamp():
                return "dating"
        except Exception:
            pass  # cleanup: best-effort
        try:
            act = amo.state.current_activity() or {}
            if str(act.get("category") or "") == "sleep":
                return "sleeping"
        except Exception:
            pass  # cleanup: best-effort
        return ""

    def _fatigue(self):
        fn = getattr(self.finance, "fatigue", None)
        if callable(fn):
            try:
                return int(fn() or 0)
            except Exception:
                return 0
        return 0

    def _mood_word(self):
        try:
            return str(self.finance.mood().get("word") or "平静")
        except Exception:
            return "平静"

    def _windows(self, day):
        fn = getattr(self.finance, "schedule_windows", None)
        if not callable(fn):
            return []
        try:
            return list(fn(str(day)) or [])
        except Exception:
            return []

    def _conflict(self, day, start, end):
        """这个时段她是不是已经答应过别人的事。"""
        begin, finish = _minutes(start), _minutes(end)
        for row in self._windows(day):
            other_begin = _minutes(row.get("start"))
            other_end = _minutes(row.get("end"))
            if begin < other_end and other_begin < finish:
                return row
        return None

    # ---------- 冷却闸（防止 4 分钟 5 次邀约） ----------
    def can_invite(self, now=None, npc=""):
        now = now or _dt.datetime.now()
        if self.repo is None:
            return False, "not_ready"
        if self.pending_invite():
            return False, "pending"
        low, high = INVITE_WINDOW
        if not (low <= now.hour < high):
            return False, "quiet_hours"
        day = now.date().isoformat()
        try:
            count = int(self.repo.meta_get("social:count:" + day, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if count >= MAX_PER_DAY:
            return False, "daily_cap"
        try:
            last = float(self.repo.meta_get("social:last_ts", 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last and (now.timestamp() - last) < MIN_GAP_HOURS * 3600:
            return False, "gap"
        if npc and self.repo.task_done("social:npc:%s:%s" % (day, npc)):
            return False, "npc_today"
        return True, ""

    def ready(self, now=None):
        return self.can_invite(now)[0]

    def _bump(self, day, npc, now=None):
        try:
            count = int(self.repo.meta_get("social:count:" + day, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        self.repo.meta_set("social:count:" + day, str(count + 1))
        self.repo.meta_set("social:last_ts", str((now or _dt.datetime.now()).timestamp()))
        self.repo.meta_set("social:last_day", day)
        self.repo.mark_task("social:roll:" + day, "invite")
        if npc:
            self.repo.mark_task("social:npc:%s:%s" % (day, npc), "invited")

    # ---------- 抛邀约：先让 LLM 决定 NPC 约什么 ----------
    def _candidates(self, day):
        return [row for row in self.repo.npcs()
                if not self.repo.task_done("social:npc:%s:%s" % (day, row.get("name")))]

    def compose_facts(self, now, rows):
        busy = []
        for offset in range(3):
            target = (now.date() + _dt.timedelta(days=offset)).isoformat()
            for row in self._windows(target):
                busy.append("%s %s-%s %s" % (target, row.get("start"), row.get("end"),
                                             row.get("activity") or ""))
        her = "余额 %s，疲惫值 %d，心情%s" % (human_money(self._cash()), self._fatigue(),
                                             self._mood_word())
        lines = []
        for row in rows:
            lines.append("%s（%s，好感 %s）：%s" % (row.get("name"), row.get("relation"),
                                                    row.get("affinity"),
                                                    _brief_status(row)))
        return {"now": now.strftime("%Y-%m-%d %H:%M %A"),
                "day_facts": "今天是 %s（%s）" % (now.strftime("%Y-%m-%d %A"), _day_label(now.date())),
                "npc": "候选（自己挑一个最合适的）",
                "relation": "、".join(row.get("name") or "" for row in rows),
                "status": "\n".join(lines),
                "her_state": her,
                "calendar": "；".join(busy) or "这几天没别的安排"}

    async def compose_with_llm(self, facts):
        try:
            from .social_ai import compose
        except ImportError:
            from services.social_ai import compose
        return await compose(self.plugin, facts)

    def _normalize(self, now, data, rows):
        allowed = {row.get("name"): row for row in rows if row.get("name")}
        npc = _nearest_name(data.get("npc"), allowed)
        if not npc and len(rows) == 1:
            npc = str(rows[0].get("name") or "")
        if npc not in allowed:
            return None
        row = allowed[npc]
        kind = str(data.get("kind") or "other").strip().lower()
        if kind not in KIND_PLACES:
            kind = "other"
        place, zone = KIND_PLACES[kind]
        said_place = str(data.get("place") or "").strip()
        if said_place in ("家", "商场", "楼下小馆", "酒店", "公司"):
            place = said_place
        start = str(data.get("start") or "").strip()
        end = str(data.get("end") or "").strip()
        if not start or not end or _minutes(end) <= _minutes(start):
            start, end = {
                "meal": ("18:00", "20:00"), "drink": ("20:00", "22:00"),
                "shopping": ("14:00", "17:00"), "ceremony": ("11:00", "13:00"),
                "work": ("10:00", "12:00"), "help": ("15:00", "17:00"),
                "borrow": ("", ""), "other": ("15:00", "17:00"),
            }[kind]
        day = str(data.get("day") or "").strip()
        try:
            target = _dt.date.fromisoformat(day)
        except Exception:
            target = now.date()
        if target < now.date():
            target = now.date()
        circle = str(row.get("circle") or "")

        # 硬常识：酒席只能周末/节假日且提前 ≥3 天；工作日白天朋友约只能改晚上；
        # 领导约饭只能下班后。日期/时段被改后，微信原话里的“明天/下午”同步改掉。
        if kind == "ceremony":
            target = max(target, now.date() + _dt.timedelta(days=CEREMONY_LEAD_DAYS))
            for _ in range(21):
                if _off_day(target):
                    break
                target += _dt.timedelta(days=1)
            else:
                return None
            start, end = "11:00", "13:00"
        elif circle == "work":
            while _off_day(target):
                target += _dt.timedelta(days=1)
            if kind in ("meal", "drink") and _minutes(start) < 18 * 60:
                start, end = "19:00", "20:30"
        elif circle == "friend" and kind in ("meal", "drink", "shopping", "other"):
            if not _off_day(target) and _minutes(start) < 18 * 60:
                start, end = "19:00", "21:00"

        # 冲突：挪到下一个空的日子（最多试 14 天；酒席仍只能落在休息日）
        for _ in range(14):
            day_text = target.isoformat()
            if not start or not self._conflict(day_text, start, end):
                break
            target += _dt.timedelta(days=1)
            if kind == "ceremony":
                while not _off_day(target):
                    target += _dt.timedelta(days=1)
        else:
            return None
        budget = int(data.get("budget_cents") or 0)
        if budget <= 0:
            budget = KIND_BUDGET.get(kind, 10000)
        ask = _retime_ask(str(data.get("ask") or ""), target, now, start)
        wrong_name = str(data.get("npc") or "").strip()
        if wrong_name and wrong_name != npc:
            ask = ask.replace(wrong_name, npc)
        ask = humanize_numbers(npc_roster.sanitize(ask, npc))
        if not ask:
            return None
        return {
            "token": "soc-%s-%s" % (day_text, uuid.uuid4().hex[:8]),
            "kind": kind, "topic": str(data.get("topic") or "").strip()[:20],
            "npc": npc, "relation": str(row.get("relation") or ""), "circle": circle,
            "status": _brief_status(row),
            "place": place, "day": day_text, "start": start, "end": end, "zone": zone,
            "amount_cents": budget, "created": now.timestamp(),
            "expires": now.timestamp() + INVITE_TTL_HOURS * 3600, "ask": ask,
        }

    def _manual_data(self, rows, kind, npc="", amount_cents=0):
        """手动/测试触发（`/社交` 或面板指定类型）用：事实生成，不依赖 LLM。"""
        row = next((item for item in rows if str(item.get("name")) == str(npc)), rows[0])
        topic = {"meal": "吃个饭", "drink": "喝一杯", "shopping": "逛街",
                 "ceremony": "喝喜酒", "work": "谈点事", "help": "帮个忙",
                 "borrow": "借点钱", "other": "见一面"}.get(kind, "见一面")
        return {"npc": str(row.get("name") or ""), "kind": kind, "topic": topic,
                "place": "", "day": "", "start": "", "end": "",
                "budget_cents": int(amount_cents or 0),
                "ask": "最近怎么样？有空一起%s吗？" % topic}

    async def create_invite(self, now=None, composer=None, npc="", kind="", amount_cents=0):
        now = now or _dt.datetime.now()
        self.ensure_seed()
        if self.repo is None or self.finance is None:
            return {"ok": False, "reason": "not_ready"}
        if npc and str(npc) not in npc_roster.NAME_SET:
            return {"ok": False, "reason": "unknown_npc"}
        ok, reason = self.can_invite(now, npc)
        if not ok:
            return {"ok": False, "reason": reason}
        day = now.date().isoformat()
        rows = self._candidates(day)
        if npc:
            rows = [row for row in rows if str(row.get("name")) == str(npc)]
        if not rows:
            return {"ok": False, "reason": "npc_today"}
        if composer is None and kind:
            data = self._manual_data(rows, kind, npc, amount_cents)
        else:
            facts = self.compose_facts(now, rows)
            if composer is None:
                composer = self.compose_with_llm
            try:
                data = await composer(facts)
            except Exception as exc:
                isolated("social.compose", exc)
                data = None
        invite = self._normalize(now, data, rows) if data else None
        if invite is None:
            return {"ok": False, "reason": "compose_failed"}
        busy = self.busy_state(now)
        if busy:
            # 约会/睡觉期间：消息先存着，绝不触发她的决策；等状态恢复再处理
            invite["deferred"] = time.time()
            invite["deferred_reason"] = busy
        self.set_invite(invite)
        self.record("social:invite", invite["npc"], invite["ask"], status="pending",
                    amount_cents=invite["amount_cents"], day=day,
                    extra={"kind": invite["kind"], "topic": invite["topic"],
                           "place": invite["place"], "start": invite["start"],
                           "end": invite["end"]},
                    topic=invite["topic"], place=invite["place"],
                    start=invite["start"], end=invite["end"],
                    idem="evt:invite:" + str(invite.get("token") or ""))
        self._bump(day, invite["npc"], now)
        return {"ok": True, "invite": invite}

    def invite_facts(self, invite=None, now=None):
        inv = invite or self.pending_invite()
        if not inv:
            return ""
        now = now or _dt.datetime.now()
        when = str(inv.get("day") or "")
        if inv.get("start"):
            when = "%s %s-%s" % (when, inv.get("start"), inv.get("end") or inv.get("start"))
        cash = self._cash()
        cost = int(inv.get("amount_cents") or 0)
        lines = [
            "【当前时间事实】%s（%s）" % (now.strftime("%Y-%m-%d %A"), _day_label(now.date())),
            npc_roster.ROSTER_LINE,
            "【社交邀约·真实发生】%s（%s）发微信给你：" % (inv.get("npc"), inv.get("relation") or ""),
            "「%s」" % str(inv.get("ask") or ""),
        ]
        if inv.get("topic"):
            lines.append("- 约的事：%s" % inv["topic"])
        if inv.get("status"):
            lines.append("- 她最近：%s" % inv["status"])
        if inv.get("place"):
            lines.append("- 地点：%s" % inv["place"])
        lines.append("- 时间：%s" % when)
        if cost:
            lines.append("- 大概要花：%s" % human_money(cost))
        lines.append("- 你现在的可用余额：%s（真实余额，不许编）" % human_money(cash))
        lines.append("- 你现在的疲惫值：%d（0 不累，100 累垮）" % self._fatigue())
        lines.append("- 你现在的心情：%s" % self._mood_word())
        if inv.get("deferred"):
            lines.append("（你刚才在约会/睡觉，手机静音了，现在才看到这条消息。）")
        if str(inv.get("day") or "") != now.date().isoformat():
            lines.append("- 约的是 %s 的事，不是现在出门；回她的时候别让她以为你马上要走。"
                         % (inv.get("day") or ""))
        busy = []
        for row in self._windows(str(inv.get("day") or "")):
            busy.append("%s-%s %s" % (row.get("start"), row.get("end"), row.get("activity") or ""))
        if busy:
            lines.append("- 那天你已有的安排：%s（撞车就别答应，说清楚有约了）" % "；".join(busy))
        if str(inv.get("kind")) == "borrow":
            if cost > cash:
                lines.append("她开口要借 %s，但你只有 %s —— 超出就借不出。" % (human_money(cost), human_money(cash)))
                lines.append("你只能少借一点，或者直接拒绝；绝对不能透支、不能编造余额。")
        if str(inv.get("circle")) == "friend":
            lines.append("- 她八成会顺口问起你家那位（老公）最近怎么样；你可以自己夸两句，也可以吐槽。")
        lines.append("怎么回由你自己决定：想去就调用 accept_social_event"
                     "（会真实扣钱、占日程、写记忆）；不想去或去不了就调用 reject_social_event 说清真实理由；"
                     "不想去也不用勉强。")
        return "\n".join(lines)

    # ---------- 她的决定 ----------
    def _mood(self, deltas, idem):
        try:
            self.finance.nudge_mood(dict(deltas), str(idem) + ":mood")
            return True
        except Exception as exc:
            isolated("social.mood", exc)
            return False

    async def _memory(self, text, idem):
        text, idem = str(text), str(idem)
        try:
            if await self.finance.permanent_memory(text, idem):
                return True
        except Exception as exc:
            isolated("social.memory", exc)
        try:
            fn = getattr(self.finance, "long_memory", None)
            if callable(fn):
                return bool(await fn(text, idem + ":long"))
        except Exception as exc:
            isolated("social.memory.long", exc)
        return False

    @staticmethod
    def _sentence(text):
        value = str(text or "").strip()
        if not value:
            return ""
        return value if value[-1] in "。！？…～!?" else value + "。"

    def _diary(self, text, npc=""):
        clean = humanize_numbers(npc_roster.sanitize(text, npc))
        if not clean:
            return False
        try:
            self.finance.small_diary(self._sentence(clean))
            return True
        except Exception as exc:
            isolated("social.diary", exc)
            return False

    def _drop_schedule(self, day, start, name):
        """扣钱失败时把刚写进去的日程撤回，避免留下没付钱的行程。"""
        try:
            amo = self._amo()
            state = getattr(amo, "state", None)
            items = list((getattr(state, "schedule", {}) or {}).get(day) or [])
            kept = [row for row in items
                    if not (str(row.get("start")) == start and name in str(row.get("activity") or ""))]
            if len(kept) != len(items):
                state.commit_schedule_change(day, kept, old_items=items,
                                             source="social", reason="社交回滚日程")
        except Exception as exc:
            isolated("social.drop_schedule", exc)

    def _pending_for_decision(self):
        """待回邀约；已被处理（resolved）的直接清掉，定时器再也扫不到。"""
        inv = self.pending_invite()
        if inv and self.is_resolved(inv.get("token")):
            self.clear_invite()
            return None
        return inv

    async def accept_invite(self, npc="", place="", amount_cents=0, note="", reason="",
                            start="", end="", day="", token="", about_user="", ledger_line=""):
        inv = self._pending_for_decision()
        if not inv:
            return {"ok": False, "reason": "no_invite", "message": "现在没有待回的邀约"}
        if token and str(token) != str(inv.get("token") or ""):
            return {"ok": False, "reason": "stale", "message": "这条邀约已经过去了"}
        busy = self.busy_state()
        if busy:
            return {"ok": False, "reason": "busy",
                    "message": "你正在%s，手机静音着，等忙完再看" % ("约会" if busy == "dating" else "睡觉")}
        name = str(npc or inv.get("npc") or "")
        if name not in npc_roster.NAME_SET:
            return {"ok": False, "reason": "unknown_npc", "message": "这个人不在你的社交圈里"}
        kind = str(inv.get("kind") or "")
        label = str(inv.get("topic") or inv.get("label") or "")
        cost = max(0, int(amount_cents or inv.get("amount_cents") or 0))
        cash = self._cash()
        if cost > cash:
            if kind == "borrow":
                return {"ok": False, "reason": "insufficient",
                        "message": "你最多只能借出 %s，借不了这么多；少借一点或者直接拒绝" % human_money(cash)}
            return {"ok": False, "reason": "insufficient",
                    "message": "卡里只剩 %s，去不起" % human_money(cash)}
        day_text = str(day or inv.get("day") or "").strip()
        use_start = str(start or inv.get("start") or "").strip()
        use_end = str(end or inv.get("end") or "").strip()
        if not use_start:
            parsed_day, parsed_start, parsed_end = parse_when(
                inv.get("ask") or inv.get("topic") or "")
            day_text = day_text or parsed_day
            use_start, use_end = parsed_start, parsed_end
        if not day_text:
            day_text = _dt.date.today().isoformat()
        # 幂等：这场约会已经写进日程了，就不再挪日子、不再写第二条
        existing = False
        if use_start:
            try:
                for row in self.finance.schedule_windows(day_text) or []:
                    if str(row.get("start")) == use_start and name in str(row.get("activity") or ""):
                        existing = True
                        break
            except Exception:
                existing = False
        moved = 0
        while (not existing) and use_start and self._conflict(day_text, use_start, use_end) and moved < 7:
            day_text = (_dt.date.fromisoformat(day_text) + _dt.timedelta(days=1)).isoformat()
            moved += 1
        if (not existing) and use_start and self._conflict(day_text, use_start, use_end):
            return {"ok": False, "reason": "conflict",
                    "message": "接下来一周那个时间都排满了，跟她改天吧"}
        # 事件级幂等键：同一个人、同一天、同一时段只可能扣一次钱
        idem = "social_event:%s:%s:%s" % (name, day_text, use_start or "any")
        thing = "%s%s" % (name, label or "")
        fact_reason = ("借给%s" % name) if kind == "borrow" else thing
        reason_text = humanize_numbers(npc_roster.sanitize(ledger_line, name)) or fact_reason
        steps = {}
        sched = {}
        if use_start:
            sched = {"day": day_text, "start": use_start, "end": use_end or use_start,
                     "place": place or inv.get("place") or "外面"}
            if not existing:
                written = bool(self.finance.add_schedule(
                    day_text, thing, detail=str(note or thing), place=sched["place"],
                    category="social", start=use_start, end=sched["end"],
                    zone=str(inv.get("zone") or "")))
                if not written:
                    return {"ok": False, "reason": "schedule_failed",
                            "message": "日程没写进去，先别答应；再试一次"}
            steps["schedule"] = True
        res = await self.finance.expense(cost, reason_text, idem, category="社交")
        if not (isinstance(res, dict) and res.get("ok")):
            if sched:
                self._drop_schedule(day_text, use_start, name)
            return {"ok": False, "reason": "insufficient", "message": "钱没扣成功"}
        steps["wallet"] = True
        if kind == "borrow":
            due = (_dt.date.today() + _dt.timedelta(days=BORROW_DAYS)).isoformat()
            self.repo.add_receivable(name, cost, due)
            self.adjust(name, 8)
            steps["mood"] = self._mood({"焦虑": 10, "委屈": 4}, idem)
        else:
            self.adjust(name, 6)
            steps["mood"] = self._mood({"开心": 9, "兴奋": 5, "疲惫": -6}, idem)
        if use_start:
            until = _ts(day_text, sched["end"], time.time())
            begin = _ts(day_text, use_start, 0.0)
            if begin <= time.time() <= until:
                steps["current_action"] = bool(self.finance.set_current_action(
                    thing, until, place=sched["place"], zone=str(inv.get("zone") or "restaurant")))
        text = humanize_numbers(npc_roster.sanitize(note or reason or "", name))
        if not text:
            text = self.fallback_line("social:accept", npc=name, topic=label,
                                      place=sched.get("place") or "", start=use_start,
                                      end=use_end, amount_cents=cost)
        about = npc_roster.sanitize(about_user, name)
        if about and not any(word in about for word in ("老公", "他", "你", "亲爱的", "对象")):
            about = ""
        self.repo.set_npc_event(name, text)
        self.record("social:accept", name, text, status="accepted", amount_cents=cost,
                    day=day_text,
                    extra={"kind": kind, "place": sched.get("place") or inv.get("place") or "",
                           "about_user": about, "ledger_line": reason_text},
                    topic=label, place=sched.get("place") or "", start=use_start, end=use_end,
                    idem="evt:accept:" + str(inv.get("token") or name))
        memory = self._sentence(text + ((" 她还聊起你：" + about) if about else ""))
        steps["memory"] = await self._memory(memory, idem + ":mem")
        steps["diary"] = self._diary(memory, name)
        self.clear_invite()
        self.mark_resolved(inv.get("token"))
        return {"ok": True, "npc": name, "cost": cost, "note": text, "line": text,
                "about_user": about, "invite": inv, "schedule": sched, "steps": steps}

    async def reject_invite(self, reason="", note="", token=""):
        inv = self._pending_for_decision()
        if not inv:
            return {"ok": False, "reason": "no_invite", "message": "现在没有待回的邀约"}
        if token and str(token) != str(inv.get("token") or ""):
            return {"ok": False, "reason": "stale", "message": "这条邀约已经过去了"}
        name = str(inv.get("npc") or "")
        if name not in npc_roster.NAME_SET:
            return {"ok": False, "reason": "unknown_npc", "message": "这个人不在你的社交圈里"}
        why = str(reason or "").strip()
        text = humanize_numbers(npc_roster.sanitize(note or why or "", name))
        if not text:
            text = self.fallback_line("social:reject", npc=name,
                                      topic=str(inv.get("topic") or ""))
        if any(word in why for word in ("累", "困", "熬夜", "没力气")):
            deltas = {"郁闷": 4}
        elif any(word in why for word in ("没钱", "贵", "余额", "花呗", "预算", "借不出")):
            deltas = {"委屈": 5}
        else:
            deltas = {"郁闷": 3}
        self._mood(deltas, "social:reject:" + str(inv.get("token") or name))
        self.repo.set_npc_event(name, text)
        self.record("social:reject", name, text, status="rejected",
                    day=str(inv.get("day") or ""),
                    extra={"kind": inv.get("kind"), "reason": why},
                    topic=str(inv.get("topic") or ""))
        self._diary(text, name)
        self.clear_invite()
        self.mark_resolved(inv.get("token"))
        return {"ok": True, "npc": name, "note": text, "line": text, "reason": why,
                "invite": inv, "steps": {"wallet": False, "mood": True}}

    def expire_invite(self, now=None):
        inv = self.pending_invite()
        if not inv:
            return None
        now = now or _dt.datetime.now()
        if float(now.timestamp()) < float(inv.get("expires") or 0):
            return None
        self.record("social:expire", str(inv.get("npc") or ""), "", status="expired",
                    day=str(inv.get("day") or ""), extra={"kind": inv.get("kind")},
                    topic=str(inv.get("topic") or ""))
        self.clear_invite()
        return {"ok": False, "reason": "expired", "invite": inv}

    # ---------- 立刻发生、不需要她决定的事 ----------
    async def _narrate(self, facts, fallback):
        text = ""
        try:
            try:
                from .social_ai import narrate
            except ImportError:
                from services.social_ai import narrate
            text = await narrate(self.plugin, facts, fallback)
        except Exception as exc:
            isolated("social.narrate", exc)
        return humanize_numbers(npc_roster.sanitize(text, str((facts or {}).get("npc") or ""))) or fallback

    async def red_packet(self):
        self.ensure_seed()
        day = _dt.date.today().isoformat()
        key = "social:red:" + day
        if self.repo is None or self.repo.task_done(key):
            return {"ok": False, "reason": "done"}
        amount = int(stable_int("red:" + day, RED_RANGE[0], RED_RANGE[1]))
        res = await self.finance.income(amount, "妈妈发红包", key)
        if not (isinstance(res, dict) and res.get("ok", True)):
            return {"ok": False, "reason": "failed"}
        self.adjust("妈妈", 3)
        self._mood({"开心": 10, "依恋": 6}, key)
        fallback = self.fallback_line("social:red", npc="妈妈", amount_cents=amount)
        text = await self._narrate({"action": "red_packet", "npc": "妈妈",
                                    "amount": human_money(amount)}, fallback)
        self.repo.set_npc_event("妈妈", text)
        self.record("social:red", "妈妈", text, status="received", amount_cents=amount, day=day)
        self._diary(text, "妈妈")
        self.repo.mark_task(key, "red_packet")
        return {"ok": True, "amount_cents": amount, "text": text}

    async def criticism(self, note=""):
        self.ensure_seed()
        day = _dt.date.today().isoformat()
        row = (self.by_circle("work") or [{}])[0]
        name = str(row.get("name") or "季博达")
        self._mood({"委屈": 10, "郁闷": 6}, "social:criticism:" + day)
        fallback = self.fallback_line("social:criticism", npc=name)
        text = humanize_numbers(npc_roster.sanitize(note, name))
        if not text:
            text = await self._narrate({"action": "criticism", "npc": name,
                                        "npc_status": str(row.get("status") or "")}, fallback)
        self.repo.set_npc_event(name, text)
        self.record("social:criticism", name, text, status="happened", day=day)
        self._diary(text, name)
        return {"ok": True, "npc": name, "text": text}

    # ---------- 旧入口：改成「只抛邀约」，绝不自动扣款 ----------
    async def gift_money(self, name=None, amount_cents=None):
        return await self.create_invite(npc=name or "", kind="ceremony", amount_cents=amount_cents or 0)

    async def dinner(self, name=None):
        return await self.create_invite(npc=name or "", kind="meal")

    async def borrow_request(self, name="柳如烟", amount_cents=None, days=BORROW_DAYS):
        return await self.create_invite(npc=name or "", kind="borrow", amount_cents=amount_cents or 0)

    # ---------- 定时 ----------
    async def decide_with_llm(self, invite, facts):
        try:
            from .social_ai import respond
        except ImportError:
            from services.social_ai import respond
        return await respond(self.plugin, invite, facts)

    async def _ask(self, decider, invite, facts):
        if decider is None:
            decider = self.decide_with_llm
        try:
            return await decider(invite, facts)
        except Exception as exc:
            isolated("social.decide", exc)
            return None

    async def _apply(self, invite, verdict, out):
        if not isinstance(verdict, dict):
            return None
        ask = humanize_numbers(npc_roster.sanitize(verdict.get("ask") or "",
                                                   str(invite.get("npc") or "")))
        if ask:
            invite["ask"] = ask[:60]
            self.set_invite(invite)
        action = str(verdict.get("action") or "").strip().lower()
        if action == "accept":
            result = await self.accept_invite(
                amount_cents=int(round(float(verdict.get("amount") or 0) * 100)),
                note=verdict.get("note") or "", reason=verdict.get("reason") or "",
                about_user=verdict.get("about_user") or "",
                ledger_line=verdict.get("ledger_line") or "")
        elif action == "reject":
            result = await self.reject_invite(reason=verdict.get("reason") or "",
                                              note=verdict.get("note") or "")
        else:
            return None
        out["events"].append(result)
        if result.get("ok") and result.get("line"):
            out["push"].append(str(result["line"]))
        return result

    async def scheduled(self, now=None, decider=None, composer=None):
        now = now or _dt.datetime.now()
        out = {"ok": True, "events": [], "push": []}
        self.ensure_seed()
        expired = self.expire_invite(now)
        if expired:
            out["events"].append(expired)
        busy = self.busy_state(now)
        if busy:
            # 约会中 / 睡觉中：手机静音，不抛新邀约、也不替她回消息
            out["busy"] = busy
            pending = self.pending_invite()
            if pending:
                out["pending"] = pending
            await self.repay_tick(now)
            return out
        pending = self.pending_invite()
        if pending:
            out["pending"] = pending
            verdict = await self._ask(decider, pending, self.invite_facts(pending, now))
            if verdict:
                out["decision"] = await self._apply(pending, verdict, out)
            await self.repay_tick(now)
            return out
        if not self.can_invite(now)[0]:
            await self.repay_tick(now)
            return out
        made = await self.create_invite(now, composer=composer)
        if not made.get("ok"):
            out["refused"] = made.get("reason")
            await self.repay_tick(now)
            return out
        invite = made["invite"]
        out["invite"] = invite
        verdict = await self._ask(decider, invite, self.invite_facts(invite, now))
        if verdict:
            out["decision"] = await self._apply(invite, verdict, out)
        await self.repay_tick(now)
        return out

    async def repay_tick(self, today=None):
        if self.repo is None or self.finance is None:
            return
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
                fallback = self.fallback_line("social:repay", npc=npc, amount_cents=amount)
                text = await self._narrate({"action": "repay", "npc": npc,
                                            "amount": human_money(amount)}, fallback)
                self.repo.set_npc_event(npc, text)
                self.record("social:repay", npc, text, status="paid", amount_cents=amount, day=today)
                self._diary(text, npc)
            else:
                self.repo.settle_receivable(row["id"], "defaulted")
                self.finance.nudge_mood({"生气": 12, "委屈": 10}, key + ":mood")
                fallback = self.fallback_line("social:default", npc=npc, amount_cents=amount)
                text = await self._narrate({"action": "default", "npc": npc,
                                            "amount": human_money(amount)}, fallback)
                self.repo.set_npc_event(npc, text)
                self.record("social:default", npc, text, status="defaulted", amount_cents=amount, day=today)
                self._diary(text, npc)
                await self._memory(self._sentence(text), key + ":mem")
            self.repo.mark_task(key, "repay")


def build_social(plugin):
    return SocialEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None))
