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
import logging
import re
import time
import uuid

try:
    from ..core.amo_bridge import AmoBridge
    from ..guard import isolated
    from ..utils.helpers import stable_int
    from ..utils.humanize import human_money, humanize_numbers
    from . import npc_roster
    from .date_rules import CLAIM_WINDOWS, is_meal_text, slot_of_hhmm
    from .social_rules import (INVITE_TTL_HOURS, INVITE_WINDOW, MAX_PER_DAY, MIN_GAP_HOURS,
                               PENDING, align_topic_place, can_invite_decision, common_sense,
                               day_label, find_free_day, invite_idem, invite_status, is_off_day,
                               live_invite, minutes, parse_when, relative_label,
                               retime_ask, same_invite)
except ImportError:  # 按顶层包/单文件加载时
    from core.amo_bridge import AmoBridge
    from guard import isolated
    from utils.helpers import stable_int
    from utils.humanize import human_money, humanize_numbers
    from services import npc_roster
    from services.date_rules import CLAIM_WINDOWS, is_meal_text, slot_of_hhmm
    from services.social_rules import (INVITE_TTL_HOURS, INVITE_WINDOW, MAX_PER_DAY,
                                       MIN_GAP_HOURS, PENDING, align_topic_place,
                                       can_invite_decision, common_sense, day_label,
                                       find_free_day, invite_idem, invite_status, is_off_day,
                                       live_invite, minutes, parse_when, relative_label,
                                       retime_ask, same_invite)

logger = logging.getLogger("watch.social")
#: accept_invite 重入锁：同一条邀约不能被面板和 tick 同时处理
_DECIDING: set = set()

#: 好感度分档（2026-11-20 用户口径）：≥60 正常考虑；40-59 一半概率推掉；<40 基本婉拒
AFFINITY_ACCEPT_OK = 60
AFFINITY_HALF_TRIES = 40

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
#: 地点 → zone（改地点时必须同步，否则会出现「家 + restaurant」这种组合）
PLACE_ZONES = {
    "家": "home", "父母家": "home", "妈妈家": "home", "爸爸家": "home",
    "商场": "mall", "楼下小馆": "restaurant", "酒店": "hotel", "公司": "office",
}
KIND_BUDGET = {"meal": 12000, "drink": 8000, "shopping": 25000, "ceremony": 40000,
               "work": 0, "help": 0, "borrow": 60000, "other": 10000}

#: 硬上限：模型给的金额只能是「大概」，不能随口开到天文数字（2026-11-20 用户要求）
KIND_MAX = {"meal": 30000, "drink": 12000, "shopping": 60000, "ceremony": 120000,
            "work": 0, "help": 0, "borrow": 60000, "other": 20000}

#: 对方说「我请 / 请你 / 我买单」= 这顿不花她的钱（2026-11-20 线上：如烟说「我请」却扣了她 200）
TREAT_WORDS = ("我请", "请你", "请你吃", "我买单", "我来买", "我付", "我出钱")

#: 「最近关系动态」只认这些事件；别的模块（基金/股票/工具名）绝不许混进来。
SOCIAL_KIND_PREFIX = "social:"
SOCIAL_KINDS = ("social:invite", "social:accept", "social:reject", "social:expire",
                "social:red", "social:repay", "social:default", "social:criticism")

#: 代码变量名 → 人话；面板上永远不许出现 skip_fund_trade 这种带下划线的函数名
ACTION_WORDS = {
    "skip_fund_trade": "今天不想买基金了",
    "reallocate_funds": "把钱挪去别处了",
    "send_message_to_user": "想跟他说句话",
    "buy_fund": "买了点基金",
    "sell_fund": "卖了点基金",
    "hold": "先观望",
    "deposit": "存了笔钱",
}
_CODE_ONLY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_CODE_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![A-Za-z0-9])")


def is_social_kind(kind):
    """只认 social:* 的互动流水；social:status 是旧版长期状态，不算动态。"""
    k = str(kind or "")
    return k.startswith(SOCIAL_KIND_PREFIX) and k != "social:status"


def _watch_dating(plugin):
    """能陪未就绪时的兜底：守护自己记的赴约状态（同一事实，别判丢）。"""
    try:
        try:
            from . import date_engine
        except ImportError:  # 按顶层包/单文件加载时
            import date_engine
        return bool(date_engine.active_date(plugin))
    except Exception:
        return False


def _code_word(token):
    return ACTION_WORDS.get(str(token or "").strip().strip("_").lower(), "")


#: 她说自己正在生病时的常见说法；只有当前确实健康时才用它拦「病句」。
_SICK_CLAIM = re.compile(
    r"我[^，。；！？]{0,4}(?:发烧|烧着|烧得|低烧|感冒|咳嗽|生病|病倒|还烧)"
    r"|你[^，。；！？]{0,4}(?:发烧|烧着|烧得|低烧|感冒|咳嗽|生病|病倒|还烧)")

#: 老数据迁移标记：第一人称的 last_event 只清一次
LAST_EVENT_MIGRATION = "social:last_event_obj_v1"

#: 能陪 health_bridge 认可的生病状态；这些之外都按健康处理（健康时才拦病句）
SICK_STATUS = ("轻微不适", "感冒", "低烧", "发烧", "肠胃炎")


def claims_sick(text):
    """这句话是不是在说她（我/你）正病着；给健康守卫用，纯函数。"""
    return bool(_SICK_CLAIM.search(str(text or "")))


def _when_label(day, start=""):
    """「今天 18:30」/「11-21 14:00」这种短时间标签。"""
    text = str(day or "")
    label = text[5:] if len(text) >= 10 else text
    try:
        target = _dt.date.fromisoformat(text) if text else None
        if target == _dt.date.today():
            label = "今天"
        elif target == _dt.date.today() + _dt.timedelta(days=1):
            label = "明天"
    except Exception:
        label = text[5:] if len(text) >= 10 else text
    start = str(start or "")
    if label and start:
        return "%s %s" % (label, start)
    return label or start


