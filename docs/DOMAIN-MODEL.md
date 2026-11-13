# 社会关系模拟 · 领域设计 v0.2（未实现）

> 插件：**Astrbot_amoupup_watch（我能永远守护你）** · 版本仍 0.1.0 · 本文只是设计
> 依据：docs/ISOLATION-DESIGN.md 的五条硬边界；数据库沿用 store.py 的独立库 + 单写线程。

## 0. 一句话原则

**状态是流水的投影，余额是账本的聚合；业务代码只允许 append，不允许改字段。**

---

## 1. 领域模型

### 1.1 Person（节点）

| 字段 | 类型 | 说明 |
|---|---|---|
| `pid` | TEXT PK | 稳定 ID：`self` / `amo` / `npc:<ulid>`。**不用自增**，跨库合并与重放才安全 |
| `name` | TEXT | 显示名，可重名（不设唯一） |
| `aliases` | JSON | 别名/称呼（他、宝贝、王姐） |
| `kind` | TEXT | `self` / `amo` / `npc` |
| `relation_hint` | JSON | 现实身份标签：同事/邻居/大学同学（**标签不是关系本身**） |
| `birthday` | TEXT | `MM-DD` 或 `YYYY-MM-DD` |
| `status` | TEXT | `active` / `archived`（搬家、离职只归档不删） |
| `created_at` / `updated_at` | REAL | UTC epoch |
| `meta` | JSON | 扩展位，禁止放状态/数值 |

### 1.2 Relation（有向边 + 投影）

| 字段 | 类型 | 说明 |
|---|---|---|
| `src_pid` / `dst_pid` / `rtype` | TEXT PK | **三元组主键**：同两人可同时是「同事+朋友」 |
| `rtype` | TEXT | `parent` / `child` / `friend` / `colleague` / `neighbor` / `partner` |
| `intimacy` | INT 0–100 | 亲密度（投影值） |
| `trust` | INT 0–100 | 信任度 |
| `conflict` | INT 0–100 | 冲突值 |
| `debt` | INT −100–100 | 人情债：**正=我欠 TA，负=TA 欠我**（人情，不是钱） |
| `state` | TEXT | 见第 3 节状态机 |
| `state_since` | REAL | 进入当前状态的时间（防抖/冷却用） |
| `last_event_at` | REAL | 最近一次计分事件 |
| `event_count` | INT | 累计计分事件数（状态转移的守卫条件） |
| `version` | INT | 乐观锁；每次投影 +1 |
| `meta` | JSON | 备注 |

**硬规则**：`intimacy/trust/conflict/debt/state` 只能由投影器写；任何业务代码直接 UPDATE 都是 bug（CI 检查）。

### 1.3 RelationEvent（追加流水）

| 字段 | 类型 | 说明 |
|---|---|---|
| `eid` | TEXT PK | ULID，可排序 |
| `idem` | TEXT UNIQUE | 幂等键，见 4.3 |
| `seq` | INT UNIQUE | outbox 序号（amo 拉取用），单调递增 |
| `kind` | TEXT | 事件类型，见 4.1 |
| `occurred_at` / `day` | REAL / TEXT | UTC + 派生 `YYYY-MM-DD`（Asia/Shanghai） |
| `src_pid` / `dst_pid` / `rtype` | TEXT | 谁对谁、哪条边 |
| `delta` | JSON | `{intimacy,trust,conflict,debt}`，**实际生效值**（已 clamp） |
| `delta_raw` | JSON | 规则算出的原始值（审计用） |
| `payload` | JSON | 细节：金额、地点、消息摘要 |
| `source` / `source_ref` | TEXT | `chat/schedule/manual/settlement` + 源系统 ID |
| `cause_eid` | TEXT | 父事件（例如吵架→和好） |
| `created_at` | REAL | 写入时间 |

### 1.4 财务三件套

| 表 | 主键 | 关键字段 | 说明 |
|---|---|---|---|
| `ledger` | `entry_id` | `idem UNIQUE, account, direction(in/out), amount_cents, category, period_key, counterparty_pid` | **唯一**记录钱的地方；余额 = SUM |
| `asset` | `asset_id` | `kind(house/car), label, value_cents, mortgage_balance_cents, status` | 多套多辆，独立标识 |
| `settlement` | `(period_key, kind)` | `at, payload, amount_cents` | 工资/水电/房贷的幂等结算凭证 |

账户：`cash / bank / alipay / wechat`；金额一律 **整数分**，禁止 float。

---

## 2. 关系图设计

### 2.1 表达方式

