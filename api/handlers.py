# -*- coding: utf-8 -*-
"""HTTP API 层：面板与接口处理（由 main 的 WatchPlugin 继承调用）。

保持 class 形式，是因为这些方法都依赖 self.finance_* / self.work_events；
放成 mixin 可以原样搬运，不改一行业务代码。
"""
from __future__ import annotations
from typing import Any, TYPE_CHECKING

import base64
import datetime as _dt
import logging
import math
import os
import time

try:
    from astrbot.api import logger
except Exception:  # 离线测试
    logger = logging.getLogger("amo.watch")

try:
    from ..guard import isolated
    from ..utils.clock import today_str as _today_str
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated
    from utils.clock import today_str as _today_str


def _ledger_ts(value):
    """流水时间戳（epoch 秒）-> float；缺失/非法按 0，保证排序不崩。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    return ts if math.isfinite(ts) else 0.0


def _ledger_date_time(value):
    """epoch 秒 -> 本地时区 (YYYY-MM-DD, HH:MM)；缺失/非法返回空串。"""
    ts = _ledger_ts(value)
    if ts <= 0:
        return "", ""
    try:
        when = _dt.datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError) as exc:
        isolated("web.ledger.time", exc)
        return "", ""
    return when.strftime("%Y-%m-%d"), when.strftime("%H:%M")
class ApiMixin:
    if TYPE_CHECKING:  # 宿主成员（插件主类/其它 mixin 提供）：严格口径用声明而不是 ignore
        _finance_snapshot: Any
        diag: Any
        finance: Any
        finance_consume: Any
        finance_decision: Any
        finance_expense: Any
        finance_help: Any
        finance_property: Any
        finance_repo: Any
        finance_salary: Any
        work_events: Any
    async def _api_finance_demo(self, request=None):
        try:
            return self._finance_snapshot(demo=True)
        except Exception as exc:
            isolated("web.finance_demo", exc)
            return {"status": "error", "message": "demo failed"}

    async def _api_ledger(self, request=None):
        """GET 明细流水：每行补 date/time，全局按时间倒序（排序只在后端做一次）。"""
        try:
            finance = getattr(self, "finance", None)
            amo = finance._amo() if finance is not None and hasattr(finance, "_amo") else None
            if amo is None:
                return {"ok": False, "message": "钱暂时还没接上，稍后再看"}
            try:
                from ..utils.ui_sanitize import sanitize_ledger_item
            except ImportError:
                from utils.ui_sanitize import sanitize_ledger_item
            rows = []
            for item in (amo.wallet.ledger(80) or []):
                row = sanitize_ledger_item(item)
                if not isinstance(row, dict):
                    continue
                row["date"], row["time"] = _ledger_date_time(row.get("ts"))
                rows.append(row)
            rows.sort(key=lambda row: _ledger_ts(row.get("ts")), reverse=True)
            return {"ok": True, "items": rows}
        except Exception as exc:
            isolated("web.ledger", exc)
            return {"ok": False, "message": "流水暂时读不到，稍后再看"}

    async def _api_finance(self, request=None):
        try:
            demo = False
            try:
                raw = str(getattr(request, "url", "") or "")
                query = getattr(request, "query_params", None)
                if query:
                    raw += " " + str(dict(query))
                demo = "demo=1" in raw
            except Exception as exc:
                isolated("web.finance.demo", exc)
            return self._finance_snapshot(demo=demo)
        except Exception as exc:
            isolated("web.finance", exc)
            return {"status": "error", "message": "finance failed"}

    async def _api_funds(self, request=None):
        """GET 基金候选池当日行情：名称/昨日净值/今日估值/涨跌幅/来源。"""
        try:
            repo = getattr(self, "finance_repo", None)
            if repo is None:
                return {"ok": False, "message": "账本还没准备好"}
            day = _today_str()
            rows = repo.fund_quotes(day)
            items = [{
                "code": row.get("code"),
                "name": row.get("name"),
                "nav": int(row.get("dwjz_e4") or 0) / 10000.0,
                "estimate": int(row.get("gsz_e4") or 0) / 10000.0,
                "change_pct": int(row.get("gszzl_bp") or 0) / 100.0,
                "source": row.get("source"),
                "tradeable": bool(row.get("tradeable")),
                "time": row.get("gztime"),
                "ts": row.get("ts"),
            } for row in rows]
            return {"ok": True, "day": day, "items": items,
                    "refreshed": repo.meta_get("fund_refresh:" + day, "")}
        except Exception as exc:
            isolated("web.funds", exc)
            return {"ok": False, "message": "基金行情暂时读不到，稍后再看"}

    async def _api_funds_refresh(self, request=None):
        """POST 手动刷新（不受 14:50 时间限制，但仍只在交易日抓取，不交易）。"""
        try:
            repo = getattr(self, "finance_repo", None)
            runtime = getattr(self, "runtime", None)
            if repo is None or runtime is None:
                return {"ok": False, "message": "账本还没准备好"}
            try:
                from ..services.fund_market import FundMarket
            except ImportError:
                from services.fund_market import FundMarket
            market = getattr(self, "fund_market", None) or FundMarket(self, runtime, repo)
            self.fund_market = market
            return await market.refresh(force=True)
        except Exception as exc:
            isolated("web.funds_refresh", exc)
            return {"ok": False, "message": "刷新失败，晚点再试"}

    async def _read_json(self):
        try:
            from astrbot.api.web import request as web_request
            if web_request is not None:
                try:
                    data = await web_request.json(default={})
                except TypeError:      # 老版本没有 default 参数
                    data = await web_request.json()
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            isolated("web.body", exc)
        return {}

    async def _api_bank(self, request=None):
        """GET 活期/定期明细；POST {action:deposit|withdraw, amount_cents, term}。"""
        try:
            body = await self._read_json()
            bank = getattr(self, "bank", None)
            if bank is None:
                try:
                    from ..services.bank_engine import build_bank
                except ImportError:
                    from services.bank_engine import build_bank
                bank = build_bank(self)
                self.bank = bank
            if not body:
                return {"ok": True, "current_cents": bank.current_cents(),
                        "fixed": bank.active_fixed_rows(), "fixed_cents": bank.fixed_cents(),
                        "last_matured": bank.last_matured(),
                        "total_cents": bank.total_cents()}
            action = str(body.get("action") or "deposit")
            amount = int(body.get("amount_cents") or 0)
            term = int(body.get("term") or 0)
            if action == "settle_matured":
                return {"ok": True, "matured": await bank.process_matured_fixed_deposits()}
            if action == "deposit":
                return await bank.deposit(amount, term)
            return await bank.withdraw(amount, None if not term else 0)
        except Exception as exc:
            isolated("web.bank", exc)
            return {"ok": False, "message": "银行接口异常"}

    async def _api_social(self, request=None):
        """GET 核心关系/应收；POST {action:"event", name:"随礼"} 触发一次社交事件。"""
        try:
            try:
                from ..services.social_engine import panel_events
            except ImportError:
                from services.social_engine import panel_events
            social = getattr(self, "social", None)
            if social is None:
                try:
                    from ..services.social_engine import build_social
                except ImportError:
                    from services.social_engine import build_social
                social = build_social(self)
                self.social = social
            social.ensure_seed()
            if not await self._read_json():
                rows = social.repo.npcs()
                try:
                    try:
                        from ..services.gift_engine import birthday_info
                    except ImportError:
                        from services.gift_engine import birthday_info
                    for row in rows:
                        info = birthday_info(self, str((row or {}).get("name") or ""))
                        row["birthday"] = info.get("birthday")
                        row["birthday_text"] = info.get("text")
                except Exception as exc:
                    isolated("web.social.birthday", exc)
                recv = social.repo.receivables()
                return {"ok": True, "npcs": rows, "receivables": recv,
                        "line": social.relation_line(),
                        "events": panel_events(social.events(10)),
                        "pending": social.pending_invite()}
            body = await self._read_json()
            if str(body.get("action") or "").lower() == "invite":
                try:
                    amount_cents = int(round(float(body.get("amount") or 0) * 100))
                except (TypeError, ValueError):
                    amount_cents = 0
                made = await social.create_invite(kind=str(body.get("kind") or ""),
                                                   npc=str(body.get("npc") or ""),
                                                   amount_cents=amount_cents)
                return {"ok": bool(made.get("ok")), "result": made}
            if str(body.get("action") or "").lower() == "cancel":
                social.clear_invite()
                return {"ok": True, "result": {"pending": social.pending_invite()}}
            if str(body.get("action") or "").lower() == "decide":
                # 手动推一次「抛事实 -> 她自己决定 -> 结算」，方便在面板上验收
                out = await social.scheduled()
                return {"ok": True,
                        "result": {"decision": (out or {}).get("decision"),
                                   "invite": (out or {}).get("invite"),
                                   "pending": social.pending_invite(),
                                   "events": panel_events(social.events(5))}}
            name = str(body.get("name") or "随礼")
            table = {"随礼": social.gift_money, "红包": social.red_packet,
                     "聚餐": social.dinner, "借钱": social.borrow_request, "批评": social.criticism}
            fn = table.get(name)
            if fn is None:
                return {"ok": False, "message": "未知事件"}
            return {"ok": True, "result": await fn()}
        except Exception as exc:
            isolated("web.social", exc)
            return {"ok": False, "message": "社交接口异常"}

    async def _api_insurance(self, request=None):
        """GET 保单列表；POST {kind:"critical|medical"} 购买（年费扣款，幂等）。"""
        try:
            body = await self._read_json()
            medical = getattr(self, "medical", None)
            if medical is None:
                return {"ok": False, "message": "医疗模块未就绪"}
            try:
                from ..services.insurance_engine import PRODUCTS, build_insurance
            except ImportError:
                from services.insurance_engine import PRODUCTS, build_insurance
            ins = getattr(self, "insurance", None) or build_insurance(self)
            self.insurance = ins
            if body.get("kind"):
                return await ins.buy(body["kind"])
            st = ins.status()
            repo = getattr(self, "finance_repo", None)
            def owned(kind):
                try:
                    return bool(repo.assets_of(kind)) if repo else False
                except Exception:
                    return False
            has_car, has_house = owned("car"), owned("house")
            def line(key):
                spec = PRODUCTS[key]; row = st.get(key) or {}
                if row.get("expired") or row.get("lapsed"):
                    status = "已失效（断缴）"
                elif row.get("grace"):
                    status = "宽限期（至 %s）" % row.get("grace_until")
                elif row.get("active"):
                    status = "生效中" + (("（下次缴费：%s 年）" % (int(row["year"]) + 1)) if row.get("year") else "")
                else:
                    status = "未购买"
                if spec.get("kind") == "car" and not has_car: status = "无车不适用"
                if spec.get("kind") == "house" and not has_house: status = "无房不适用"
                return {"name": spec["name"], "status": status}
            groups = [{"title": "人身保险", "items": [line(k) for k in ("e生保", "金钟罩", "重疾")]},
                      {"title": "财产保险", "items": [line(k) for k in ("交强险", "商车险", "家财险")]}]
            return {"ok": True, "groups": groups, "hint": "发送 /督促她买保险 <险种> 可以为她提供保障",
                    "policies": st, "has_policy": bool(st),
                    "catalog": [{"kind": k, "name": v["name"],
                                 "premium_cents": v["premium"][0]} for k, v in PRODUCTS.items()]}
            return {"ok": True, "policies": medical.policies(),
                    "catalog": [{"kind": "critical", "name": "重疾险", "premium_cents": medical.PREMIUM["critical"]},
                                {"kind": "medical", "name": "医疗险", "premium_cents": medical.PREMIUM["medical"]}],
                    "has_policy": medical.has_policy("critical")}
        except Exception as exc:
            isolated("web.insurance", exc)
            return {"ok": False, "message": "保险接口异常"}

    async def _api_fortune(self, request=None):
        try:
            fortune = getattr(self, "fortune", None)
            if fortune is None:
                return {"ok": False, "message": "突发引擎未就绪"}
            return await fortune.monthly_roll()
        except Exception as exc:
            isolated("web.fortune", exc)
            return {"ok": False, "message": "突发事件接口异常"}

    async def _api_lending(self, request=None):
        try:
            body = await self._read_json()
            lending = getattr(self, "lending", None)
            if lending is None:
                return {"ok": False, "message": "人情债模块未就绪"}
            if body.get("friend"):
                return await lending.lend(body["friend"], int(body.get("amount_cents") or 0),
                                          int(body.get("months") or 6))
            return await lending.repay_tick()
        except Exception as exc:
            isolated("web.lending", exc)
            return {"ok": False, "message": "人情债接口异常"}

    def _house_image_data(self, path):
        try:
            if not path or not os.path.isfile(path):
                return ""
            if os.path.getsize(path) > 8 * 1024 * 1024:
                return ""
            # 原图 1.7MB+ 直接转 data URI 会超过浏览器地址上限，表现为裂图。
            # 统一缩到 960 内、转 JPEG，通常 100-300KB，足够面板看。
            try:
                from io import BytesIO
                from PIL import Image
                with Image.open(path) as im:
                    im.thumbnail((960, 960))
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGBA")
                        bg = Image.new("RGB", im.size, "white")
                        bg.paste(im, mask=im.split()[-1])
                        im = bg
                    else:
                        im = im.convert("RGB")
                    buf = BytesIO()
                    im.save(buf, "JPEG", quality=82, optimize=True)
                    raw = buf.getvalue()
                if len(raw) > 2 * 1024 * 1024:
                    return ""
                return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
            except Exception:
                with open(path, "rb") as fh:
                    raw = fh.read()
                if len(raw) > 2 * 1024 * 1024:
                    return ""
                return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except Exception:
            return ""

    async def _api_housing(self, request=None):
        """房屋数据仍由能陪维护，守护只做面板代理和展示组装。"""
        try:
            finance = getattr(self, "finance", None)
            amo = finance._amo() if finance is not None and hasattr(finance, "_amo") else None
            housing = getattr(amo, "housing", None) if amo is not None else None
            if housing is None:
                return {"ok": False, "message": "能陪房屋模块未就绪", "homes": []}
            current = housing.current() or {}
            homes = []
            for home in housing.homes():
                images = {}
                for room, path in (home.get("images") or {}).items():
                    data = self._house_image_data(path)
                    if data:
                        images[room] = data
                homes.append({
                    "id": home.get("id"), "name": home.get("name") or "家",
                    "kind": home.get("kind") or "rent",
                    "status": home.get("status") or "idle",
                    "style": home.get("style") or "简约风",
                    "colors": home.get("colors") or "米白色",
                    "furniture": list(home.get("furniture") or []),
                    "rent_cents": int(home.get("rent_cents") or 0),
                    "is_current": home.get("id") == current.get("id"),
                    "images": images,
                })
            current_images = next((h["images"] for h in homes if h["is_current"]), {})
            return {"ok": True, "overview": housing.card_text(), "own": housing.card_text("房产"),
                    "rent": housing.card_text("租房"), "current": {
                        "id": current.get("id"), "name": current.get("name") or "家",
                        "kind": current.get("kind") or "rent",
                        "status": current.get("status") or "living",
                        "style": current.get("style") or "简约风",
                        "colors": current.get("colors") or "米白色",
                        "furniture": list(current.get("furniture") or []),
                    "furniture_detail": (housing.furniture_detail(current)
                                         if hasattr(housing, "furniture_detail") else []),
                        "rent_cents": int(current.get("rent_cents") or 0),
                        "images": current_images,
                    }, "homes": homes}
        except Exception as exc:
            return {"ok": False, "message": "房屋读取失败：%s" % exc, "homes": []}

    async def _api_decisions(self, request=None):
        """GET 她最近的自主决定：只读展示，默认只看今天（days/limit 仅供测试）。

        2026-11-17 W5：除宵夜外补约会/社交/购物/看病（只读现有 kv/审计），
        并把决定金额与 wallet 流水的块金额对账；对不上标 pending（前端「待对账」），
        不静默吞掉。
        """
        try:
            try:
                from ..core.amo_bridge import AmoBridge
            except ImportError:      # 离线测试 / 顶层加载
                from core.amo_bridge import AmoBridge
            try:
                from . import decisions_ext
            except ImportError:      # 单文件作为顶层模块加载
                try:
                    from api import decisions_ext
                except ImportError:
                    import decisions_ext
            days, limit = 1, 50
            try:
                query = getattr(request, "query_params", None)
                if query:
                    params = dict(query)
                    days = int(params.get("days") or 1)
                    limit = int(params.get("limit") or 50)
            except Exception as exc:
                isolated("web.decisions.query", exc)
            days = max(1, min(int(days or 1), 30))
            limit = max(1, min(int(limit or 50), 200))
            day = _dt.date.today().isoformat()
            blocks = []
            summary = {"status": decisions_ext.STATUS_OK, "checked": 0, "pending": 0,
                       "decision_total_cents": 0, "ledger_total_cents": 0, "items": []}
            bridge = AmoBridge(self, finance=getattr(self, "finance", None), scope="decisions")
            if not bridge.ready:
                return {"ok": False, "day": day, "items": [], "kinds": [], "blocks": [],
                        "reconcile": summary, "message": "她还不在线，决定稍后再看"}
            result = bridge.call("late_snack_decisions", days=days, limit=limit)
            if not result.ok:
                return {"ok": False, "day": day, "items": [], "kinds": [], "blocks": [],
                        "reconcile": summary, "message": "决定暂时读不到，稍后再看"}
            items = result.value if isinstance(result.value, list) else []
            try:
                blocks = decisions_ext.ledger_blocks(decisions_ext.ledger_rows(bridge))
                _, summary = decisions_ext.reconcile(items, blocks, day=day)
                kinds = decisions_ext.collect_extra(
                    bridge, getattr(self, "finance_repo", None), day)
            except Exception as exc:      # 扩展/对账失败：宵夜照常显示，不整页报错
                isolated("web.decisions.extend", exc)
                kinds = []
            return {"ok": True, "day": day, "items": items, "kinds": kinds,
                    "blocks": decisions_ext.blocks_for_day(blocks, day),
                    "reconcile": summary}
        except Exception as exc:
            isolated("web.decisions", exc)
            return {"ok": False, "message": "决定暂时读不到，稍后再看", "items": []}

    async def _api_medical(self, request=None):
        """GET 看病历/生病状态；POST {level,name,days,event_id} 模拟一场病（走同一入口）。"""
        try:
            body = await self._read_json()
            medical = getattr(self, "medical", None)
            if medical is None:
                return {"ok": False, "message": "医疗模块未就绪"}
            if not body:
                hunger = {}
                try:
                    finance = getattr(self, "finance", None)
                    amo = finance._amo() if finance is not None and hasattr(finance, "_amo") else None
                    if amo is not None and hasattr(amo, "hunger_state"):
                        hunger = amo.hunger_state()
                except Exception:
                    hunger = {}
                snap = medical.health_snapshot() or {}
                sick = bool(snap.get("sick")) or medical.is_sick()
                return {"ok": True, "is_sick": sick,
                        "sick_status": snap.get("status") or "",
                        "last_checkup": self.finance_repo.meta_get("checkup:last", "") if self.finance_repo else "", "last_hp": self.finance_repo.meta_get("checkup:last_hp", "") if self.finance_repo else "",
                        "in_bed_rest": sick or medical.in_bed_rest(),
                        "hunger": hunger,
                        "sick_until": str(snap.get("until") or "") or (self.finance_repo.meta_get("is_sick_until", "") if self.finance_repo else "")}
            if str(body.get("action") or "") == "checkup":
                return await medical.checkup()
            result = await medical.handle_sick_event(
                body.get("level", "light"), body.get("name", "感冒"),
                body.get("days", 1), body.get("event_id", ""))
            return {"ok": bool(result.get("ok")), **result}
        except Exception as exc:
            isolated("web.medical", exc)
            return {"ok": False, "message": "系统扣款异常，但身体重要，请先休息。"}

    async def _api_city(self, request=None):
        """GET 看当前城市；POST {city:"东莞"} 搬家（同步 amo 面板城市与天气）。"""
        try:
            try:
                from ..services.city import city_view, move_to
            except ImportError:
                from services.city import city_view, move_to
            body = await self._read_json()
            name = str(body.get("city") or "").strip()
            if not name:
                return {"ok": True, **city_view(self)}
            moved = await move_to(self, name)
            self.city_override = name
            if self.finance_repo is not None:
                try:
                    self.finance_repo.meta_set("city", name)
                except Exception as exc:
                    isolated("web.city.meta", exc)
            return {"ok": True, "moved": moved, **city_view(self)}
        except Exception as exc:
            isolated("web.city", exc)
            return {"ok": False, "message": "城市接口失败：%s" % exc}

    async def _api_work(self, request=None):
        """GET 今日职场状态；POST {kind: late|sick|project|overtime} 触发事件。"""
        try:
            if self.work_events is None:
                return {"ok": False, "message": "职场引擎未就绪"}
            body = await self._read_json()
            action = str(body.get("action") or "state").lower()
            if action in ("fire", "trigger"):
                raw = str(body.get("kind") or body.get("event") or "").strip()
                alias = {"迟到": "late", "早退": "late", "病假": "sick",
                         "全勤": "full_attendance", "全勤奖": "full_attendance",
                         "项目": "project", "奖金": "project", "加班": "overtime"}
                kind = alias.get(raw, raw.lower())
                return await self.work_events.fire(kind, day=str(body.get("day") or ""), source="api")
            return {"ok": True, "events": self.work_events.today_events(),
                    "prompt": self.work_events.prompt_line()}
        except Exception as exc:
            isolated("web.work", exc)
            return {"ok": False, "message": "职场接口失败：%s" % exc}

    async def _api_leave(self, request=None):
        """替她请假：{kind: 事假|病假, days: 1}，走完整链路。"""
        try:
            if self.finance_salary is None:
                return {"ok": False, "message": "工资模块未就绪"}
            body = await self._read_json()
            kind = str(body.get("kind") or "事假")
            days = float(body.get("days") or 1.0)
            return await self.finance_salary.apply_leave(kind=kind, days=days,
                                                         reason=str(body.get("reason") or ""),
                                                         day=str(body.get("day") or ""))
        except Exception as exc:
            isolated("web.leave", exc)
            return {"ok": False, "message": "请假失败：%s" % exc}

    async def _api_expenses(self, request=None):
        """本月日常开销：分类汇总 + 最近逐笔。"""
        try:
            if self.finance_expense is None:
                return {"ok": False, "message": "开销模块未就绪"}
            now = _dt.date.today()
            summary = self.finance_expense.month_summary(now)
            rows = self.finance_repo.expenses(period=now.strftime("%Y-%m")) if self.finance_repo else []
            return {"ok": True, "month": now.strftime("%Y-%m"),
                    "total_cents": summary["total_cents"],
                    "categories": summary["categories"],
                    "items": [{"day": row.get("day"), "category": row.get("category"),
                               "label": row.get("label"),
                               "amount_cents": int(row.get("amount_cents") or 0)}
                              for row in (rows or [])[:60]]}
        except Exception as exc:
            isolated("web.expenses", exc)
            return {"ok": False, "message": "开销接口失败：%s" % exc}

    async def _api_salary(self, request=None):
        """GET 看基本工资/明细/实发；POST {action:add|raise|settle}。"""
        try:
            if self.finance_salary is None:
                return {"ok": False, "message": "工资模块未就绪"}
            body = await self._read_json()
            action = str(body.get("action") or "state").lower()
            period_day = _dt.date.today()
            if period_day.day > 15:
                period_day = period_day.replace(day=1) + _dt.timedelta(days=32)
            period = period_day.strftime("%Y-%m")
            if action == "set":
                self.finance_salary.set_base(int(round(float(body.get("base") or 0) * 100)))
            elif action == "add":
                kind = "bonus" if str(body.get("kind")) in ("bonus", "奖金") else "deduction"
                amount = int(round(float(body.get("amount") or 0) * 100))
                if kind == "deduction":
                    amount = -abs(amount)
                label = str(body.get("label") or ("奖金" if kind == "bonus" else "扣款"))
                self.finance_salary.add_item(period, kind, label, amount)
            elif action == "raise":
                pct = float(body.get("pct") or 0.1)
                return await self.finance_salary.raise_or_cut(pct)
            elif action == "settle":
                return await self.finance_salary.settle(_dt.date.today(), period)
            summary = self.finance_salary.summary(period)
            summary["thrifty_until"] = self.finance_salary.thrifty_until()
            summary["ok"] = True
            return summary
        except Exception as exc:
            isolated("web.salary", exc)
            return {"ok": False, "message": "工资接口失败：%s" % exc}

    async def _api_property(self, request=None):
        """GET 看房车状态，POST {action:"buy", kind:"house"|"car"} 下单。"""
        try:
            if self.finance_property is None:
                return {"ok": False, "message": "房产模块未就绪"}
            # Quart 兼容层不会把 request 传进来，直接读请求体；GET 没有 body 时就是看状态
            body = await self._read_json()
            action = str(body.get("action") or "state").lower()
            kind = "house" if str(body.get("kind") or "house") in ("house", "房", "买房") else "car"
            if action == "buy":
                return await self.finance_property.buy(kind)
            state = self.finance_property.state()
            state["afford"] = {"house": self.finance_property.afford("house"),
                               "car": self.finance_property.afford("car")}
            state["ok"] = True
            return state
        except Exception as exc:
            isolated("web.property", exc)
            return {"ok": False, "message": "房产接口失败：%s" % exc}

    async def _api_finance_help(self, request=None):
        try:
            state = self.finance_help.status() if self.finance_help else {}
            state["prompt"] = self.finance_help.prompt_text() if self.finance_help else ""
            return state
        except Exception as exc:
            isolated("web.help", exc)
            return {"low": False, "prompt": ""}

    async def _api_loan(self, request=None):
        try:
            body = await self._read_json()
            amount = int(round(float(body.get("amount") or 0) * 100))
            periods = int(body.get("periods") or 12)
            rate = float(body.get("rate") or 0.06)
            return await self.finance_help.take_loan(amount, periods, rate)
        except Exception as exc:
            isolated("web.loan", exc)
            return {"ok": False, "message": "借款失败：%s" % exc}

    async def _api_consume(self, request=None):
        try:
            body = await self._read_json()
            items = body.get("items") or []
            return {"items": await self.finance_consume.buy_items(items)}
        except Exception as exc:
            isolated("web.consume", exc)
            return {"items": [], "message": "消费失败：%s" % exc}

    async def _api_decision(self, request=None):
        try:
            body = await self._read_json()
            slot = str(body.get("slot") or "close")
            return await self.finance_decision.run_slot(slot)
        except Exception as exc:
            logger.exception("[WATCH] finance decision failed")
            isolated("web.decision", exc)
            return {"ok": False, "message": "决策失败：%s" % exc}

    async def _api_debug(self, request=None):
        try:
            body = await self._read_json()
            action = str(body.get("action") or "").lower()
            if action == "salary" and self.finance is not None:
                return await self.finance.income(680000, "工资(调试)", "debug:salary:%d" % int(time.time()), salary=True)
            if action == "rent" and self.finance is not None:
                return await self.finance.expense(120000, "房租(调试)", "debug:rent:%d" % int(time.time()), necessary=True)
            if action == "spend" and self.finance_expense is not None:
                category = str(body.get("category") or "food")
                label = str(body.get("label") or "一笔开销")
                amount = int(round(float(body.get("amount") or 0) * 100))
                return await self.finance_expense.spend(category, label, amount)
            if action == "quotes" and self.finance_decision is not None:
                result = await self.finance_decision.run_slot("close")
                return {"ok": True, "result": result}
            if action == "fund_outage" and self.finance_decision is not None:
                result = await self.finance_decision.simulate_fund_outage()
                return {"ok": True, "result": result}
            return {"ok": False, "message": "未知调试动作"}
        except Exception as exc:
            isolated("web.debug", exc)
            return {"ok": False, "message": str(exc)}

    async def _api_diag(self, request=None):
        try:
            return await self.diag()
        except Exception as exc:
            isolated("web.diag", exc)
            return {"status": "error", "message": "diag failed"}

    # ---------------- 命令（入口，必须被隔离） ----------------
