# -*- coding: utf-8 -*-
"""约会纯规则（无 IO / 不读 amo / 不写库）。

Phase 14 从 date_engine 抽出来的「只算不做」的部分：
- 关键词识别：赴约 / 中断 / 收尾 / 暂停 / 恢复 / 同意收尾；
- 时间：自然语言 -> 具体时间、具体时间 -> 人话、时段词；
- 日程：约会块的截断与残留判定、撞车判定、买单模式、基础花费。

副作用（写日程 / 情绪 / 记忆 / 日记 / 会话 API）全部留在 date_engine。
"""
from __future__ import annotations

import datetime as _dt
import re

CANCEL_WORDS = ("去不了", "去不成", "不去了", "取消约会", "放鸽子", "临时加班", "改天吧")
ARRIVAL_WORDS = ("好", "行", "嗯", "到了", "来了", "来啦", "马上到", "等我", "出发了",
                 "在路上了", "在路上", "到楼下", "到门口", "ok", "OK")
INTERRUPT_WORDS = ("老板叫我", "老板叫", "领导叫", "加班", "得走了", "得回去", "先走了",
                   "临时有事", "公司有事", "走不开", "对不起", "改天")
#: 2026-11-16：B 方案——约会当天可以面对面；约会一结束他就不在她身边，各回各家。
#: 收尾是「该回去了 / 她自己先回去」，不是两个人回同一个家；「我走了 / 分开吧」仍不触发收尾。
END_WORDS = ("吃好了", "吃完了", "吃饱了", "吃饱喝足", "结账吧", "我们回家", "回家吧",
             "该回家了", "一起回家", "走回家", "太晚了", "回去休息", "不早了")

#: 她先提出「该回家了」，他只用短肯定词同意；这些不算独立的结束词，必须结合 date_ending 判断
AGREE_EXACT = ("好", "好啊", "好呀", "好的", "行", "行啊", "嗯", "嗯嗯", "可以", "ok", "OK",
               "走吧", "走呗", "回家", "回家吧", "回去吧", "回吧", "那就回", "听你的", "好那就回家")

GENERIC_YES = ("好", "行", "嗯", "ok", "OK", "好的", "好呀", "好滴")

#: 2026-11-15：约会中途短暂离开（洗手间/接电话）绝不能算结束
PAUSE_WORDS = ("洗手间", "去个厕所", "上个厕所", "接个电话", "接电话", "去趟卫生间",
               "去个卫生间", "买包烟", "买个东西马上")
RESUME_WORDS = ("我回来了", "回来了", "回来啦", "我好了", "久等了")
PAUSE_MAX_MINUTES = 15

BASE_COST = (("火锅", 30000), ("烤肉", 28000), ("电影", 12000),
             ("游乐园", 25000), ("咖啡", 6000))
DEFAULT_COST = 20000


#: 饭点占位窗口（宽）：决定「这一顿有没有着落」，与窄窗口（几点吃）分工不同。
#: 陪伴侧 amo_food_cost.CLAIM_WINDOWS 必须同口径（tests/test_meal_claim_contract.py 比对）。
CLAIM_WINDOWS = {
    "breakfast": (6 * 60, 10 * 60),
    "lunch": (10 * 60, 15 * 60),
    "dinner": (16 * 60, 22 * 60),
}

#: 算「一顿饭」的活动词。不收单字「吃」：16:30 的「吃点东西」不能占掉晚饭。
#: 必须与陪伴侧 amo_food_cost.CLAIM_WORDS 逐字一致（契约测试比对）。
CLAIM_WORDS = ("饭", "餐", "面条", "牛肉面", "米饭", "快餐", "包子", "豆浆", "白粥",
               "粥", "外卖", "家常菜", "泡面", "饺子", "汉堡", "披萨", "烧烤", "肠粉",
               "米粉", "火锅", "烤肉", "聚餐", "夜宵", "宵夜", "麻辣烫", "小龙虾", "寿司")

#: 明确指向「你/咱们」的邀请词；带这些词才算他在约她。
INVITE_TOGETHER = ("一起", "一块", "咱俩", "咱们", "陪我", "带你", "请你", "约你",
                   "和你", "跟你", "我们去", "要不要", "去不去", "好不好", "怎么样")

#: 过去时/问近况（「你吃饭了吗」「吃了没」），不是邀约。
INVITE_PAST = ("了吗", "了没", "了没有", "吃过没", "吃了吗", "有没有吃", "吃了没")

#: 陈述句特征：他在说自己/别人去吃饭，不是约她。
INVITE_SELF_STATEMENT = ("我也", "我今天", "我这", "我先", "我在", "你们", "他们",
                         "你慢慢", "你自己", "我马上")


def is_meal_text(text, category=""):
    """category=meal 的块本身就是一顿；social/date 只有命中餐饮词才算。"""
    if str(category or "") == "meal":
        return True
    body = str(text or "")
    return any(word in body for word in CLAIM_WORDS)


