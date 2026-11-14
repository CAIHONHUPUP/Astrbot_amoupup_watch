# -*- coding: utf-8 -*-
"""真实新闻：东方财富（主）+ 财联社（备），每天三次，失败不阻断交易。"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

try:
    from .guard import isolated
except ImportError:  # 直接按文件加载时
    from guard import isolated

EM_URL = "https://search-api-web.eastmoney.com/search/jsonp"
CLS_URL = "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=7.7.5&rn=10"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _get(url, timeout=8):
    """GET 文本；弱网只重试一次，仍失败就抛给调用方降级。"""
    last_exc = None
    for attempt in range(2):
        request = urllib.request.Request(
            url, headers={"User-Agent": UA, "Referer": "https://www.eastmoney.com/"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.2)
    raise last_exc


def strip_jsonp(text):
    raw = str(text or "").strip()
    match = re.search(r"^[^({]*\((.*)\)\s*;?\s*$", raw, re.S)
    return match.group(1) if match else raw


def parse_eastmoney(text, limit=5):
    """剥 JSONP，取 title/url/source/published/snippet。"""
    try:
        payload = json.loads(strip_jsonp(text))
    except Exception:
        return []
    rows = []
    result = payload.get("result") or payload.get("data") or {}
    items = result.get("cmsArticleWebOld") or result.get("list") or []
    if isinstance(items, dict):
        items = items.get("list") or []
    for item in items[:max(1, int(limit))]:
        title = str(item.get("title") or item.get("artTitle") or "").replace("<em>", "").replace("</em>", "").strip()
        if not title:
            continue
        rows.append({
            "title": title,
            "url": str(item.get("url") or item.get("artUrl") or ""),
            "source": str(item.get("mediaName") or item.get("source") or "东方财富"),
            "published": str(item.get("date") or item.get("showTime") or ""),
            "snippet": re.sub(r"<[^>]+>", "", str(item.get("content") or item.get("digest") or ""))[:120],
        })
    return rows


def fetch_eastmoney(keyword, scope="stock", limit=3):
    param = {
        "uid": "", "keyword": str(keyword), "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": scope, "sort": "default", "pageIndex": 1,
            "pageSize": max(1, int(limit)), "preTag": "", "postTag": "",
        }},
    }
    url = EM_URL + "?cb=jQuery&param=" + urllib.parse.quote(json.dumps(param, ensure_ascii=False))
    try:
        return parse_eastmoney(_get(url), limit)
    except Exception as exc:
        isolated("finance.news.eastmoney", exc)
        return []


def parse_cls(text, limit=8):
    try:
        payload = json.loads(text)
    except Exception:
        return []
    rows = []
    data = payload.get("data") or {}
    items = data.get("roll_data") or data.get("list") or []
    for item in items[:max(1, int(limit))]:
        title = str(item.get("title") or item.get("brief") or "").strip()
        if not title:
            continue
        rows.append({"title": title[:80], "url": "https://www.cls.cn/telegraph",
                     "source": "财联社", "published": str(item.get("ctime") or ""),
                     "snippet": str(item.get("brief") or "")[:120]})
    return rows


def fetch_cls(limit=8):
    try:
        return parse_cls(_get(CLS_URL), limit)
    except Exception as exc:
        isolated("finance.news.cls", exc)
        return []


def merge_stock_news(code, items, day):
    """同一股票同一天合并成一条摘要（不是每条一条记忆）。"""
    if not items:
        return ""
    titles = "；".join(str(x.get("title") or "")[:40] for x in items[:3])
    return "%s %s 今日%d条：%s" % (day, code, len(items), titles)
