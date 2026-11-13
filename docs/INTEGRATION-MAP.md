# 金融管家 × amo × 守护 · 联动接口映射表

> 版本：v1（2026-11-13）· 仅调研，不含实现
> amo：`D:\dafeiyu\astrbot\data\plugins\Astrbot_amoupup_forever`（6.9.4）
> 守护：`D:\dafeiyu\astrbot\data\plugins\Astrbot_amoupup_watch`（0.1.0）

## 0. 结论摘要

| 能力 | 状态 |
|---|---|
| 心情读写 | ✅ 现成（HumanState.nudge / mood_score） |
| 日程读 | ✅ 现成（db.get_schedule） |
| 日程追加单条 | ❌ 缺失（只有整天覆盖） |
| 日程提醒 | ❌ 缺失（amo 只有天气提醒内部逻辑） |
| 小日记写入 | ✅ 现成（db.save_diary），但会被日程变更标 stale 重生成 |
| 长日记写入指定文本 | ❌ 缺失（现成 api 是「重新生成」不是「写入给定文本」） |
| 资产/余额 | ❌ 完全缺失（amo 只有模型供应商余额，没有个人钱包） |
| 守护对外接口 | ✅ describe/diag + 4 个 HTTP 接口；❌ 无金融数据接入点 |

---

## 1. 心情 / 情绪状态

| 项 | 内容 |
|---|---|
| 文件 | `amo_human_state.py`，类 `HumanState` |
| 存储 | kv 表，key `mood`（dict）、`mood_ts`（float）；通过 `AmoDB.kv_set` 落库，**没有独立表** |
| 字段 | 十维：`开心 平静 郁闷 生气 暴躁 委屈 疲惫 焦虑 兴奋 依恋`（`amo_shared.py:727 MOOD_KEYS`），基础值 `MOOD_BASE`（同文件 743） |
| 读全部 | `state.mood -> dict[str, float]` |
| 读整体 | `state.mood_score() -> float`（0–100，50 为中性） |
| 读文案 | `state.mood_text() -> str`；`state.dominant_mood() -> str` |
| 写 | `state.nudge(**deltas) -> None`，键必须是十维之一，内部 `clamp` 后 `save_mood()` |
| 手动存 | `state.save_mood() -> None` |
| 缺失 | **幂等情绪记账**：重复事件会重复加心情。建议新增 `HumanState.nudge_once(idem, **deltas)`（同文件），用 kv `mood_applied:<idem>` 去重 |

示例调用：`amo.state.nudge(开心=4, 兴奋=2)`

---

## 2. 日程系统

| 能力 | 位置 / 函数 | 参数 | 返回 |
|---|---|---|---|
| 读某天 | `amo_db.py: AmoDB.get_schedule(day)` | `day: "YYYY-MM-DD"` | `list[dict] \| None` |
| 读内存 | `state.schedule[day]` | — | `list[dict]` |
| 写整天 | `amo_human_state.py: HumanState.commit_schedule_change(day, new_items, old_items=None, source="", reason="", invalidate_from=None)` | 整天的 items 列表 | `{"changed": bool, "version": int, "changed_hours": [...], "cancelled": [...]}` |
| 版本号 | `AmoDB.schedule_version(day)` / `bump_schedule_version(day)` | day | int |
| 生成骨架 | `HumanState.ensure_schedule(umo, force=False)`（async） | umo | None |
| 当前活动 | `HumanState.current_activity_info()` | — | dict |
| 事件 | `ScheduleChanged`（`amo_events.py`）由 `commit_schedule_change` 发布 | 见 dataclass | — |

**item 结构**（必须字段）：`{"start":"09:00","end":"10:00","activity":"上班","place":"公司","category":"work","zone":"office","transit":"","vehicle":"","detail":"","event":""}`

| 缺失 | 方案 |
|---|---|
| 追加单条日程 | **没有 append 接口**，commit 是整天覆盖。建议 `HumanState.add_schedule_item(day, item, source="finance", reason="")`：在 `_schedule_commit_lock` 内读 `db.get_schedule` → 合并 → `commit_schedule_change` |
| 提醒 | **没有提醒接口**（`amo_schedule.py` 里只有天气提醒内部函数 `_weather_remind_*`）。建议加 `HumanState.add_reminder(day, hhmm, text, idem)` + 一张 `reminders` 表，由现有 `_background_loop` 每分钟扫一次；或 v1 先由金融插件自己发消息 |

---

## 3. 小日记

| 能力 | 位置 / 函数 | 参数 | 返回 |
|---|---|---|---|
| 写入 | `amo_db.py: AmoDB.save_diary(day, hour, text, version=None, status="valid")` | day `"YYYY-MM-DD"`、hour `"0".."23"`（字符串）、text | None |
| 读 | `get_diary(day, hour) -> str`；`get_diary_record(day, hour) -> dict \| None` | 同上 | — |
| 有效性 | `diary_is_valid(day, hour, version=None) -> bool` | — | — |
| 失效 | `mark_diaries_stale(day, from_hour, version=None) -> int` | — | 行数 |

