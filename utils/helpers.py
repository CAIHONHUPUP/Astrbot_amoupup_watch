# -*- coding: utf-8 -*-
"""工具层：纯函数。不碰数据库、不碰网络、不碰文件。

为什么集中在这里：金额单位、城市系数、节假日这三件事，全插件必须只有一个口径。
业务层（services/）只调函数，不再各自写 1.5 / 1.0 / 0.75 或自己四舍五入。

金额一律用「分」的整数表示，避免浮点误差。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from decimal import Decimal, ROUND_HALF_UP

# ---------- 城市等级与物价系数 ----------
CITY_TIER_FACTOR = {"一线": 1.5, "二线": 1.0, "三线": 0.75}
DEFAULT_TIER = "三线"

#: 主流城市 → 等级（生僻城市回落三线；可在 config 里覆盖）
CITY_TIERS = {
    "北京": "一线", "上海": "一线", "广州": "一线", "深圳": "一线",
    "杭州": "二线", "南京": "二线", "苏州": "二线", "无锡": "二线", "宁波": "二线",
    "成都": "二线", "重庆": "二线", "武汉": "二线", "长沙": "二线", "郑州": "二线",
    "西安": "二线", "天津": "二线", "青岛": "二线", "济南": "二线", "合肥": "二线",
    "福州": "二线", "厦门": "二线", "南昌": "二线", "昆明": "二线", "贵阳": "二线",
    "南宁": "二线", "沈阳": "二线", "大连": "二线", "哈尔滨": "二线", "长春": "二线",
    "石家庄": "二线", "太原": "二线", "佛山": "二线", "东莞": "二线",
    "兰州": "三线", "银川": "三线", "西宁": "三线", "乌鲁木齐": "三线",
    "呼和浩特": "三线", "海口": "三线", "三亚": "三线", "珠海": "三线",
    "中山": "三线", "惠州": "三线", "泉州": "三线", "温州": "三线", "常州": "三线",
    "徐州": "三线", "洛阳": "三线", "唐山": "三线", "保定": "三线", "烟台": "三线",
}

#: 二线基准：1.2 万/平米、80 平 → 96 万；一线 ×1.5、三线 ×0.75
HOUSE_PRICE_PER_M2_CENTS = 1200000
HOUSE_AREA_M2 = 80
#: 二线基准房租 1600 元/月
RENT_BASE_CENTS = 160000
#: 代步车 12 万（不分城市）
CAR_PRICE_CENTS = 12000000


def normalize_city(name):
    text = str(name or "").strip()
    for suffix in ("特别行政区", "市", "省", "自治区"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    return text


def city_tier(name):
    """城市 → 一线/二线/三线；不认识的按三线算。"""
    return CITY_TIERS.get(normalize_city(name), DEFAULT_TIER)


def city_factor(name):
    """空城市 = 没设置，按 1.0（二线基准）；设了但不认识 → 三线 0.75。"""
    if not str(name or "").strip():
        return 1.0
    return float(CITY_TIER_FACTOR.get(city_tier(name), CITY_TIER_FACTOR[DEFAULT_TIER]))


def house_price_cents(city=None):
    return int(round(HOUSE_PRICE_PER_M2_CENTS * HOUSE_AREA_M2 * city_factor(city)))


def rent_cents(city=None):
    return int(round(RENT_BASE_CENTS * city_factor(city)))


def car_price_cents(city=None):
    return int(CAR_PRICE_CENTS)


# ---------- 金额（分） ----------
def cents(value):
    """元 → 分（十进制四舍五入，避免 12.345*100=1234.4999… 这种浮点误差）。"""
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def round_cents(value):
    return int(round(float(value)))


def to_yuan(value_cents):
    return int(round(value_cents)) / 100.0


def yuan(value_cents):
    return "¥%.2f" % (int(round(value_cents)) / 100.0)


# ---------- 日期 ----------
def day_key(value=None):
    value = value or _dt.date.today()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def period_key(value=None):
    value = value or _dt.date.today()
    return value.strftime("%Y-%m")


def parse_day(text):
    try:
        return _dt.date.fromisoformat(str(text)[:10])
    except Exception:
        return None


def days_between(later, earlier):
    a, b = parse_day(day_key(later)), parse_day(day_key(earlier))
    if not a or not b:
        return 0
    return (a - b).days


# ---------- 稳定随机（同一天同一件事不会每刷新一次就变） ----------
def stable_int(text, low, high):
    span = max(1, int(high) - int(low))
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    return int(low) + int(digest[:8], 16) % span


def pick_stable(seed, options):
    items = list(options or [])
    if not items:
        return None
    return items[stable_int(seed, 0, len(items)) % len(items)]


# ---------- 法定节假日（全国统一，2026） ----------
HOLIDAYS_2026 = frozenset({
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
})

MORNING = ((9 * 60 + 30), (11 * 60 + 30))
AFTERNOON = ((13 * 60), (15 * 60))


def is_holiday(value, holidays=None):
    day = parse_day(day_key(value))
    return bool(day) and day.isoformat() in (holidays or HOLIDAYS_2026)


def is_trading_day(value, holidays=None):
    day = parse_day(day_key(value))
    if not day:
        return False
    return day.weekday() < 5 and not is_holiday(day, holidays)


def is_weekend(day):
    moment = parse_day(day_key(day))
    return bool(moment) and moment.weekday() >= 5


def minutes_of(now):
    return now.hour * 60 + now.minute


def in_session(now, holidays=None):
    moment = now or _dt.datetime.now()
    if not is_trading_day(moment.date(), holidays):
        return False
    minutes = minutes_of(moment)
    return MORNING[0] <= minutes < MORNING[1] or AFTERNOON[0] <= minutes < AFTERNOON[1]


def fund_cutoff(now, holidays=None):
    """15:00 前申购按当日净值；15:00 后顺延下一交易日。"""
    moment = now or _dt.datetime.now()
    return is_trading_day(moment.date(), holidays) and minutes_of(moment) < 15 * 60


def next_trading_day(day, holidays=None):
    moment = parse_day(day_key(day)) or _dt.date.today()
    for _ in range(30):
        moment = moment + _dt.timedelta(days=1)
        if is_trading_day(moment, holidays):
            return moment.isoformat()
    return moment.isoformat()


def prev_trading_day(day, holidays=None):
    moment = parse_day(day_key(day)) or _dt.date.today()
    for _ in range(30):
        moment = moment - _dt.timedelta(days=1)
        if is_trading_day(moment, holidays):
            return moment.isoformat()
    return moment.isoformat()