- **节点**：Person。`self` 与 `amo` 是固定节点，避免"她"没有身份。
- **边**：`(src, dst, rtype)` 三元组。所以 A 对 B 可以同时有「同事」和「朋友」两条边，互不覆盖。
- **有向**：父子用 `parent` / `child` 两条边成对写入（同一事务）；朋友/同事是对称语义，但依然存两条边——**单向喜欢/单向借钱**本来就存在。
- **状态在边上**，不在人上：同一个人对 A 是挚友、对 B 是冷战，是正常社会。

### 2.2 需要的索引（只查邻居，绝不扫全表）

| 索引 | 用途 |
|---|---|
| `(src_pid, rtype, intimacy DESC)` | 我的好友榜 / 某类关系 top-k |
| `(dst_pid, rtype)` | 反向查询（谁把我当朋友） |
| `(src_pid, last_event_at DESC)` | 最近互动（衰减/主动消息） |
| `(day, kind)` | 按天回放 / 审计 |
| `(idem)` UNIQUE | 幂等 |

### 2.3 图的"视图"

| 视图 | 粒度 | 用途 |
|---|---|---|
| `person_view` | 每人 1 行 | 给 amo 的 O(1) 快照：top3 关系、状态计数、最近事件摘要 |
| `relation_rank` | 每人 top20 | 预计算排序，避免每次 ORDER BY 全表 |
| `circle`（v2） | 社区 | 社交圈/信息传播才需要 |

---

## 3. 关系状态机

### 3.1 状态列表

| 状态 | 含义 | 可逆 |
|---|---|---|
| `stranger` | 陌生 | 是 |
| `acquaintance` | 认识 | 是 |
| `friend` | 朋友 | 是 |
| `close` | 挚友 | 是 |
| `cold_war` | 冷战 | 是（和好） |
| `broken` | 决裂 | **v1 不可逆**（v2 才考虑多年后重逢） |

### 3.2 转移表

| 从 | 触发条件 | 到 | 守卫 |
|---|---|---|---|
| stranger | 有效互动 ≥3 次，或单次强事件（借钱/探望/安慰） | acquaintance | 30 天内 |
| acquaintance | intimacy≥40 且 trust≥35 且 conflict<30 且 event_count≥8 | friend | 距上次转移 ≥7 天 |
| friend | intimacy≥75 且 trust≥70 且 维持 ≥90 天 且 有过 ≥1 次冲突后恢复 | close | 冷却 30 天 |
| friend / close | conflict≥60，或 `quarrel` 严重档 | cold_war | 立即 |
| cold_war | `apologize`/`reconcile` 且 conflict<40 | friend | 距进入冷战 ≥3 天 |
| cold_war | conflict≥90，或冷战 >90 天且 0 互动 | broken | 终态 |
| close | intimacy<60 且持续 60 天 | friend | 衰减驱动 |

### 3.3 规则

1. 每次转移写一条 `kind=state_changed` 的流水（`from/to/cause_eid`），**状态本身可随时重放重建**。
2. 转移与数值更新在同一事务；先写事件、再改投影。
3. 任何转移都有冷却（`state_since`），防止「吵架→和好→吵架」一天刷十轮。

---

## 4. 事件流水

### 4.1 事件类型清单（v1 12 种）

| kind | 场景 | 方向 |
|---|---|---|
| `meet` | 初次认识 | 双向各一条 |
| `chat` | 日常聊天 | src→dst |
| `meal` | 约饭 | 双向 |
| `gift` | 送礼 | src→dst |
| `borrow_money` / `lend_money` | 借钱/借出 | 有向，钱走 ledger |
| `comfort` | 安慰 | src→dst |
| `quarrel` | 吵架 | 双向（冲突值双方都涨） |
| `apologize` | 道歉 | src→dst |
| `visit` | 探望 | src→dst |
| `help` | 帮忙 | src→dst |
| `birthday` / `festival` | 生日/节日 | 双向 |
| `neglect` | 长期不联系（系统生成） | 单向衰减 |
| `state_changed` | 状态迁移（系统） | 由投影器写 |

v2 再加：`date`（约会）、`wedding`（婚礼）、`betray`（背叛）、`gossip`（信息传播）。

### 4.2 delta 设计

- 事件表存 **三个值**：`delta_raw`（规则算的）、`delta`（clamp 后实际生效）、`caps_applied`（被哪些上限削过）。
- 好处：数值异常时能回答「是规则错了，还是被上限削了」。

### 4.3 幂等键构造

    idem = "v1|" + kind + "|" + src + "|" + dst + "|" + bucket
    idem = sha256(idem)[:32]

