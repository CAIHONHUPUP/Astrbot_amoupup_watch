# -*- coding: utf-8 -*-
"""专属 NPC 名单（唯一合法社交圈）+ 防捏造校验。

2026-11-15 用户明确：核心 NPC 只有这 7 个，任何文本（聊天/日记/日程/社交动态）里
出现名单外的「人名」都必须拦住，绝不写进库。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("watch.social")

ROSTER: tuple[dict[str, Any], ...] = (
    {"name": "柳如烟", "circle": "friend", "relation": "闺蜜", "affinity": 88},
    {"name": "白凝冰", "circle": "friend", "relation": "死党", "affinity": 80},
    {"name": "季博达", "circle": "work", "relation": "直属领导", "affinity": 45},
    {"name": "方圆", "circle": "work", "relation": "普通同事", "affinity": 55},
    {"name": "妈妈", "circle": "family", "relation": "父母", "affinity": 95},
    {"name": "爸爸", "circle": "family", "relation": "父亲", "affinity": 92},
    {"name": "沈幼微", "circle": "edge", "relation": "远房表姐", "affinity": 40},
)

NAMES = tuple(item["name"] for item in ROSTER)
NAME_SET = frozenset(NAMES)
BY_NAME = {item["name"]: item for item in ROSTER}

#: 直接注入所有社交/日记/日程 Prompt 的红线句
ROSTER_LINE = ("你认识的人只有：[%s]。严禁凭空捏造任何其他名字，"
               "不要提任何名单外的朋友、同事、亲戚、路人。" % "、".join(NAMES))

#: 名单里这几个人的小名/简称，也算合法
NICKNAMES = frozenset({"如烟", "凝冰", "博达", "幼微", "妈妈", "爸爸"})

#: 用户本人和她嘴里的泛指，不算编造
SELF_ALIASES = frozenset({"老公", "亲爱的", "宝贝", "对象", "男朋友", "他", "你", "我", "她",
                          "自己", "人家"})

#: 出现在「人名位置」但不是名字的常见词（防止误伤）
STOPWORDS = frozenset(""".split()
家里 家人 公司 单位 学校 商场 超市 菜场 店里 饭店 餐厅 食堂 楼下 楼上 隔壁 小区 医院 银行 地铁 公交
朋友 同事 领导 老板 经理 主管 总监 客户 甲方 乙方 保安 前台 保洁 服务员 厨子 同学 老师 医生 护士
房东 室友 邻居 物业 中介 快递 外卖 客服 司机 店员 客人 阿姨 叔叔 师傅 哥哥 姐姐 弟弟 妹妹 爷爷 奶奶
外婆 外公 亲戚 表姐 表哥 表妹 堂姐 闺蜜 死党 姐妹 兄弟 大家 他们 她们 我们 你们 别人 有人 对方
妈妈 爸爸 老公 老婆 对象 心里 时候 今天 昨天 明天 前天 后天 周末 周一 周二 周三 周四 周五 周六
周日 周年 周围 周报 上周 本周 下周 月底 月初 年头 年底 上午 下午 晚上 半夜 凌晨 早上 中午 傍晚
一会儿 一阵 一点 一起 刚才 最近 现在 以后 之前 后来 其实 反正 可能 大概 应该 真的 特别 有点 还有
就是 但是 所以 因为 如果 虽然 然后 而且 不过 觉得 知道 看见 听到 想着 说着 天气 心情 肚子 胃口
眼睛 身体 头发 衣服 鞋子 工资 房租 房价 股票 基金 余额 手机 电脑 电视 游戏 水电 花呗 信用卡
酒店 车站 机场 景区 公园 火锅店 奶茶店 咖啡店 理发店 药店 洗衣店 前台 保洁 这家 那家 一家 两家 三家 几家 东西
""".split())

_C = r"[一-龥]"
#: 名字里绝不会出现的虚词/代词/时间字（用来挡住「今晚正陪我」「家那位最近」这种误判）
_FUNCTION_CHARS = frozenset(
    "的了在是正还又就也都很大太要会能把被给和跟与我和你他她它这那哪今昨明早晚午天日月年号点分时"
    "上下前后里外中间边么吧呢啊呀哦嘛着自己俩位个些点次趟回")
_VERBS = "说|聊|吃|去|逛|来|问|约|请|走|接|送|借|还|骂|批评|结婚|生日|出差|相亲|逛街|玩"
_SAYS = "说|约我|问我|问起|告诉我|发微信|打电话|请我|带我|陪我|批评我|骂我|跟我|找我"
_TIMES = "今天|昨天|最近|刚刚|又|下周|明天|周末"

#: 词序很重要：先试 3 字名，再退 2 字名，都要求动词紧跟在名字后面（不能隔着别的字）
_REL_PATTERN = re.compile(
    r"(?:和|跟|与|给|陪|请|被|让|叫|找|对|向)(%s{3})(?=(?:%s))"
    r"|(?:和|跟|与|给|陪|请|被|让|叫|找|对|向)(%s{2})(?=(?:%s))"
    % (_C, _VERBS, _C, _VERBS))

_SAY_PATTERN = re.compile(
    r"(%s{3})(?=(?:%s))|(%s{2})(?=(?:%s))" % (_C, _SAYS, _C, _SAYS))

_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛"
    "奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余"
    "元卜顾孟平黄穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强"
    "贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫房裘缪解应宗丁宣邓郁单杭洪包"
    "诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓"
    "蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖"
    "卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温别"
    "庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔"
    "隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)

_SUR_CLASS = "[" + "".join(sorted(_SURNAMES)) + "]"
#: 姓氏开头的 2-3 字（人名形状），用来抓「某某和…」「某某今天…」这种没有动词的写法
_NAMEISH = _SUR_CLASS + _C + "{1,2}"
_TIME_PATTERN = re.compile(r"(%s)(?=(?:%s))" % (_NAMEISH, _TIMES))
_LIST_PATTERN = re.compile(r"(%s)(?=(?:和|跟|与|、))" % _NAMEISH)


def _suspect(token):
    token = str(token or "")
    if not token or token in NAME_SET:
        return False
    if token in SELF_ALIASES or token in STOPWORDS or token in NICKNAMES:
        return False
    # 代词/语气字开头的一定不是人名（「陪她逛逛」「和他说」）
    if token[:1] in "她他你我它咱您":
        return False
    if any(char in _FUNCTION_CHARS for char in token):
        return False
    # 「答应表姐」「周末公司」这种：尾巴上是常见词就不算人名
    for cut in (token[1:], token[2:]):
        if cut and cut in STOPWORDS:
            return False
    return True


def _surname_like(token):
    return bool(token) and token[:1] in _SURNAMES


def foreign_names(text):
    """找出文本里疑似「名单外人名」的词（去重保序）。"""
    raw = str(text or "")
    if not raw.strip():
        return []
    hits = []
    for match in _REL_PATTERN.finditer(raw):
        token = match.group(1) or match.group(2) or ""
        if _suspect(token):
            hits.append(token)
    for match in _SAY_PATTERN.finditer(raw):
        token = match.group(1) or match.group(2) or ""
        if _suspect(token):
            hits.append(token)
    for match in _TIME_PATTERN.finditer(raw):
        token = match.group(1) or ""
        if _suspect(token):
            hits.append(token)
    for match in _LIST_PATTERN.finditer(raw):
        token = match.group(1) or ""
        if _suspect(token):
            hits.append(token)
    out = []
    for item in hits:
        if item not in out:
            out.append(item)
    return out


def sanitize(text, npc=""):
    """写入数据库前的最后一道闸。

    能确定是同一个人（事件绑定了 npc）就把编造名换成真实那个人；否则整句拦掉，
    由调用方退回结构化兜底文案（绝不带着陌生人名入库）。
    """
    value = str(text or "").strip()
    if not value:
        return ""
    hits = foreign_names(value)
    if not hits:
        return value
    npc = str(npc or "")
    logger.warning("[社交] 拦截名单外人名 %s | npc=%s | %s", hits, npc, value[:80])
    try:
        from ..guard import isolated as _isolated
    except ImportError:
        try:
            from guard import isolated as _isolated
        except ImportError:
            _isolated = None
    if _isolated is not None:
        _isolated("social.roster.blocked", RuntimeError("%s | npc=%s | %s" % (hits, npc, value[:80])))
    if npc and len(hits) == 1 and npc not in hits:
        fixed = value.replace(hits[0], npc)
        if not foreign_names(fixed):
            return fixed
    return ""


def roster_hint():
    return ROSTER_LINE


# ---------------------------------------------------------------- 名册/状态唯一入口
#: Phase 14：名册与 NPC 状态都归 npc_roster 管；social_engine 只调这里，
#: npc_status 退化成实现细节（状态池 + 7-14 天刷新）。
def refresh_status(repo, now=None):
    try:
        from . import npc_status
    except ImportError:  # 按顶层包/单文件加载时
        import npc_status
    return npc_status.refresh(repo, now)


def status_map(repo):
    try:
        from . import npc_status
    except ImportError:  # 按顶层包/单文件加载时
        import npc_status
    return npc_status.status_map(repo)