**风险**：日程一改，`from_hour` 及以后的小日记会被标 `stale` 并重新生成，金融插件写的文本会被 amo 覆盖。v1 建议只写「已经过去的小时」，或让 amo 在 `schedule_diary` 加 `source` 列（缺失，需迁移）。

---

## 4. 日记（长）

| 能力 | 位置 / 函数 | 参数 | 返回 |
|---|---|---|---|
| 写文件 | `diary_engine.py: write_diary(root, day, text)` | root：`amo._diary_dir()`；day：`date`；text | 文件路径 str |
| 读文件 | `read_diary(root, day) -> str` | — | — |
| 路径 | `diary_path(root, day) -> str` | — | — |
| HTTP 写 | `amo_api_diary.py: DiaryApiMixin.api_diary_write()` | body `{"date": "YYYY-MM-DD"}` | `{"code":0,"data":{date,path,text}}`；**内部调 `_generate_daily_diary` 重新生成，不接受现成文本** |

| 缺失 | 方案 |
|---|---|
| 写入指定文本 | 新增 `HumanCorePlugin.write_diary_text(day, text)`（`amo_api_diary.py`）：`_diary_write(self._diary_dir(), day, text)` + 写 kv `diary_written/diary_version/diary_stale=False` + 日志。**不要**让金融插件直接碰文件 |

---

## 5. 资产 / 余额

**现状：完全缺失。** amo 里的 `balance` 全部是**模型供应商余额**（`amo_api.py: api_balance` → `amo_shared.fetch_provider_balance`），不是她的钱包。

方案：新增 `amo_wallet.py`（amo 侧，独立类，不碰金融插件）：

| 接口 | 签名 | 返回 |
|---|---|---|
| 初始化 | `Wallet(db).ensure_schema()` | None（建表，幂等） |
| 余额 | `balance(account="cash") -> int`（分） | int |
| 总资产 | `total_assets() -> int`（现金 + 固定资产，固定资产由金融插件登记） | int |
| 入账 | `credit(amount_cents, reason, idem, ref="") -> bool` | 新插入 True / 重复 False |
| 扣款 | `debit(amount_cents, reason, idem, ref="") -> bool` | 同上；余额不足返回 False |
| 流水 | `ledger(limit=50) -> list[dict]` | — |

表（由代码建，不手改库）：`wallet_account(account TEXT PK, balance_cents INT)`、`wallet_ledger(idem TEXT PK, account, delta_cents, reason, ref, ts REAL)`。
接线：`main.py __init__` 加 `self.wallet = Wallet(self.db)`，`_ensure_ready` 里 `await asyncio.to_thread(self.wallet.ensure_schema)`。

---

## 6. 守护插件（watch）对外能力

| 类型 | 名称 | 参数 | 返回 |
|---|---|---|---|
| 实例 | `context.get_registered_star("Astrbot_amoupup_watch").star_cls` | — | WatchPlugin |
| 方法 | `describe()` | — | `{plugin, display_name, version, api_version, features, status, generation, tasks}` |
| 方法 | `diag()`（async） | — | `{generation, tasks, lag, degraded, store:{events,people,edges,settlements,db_mb,free_mb}}` |
| 方法 | `diag_sync()` | — | 同上（无 store） |
| 命令 | `/watch` | — | 文本 |
| HTTP | `GET /api/plug/Astrbot_amoupup_watch/diag` | — | JSON |
| HTTP | `GET /api/plug/Astrbot_amoupup_watch/finance` | — | 金融快照（当前空态） |
| HTTP | `GET /api/plug/Astrbot_amoupup_watch/finance_demo` | — | 演示快照 |
| HTTP | `POST /api/plug/Astrbot_amoupup_watch/trade` | `{action, code, qty, price}` | 当前固定返回未接入 |
| 表 | `meta / events / people / edges / settlements` | — | **金融不得读写**（独立存储） |

| 缺失 | 方案 |
|---|---|
| 金融 → 守护 UI 的数据通道 | 守护的 `_finance_snapshot` 目前返回空态。建议在 `watch/main.py::_finance_snapshot` 里懒解析 `Astrbot_amoupup_finance` 实例并调 `finance.snapshot()`，拿不到就回退空态 |
| 金融 → 守护的事件（可选） | 建议 `WatchPlugin.notify(kind, payload)`（有界队列 + 丢弃），只用于面板刷新，不参与业务 |

---

## 7. 调用约束（实现时必须守）

