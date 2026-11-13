# 接口映射表（多插件联动）

本插件（AstrBot_amoupup_watch，守护/金融）**不复制 amo 的余额，不改 amo 源码**，只调它公开的方法。
下表就是全部调用面，新增接口一律在本插件内实现（不往 amo 加）。

## 1. amo（Astrbot_amoupup_forever）

入口：`context.get_registered_star("Astrbot_amoupup_forever").star_cls`，取到实例后按下表调用。

### 资产余额（唯一权威）

| 用途 | 方法 | 说明 |
|---|---|---|
| 读可用余额 | `amo.wallet.balance(account="cash")` | 返回分 |
| 读可取余额 | `amo.wallet.balance("withdrawable")` | 卖出资金 T+1 转可取 |
| 入账 | `amo.wallet.credit(amount_cents, reason, idem, ref="", account="cash")` | 重复 idem 返回 False（幂等）|
| 出账 | `amo.wallet.debit(amount_cents, reason, idem, ref="", account="cash")` | 余额不足返回 False |
| 流水 | `amo.wallet.ledger(limit=200)` | 面板生活账单按分类汇总用 |
| 总资产 | `amo.wallet.total_assets()` | 仅现金口径，持仓/房产由本插件加 |

> 本插件**没有** `wallet_account` 之类的副本表；`models/ledger.py` 只记资产/贷款/流水。

### 心情状态

| 用途 | 方法 |
|---|---|
| 读心情分 | `amo.state.mood_score()` |
| 读情绪词 | `amo.state.mood_words()`（对外只展示词，不出现数值）|
| 读主导情绪 | `amo.state.dominant_mood()` |
| 加减心情 | `amo.state.nudge_once(idem, **十维增量)`（无此方法时退回 `amo.state.nudge(**增量)`）|

十维键：开心 / 平静 / 郁闷 / 生气 / 暴躁 / 委屈 / 疲惫 / 焦虑 / 兴奋 / 依恋。

### 日程系统

| 用途 | 方法 |
|---|---|
| 追加一条当天日程 | `amo.state.add_schedule_item(day, item, source="watch", reason=...)` |

item 字段：`start/end/activity/place/category/detail/event/subject`（`start` 必须 `HH:MM`）。
请假/职场事件用 `00:00-23:59` 全天条目，`category="leave" | "work"`。

### 小日记（每小时）

`amo.db.save_diary(day, hour, text)` —— 本插件每笔开销/发薪/职场事件都写一条。
**amo 22:00 的每日总结会把这些「HH:MM 内容」原样喂给模型**（`_diary_hourly_lines`），所以职场事件用 `【职场】` 前缀、每日小结用 `【职场小结】`。

### 长日记（一天一篇）

`amo.write_diary_text(day, text)` —— 买房/买车/请假/涨薪/还贷这类大事件用，返回 `{code:0}` 表示成功。

### 记忆

| 用途 | 方法 |
|---|---|
| 写 | `amo.finance_remember(content, kind, tier, idem_key)` |
| 检索 | `amo.finance_recall(query, limit)` |
| 写入后回读校验 | 再调一次 `finance_recall`，查不到只记 `verified=False`，不影响交易 |

## 2. 记忆插件（Astrbot_amoupup_data）

amo 的 `finance_remember` 内部转发到这里；本插件**不直连**记忆插件，避免双写。
（若将来需要直连，签名是 `mem.remember(bot_id, uid, content, kind, importance, scope, group_id, idem_key)` / `mem.recall(bot_id, uid, query, limit, scope, group_id)`。）

## 3. 守护插件自身（本插件）

| 用途 | 接口 |
|---|---|
| 握手（amo 唯一入口） | `describe()`（同步、纯内存）|
| 状态 | `diag_sync()` / `async diag()` |
| 面板/接口 | `/api/plug/Astrbot_amoupup_watch/{diag,finance,finance_demo,finance/help,finance/loan,finance/consume,finance/decision,finance/debug,finance/property,finance/salary,finance/leave,finance/work,finance/expenses,trade}` |
| LLM 工具 | `finance`：action = status / positions / decide / leave / work |

## 4. LLM 工具（对话里自然触发）

注册：`@filter.llm_tool(name="finance")`，参数 `action` / `day` / `event_kind`。
模型只能「查账、请假、看职场状态、让她自己决策」，**买卖必须由她的决策引擎执行**（用户只能推荐入候选池）。

## 5. 本插件新增的接口（均在插件内，未改 amo）

- `services/leave`：请假五步链路（工资明细 → 日程 → 长期记忆 → 小日记 → 情绪）——实现在 `services/work_engine.py:apply_leave`；
- `services/work_events`：职场事件统一链路 + 每日职场小结（`daily_recap`）；
- `services/city`：城市解析（指令 > 配置 > amo 日程城市）；
- `models/ledger`：`expenses`（逐笔开销）、`salary_items`（工资明细）、`settlements`（月度幂等）。

> 需要 amo 再加接口时，请在此文件追加一行并注明「amo 侧待实现」，不要偷偷改 amo。
