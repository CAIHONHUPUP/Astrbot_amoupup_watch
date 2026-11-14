# -*- coding: utf-8 -*-
"""凌晨 3 点：把记忆插件过去 24h 的流水记忆交给 LLM 提炼成长期事实。

- 白天写入保持纯规则，本任务只在 03:00 跑一次/天；
- LLM 失败或返回空：不删除、不归档原流水，直接跳过；
- 成功才写长期层并把原流水标记 archived（不物理删除）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt

try:
    from astrbot.api import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)

SYSTEM = ("你是一个记忆提炼助手。请将以下当天的流水记忆提取为核心事实。"
          "合并同义句（如“爱吃螺蛳粉”和“螺蛳粉真香”合并为“[偏好] 喜欢吃螺蛳粉”），"
          "过滤无意义的寒暄。只输出提炼后的结果，不要解释。")

MAX_ROWS = 300
MAX_GROUPS = 4
MAX_FACTS = 8


def _data_plugin(plugin):
    context = getattr(plugin, "context", None)
    getter = getattr(context, "get_registered_star", None) if context else None
    if not callable(getter):
        return None
    for name in ("Astrbot_amoupup_data", "Astr_Amo_data"):
        try:
            meta = getter(name)
        except Exception:
            meta = None
        if meta is not None:
            return getattr(meta, "star_cls", None) or getattr(meta, "star", None)
    return None


async def _provider(plugin):
    try:
        from .core.llm_decider import _provider as provider_of
    except ImportError:
        from core.llm_decider import _provider as provider_of
    return await provider_of(plugin)


def _parse_facts(text):
    facts = []
    for line in str(text or "").splitlines():
        line = line.strip().lstrip("-*•0123456789.、) ").strip()
        if not line or line in ("无", "没有", "暂无"):
            continue
        if len(line) > 120:
            line = line[:120]
        if line not in facts:
            facts.append(line)
    return facts[:MAX_FACTS]


async def _generalize(plugin, rows):
    provider = await _provider(plugin)
    if provider is None:
        return []
    memories = "\n".join("- " + str(row.get("content") or "")[:120] for row in rows)
    prompt = "以下是今天的流水记忆：\n" + memories + "\n\n请提炼为简洁的核心事实，每行一条。"
    try:
        response = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, system_prompt=SYSTEM, contexts=[]),
            timeout=120,
        )
    except Exception as exc:
        logger.warning("[watch] 夜间记忆提炼失败（保留原记忆）: %s" % exc)
        return []
    text = getattr(response, "completion_text", None)
    if text is None:
        text = (response.get("completion_text") or response.get("text") or "") if isinstance(response, dict) else ""
    return _parse_facts(text)


async def nightly_tick(plugin, now=None):
    now = now or _dt.datetime.now()
    if int(now.hour) != 3:
        return {"ok": False, "reason": "not_night"}
    repo = getattr(plugin, "finance_repo", None)
    day = now.date().isoformat()
    if repo is not None and str(repo.meta_get("nightly_memory_day", "") or "") == day:
        return {"ok": False, "reason": "done"}
    data = _data_plugin(plugin)
    if data is None or not hasattr(data, "nightly_memory_candidates"):
        logger.warning("[watch] 夜间记忆提炼：记忆插件不可用，跳过")
        return {"ok": False, "reason": "no_memory_plugin"}
    try:
        rows = data.nightly_memory_candidates(hours=24, limit=MAX_ROWS) or []
    except Exception as exc:
        logger.warning("[watch] 夜间记忆读取失败: %s" % exc)
        if repo is not None:
            repo.meta_set("nightly_memory_day", day)
        return {"ok": False, "reason": "read_failed"}
    if not rows:
        if repo is not None:
            repo.meta_set("nightly_memory_day", day)
        return {"ok": True, "reason": "empty", "written": 0, "archived": 0}

    groups = {}
    for row in rows:
        key = (str(row.get("bot_id") or ""), str(row.get("uid") or ""),
               str(row.get("scope") or "private"), str(row.get("group_id") or ""),
               str(row.get("subject") or "user"))
        groups.setdefault(key, []).append(row)
    written = archived = 0
    for key, items in list(groups.items())[:MAX_GROUPS]:
        facts = await _generalize(plugin, items)
        if not facts:
            continue
        meta = {"bot_id": key[0], "uid": key[1], "scope": key[2],
                "group_id": key[3], "subject": key[4]}
        ids = [row.get("id") for row in items]
        try:
            out = data.nightly_apply(meta, facts, ids)
        except Exception as exc:
            logger.warning("[watch] 夜间记忆写回失败（不归档）: %s" % exc)
            continue
        written += int((out or {}).get("written") or 0)
        archived += int((out or {}).get("archived") or 0)
    if written <= 0:
        logger.warning("[watch] 夜间记忆提炼没有产出，保留原流水，明天再试")
        if repo is not None:
            repo.meta_set("nightly_memory_day", day)
        return {"ok": False, "reason": "no_facts", "written": 0, "archived": 0}
    if repo is not None:
        repo.meta_set("nightly_memory_day", day)
    logger.info("[watch] 夜间记忆提炼完成：写入 %d 条，归档 %d 条" % (written, archived))
    return {"ok": True, "written": written, "archived": archived}