1. **同一事件循环**：金融插件跑在 AstrBot 主循环里，调用 amo 方法就是同步调用；amo 的 DB 带锁、`check_same_thread=False`，小操作可以直接调，**重活必须丢到金融自己的线程池**。
2. **amo 未就绪**：`amo.state` 在 `_ensure_ready` 之后才有，所有调用先判空。
3. **幂等**：钱包 `credit/debit` 必须带 `idem`；心情 `nudge` 目前无幂等，需 `nudge_once`。
4. **不碰 amo 库**：金融插件只调方法，不执行 SQL、不读 amo_forever.db。
5. **单向依赖**：金融 import 不到 amo 的模块也无所谓，全部走实例方法；amo 不 import 金融。

---

## 8. 最小改动清单（待你确认）

| # | 文件 | 新增 | 用途 |
|---|---|---|---|
| 1 | `amo_wallet.py`（新） | `Wallet` 类 + 两表 | 资产/余额/流水唯一入口 |
| 2 | `amo/main.py` | `self.wallet = Wallet(self.db)`；`_ensure_ready` 初始化 | 接线 |
| 3 | `amo_human_state.py` | `nudge_once(idem, **deltas)` | 情绪幂等 |
| 4 | `amo_human_state.py` | `add_schedule_item(day, item, source, reason)` | 日程追加（锁内读改写） |
| 5 | `amo_api_diary.py` | `write_diary_text(day, text)` | 长日记写指定文本 |
| 6 | `amo_human_state.py` + 表 | `add_reminder(...)` + `reminders` | 工资/房租提醒（可延后） |
| 7 | `watch/main.py` | `_finance_snapshot` 委托金融实例 | 面板数据 |

---

## 9. 实施记录（2026-11-13，已落地）

| # | 改动 | 文件 | 状态 |
|---|---|---|---|
| 1 | 钱包（表 + 方法，无 UI） | `amo_wallet.py`（新） | ✅ 已上线，`wallet_account` / `wallet_ledger` 已建，cash=0 |
| 2 | 接线 | `amo/main.py`：`self.wallet = Wallet(self.db)`；`_ensure_ready` 里 `to_thread(self.wallet.ensure_schema)` | ✅ |
| 3 | 心情幂等 | `HumanState.nudge_once(idem, **deltas) -> bool` | ✅ |
| 4 | 日程追加 | `HumanState.add_schedule_item(day, item, source="finance", reason="") -> dict` | ✅ |
| 5 | 长日记写指定文本 | `DiaryApiMixin.write_diary_text(day, text) -> {"code","data"/"msg"}` | ✅ |
| 6 | 守护面板委托 | `WatchPlugin._finance_provider()` + `_finance_snapshot` 优先问 `Astrbot_amoupup_finance.snapshot()` | ✅（金融插件未装时回退空态） |

验证：amo 全量 **1462 passed / 8 skipped**；线上重载成功；钱包两表存在、cash=0；日志无钱包报错。
提醒（`add_reminder`）仍按约定留到 v2。

---

## 10. 记忆插件接口映射（Astrbot_amoupup_data）

| 能力 | 位置 | 签名 | 返回 |
|---|---|---|---|
| 写入（amo 门面） | `amo_memory_data.py` HumanCorePlugin | `async finance_remember(content, kind="finance", tier=2, idem_key="") -> bool` | bool |
| 写入（底层） | `amo_memory_data.py` HumanCorePlugin | `async _data_memory_write(uid, content, kind, tier, scope, group_id, subject, platform, idem_key)` | bool |
| 记忆插件本体 | `Astrbot_amoupup_data/main.py` MemoryAPI | `async remember(bot_id, uid, content, kind="fact", importance=0.5, scope="private", group_id="", idem_key="", subject="user", platform="") -> dict` | `{ok, reason?}` |
| 检索（amo 门面） | `amo_memory_data.py` | `async finance_recall(query, limit=6) -> list[dict]` | list |
| 检索（底层） | `amo_memory_data.py` | `async _data_memory_recall(uid, text, limit=6, event=None)` | list |
| 记忆插件本体 | `Astrbot_amoupup_data/main.py` | `async recall(bot_id, uid, query="", limit=6, scope="", group_id="", platform="", pid=0) -> list[dict]` | list |

字段：`content`（正文）、`kind`（分类：finance / finance_trade / finance_pnl）、`importance`（0–1，tier=2 → 0.75）、`subject="bot"`（她自己的记忆）、`idem_key`（幂等）、`scope="private"`。
不写表名/字段名，全部走方法。

---

## 11. 联动点 → 接口 → 调用位置（最终）