| 来源 | bucket 取什么 | 禁止 |
|---|---|---|
| 聊天消息 | 平台 message_id | ❌ 时间戳（同一秒两条就撞） |
| amo 事件 | amo 的 event_id | ❌ 自增序号（重启会归零） |
| 日程生成 | `schedule:{day}:{slot}` | ❌ 随机数 |
| 周期结算 | `{YYYY-MM}` / `{YYYY-MM-DD}` | ❌ 当前时间 |
| 手工/测试 | uuid4 | — |

写入一律 `INSERT OR IGNORE`，`rowcount==0` 即「重复」，计数打点、**不重放 delta**。

### 4.4 消费位点

- watch 侧 outbox：`seq` 单调，保留 30 天。
- amo 侧存 `since_seq`；拉到重复事件也能安全丢弃（idem 兜底）。
- 结算类不经过 outbox，直接 `settlement` 表判重。

---

## 5. 关系动态数值规则

### 5.1 单事件基础 delta（clamp 前）

| kind | intimacy | trust | conflict | debt |
|---|---|---|---|---|
| chat | +1~+3 | 0~+1 | 0 | 0 |
| meal | +4~+8 | +2 | −3 | ±1 |
| gift | +3~+10（按金额档） | +1~+4 | −2 | +2（我欠/TA 欠按方向） |
| comfort | +5~+10 | +5 | −5 | +1 |
| visit | +6 | +4 | −4 | 0 |
| help | +4 | +5 | −3 | +3 |
| quarrel | −8~−15 | −5~−10 | +12~+25 | 0 |
| apologize | +6~+12 | +3 | −10~−20 | 0 |
| birthday | +5 | +2 | −2 | 0 |
| festival | +3 | +1 | 0 | ±1 |
| borrow_money | 0 | −2~+2 | 0 | ±5 |
| neglect | −1~−3 | 0 | −1 | 0 |

### 5.2 上限与衰减

| 规则 | 值 | 目的 |
|---|---|---|
| 单维度 clamp | intimacy/trust/conflict ∈ [0,100]，debt ∈ [−100,100] | 不跑飞 |
| 同对同 kind 每日计分 | ≤2 次，超出只记流水不记 delta | 防刷好感 |
| 单日总增幅 | 每维度 ≤ +12 | 防「一天聊 40 句变挚友」 |
| 冲突惩罚增益 | 当 conflict>0，intimacy 增益 ×(1 − conflict/200) | 吵架期间亲近变慢 |
| 自然衰减 | 7 天无互动 +0；30 天 −3；冲突每 7 天 −1 | 关系要维护 |
| 人情债 | 不自动衰减，只能靠事件还 | 人情不能凭空消失 |

### 5.3 事件对状态的影响优先级

1. `quarrel` / `betray`（v2）先判状态（可能直接进冷战）
2. `apologize` 再判和好
3. 其余事件只改数值，状态由「数值阈值 + 守卫」在投影器里统一判定

**只有投影器能判状态**：业务代码不写 `if intimacy>75: state=close`。

---

## 6. 与 amo 的事件契约

### 6.1 方向与原则

- **单向**：watch → amo（事件）；amo → watch 只有 `notify("accepted", ...)` 这种回执，不写 watch 的表。
- **可选订阅**：amo 配置 `social_subscribe`（默认空 = 只握手、不联动）。
- **不阻塞**：默认 **amo 定时拉**（60s 一次），不往 amo 的同步 EventBus 里推。

### 6.2 契约事件（v1 发 6 类）

| kind | 触发 | payload | amo 可选动作 |
|---|---|---|---|
| `social.dinner_invite` | 约饭 | who, when, place_hint, deadline | 改日程 / 主动消息 |
| `social.birthday` | 生日前 3 天 | who, date | 日程 + 礼物提醒 |
| `social.visit` | 探望 | who, why | 改日程 |
| `social.quarrel` | 吵架 | who, severity | 心情 / 小日记 |
| `social.reconcile` | 和好 | who | 心情 |
| `social.daily_summary` | 每日 22:30 | top_relations, delta, money_delta | 小日记素材 |

### 6.3 信封字段（统一）

| 字段 | 说明 |
|---|---|
| `schema` | "1.0" |
| `event_id` | 同一 idem（amo 去重用） |
| `seq` | outbox 序号（amo 存 watermark） |
| `kind` | 上表 |
| `occurred_at` | UTC epoch |
| `actor` / `target` | `{pid, name, rtype}` |
| `effects` | 提示，不是命令：`{schedule_hint, outfit_hint, mood, diary_hint}` |
| `ttl_seconds` | 过期作废（约饭邀请 6h） |

amo 拿到 `effects` **自己决定**怎么落地：它不认 `outfit_hint` 就忽略，不影响事件本身。

### 6.4 端口（沿用握手契约）