def objective_last(kind, npc="", topic="", day="", start=""):
    """写进 npc_relations.last_event 的「上一次」：永远是第三人称客观事实。

    2026-11-19 修复：以前这里存的是她第一人称的日记句，下一轮又被当成
    「这个 NPC 最近的状态」喂回模型，模型把里面的「我发烧了」当成她现在的身体，
    健康恢复后仍一直写她生病。客观句既能给面板看懂，也不会再被误读。
    """
    name = str(npc or "")
    what = str(topic or "").strip()
    when = _when_label(day, start)
    tail = " ".join(x for x in (when, what) if x)
    suffix = ("（%s）" % tail) if tail else ""
    k = str(kind or "")
    if k.endswith(":accept"):
        return "她答应了%s的邀约%s" % (name, suffix)
    if k.endswith(":reject"):
        return "她婉拒了%s的邀约%s" % (name, suffix)
    if k.endswith(":red"):
        return "%s给她发了个红包" % name
    if k.endswith(":repay"):
        return "%s把钱还了" % name
    if k.endswith(":default"):
        return "%s借的钱一直没还" % name
    if k.endswith(":criticism"):
        return "%s批评了她" % name
    return ("%s那边有点动静" % name) if name else ""


def plan_line(npc, day="", start="", topic="", place=""):
    """刚答应时她说的那句：说的是「接下来要去」，不是「已经去过」。"""
    when = _when_label(day, start)
    topic = str(topic or "").strip()
    place = str(place or "").strip()
    if topic:
        target = topic if topic.startswith("去") else "去" + topic
    elif place:
        target = "去" + place
    else:
        target = "见个面"
    if when:
        return "答应了%s，%s %s" % (npc, when, target)
    return "答应了%s，回头%s" % (npc, target)


def _period_word(start):
    """把开始时间翻成她跟老公开口时会用的时段词；没有时间就不加。"""
    text = str(start or "").strip()
    if not text:
        return ""
    clock = minutes(text)
    if clock < 11 * 60 + 30:
        return "上午"
    if clock < 13 * 60 + 30:
        return "中午"
    if clock < 17 * 60 + 30:
        return "下午"
    if clock < 23 * 60:
        return "晚上"
    return "夜里"


def relay_line(npc, action="accept", day="", start="", topic="", place="", now=None):
    """给老公的转述句：只说「谁约我什么、我答没答应」。

    2026-11-22 11:31 线上串台：妈妈约她中午回家吃饭，她回妈妈
    「妈，我地铁上呢……我马上到」，这句（accept_invite 的 line）被 _apply
    原样塞进 out["push"]，jobs 又 request_push 给老公——收到的是她对妈妈说的话。
    回 NPC 的微信和给老公的话是两个方向，绝不能复用；这里只生成第三人称事实转述。
    """
    now = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    name = str(npc or "").strip() or "朋友"
    when = ""
    day_text = str(day or "").strip()
    if day_text:
        try:
            when = relative_label(_dt.date.fromisoformat(day_text), now)
        except (TypeError, ValueError):
            when = day_text[5:] if len(day_text) >= 10 else day_text
    when += _period_word(start)
    what = str(topic or "").strip()
    if not what:
        where = str(place or "").strip()
        what = ("去" + where) if where else "见个面"
    verb = "，我答应了" if str(action or "").strip().lower() == "accept" else "，我婉拒了"
    said = humanize_numbers(npc_roster.sanitize("老公，%s约我%s%s%s" % (name, when, what, verb), name))
    if said:
        return said
    # 标题里真夹了名单外的名字时 sanitize 会整句拦掉：退到只含名单内名字的最小转述
    return humanize_numbers(npc_roster.sanitize("老公，%s约我%s%s" % (name, when, verb), name))


def _dynamic_text(kind, payload, now=None):
    """动态正文取哪一句：答应/赴约这类「未来的事」，到点前只说约定，事后才说回忆。"""
    k = str(kind or "")
    said = str(payload.get("text") or "").strip()
    note = str(payload.get("note") or "").strip()
    fallback = str(payload.get("fallback") or "").strip()
    if k.endswith(":accept"):
        if payload.get("cancelled"):
            return "本来约了%s，后来没去成" % (str(payload.get("npc") or "") or "朋友")
        day = str(payload.get("day") or "")
        end = str(payload.get("end") or payload.get("start") or "")
        until = _ts(day, end, 0.0) if (day and end) else 0.0
        current = float(now if now is not None else time.time())
        if until and current < until:
            return said or plan_line(str(payload.get("npc") or ""), day,
                                     str(payload.get("start") or ""),
                                     str(payload.get("topic") or ""),
                                     str(payload.get("place") or ""))
        return note or said or fallback
    return said or note or fallback


def cap_budget(kind, cents):
    """金额硬上限：模型给多少都要落在 KIND_MAX 里。"""
    try:
        value = max(0, int(cents or 0))
    except (TypeError, ValueError):
        value = 0
    return min(value, int(KIND_MAX.get(str(kind or ""), 20000)))


def grounded_ask(ask, npc, status, topic):
    """把「对方自己的处境」安到她头上的邀约文案打回去。

    2026-11-20 线上：如烟自己的状态是「跟男朋友冷战了三天」，
    发出来的微信却是「听说你跟对象冷战好几天了」——主语说反了。
    """
    text = str(ask or "").strip()
    status = str(status or "").strip()
    if not text or not status:
        return text
    for word in ("冷战", "发烧", "感冒", "失恋", "生病", "住院", "相亲", "辞职", "离婚"):
        if word in status and re.search(r"你[^，。！？\s]{0,8}" + re.escape(word), text):
            base = status.strip("。！？ ")
            target = str(topic or "").strip() or "见个面"
            return "最近%s，有空一起%s吗？" % (base, target)
    return text


def when_text(day, start, created_at=None):
    """卡片上显示「什么时候的事」：未来的约显示约定时间，不是答应的时间。"""
    day = str(day or "")
    start = str(start or "")
    if day and start:
        return "%s %s" % (day[5:], start)
    try:
        created = float(created_at or 0)
    except (TypeError, ValueError):
        created = 0.0
    if not created:
        return ""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(created))
    except (ValueError, OSError):
        return ""