def slot_of_minute(minute):
    """分钟数 → 饭点名；落在加餐空档（15-16、22-24）返回空串。"""
    for slot, (low, high) in CLAIM_WINDOWS.items():
        if int(low) <= int(minute) < int(high):
            return slot
    return ""


def claim_slot(when_dt):
    """datetime → 饭点名。"""
    return slot_of_minute(int(when_dt.hour) * 60 + int(when_dt.minute))


def slot_of_hhmm(text):
    """HH:MM → 饭点名；格式不对返回空串。"""
    minute = clock_minutes(text)
    return "" if minute is None else slot_of_minute(minute)


def looks_like_date_invite(text):
    """他这句话是不是在约她（2026-11-20）。

    现场：他说「你慢慢吃，我也去吃饭了」，旧逻辑同时命中 foods(吃饭) 和
    trigger(去吃)，直接建了一场 20:39 的约会。这里要求：要么带「一起/咱们/
    陪你」这类指向词，要么是祈使/疑问句；他在说自己或别人的陈述句一律不算。
    """
    body = str(text or "").strip()
    if not body:
        return False
    if any(word in body for word in INVITE_PAST):
        return False  # 「你吃饭了吗」是在问近况
    together = any(word in body for word in INVITE_TOGETHER)
    if any(word in body for word in INVITE_SELF_STATEMENT) and not together:
        return False
    if together:
        return True
    tail = body.rstrip("。！!")
    if body.endswith(("吧", "吗", "呢", "？", "?")):
        return True
    # 祈使：「去吃火锅」「吃火锅去」「好的，吃火锅」
    if body.startswith(("去", "走", "来")) or tail.endswith(("去", "走")):
        return True
    if body.startswith(("好", "行", "可以", "嗯")) and any(word in body for word in ("吃", "饭")):
        return True
    return False


def compact(text):
    return re.sub(r"[\s，。！？!?、~～…,.]+", "", str(text or ""))


def is_arrival(text):
    """等待赴约时，极短肯定词直接视为“他到了”。"""
    body = compact(text)
    if not body or len(body) > 10:
        return False
    if body in GENERIC_YES:
        return True
    return any(word in body for word in ARRIVAL_WORDS if word not in GENERIC_YES)


def is_interrupt(text):
    body = str(text or "")
    return any(word in body for word in INTERRUPT_WORDS) and any(
        word in body for word in ("走", "加班", "回去", "有事", "对不起"))


def is_date_end(text):
    body = str(text or "")
    return any(word in body for word in END_WORDS)


def is_date_pause(text):
    """中途离开一会儿：不算结束。"""
    body = str(text or "")
    if is_date_end(body):
        return False
    return any(word in body for word in PAUSE_WORDS)


def is_date_resume(text):
    body = str(text or "")
    return any(word in body for word in RESUME_WORDS)


def is_agreement(text):
    """极短肯定词（是否算同意收尾还要看 date_ending，由 engine 判定）。"""
    body = compact(text)
    return bool(body) and len(body) <= 8 and body in AGREE_EXACT


def minutes(hhmm, default=0):
    try:
        hour, minute = str(hhmm or "").split(":")[:2]
        return int(hour) * 60 + int(minute)
    except Exception:
        return int(default)


def clock_minutes(value):
    """HH:MM -> 分钟；格式不对返回 None（约会撞车判定用）。"""
    try:
        hour, _, minute = str(value or "").partition(":")
        return int(hour) * 60 + int(minute or 0)
    except (TypeError, ValueError):
        return None


def parse_when(text, now=None):
    """把「晚点 / 今晚 / 明天傍晚 / 18:27」解析成具体时间。

    2026-11-15 用户要求：只要没说「明天」，且今天时间还允许，就必须定在今天；
    以前「没解析出来 → 明天」「时间已过 → 明天」会把「搞完就出发」直接顺延到第二天。
    """
    now = now or _dt.datetime.now()
    body = str(text or "").strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", body)
    if match:
        return _dt.datetime(*[int(x) for x in match.groups()])

    tomorrow = any(word in body for word in ("明天", "明晚", "明早", "明日"))
    base = now + _dt.timedelta(days=1) if tomorrow else now
    target = None
    match = re.search(r"(\d{1,2})[:：](\d{2})", body)
    explicit_clock = bool(match)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        for word, hour, minute in (
            ("凌晨", 0, 30), ("早上", 8, 0), ("上午", 10, 0), ("中午", 12, 0),
            ("下午", 15, 0), ("傍晚", 18, 0), ("晚上", 19, 30), ("深夜", 23, 0),
        ):
            if word in body:
                target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                break
    if target is None:
        # 没给具体时间（晚点/搞完就出发/稍后）：说「明天」才顺延，否则就是今天 90 分钟后
        return now + _dt.timedelta(days=1) if tomorrow else now + _dt.timedelta(minutes=90)
    if not tomorrow and target <= now:
        # 刚过几分钟（模型给的「现在/稍后」时间点）→ 仍算今天，顺延到 30 分钟后；
        # 已经过了超过半小时（比如晚上说「中午」）→ 才顺延到明天同一时间。
        if explicit_clock and (now - target) <= _dt.timedelta(minutes=30):
            target = now + _dt.timedelta(minutes=30)
        else:
            target = target + _dt.timedelta(days=1)
    return target