| 方法 | 用途 |
|---|---|
| `describe()` | 握手（已实现） |
| `poll_events(since_seq, limit, kinds)` | 拉事件（v1） |
| `notify(kind, payload)` | amo 回执（accepted/declined），不阻塞 |

---

## 7. 性能设计

| 问题 | 做法 |
|---|---|
| 关系多时查询 | 只查邻居：`WHERE src_pid=? AND rtype=?`，配合 2.2 的索引；永不 `SELECT * FROM relation` |
| 全量重算 | 只在 04:00，节点 ≤ `graph_max_nodes`(300)，超时 60s 放弃，下次再来 |
| 增量 | 一条事件 = 一次 `INSERT` + 一次 `UPDATE relation`（同一事务），O(1) |
| amo 读快照 | `person_view` 每人一行，O(1)；不实时算 |
| top-k | `relation_rank` 预计算，写入时更新 |
| 流水增长 | 90 天归档到 `archive/`，主库只留热数据 |
| 并发 | 全部走 `runtime.run_db`（单写线程），事务 + `version` 乐观锁 |
| 结算 | `UNIQUE(period_key, kind)`，`INSERT OR IGNORE` 判重 |

---

## 8. 分期计划

### v1（最小可用，约 3–4 周）

- 模型：Person / Relation / RelationEvent / Ledger / Asset / Settlement
- 事件：第 4.1 节的 12 种
- 状态：stranger / acquaintance / friend / cold_war（`close` 只做数值，不单独迁移）
- 财务：工资、水电物业、房贷车贷三类周期结算；资产支持多套多辆；余额由账本聚合
- 联动：amo 拉 `poll_events` + 3 类 effects（日程/穿搭/心情），默认关，配置开启

**v1 明确不做**：信息传播、社交压力、多级关系（朋友的朋友）、弱关系、投资行情、婚礼丧事。

### v2

- 社交圈：社区发现（Louvain 或标签传播），圈内事件传播
- 信息传播：gossip 事件 + 可见性（谁知道谁不知道）
- 弱关系：互动 <3 次的边单独标记，用于「突然联系」
- 社交压力：多个邀请/人情冲突的仲裁（同日两个约饭）
- 投资：账户 + 持仓 + **只读行情**（独立限流，绝不进聊天链路）
- 多级：二度关系查询（`friend of friend`），带衰减

### 为什么 v1 不做这三样

1. **信息传播**需要「谁在什么圈里」的稳定社区划分，而社区划分要真实边数据；现在一条边都没有，做了也是拍脑袋。
2. **社交压力**本质是「多事件冲突仲裁」，依赖日程联动先跑通；否则算出来的压力没有出口。
3. **多级关系**查询成本随度数指数涨，且二度关系很容易变成「她认识全世界」；等 v1 的边密度稳定（每节点 ≥5 条）再开。

---

## 9. 风险清单

| # | 风险 | 触发 | 后果 | 防护 |
|---|---|---|---|---|
| 1 | 投影与流水不一致 | 业务代码直接改 relation 字段 / 事务半成功 | 状态错乱且不可解释 | 唯一投影器写入 + 事务 + `version` + 每日重放校验 |
| 2 | 事件重复计分 | bucket 用了时间戳/随机数；amo 重拉 | 好感被刷、状态提前跳 | `idem UNIQUE` + 只认源系统稳定 ID + 重复计数告警 |
| 3 | 结算重复 | 重载/跨天/时区漂移 | 工资多发、房贷多扣 | `UNIQUE(period_key,kind)` + UTC + 固定 TZ 派生 |
| 4 | 耦合过深 | amo 直接读 watch 表；watch import amo | 改一边崩两边 | ★ 只读端口 + CI grep（禁止 amo 文件名/表名）+ 契约测试 |
| 5 | 时钟回拨/时区 | 服务器 UTC、夏令时 | 周期错位、生日错天 | 事件全 UTC；day/period 用 Asia/Shanghai 推导 |
| 6 | 数值跑飞 | 刷好感 / 重复衰减 | 关系失真 | 单事件上限 + 日上限 + 递减 + clamp + 审计脚本 |
| 7 | 大图拖慢 | 全表排序 / 全量重算 | 卡住事件循环 | 邻接索引 + top-k 预计算 + 04:00 窗口 + 超时放弃 |
| 8 | 拉取丢事件 | amo watermark 丢 / outbox 过短 | 联动缺失 | outbox 留 30 天 + seq 单调 + 重复安全 |
| 9 | 隐私说漏 | amo 拿到内部标签（"她欠我 200"） | 人设崩 | 快照只给摘要 + `effects` 不含金额 |
