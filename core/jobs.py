# -*- coding: utf-8 -*-
"""定时任务：待入账 / 财务结算 / 心跳（scheduler 回调都在这里）。

分层约定（docs/ARCHITECTURE.md）：
    handler（命令 / 消息）→ 留在 main.py；AstrBot 按 handler_module_path 派发
    job（定时回调）      → 本文件，统一签名 async def xxx(plugin)
    业务逻辑             → services/*
main.py 里同名方法只剩一行转发，scheduler 的引用和测试都不受影响。
"""
from __future__ import annotations

import datetime as _dt
import logging
import time

try:  # 正常运行时
    from astrbot.api import logger
except Exception:  # 离线单测 / 直接按文件导入
    logger = logging.getLogger("watch")

try:  # 包模式
    from ..guard import isolated
except ImportError:  # 直接按文件加载
    from guard import isolated


async def process_pending(plugin):
    """到期的待入账：卖出资金 T+1 转可取，基金赎回 T+7 到账。"""
    repo = plugin.finance_repo
    amo = plugin.finance._amo()
    if repo is None or amo is None:
        return
    today = time.strftime("%Y-%m-%d")
    try:
        rows = await plugin.runtime.run_db(repo.due_pending, today, timeout=5)
    except Exception as exc:
        isolated("finance.pending.query", exc)
        return
    for row in rows or []:
        try:
            amount = int(row.get("amount_cents") or 0)
            kind = str(row.get("kind") or "")
            idem = str(row.get("idem") or "")
            if kind == "withdrawable":
                amo.wallet.credit(amount, "卖出资金可取", idem + ":w", account="withdrawable")
            elif kind == "cash_withdrawable":
                amo.wallet.credit(amount, "基金赎回到账", idem)
                amo.wallet.credit(amount, "基金赎回可取", idem + ":w", account="withdrawable")
            await plugin.runtime.run_db(repo.finish_pending, idem, timeout=5)
        except Exception as exc:
            isolated("finance.pending.apply", exc)


async def finance_tick(plugin):
    """每 30 分钟检查一次：工资 / 房租 / 水电是否到了该结算的日期。"""
    try:
        plugin.finance.backfill_withdrawable()
        await plugin._guard_step("life", plugin.finance_life.tick())
        try:
            from ..services import bill_flow
        except ImportError:
            from services import bill_flow
        try:
            bill_flow.create_due(plugin)
            await bill_flow.overdue_tick(plugin)
            hour = time.localtime().tm_hour
            if 7 <= hour < 24:
                line = bill_flow.prompt_line(plugin)
                survival = bill_flow.survival_line(plugin)
                text = "；".join(item for item in (line, survival) if item)
                amo = None
                try:
                    amo = plugin.finance._amo() if hasattr(plugin.finance, "_amo") else None
                except Exception:
                    amo = None
                if text and amo is not None and hasattr(amo, "note_expense_summary"):
                    amo.note_expense_summary(text)
        except Exception as exc:
            isolated("finance.bill_flow", exc)
        try:
            from ..services.date_engine import tick as date_tick
        except ImportError:
            from services.date_engine import tick as date_tick
        await plugin._guard_step("date", date_tick(plugin))
        try:
            from ..services.nightly_memory import nightly_tick
        except ImportError:
            from services.nightly_memory import nightly_tick
        try:
            await nightly_tick(plugin)
        except Exception as exc:
            logger.warning("[watch] 夜间记忆任务异常（不影响白天）: %s" % exc)
        try:
            from ..services.late_night_engine import tick as late_tick
        except ImportError:
            from services.late_night_engine import tick as late_tick
        await plugin._guard_step("late_night", late_tick(plugin))
        try:
            from ..services.shopping_engine import tick as shopping_tick
        except ImportError:
            from services.shopping_engine import tick as shopping_tick
        await plugin._guard_step("shopping", shopping_tick(plugin))
        try:
            from ..services.shopping_engine import flush as shop_flush
        except ImportError:
            from services.shopping_engine import flush as shop_flush
        await plugin._guard_step("shop_flush", shop_flush(plugin))
        try:
            from ..services.gift_engine import tick as gift_tick
        except ImportError:
            from services.gift_engine import tick as gift_tick
        await plugin._guard_step("gift", gift_tick(plugin))
        try:
            from ..services.social_engine import build_social
        except ImportError:
            from services.social_engine import build_social
        try:
            plugin.social = getattr(plugin, "social", None) or build_social(plugin)
            social_out = await plugin.social.scheduled(_dt.datetime.now())
            # 只推她自己说出口的那句（LLM 生成的 note）；系统事实不直接当她的消息发出去
            for text in (social_out or {}).get("push", []):
                text = str(text or "").strip()
                if not text:
                    continue
                amo = plugin.finance._amo() if hasattr(plugin.finance, "_amo") else None
                if amo is not None and hasattr(amo, "push_to_owner"):
                    await amo.push_to_owner(text)
        except Exception as exc:
            isolated("finance.social_tick", exc)
        await plugin._guard_step("pending", plugin._process_pending())
        if plugin.finance_help is not None:
            plugin.finance_help.maybe_reset()
            await plugin._guard_step("help_repay", plugin.finance_help.repay_tick())
        if plugin.finance_decision is not None:
            await plugin._guard_step("decision", plugin.finance_decision.tick())
        if plugin.finance_daily is not None:
            await plugin._guard_step("daily", plugin.finance_daily.tick())
        if getattr(plugin, "bank", None) is None:
            from ..services.bank_engine import build_bank
            plugin.bank = build_bank(plugin)
        await plugin._guard_step("bank_daily", plugin.bank.daily_tick())
        if plugin.work_events is not None:
            await plugin._guard_step("work_random", plugin.work_events.daily_random(_dt.datetime.now()))
            await plugin._guard_step("work_recap", plugin.work_events.daily_recap(_dt.datetime.now()))
            try:
                if any("加班" in str(e) for e in (plugin.work_events.today_events() or [])):
                    amo = plugin.finance._amo() if hasattr(plugin.finance, "_amo") else None
                    if amo is not None and hasattr(amo, "on_forced_overtime"):
                        amo.on_forced_overtime()
            except Exception:
                pass
        # 下面这些和 work_events 无关：以前缩进在 if 里，职场模块一出错就全不跑
        await plugin._guard_step("fortune", plugin.fortune.monthly_roll(_dt.datetime.now()))
        await plugin._guard_step("lending", plugin.lending.repay_tick(_dt.datetime.now()))
        await plugin._guard_step("medical", plugin.medical.insurance_tick(_dt.datetime.now()))
        try:
            from ..services.bankruptcy_engine import build_bankruptcy
        except ImportError:
            from services.bankruptcy_engine import build_bankruptcy
        await plugin._guard_step("bankruptcy", build_bankruptcy(plugin).check(_dt.datetime.now()))
    except Exception as exc:
        isolated("finance.settle", exc)


async def heartbeat(plugin):
    """心跳必须走 DB 线程：scheduler 回调在事件循环上，同步写库就是阻塞 amo。"""
    if plugin.store is None:
        return
    try:
        await plugin.runtime.run_db(plugin.store.touch, timeout=5)
    except Exception as exc:
        isolated("timer.heartbeat", exc)