def _talk_period(hour):
    if 5 <= hour < 11:
        return "早上"
    if 11 <= hour < 13:
        return "中午"
    if 13 <= hour < 17:
        return "下午"
    if 17 <= hour < 18:
        return "傍晚"
    if 18 <= hour < 23:
        return "晚上"
    return "夜里"


def human_when(when_dt, now=None):
    """把时间说成人话（今晚六点半左右 / 明天傍晚六点左右），绝不输出 11-16 18:27。"""
    now = now or _dt.datetime.now()
    days = (when_dt.date() - now.date()).days
    day_word = "今天" if days <= 0 else ("明天" if days == 1 else "那天")
    hour, minute = int(when_dt.hour), int(when_dt.minute)
    period = _talk_period(hour)
    total = int(round((hour * 60 + minute) / 30.0) * 30) % (24 * 60)
    hh, mm = divmod(total, 60)
    hour_words = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六",
                  7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}
    if hh == 0:
        clock = "零点"
    else:
        display = hh if hh <= 12 else hh - 12
        clock = hour_words.get(display, str(display)) + "点"
    clock += "半" if mm == 30 else ("整" if mm == 0 else "多")
    if day_word == "今天" and period in ("晚上", "夜里"):
        return "今晚" + clock + "左右"
    if day_word == "今天":
        return "今天" + period + clock + "左右"
    return day_word + period + clock + "左右"


def human_day_period(when_dt, now=None):
    """「明天下午 / 今晚」这种只到时段的说法，用来跟老公商量换时间。"""
    now = now or _dt.datetime.now()
    days = (when_dt.date() - now.date()).days
    day_word = "今天" if days <= 0 else ("明天" if days == 1 else "那天")
    period = _talk_period(int(when_dt.hour))
    return "今晚" if (day_word == "今天" and period in ("晚上", "夜里")) else day_word + period


def meeting_period(hour):
    """赴约回忆用的时段（早上/中午/下午/晚上/深夜），和 human_when 的口语时段不同。"""
    if 5 <= hour < 11:
        return "早上"
    if 11 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


def pick_pending(rows, title=""):
    """从约会列表里挑这次要处理的 pending 条目（纯函数）。"""
    pending = [row for row in (rows or []) if row.get("status") == "pending"]
    if not pending:
        return None
    text = str(title or "")
    for row in reversed(pending):
        if text and text in str(row.get("title") or ""):
            return row
    return pending[-1] if not text or len(pending) == 1 else None


def base_cost(title):
    for word, cents in BASE_COST:
        if word in str(title or ""):
            return cents
    return DEFAULT_COST


def pay_mode(mode, fund, title, total):
    """买单模式：她请客 / AA / 火锅基金兜底，默认她买单。"""
    kind = str(mode or "").strip().lower()
    proud = kind in ("b", "她请客", "我请客", "富婆")
    if proud:
        return total, "her", True
    if kind in ("c", "aa"):
        return total // 2, "aa", False
    if fund >= base_cost(title):
        return max(0, total - fund), "host", False
    return total, "her", False


def is_date_block(row):
    """是不是「和老公」约会块（约会日程的唯一标记）。"""
    return str((row or {}).get("activity") or "").startswith("和老公")


def truncate_finished_blocks(rows, now):
    """约会结束时的日程修补：进行中的截断到「现在」，还没开始的直接去掉。

    历史 bug：make_date 先写了一条 18:27-20:27 的「和老公」块，赴约时又写一条
    19:30-21:30，收尾只截断「开始时间 <= 现在 < 结束时间」的块；提前结束（现在
    还没到 18:27）时两条都留着，约会结束后日程端仍挂着「和老公」。
    返回 (新列表, 是否有改动)；其它社交块保持原规则（进行中截断到 now）。
    """
    now_minute = now.hour * 60 + now.minute
    hhmm = now.strftime("%H:%M")
    kept, changed = [], False
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        begin = minutes(row.get("start"), 0)
        finish = minutes(row.get("end"), 0)
        social = str(row.get("category") or "") == "social"
        if is_date_block(row) and begin > now_minute:
            changed = True
            continue
        if (is_date_block(row) or social) and begin <= now_minute < finish:
            row["end"] = hhmm
            changed = True
        kept.append(row)
    return kept, changed


def schedule_conflict(items, when_dt, skip_categories=("social", "rest")):
    """约会时段撞上上班/看病/睡觉 → 返回冲突的活动名（前 14 字），否则空串。"""
    start = int(when_dt.hour) * 60 + int(when_dt.minute)
    end = start + 120
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("category") or "") in skip_categories:
            continue
        begin, finish = clock_minutes(item.get("start")), clock_minutes(item.get("end"))
        if begin is None or finish is None:
            continue
        if finish <= begin:
            finish = 24 * 60
        if begin < end and start < finish:
            text = str(item.get("activity") or item.get("place") or "有事").strip()
            return text[:14]
    return ""