def humanize_dynamic(kind, payload, now=None):
    """把一条动态翻成人话；翻不出来就返回空串（宁可少一条也不露代码）。"""
    payload = payload if isinstance(payload, dict) else {}
    raw = _dynamic_text(kind, payload, now)
    status = str(payload.get("status") or "").strip()
    text = humanize_numbers(raw).strip()
    if not text or _CODE_ONLY.match(text):
        text = _code_word(text) or _code_word(status)
    else:
        text = _CODE_TOKEN.sub(lambda m: _code_word(m.group(0)), text).strip()
    if not text or _CODE_ONLY.match(text) or _CODE_TOKEN.search(text):
        return ""
    return text


def panel_events(rows, limit=None):
    """接口层兜底：只留 social:* 事件，并把代码词洗掉；洗不干净的直接不返回。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not is_social_kind(row.get("kind")):
            continue
        payload = row.get("payload") or {}
        if isinstance(payload, dict) and not payload.get("day") and row.get("day"):
            payload = dict(payload)
            payload["day"] = str(row.get("day"))
        text = humanize_dynamic(row.get("kind"), payload)
        if not text:
            text = str(row.get("display") or "").strip()
            if _CODE_ONLY.match(text) or _CODE_TOKEN.search(text):
                text = ""
        if not text:
            continue
        row = dict(row)
        row["display"] = text
        row["when_text"] = when_text(payload.get("day"), payload.get("start"),
                                     row.get("created_at"))
        row["created_text"] = when_text("", "", row.get("created_at"))
        out.append(row)
        if limit and len(out) >= int(limit):
            break
    return out

#: 纯规则在 services/social_rules.py；这里保留旧名，老代码/测试继续可用。
_minutes = minutes
_day_label = day_label
_off_day = is_off_day
_relative_label = relative_label
_retime_ask = retime_ask
RED_RANGE = (50000, 200000)
BORROW_DAYS = 30


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
        self._inviting = False

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
        self._migrate_last_event()
        try:
            npc_roster.refresh_status(self.repo)
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

    def _migrate_last_event(self):
        """老数据里的 last_event 是她第一人称的日记句，会被 compose 当成她现在的状态。

        2026-11-19：带「我」的旧文本只清一次（面板回落 NPC 自己的 status），
        之后新写入的 last_event 全是第三人称客观句；清过就不再重复扫。
        """
        if self.repo is None:
            return 0
        try:
            if str(self.repo.meta_get(LAST_EVENT_MIGRATION, "")) == "1":
                return 0
        except Exception:
            return 0
        try:
            rows = self.repo.npcs()
        except Exception as exc:
            isolated("social.last_event.read", exc)
            return 0
        cleared = 0
        for row in rows:
            text = str((row or {}).get("last_event") or "")
            if not text or "我" not in text:
                continue
            try:
                self.repo.set_npc_event(str(row.get("name") or ""), "")
                cleared += 1
            except Exception as exc:
                isolated("social.last_event.migrate", exc)
        try:
            self.repo.meta_set(LAST_EVENT_MIGRATION, "1")
        except Exception as exc:
            isolated("social.last_event.flag", exc)
        if cleared:
            logger.info("[WATCH] 旧 last_event 已清成第三人称：%d 条", cleared)
        return cleared

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
                row["name"], self._relation_of(row["name"]), row["affinity"],
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
        if not is_social_kind(kind):
            isolated("social.record.reject", ValueError("非社交事件禁止写入: %s" % str(kind)[:40]))
            return False
        if self.repo is None:
            return False
        clean = humanize_numbers(npc_roster.sanitize(text, npc))
        payload = dict(extra or {})
        # 事件日期必须进 payload：面板靠 payload.day 判断「到点前只说约定、过后才说回忆」，
        # 只写进 events.day 列的话（2026-11-20 那场火锅）会当场就显示成已经吃完了。
        if day and not payload.get("day"):
            payload["day"] = str(day)
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
                if is_social_kind(row.get("kind"))
                and float(row.get("created_at") or 0) >= cutoff][:limit]
        out = []
        for row in rows:
            payload = row.get("payload") or {}
            if isinstance(payload, dict) and not payload.get("day") and row.get("day"):
                payload = dict(payload)
                payload["day"] = str(row.get("day"))
            text = humanize_dynamic(row.get("kind"), payload)
            if not text:
                continue
            row["display"] = text
            row["when_text"] = when_text(payload.get("day"), payload.get("start"),
                                         row.get("created_at"))
            row["created_text"] = when_text("", "", row.get("created_at"))
            out.append(row)
        return out

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

    def _available(self):
        """可花的钱（余额减掉已冻结的）；旧钱包没有 available 就退回余额。"""
        try:
            fn = getattr(self.finance, "available_cents", None)
            if callable(fn):
                value = fn()
                if value is not None:
                    return int(value)
        except Exception as exc:
            isolated("social.available", exc)
        return self._cash()

    def _release_charge_hold(self, charge):
        """把答应时冻结的那笔解冻（取消/付不出/过期时）。"""
        hold_idem = str((charge or {}).get("hold_idem") or "")
        if not hold_idem:
            return False
        fn = getattr(self.finance, "release_hold", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(hold_idem))
        except Exception as exc:
            isolated("social.release_hold", exc)
            return False

    def _bridge(self):
        """跨插件调用门面（Phase 14）：不再自己 finance._amo() 后随手调。"""
        return AmoBridge(self.plugin, finance=self.finance, scope="social")

    def busy_state(self, now=None):
        """约会中 / 睡眠中：她把手机静音了，绝不处理第三方社交。"""
        now = now or _dt.datetime.now()
        bridge = self._bridge()
        if bridge.ready:
            if callable(getattr(bridge.resolve(), "is_dating", None)):
                # 约会会话单一源（Phase 2）：只认会话，不再各自读 date_now
                result = bridge.call_compat("is_dating", [((now,), {}), ((), {})])
                if result.ok and result.value:
                    return "dating"
            else:
                date_now = bridge.call("kv_get", "date_now", {}).value or {}
                if isinstance(date_now, dict) and float(date_now.get("until") or 0) > now.timestamp():
                    return "dating"
            act = bridge.call("current_activity").value or {}
            if str(act.get("category") or "") == "sleep":
                return "sleeping"
        # 能陪未就绪时也别把「约会中」判丢：守护自己的赴约状态是同一事实
        if _watch_dating(self.plugin):
            return "dating"
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

    def _her_health(self):
        """她现在的真实身体状态（能陪是唯一权威；读不到就空着，绝不猜）。"""
        amo = None
        try:
            getter = getattr(self.finance, "_amo", None)
            amo = getter() if callable(getter) else None
        except Exception as exc:
            isolated("social.health.amo", exc)
            amo = None
        if amo is None:
            try:
                amo = self._bridge().resolve()
            except Exception as exc:
                isolated("social.health.bridge", exc)
                amo = None
        if amo is None:
            return ""
        try:
            try:
                from .health_bridge import get_sick_status
            except ImportError:
                from services.health_bridge import get_sick_status
            return str(get_sick_status(amo) or "")
        except Exception as exc:
            isolated("social.health.read", exc)
            return ""

    def _relation_of(self, name):
        """关系标签：优先读陪伴（唯一真源），读不到回退本地名单（2026-11-20）。"""
        name = str(name or "")
        bridge = self._bridge()
        if bridge.ready:
            try:
                value = bridge.call("relation_of", name).value
                if value:
                    return str(value)
            except Exception as exc:
                isolated("social.relation.amo", exc)
        return str((npc_roster.BY_NAME.get(name) or {}).get("relation") or "")

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

    def _affinity(self, name):
        """这个 NPC 的好感度；没有记录返回 None（不做闸）。"""
        getter = getattr(self.repo, "npc_get", None)
        if not callable(getter):
            return None
        try:
            row = getter(str(name)) or {}
        except Exception as exc:
            isolated("social.affinity.read", exc)
            return None
        if not row:
            return None
        try:
            value = row.get("affinity")
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    def _male_gate(self, name, kind):
        """男性 NPC 边界：单独吃饭/喝酒/逛街一律不行（2026-11-20）。"""
        kind = str(kind or "")
        if npc_roster.is_male(name) and kind not in npc_roster.MALE_SAFE_KINDS:
            return "他是男生，只能谈工作或普通社交，单独出去不合适"
        return ""

    def _accept_gate(self, name, kind, day=""):
        """接受新邀约前的硬闸：男性边界 + 好感度分档（2026-11-20）。

        重复接受同一场（already existing）不走好感度闸——那是幂等重试，不是新决定。
        好感度只影响「答不答应邀约」，从不参与称谓。
        """
        male = self._male_gate(name, kind)
        if male:
            return male
        affinity = self._affinity(name)
        if affinity is None:
            return ""
        if affinity >= AFFINITY_ACCEPT_OK:
            return ""
        if affinity >= AFFINITY_HALF_TRIES:
            seed = "affinity:%s:%s" % (name, str(day or _dt.date.today().isoformat()))
            if stable_int(seed, 0, 99) < 50:
                return "今天有点累，不太想去"
            return ""
        return "不太想去"

    def _meal_taken(self, day, start):
        """这个饭点是不是已经有一顿饭（餐饮类邀约互斥用，2026-11-20）。"""
        slot = slot_of_hhmm(start)
        if not slot:
            return ""
        low, high = CLAIM_WINDOWS[slot]
        for row in self._windows(day):
            if str(row.get("category") or "") not in ("meal", "social", "date"):
                continue
            begin = _minutes(row.get("start"))
            finish = _minutes(row.get("end"))
            if finish <= begin:
                continue
            if not (begin < high and low < finish):
                continue
            if is_meal_text(row.get("activity"), row.get("category")):
                return str(row.get("activity") or "")
        return ""

    # ---------- 冷却闸（防止 4 分钟 5 次邀约） ----------
    def can_invite(self, now=None, npc=""):
        now = now or _dt.datetime.now()
        if self.repo is None:
            return False, "not_ready"
        # 冷却闸：过期/已处理的待决邀约先清掉，不然会永远卡在 "pending"
        pending = bool(self._pending_for_decision(now))
        day = now.date().isoformat()
        try:
            count = int(self.repo.meta_get("social:count:" + day, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        try:
            last = float(self.repo.meta_get("social:last_ts", 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        npc_done = bool(npc and self.repo.task_done("social:npc:%s:%s" % (day, npc)))
        return can_invite_decision(pending=pending, hour=now.hour, count=count,
                                   last_ts=last, now_ts=now.timestamp(),
                                   npc_done=npc_done, window=INVITE_WINDOW,
                                   max_per_day=MAX_PER_DAY,
                                   min_gap_hours=MIN_GAP_HOURS)

    def ready(self, now=None):
        return self.can_invite(now)[0]

    def _bump(self, day, npc, now=None, target_day=""):
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
            if target_day and target_day != day:
                # 目标日也要占坑：同一天同一个 NPC 不能再约第二场
                self.repo.mark_task("social:npc:%s:%s" % (target_day, npc), "invited")

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
        health = self._her_health()
        if health:
            her += "，身体%s" % health
        lines = []
        for row in rows:
            # 对方自己的近况（第三人称客观记录）；她的身体只看 her_state 里的真实健康
            lines.append("%s（%s，好感 %s）最近一次：%s" % (row.get("name"), self._relation_of(row.get("name")),
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
        if said_place in PLACE_ZONES:
            place = said_place
            zone = PLACE_ZONES[said_place]   # 2026-11-21：地点改了 zone 必须跟着改
        # 2026-11-21：妈妈/爸爸约饭默认在父母家——LLM 没给地点时不许再排到楼下小馆；
        # 明确给了商场/酒店/公司等则尊重（said_place 非空且不是家）
        if npc in ("妈妈", "爸爸") and kind in ("meal", "drink") and (
                place in ("", "家", "父母家") or not said_place):
            place, zone = "父母家", "home"
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

        # 硬常识 + 冲突改期：纯规则在 services/social_rules.py，engine 只负责落库
        adjusted = common_sense(target, kind, circle, start, end, now)
        if adjusted is None:
            return None
        target, start, end = adjusted
        # 邀约必须落在未来：目标时刻已经过去（或只剩不到 30 分钟）就顺延到第二天
        if start:
            try:
                start_dt = _dt.datetime.combine(target, _dt.time.fromisoformat(start))
                if start_dt <= now + _dt.timedelta(minutes=30):
                    target = target + _dt.timedelta(days=1)
            except (ValueError, TypeError) as exc:
                logger.debug("[社交] 邀约时间解析失败: %r", exc)
        target = find_free_day(target, start, end, self._conflict, kind=kind)
        if target is None:
            return None
        day_text = target.isoformat()
        # 同一个 NPC 在「目标日」只能约一次（不能按发出日算）
        if npc and self.repo.task_done("social:npc:%s:%s" % (day_text, npc)):
            return None
        budget = int(data.get("budget_cents") or 0)
        if budget <= 0:
            budget = KIND_BUDGET.get(kind, 10000)
        budget = cap_budget(kind, budget)
        topic_text = align_topic_place(str(data.get("topic") or "").strip()[:20], place)
        ask = align_topic_place(str(data.get("ask") or ""), place)
        ask = _retime_ask(ask, target, now, start)
        wrong_name = str(data.get("npc") or "").strip()
        if wrong_name and wrong_name != npc:
            ask = ask.replace(wrong_name, npc)
        ask = humanize_numbers(npc_roster.sanitize(ask, npc))
        if not ask:
            return None
        # 对方自己的处境不能安到她头上（「我冷战」被写成「你冷战」）；顺手把「我请」变成不花钱
        ask = grounded_ask(ask, npc, _brief_status(row), topic_text)
        ask = humanize_numbers(npc_roster.sanitize(ask, npc)) or ask
        if any(word in ask for word in TREAT_WORDS):
            budget = 0
        return {
            "token": "soc-%s-%s" % (day_text, uuid.uuid4().hex[:8]),
            "kind": kind, "topic": topic_text,
            "npc": npc, "relation": self._relation_of(npc), "circle": circle,
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
        if getattr(self, "_inviting", False):
            # 两个定时器并发进来时只让一个去问 LLM，避免同一晚抛两条邀约
            return {"ok": False, "reason": "busy_compose"}
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
        self._inviting = True
        try:
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
        finally:
            self._inviting = False
        if invite is None:
            return {"ok": False, "reason": "compose_failed"}
        busy = self.busy_state(now)
        if busy:
            # 约会/睡觉期间：消息先存着，绝不触发她的决策；等状态恢复再处理
            invite["deferred"] = time.time()
            invite["deferred_reason"] = busy
        # 幂等：同一个邀请（同人/同类型/同天同时段）还挂着就不再覆盖、不再记一条新事件
        old = self.pending_invite()
        if old and live_invite(old, now) and same_invite(old, invite):
            return {"ok": False, "reason": "duplicate", "invite": old}
        self.set_invite(invite)
        self.record("social:invite", invite["npc"], invite["ask"], status="pending",
                    amount_cents=invite["amount_cents"], day=day,
                    extra={"kind": invite["kind"], "topic": invite["topic"],
                           "place": invite["place"], "start": invite["start"],
                           "end": invite["end"]},
                    topic=invite["topic"], place=invite["place"],
                    start=invite["start"], end=invite["end"],
                    idem="evt:invite:" + str(invite.get("token") or ""))
        self._bump(day, invite["npc"], now, target_day=str(invite.get("day") or ""))
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
        health = self._her_health()
        if health:
            lines.append("- 你现在的身体：%s（这是真实状态；不是这里写的，就不许说自己生病）" % health)
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
        bridge = self._bridge()
        if not bridge.ready:
            return
        try:
            state = bridge.state()
            items = list((getattr(state, "schedule", {}) or {}).get(day) or [])
            kept = [row for row in items
                    if not (str(row.get("start")) == start and name in str(row.get("activity") or ""))]
            if len(kept) == len(items):
                return
            # 回滚必须无视 locked：这条刚写进去的块带着 locked=True，
            # 走 commit_schedule_change 会被 protect_locked 顶回来（2026-11-20）。
            schedule = dict(bridge.call("kv_get", "schedule", {}).value or {})
            schedule[day] = kept
            bridge.call("kv_set", "schedule", schedule)
            try:
                state.schedule[day] = kept
            except Exception as exc:
                isolated("social.drop_schedule.memory", exc)
        except Exception as exc:
            isolated("social.drop_schedule", exc)

    # ---------- 到点结算：答应时只占日程，到点才真的花钱 ----------
    CHARGE_KEY = "social:charges"
    CHARGE_TTL = 2 * 86400

    def pending_charges(self):
        """还没结算的邀约扣款（kv 持久化，重载/重启都不丢）。"""
        if self.repo is None:
            return []
        try:
            raw = self.repo.meta_get(self.CHARGE_KEY, []) or []
        except Exception as exc:
            isolated("social.charges.read", exc)
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return []
        rows = raw if isinstance(raw, list) else []
        return [dict(item) for item in rows if isinstance(item, dict) and item.get("idem")]

    def _save_charges(self, items):
        if self.repo is None:
            return False
        try:
            return bool(self.repo.meta_set(
                self.CHARGE_KEY,
                json.dumps(list(items)[-60:], ensure_ascii=False)))
        except Exception as exc:
            isolated("social.charges.save", exc)
            return False

    def _queue_charge(self, charge):
        """把「到点再扣」的账记下来；同一场约会永远只有一笔（事件级幂等键）。"""
        idem = str((charge or {}).get("idem") or "")
        if not idem:
            return False
        items = self.pending_charges()
        if any(str(item.get("idem") or "") == idem for item in items):
            return True
        items.append(dict(charge))
        return self._save_charges(items)

    def _scheduled_still_there(self, charge):
        """日程被改口取消/整块换掉，就不该再扣这顿饭钱。"""
        day = str((charge or {}).get("day") or "")
        name = str((charge or {}).get("npc") or "")
        if not day or not name:
            return True
        try:
            rows = self.finance.schedule_windows(day) or []
        except Exception as exc:
            isolated("social.charges.schedule", exc)
            return True
        if not rows:
            return True
        return any(name in str(row.get("activity") or "") for row in rows)

    def _mark_event(self, charge, patch):
        """给事件打状态（occurred / cancelled / paid / unpaid），面板靠它说话。"""
        try:
            event_id = int((charge or {}).get("event_db") or 0)
        except (TypeError, ValueError):
            event_id = 0
        if not event_id or self.repo is None:
            return False
        for row in self.repo.events(60) or []:
            if int(row.get("id") or 0) != event_id:
                continue
            payload = dict(row.get("payload") or {})
            payload.update(patch)
            return bool(self.repo.update_event(event_id, payload))
        return False

    async def settle_charges(self, now=None):
        """到点把答应过的邀约真实扣款；钱不够就付得起的部分、剩下的记成欠人家的。"""
        items = self.pending_charges()
        if not items:
            return {"paid": 0, "pending": 0, "dropped": 0}
        current = float(now if now is not None else time.time())
        keep, paid, dropped = [], 0, 0
        for charge in items:
            try:
                at = float(charge.get("settle_at") or 0)
            except (TypeError, ValueError):
                at = 0.0
            if at > current:
                keep.append(charge)
                continue
            name = str(charge.get("npc") or "朋友")
            reason = str(charge.get("reason") or "社交")
            idem = str(charge.get("idem") or "")
            cost = max(0, int(charge.get("amount_cents") or 0))
            if not self._scheduled_still_there(charge):
                # 她改口不去了：这顿饭不存在，不扣钱，冻结也解开
                self._release_charge_hold(charge)
                self._mark_event(charge, {"cancelled": True})
                dropped += 1
                continue
            # 人去了（就算钱没付成，这件事也发生过）
            self._mark_event(charge, {"occurred": True})
            if cost <= 0:
                paid += 1
                continue
            cash = max(0, self._cash())
            pay = cost if cash >= cost else cash
            if pay <= 0:
                short = cost
                try:
                    self.repo.add_npc_owed(name, short)
                except Exception as exc:
                    isolated("social.owed", exc)
                self._release_charge_hold(charge)
                self._mark_event(charge, {"paid_cents": 0, "unpaid_cents": short})
                self._diary("钱不够，这顿是%s先垫的，欠她 %s。" % (name, human_money(short)), name)
                dropped += 1
                continue
            res = await self.finance.expense(pay, reason, idem, category="社交",
                                             release_hold=str(charge.get("hold_idem") or ""))
            if isinstance(res, dict) and res.get("ok"):
                paid += 1
                if pay < cost:
                    short = cost - pay
                    try:
                        self.repo.add_npc_owed(name, short)
                    except Exception as exc:
                        isolated("social.owed", exc)
                    self._mark_event(charge, {"paid_cents": pay, "unpaid_cents": short})
                    self._diary("钱不够，先付了 %s，剩下 %s 欠%s的。"
                                % (human_money(pay), human_money(short), name), name)
                else:
                    self._mark_event(charge, {"paid_cents": pay})
                continue
            if current > at + self.CHARGE_TTL:
                dropped += 1
                self._release_charge_hold(charge)
                self._diary("本来要请%s的，结果钱不够，这顿最后没付成。" % name, name)
                continue
            keep.append(charge)
        self._save_charges(keep)
        return {"paid": paid, "pending": len(keep), "dropped": dropped}

    async def recollect_past(self, now=None):
        """事件真的结束了，才把她事后的回忆写进长期记忆（不再在答应那一刻写未来）。"""
        current = float(now if now is not None else time.time())
        done = 0
        for row in self.repo.events(60) or []:
            if str(row.get("kind")) != "social:accept":
                continue
            payload = row.get("payload") or {}
            if payload.get("recalled") or payload.get("cancelled") or not payload.get("occurred"):
                continue
            day = str(payload.get("day") or row.get("day") or "")
            end = _ts(day, str(payload.get("end") or ""), 0.0)
            if not end or end > current:
                continue
            name = str(payload.get("npc") or "")
            note = str(payload.get("note") or "").strip()
            about = str(payload.get("about_user") or "").strip()
            text = note
            if about:
                text = (text + " " if text else "") + "她还聊起你：" + about
            try:
                short = int(payload.get("unpaid_cents") or 0)
            except (TypeError, ValueError):
                short = 0
            if short > 0:
                text = ((text + " ") if text else "") + "这顿不够的部分是%s先垫的，欠她 %s。" % (
                    name or "她", human_money(short))
            if text:
                await self._memory(self._sentence(text), "evt:recall:" + str(row.get("id")))
            payload = dict(payload)
            payload["recalled"] = True
            try:
                self.repo.update_event(row.get("id"), payload)
            except Exception as exc:
                isolated("social.recall.mark", exc)
            done += 1
        return {"recalled": done}

    def _pending_for_decision(self, now=None):
        """待回邀约；已处理（resolved）或已过期（expired）的直接清掉，定时器再也扫不到。"""
        inv = self.pending_invite()
        if not inv:
            return None
        if self.is_resolved(inv.get("token")):
            self.clear_invite()
            return None
        if invite_status(inv, now or _dt.datetime.now()) != PENDING:
            self.clear_invite()
            return None
        return inv

    async def accept_invite(self, npc="", place="", amount_cents=0, note="", reason="",
                            start="", end="", day="", token="", about_user="", ledger_line="", now_line="",
                            now=None):
        inv = self._pending_for_decision(now)
        lock = str(token or "") or str((inv or {}).get("token") or "")
        if lock and lock in _DECIDING:
            return {"ok": False, "reason": "busy", "message": "这条邀约正在处理，别重复点"}
        if lock:
            _DECIDING.add(lock)
        try:
            return await self._accept_invite_locked(
                npc=npc, place=place, amount_cents=amount_cents, note=note, reason=reason,
                start=start, end=end, day=day, token=token, about_user=about_user,
                ledger_line=ledger_line, now_line=now_line, now=now)
        finally:
            if lock:
                _DECIDING.discard(lock)

    async def _accept_invite_locked(self, npc="", place="", amount_cents=0, note="", reason="",
                                    start="", end="", day="", token="", about_user="",
                                    ledger_line="", now_line="", now=None):
        inv = self._pending_for_decision(now)
        now_dt = now or _dt.datetime.now()
        if not inv:
            return {"ok": False, "reason": "no_invite", "message": "现在没有待回的邀约"}
        if token and str(token) != str(inv.get("token") or ""):
            return {"ok": False, "reason": "stale", "message": "这条邀约已经过去了"}
        busy = self.busy_state(now_dt)
        if busy:
            return {"ok": False, "reason": "busy",
                    "message": "你正在%s，手机静音着，等忙完再看" % ("约会" if busy == "dating" else "睡觉")}
        name = str(npc or inv.get("npc") or "")
        if name not in npc_roster.NAME_SET:
            return {"ok": False, "reason": "unknown_npc", "message": "这个人不在你的社交圈里"}
        kind = str(inv.get("kind") or "")
        label = str(inv.get("topic") or inv.get("label") or "")
        # 男性 NPC 边界：任何时候都生效（2026-11-20）
        male_gate = self._male_gate(name, kind)
        if male_gate:
            return {"ok": False, "reason": "boundary", "message": male_gate}
        cost = max(0, int(amount_cents or inv.get("amount_cents") or 0))
        # 模型在 accept 时还能改金额：一样要落在硬上限里
        cost = cap_budget(kind, cost)
        cash = self._available()
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
            day_text = now_dt.date().isoformat()
        requested_day = day_text
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
        # 好感度闸：只对新邀约生效；重复接受同一场是幂等重试，不拦（2026-11-20）
        if not existing:
            affinity_gate = self._accept_gate(name, kind, day_text)
            if affinity_gate:
                return {"ok": False, "reason": "boundary", "message": affinity_gate}
        # 餐饮类邀约撞上已排的饭局：婉拒，不静默改到 7 天内某一天（2026-11-20）
        if use_start and not existing and is_meal_text(label):
            clash = self._meal_taken(day_text, use_start)
            if clash:
                return {"ok": False, "reason": "meal_conflict",
                        "message": "那个点已经约好「%s」了，一顿饭不能吃两次" % clash}
        if use_start and not existing:
            shifted = find_free_day(_dt.date.fromisoformat(day_text), use_start, use_end,
                                    self._conflict, max_days=7)
            if shifted is None:
                return {"ok": False, "reason": "conflict",
                        "message": "接下来一周那个时间都排满了，跟她改天吧"}
            day_text = shifted.isoformat()
        # 事件级幂等键：按邀约原始日子算，撞车改期后重试也还是同一把钥匙
        idem = invite_idem(name, str(inv.get("day") or day_text),
                           str(inv.get("start") or use_start))
        thing = "%s%s" % (name, label or "")
        fact_reason = ("借给%s" % name) if kind == "borrow" else thing
        reason_text = humanize_numbers(npc_roster.sanitize(ledger_line, name)) or fact_reason
        steps: dict[str, object] = {}
        sched = {}
        # 到点才真的花钱：答应邀约那一刻只占日程（2026-11-20 用户要求：
        # 「不是决定了 6 点半？为什么现在扣钱？」）。借钱是当场掏现金，仍然立刻扣。
        now_ts = now_dt.timestamp()
        settle_at = _ts(day_text, use_start, 0.0) if use_start else 0.0
        deferred = bool(cost > 0 and kind != "borrow" and settle_at and settle_at > now_ts)
        # 还没到点的约：日程上写「说好要去」，绝不把她事后的回忆（note）提前挂上去
        plan_early = plan_line(name, day_text, use_start, label, place or inv.get("place") or "")
        schedule_detail = plan_early if deferred else str(note or thing)
        if use_start:
            sched = {"day": day_text, "start": use_start, "end": use_end or use_start,
                     "place": place or inv.get("place") or "外面"}
            if not existing:
                written = bool(self.finance.add_schedule(
                    day_text, thing, detail=schedule_detail, place=sched["place"],
                    category="social", start=use_start, end=sched["end"],
                    zone=str(inv.get("zone") or "")))
                if not written:
                    return {"ok": False, "reason": "schedule_failed",
                            "message": "日程没写进去，先别答应；再试一次"}
            steps["schedule"] = True
        charge = None
        if cost <= 0:
            # 对方请客 / 不收钱：照样占日程、写记忆，但钱包一分不动
            steps["wallet"] = "free"
            if use_start and settle_at and settle_at > now_ts:
                # 0 元也排一条：到点才能把「这事真的发生了」记下来，事后才好写回忆
                charge = {"idem": idem, "npc": name, "day": day_text, "start": use_start,
                          "amount_cents": 0, "reason": reason_text, "settle_at": settle_at,
                          "event_idem": "evt:accept:" + str(inv.get("token") or name)}
        elif deferred:
            # 先把这笔钱冻住：余额不变，但到点之前别的花销动不了它（2026-11-20 第七条）
            hold_idem = "hold:" + idem
            if not self.finance.hold(cost, reason_text, hold_idem,
                                     expires_at=settle_at + self.CHARGE_TTL):
                if sched:
                    self._drop_schedule(day_text, use_start, name)
                return {"ok": False, "reason": "insufficient",
                        "message": "这笔钱已经被别的安排占住了，这顿先别答应"}
            steps["held"] = True
            charge = {"idem": idem, "npc": name, "day": day_text, "start": use_start,
                      "amount_cents": cost, "reason": reason_text, "settle_at": settle_at,
                      "hold_idem": hold_idem,
                      "event_idem": "evt:accept:" + str(inv.get("token") or name)}
            steps["wallet"] = "pending"
        else:
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
        place_text = sched.get("place") or inv.get("place") or ""
        health = self._her_health()
        note_text = humanize_numbers(npc_roster.sanitize(note or reason or "", name))
        said = humanize_numbers(npc_roster.sanitize(now_line or "", name))
        if health and health not in SICK_STATUS and claims_sick(said):
            isolated("social.health_claim", RuntimeError("accept-now:" + str(said)[:40]))
            said = ""
        if not said:
            said = plan_line(name, day_text, use_start, label, place_text)
        if health and health not in SICK_STATUS and claims_sick(note_text):
            isolated("social.health_claim", RuntimeError("accept-note:" + str(note_text)[:40]))
            note_text = objective_last("social:accept", name, label, day_text, use_start)
        if not note_text:
            note_text = said
        if requested_day and day_text != requested_day:
            # 改期必须说出来，别让用户以为还是原定那天（2026-11-20）
            try:
                shifted_label = day_label(_dt.date.fromisoformat(day_text))
            except (TypeError, ValueError):
                shifted_label = day_text
            note_text = "改到%s；%s" % (shifted_label, note_text or "")
        about = npc_roster.sanitize(about_user, name)
        if about and not any(word in about for word in ("老公", "他", "你", "亲爱的", "对象")):
            about = ""
        # last_event 只写第三人称客观事实：第一人称日记会被下一次 compose 当成她现在的状态
        self.repo.set_npc_event(name, objective_last("social:accept", name, label,
                                                     day_text, use_start))
        evt_idem = "evt:accept:" + str(inv.get("token") or name)
        self.record("social:accept", name, said, status="accepted", amount_cents=cost,
                    day=day_text,
                    extra={"kind": kind, "place": place_text,
                           "start": use_start, "end": use_end,
                           "topic": label, "about_user": about,
                           "ledger_line": reason_text, "note": note_text},
                    topic=label, place=place_text, start=use_start, end=use_end,
                    idem=evt_idem)
        if charge is not None:
            try:
                charge["event_db"] = int(self.repo.event_id_by_idem(evt_idem) or 0)
            except Exception as exc:
                isolated("social.charge.event", exc)
            steps["charge_queued"] = self._queue_charge(charge)
        plan = plan_line(name, day_text, use_start, label, place_text)
        # 只写「答应了、接下来去」；她自己的事后回忆（note / 她还聊起你）等事件真的结束再写
        memory = self._sentence(plan)
        steps["memory"] = await self._memory(memory, idem + ":mem")
        steps["diary"] = self._diary(memory, name)
        self.clear_invite()
        self.mark_resolved(inv.get("token"))
        return {"ok": True, "npc": name, "cost": cost, "note": note_text, "line": said,
                "plan": plan, "about_user": about, "invite": inv, "schedule": sched,
                "steps": steps}

    async def reject_invite(self, reason="", note="", token="", now=None):
        inv = self._pending_for_decision(now)
        if not inv:
            return {"ok": False, "reason": "no_invite", "message": "现在没有待回的邀约"}
        if token and str(token) != str(inv.get("token") or ""):
            return {"ok": False, "reason": "stale", "message": "这条邀约已经过去了"}
        name = str(inv.get("npc") or "")
        if name not in npc_roster.NAME_SET:
            return {"ok": False, "reason": "unknown_npc", "message": "这个人不在你的社交圈里"}
        why = str(reason or "").strip()
        text = humanize_numbers(npc_roster.sanitize(note or why or "", name))
        health = self._her_health()
        if health and health not in SICK_STATUS and claims_sick(text):
            isolated("social.health_claim", RuntimeError("reject:" + str(text)[:40]))
            text = ""
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
        self.repo.set_npc_event(name, objective_last("social:reject", name,
                                                     str(inv.get("topic") or ""),
                                                     str(inv.get("day") or ""),
                                                     str(inv.get("start") or "")))
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
        self.mark_resolved(inv.get("token"))     # 生命周期统一：过期也是终态
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
        self.repo.set_npc_event("妈妈", objective_last("social:red", "妈妈"))
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
        self.repo.set_npc_event(name, objective_last("social:criticism", name))
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

    async def _apply(self, invite, verdict, out, now=None):
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
                now_line=verdict.get("now") or "",
                about_user=verdict.get("about_user") or "",
                ledger_line=verdict.get("ledger_line") or "", now=now)
        elif action == "reject":
            result = await self.reject_invite(reason=verdict.get("reason") or "",
                                              note=verdict.get("note") or "", now=now)
        else:
            return None
        out["events"].append(result)
        if result.get("ok"):
            # 推给老公的只放「谁约我什么、我答没答应」的第三人称转述；
            # 她回 NPC 的那句（result["line"] / accept_invite 的 said）绝不能原样转给老公。
            sched = result.get("schedule") or {}
            line = relay_line(
                str(invite.get("npc") or ""), action,
                day=str(sched.get("day") or invite.get("day") or ""),
                start=str(sched.get("start") or invite.get("start") or ""),
                topic=str(invite.get("topic") or ""),
                place=str(sched.get("place") or invite.get("place") or ""),
                now=now)
            if line:
                out["push"].append(line)
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
                out["decision"] = await self._apply(pending, verdict, out, now=now)
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
            out["decision"] = await self._apply(invite, verdict, out, now=now)
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
                self.repo.set_npc_event(npc, objective_last("social:repay", npc))
                self.record("social:repay", npc, text, status="paid", amount_cents=amount, day=today)
                self._diary(text, npc)
            else:
                self.repo.settle_receivable(row["id"], "defaulted")
                self.finance.nudge_mood({"生气": 12, "委屈": 10}, key + ":mood")
                fallback = self.fallback_line("social:default", npc=npc, amount_cents=amount)
                text = await self._narrate({"action": "default", "npc": npc,
                                            "amount": human_money(amount)}, fallback)
                self.repo.set_npc_event(npc, objective_last("social:default", npc))
                self.record("social:default", npc, text, status="defaulted", amount_cents=amount, day=today)
                self._diary(text, npc)
                await self._memory(self._sentence(text), key + ":mem")
            self.repo.mark_task(key, "repay")


def build_social(plugin):
    return SocialEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None))