| 联动点 | amo / 守护接口 | 调用位置 |
|---|---|---|
| 心情变化 | `HumanState.nudge_once(idem, **deltas)` | `finance_link.py::_nudge` |
| 情绪词（对外） | `HumanState.mood_words()` / `mood_score()` | `finance_link.py::mood` |
| 日程事件 | `HumanState.add_schedule_item(day, item, source, reason)` | `finance_link.py::_schedule` |
| 小日记 | `AmoDB.save_diary(day, hour, text)` | `finance_link.py::_small_diary` |
| 长日记 | `DiaryApiMixin.write_diary_text(day, text)` | `finance_link.py::_long_diary` |
| 余额读取 | `Wallet.balance()` | `finance_link.py::balance_cents` |
| 入账 / 扣款 | `Wallet.credit / debit` | `finance_link.py::income / expense` |
| 买入记忆 | `finance_remember` + `finance_recall` 回读 | `finance_link.py::record_trade(side="buy")` |
| 卖出记忆 | 同上 | `finance_link.py::record_trade(side="sell")` |
| 收盘盈亏记忆 | `finance_remember` | `finance_link.py::record_daily_pnl` |
| 工资记忆 | `finance_remember` | `finance_link.py::income(salary=True)` |
| 每笔支出记忆 | `finance_remember` | `finance_link.py::expense` |
| 用户转账记忆 | `finance_remember` | `finance_link.py::income(reason="用户转账")` |
| 对话前检索 | `finance_recall` | `finance_link.py::memory_context` → `finance_memory.py::MemoryLink.context` |
| 写入回读校验 | `finance_recall` | `finance_memory.py::MemoryLink.remember(verify=True)` |
| 面板数据 | `WatchPlugin._finance_provider` → `finance.snapshot()` | `watch/main.py::_finance_snapshot` |

---

## 12. 自主交易 / 三次行情 / 资金紧张 / 单品消费（2026-11-13 落地）

| 联动点 | 接口 | 调用位置 |
|---|---|---|
| 自主买入决策 | `Trading.buy(...)`（自身调用，用户推荐只进上下文） | `finance_decision.py::decide / _execute` |
| 自主卖出决策 | `Trading.sell(...)` | `finance_decision.py::_execute` |
| 决策理由记忆 | `finance.memory.remember(kind="finance_decision")` + `AmoDB.save_diary` | `finance_decision.py::_record` |
| 9:30 行情查询 | `fetch_quotes` + `repo.upsert_position` | `finance_decision.py::_refresh` |
| 午间行情查询 | 同上（11:35，只复盘） | `run_slot(slot="noon")` |
| 14:50 行情查询 | 同上（最终窗口） | `run_slot(slot="close")` |
| 15:00 盈亏结算 | `record_daily_pnl` | `finance_decision.py::close_day` |
| 低于 5% 银行借款 | `FinanceLink.borrow` + loans/installments | `finance_help.py::take_loan` |
| 低于 5% 用户求助 | `FinanceLink.income(用户转账)` | `finance_help.py::user_transfer` + `on_transfer` |
| 单品级消费 | `FinanceLink.expense`（逐项） | `finance_consume.py::buy_item` |

---

## 13. 交易细则 / 三餐 / 水电分项（2026-11-13 落地）

| 规则 | 落点 |
|---|---|
| 一字涨停不可买、跌停不可卖、停牌不成交 | `finance_trading.py::_one_word / _suspended`（Trading.buy/sell 前置校验） |
| 行情失败不成交、不更新盈亏 | `finance_trading.py`「看不到行情，今天不交易」；`finance_decision.py::_refresh` 仅成功报价才更新现价 |
| 股票 T+1 / 基金 T+2 可用 | `positions.ready_day` + `Trading.refresh_available` |
| 加权平均成本、卖出按成本结转 | `Trading.buy/sell`（成本=加权，卖出按均价结转实现盈亏） |
| 分红/送转除权 | `Trading.apply_dividend(code, cash_per_share_cents, bonus_ratio)` |
| 退市强制平仓 | `Trading.force_close(code, reason, price)` |
| 三餐单品逐笔（7:30–9:00 / 11:30–13:00 / 17:30–19:30） | `finance_daily.py::tick`（meta 幂等，逐项 `buy_item`） |
| 水/电/燃气/宽带分项 + 夏季电价 + 欠费预警 | `finance_life.py::_utilities`（顺序 房租→水→电→燃气→宽带） |

### 仍待实现（已记录，不假装完成）

1. 可用余额 / 可取余额分账（卖出当日可买、T+1 可取；基金赎回 T+7 到账）。
2. 候选池：表与 `add_candidate` 已就绪，决策行情尚未只查「持仓+候选」；用户推荐入库的入口未接。
3. 调薪公式（资产变化 + 收支比）、看病/人情概率事件、旅游区间。
4. 股息红利税说明、初始状态配置（初始存款/房车/持仓/闲置现金计息）。
5. 指令清单与模拟时钟测试清单（工资→买入→T+1→涨停→亏损→5% 二选一→还款→日记）。
