# -*- coding: utf-8 -*-
"""异常收口：watch 所有对外入口的唯一出口。

规则（docs/ISOLATION-DESIGN.md 1.1）：
- 只 catch Exception；CancelledError 必须透传，否则 terminate 会卡死；
- 同类错误每分钟最多 5 条 warning，完整 traceback 只进自己的日志文件；
- 统一前缀 `[WATCH] isolated scope=... err=... count=... trace_id=...`，验收 A 就是 grep 这一行。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
import uuid

try:  # 正常运行时
    from astrbot.api import logger
except Exception:  # 离线单测
    logger = logging.getLogger("watch")

#: 完整栈只写这里（配 RotatingFileHandler），不进 AstrBot 主日志。
#: propagate=False 是硬要求：否则 traceback 会顺着 root logger 冒到 AstrBot 日志里，
#: 隔离层自己把 amo 的日志刷屏，验收 A 的「日志干净」就废了。
file_logger = logging.getLogger("watch.file")
file_logger.propagate = False

_trace = uuid.uuid4().hex[:8]
_lock = threading.Lock()
_counts: dict[str, int] = {}
_last: dict[str, float] = {}
_LIMIT = 5
_WINDOW = 60.0


def trace_id() -> str:
    return _trace


def counters() -> dict:
    with _lock:
        return dict(_counts)


def reset_counters() -> None:
    with _lock:
        _counts.clear()
        _last.clear()


def isolated(scope: str, exc: BaseException, limit: int = _LIMIT, window: float = _WINDOW) -> str:
    """记一次被隔离的异常；返回 trace_id。日志限流，计数永远累加。"""
    key = "%s:%s" % (scope, type(exc).__name__)
    now = time.monotonic()
    with _lock:
        _counts[scope] = _counts.get(scope, 0) + 1
        count = _counts[scope]
        allow = now - _last.get(key, 0.0) >= window
        if allow:
            _last[key] = now
    tid = trace_id()
    if not allow:
        return tid
    try:
        logger.warning(
            "[WATCH] isolated scope=%s err=%s: %s count=%d trace_id=%s",
            scope, type(exc).__name__, exc, count, tid,
        )
    except Exception:
        pass
    # 只有 main.py 给 file_logger 挂了 RotatingFileHandler 才写文件；
    # 没挂时直接跳过，避免 traceback 顺着 root logger 冒进 AstrBot 日志。
    if file_logger.handlers:
        try:
            file_logger.error(
                "[WATCH] isolated scope=%s trace_id=%s", scope, tid,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        except Exception:
            pass
    return tid


def guard(scope: str, default=None):
    """给 async 入口加隔离：异常不进调用方，只留一条 [WATCH] isolated。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                isolated(scope, exc)
                return default
        return wrapper
    return deco


def guard_sync(scope: str, default=None):
    """同步入口（describe / 纯函数）用这个。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                isolated(scope, exc)
                return default
        return wrapper
    return deco


def watch_task(task, scope: str):
    """给 create_task 的结果挂兜底：没人 await 的异常也要留痕。"""
    def _done(done):
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except Exception as inner:  # pragma: no cover - 极端情况
            isolated(scope + ".exception", inner)
            return
        if exc is not None:
            isolated(scope, exc)
    task.add_done_callback(_done)
    return task
