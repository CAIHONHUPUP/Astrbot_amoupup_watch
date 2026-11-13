# 隔离设计与崩溃路径 v2

> 项目：**Astrbot_amoupup_watch**（我能永远守护你）· 独立插件 · 与 amo 只做握手
> 状态：设计定稿，代码只到骨架。改这条边界之前先改这份文档。

## 0. TL;DR

- **独立项目**：自己的仓库、自己的数据库、自己的定时器。amo 侧只有 25 行握手代码（见 docs/HANDSHAKE.md）。
- **单向依赖**：watch 不 import amo、不读 amo 任何文件；amo 懒解析 watch，失败当没装。
- **读写分离**：amo 的回复链路只读「上一轮算好的快照」（O(1)，300ms 超时）；计算全在 watch 后台。
- **线程池独立**：4 个 IO + 1 个 DB 线程，**不用** `asyncio.to_thread`（那个池子 amo 也在用，占满它就等于卡住 amo）。
- **崩就崩自己**：watch 全挂 → amo 多一个 None 判断，聊天照常。
- **不覆盖**：进程级硬崩溃（segfault / OOM / 库损坏）同进程无法隔离；端口是纯 JSON，Phase 2 可搬进独立 worker。

## 1. 五条硬边界

### 1.1 异常隔离

| 入口 | 调用方 | 隔离位置 |
|---|---|---|
| 命令/消息 handler | 框架流水线 | `@social_guard` 装饰器（async generator 也要包） |
| `describe/snapshot/notify` | amo 桥接 | **双层**：watch 内 `_guard` + amo 侧 `wait_for`+`except` |
| 定时任务 | 自带 scheduler | 任务体 `_guard` + `add_done_callback` |
| Web API 路由 | 面板 | 路由包装器 → `{code:500}` |
| 启动/迁移 | `initialize()` | try/except → 只读降级，**绝不 raise** |

**为什么不能只靠框架**：AstrBot 的 `call_handler` 护的是「进程别死、派发继续」；它护不住 **amo 在自己的
handler 里调用你**——那一下抛出去，被打断的是 amo 的 handler，用户看到的是「她不回」。所以异常必须在**调用方**也收口。

    def watch_guard(scope):
        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **kw):
                try:
                    return await fn(*a, **kw)
                except asyncio.CancelledError:
                    raise                    # 必须透传，否则 terminate 卡死
                except Exception as exc:     # 不用 BaseException
                    isolated(scope, exc)     # 同类 5 条/分钟，限流
                    return None
            return wrapper
        return deco

日志格式（验收 A 就 grep 这一行）：`[WATCH] isolated scope=port.snapshot err=TimeoutError count=3 trace_id=8f2a`
完整 traceback 只写 `logs/watch.log`，AstrBot 主日志一行、限流——隔离层不能自己把 amo 的日志淹掉。

### 1.2 不阻塞事件循环

| # | 阻塞点 | 做法 |
|---|---|---|
| 1 | SQLite 连接/迁移/写 | 单写线程 `watch-db` + WAL + `busy_timeout=5000` |
| 2 | 世界快照 JSON（MB 级） | 自有 IO 池，只在后台任务 |
| 3 | 归档轮转 | 02:00–05:00 低优先级 |
| 4 | 行情/资讯 HTTP | 自有池 + 超时 + 熔断；**handler 里禁止** |
| 5 | 全量关系/社群计算 | 后台 + `graph_max_nodes`；平时增量、04:00 全量 |
| 6 | NPC 对话生成（LLM） | 后台 + `wait_for(30)`，结果进队列不进链路 |
| 7 | 日志写文件 | QueueHandler + 后台 QueueListener |
| 8 | 启动种子/迁移 | `initialize()` 只注册，重活丢后台 |
| 9 | amo 调 `snapshot` | **只读内存快照，O(1)，禁止 IO** |
| 10 | 面板大查询 | 同 DB 池 + `LIMIT` |

    self._io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="watch-io")
    self._db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-db")
    # 全部 run_in_executor(self._io_pool / self._db_pool, fn)，不用 to_thread

看门狗：每 100ms 采样 loop 延迟，>0.5s 记 `[WATCH] loop_lag` 并 dump 最慢的 5 个协程名；正常时零输出。
CI 禁止清单：handler/port 文件里不许出现 `sqlite3.`、`requests.`、`urllib`、`open(`、`time.sleep`。

### 1.3 存储完全独立

    data/plugin_data/Astrbot_amoupup_watch/
    ├── social.db (+ -wal/-shm)
    ├── archive/2026-11/events-2026-11.db.gz
    ├── snapshots/world-20261113T0400.json.gz
    ├── logs/watch.log (+.1 .2)
    └── cache/quotes.json

1. **路径不可配置**（不提供 db_path，避免手滑写进 amo 目录）。
2. **路径守卫**：`_path()` 里 resolve 后断言在自己根目录，拒绝 `..` 与符号链接逃逸。
3. **CI 兜底**：`grep -r "amo_forever.db\|Astrbot_amoupup_forever\|Astrbot_amoupup_data" .` 必须 0 命中。

    ROOT = Path(get_astrbot_data_path()) / "plugin_data" / "Astrbot_amoupup_watch"

    def _path(self, *parts) -> Path:
        p = (ROOT.joinpath(*parts)).resolve()
        if not str(p).startswith(str(ROOT.resolve()) + os.sep):
            raise ValueError(f"[WATCH] path escape: {p}")
        return p

**真正会串到 amo 的是磁盘**：写满盘 → amo_forever.db 报 `disk I/O error` → 她记不住、状态丢。
所以写前查余量，低于 `storage_min_free_mb` 立刻只读降级。

### 1.4 定时器自管

规则：① 只在 `initialize()` 创建（handler 里建一个，热重载就翻倍）；② 任务带 name + generation；
③ 循环条件 `while not self._closed.is_set()`；④ 最小 60s、±5% 抖动；⑤ 结算 `UNIQUE(day, kind)` 兜底。

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        self._closed.set()
        tasks = self._registry.cancel_all()
        try:
            await asyncio.wait(tasks, timeout=3.0)   # 硬截止
        except Exception as exc:
            isolated("terminate.wait", exc)
        survivors = [t for t in tasks if not t.done()]
        if survivors:
            logger.warning("[WATCH] terminate: %d 个任务未退出", len(survivors))
        try:
            await asyncio.wait_for(loop.run_in_executor(self._db_pool, self._db_close), 2.0)
        except Exception as exc:
            isolated("terminate.db", exc)
        self._registry.clear()
        self._io_pool.shutdown(wait=False, cancel_futures=True)
        self._db_pool.shutdown(wait=False, cancel_futures=True)

reload = 新实例 `__init__` + 旧实例 `terminate()`；旧实例一卡，重载就卡。所以每个 await 都有硬截止。
禁用 `atexit`（每次重叠加一个）、禁用全局 `loop.set_exception_handler`（3 次重载叠 3 层）。

### 1.5 资源上限

| 配置 | 默认 | 触顶行为 |
|---|---|---|
| `max_events_per_day` | 2000 | 丢最低权重 + 告警 |
| `event_retention_days` | 90 | 归档后删 |
| `archive_keep_months` | 12 | 删归档文件 |
| `db_soft_limit_mb` | 300 | 归档+VACUUM，仍超→只读 |
| `max_people` / `max_edges` | 300 / 5000 | 拒绝新建 / 只留 top-N |
| `edges_per_person` | 50 | 合并低权重 |
| `graph_max_nodes` | 300 | 仅增量，全量推迟 04:00 |
| `poll_interval_seconds` | 300（最小 120） | clamp |
| `breaker_threshold` | 3 | 熔断 15 分钟，只用缓存 |
| `npc_dialogue_per_hour` | 20 | 只发模板句 |
| `job_queue_max` | 100 | 丢最旧 |
| `job_timeout_seconds` | 60 | 取消 + isolated |
| `io_threads` | 4 | 固定，不随负载长 |
| `log_max_mb` / `log_backups` | 10 / 3 | 轮转 |
| `inject_max_chars` / `inject_max_items` | 800 / 5 | 按权重截断 |
| `storage_min_free_mb` | 500 | 只读降级 |

**每个上限必须写「触顶怎么办」**，只写数字等于把数据丢失变成随机事故。

## 2. 崩溃路径（按概率排序）

| # | 概率 | 触发 → 对 amo 的影响 | 防护 |
|---|---|---|---|
| 1 | 高 | 热重载旧定时器没死透、新实例又起一套 → 双份主动消息、结算翻倍 | 代际 tag + `_closed` + 3s 截止 + `UNIQUE(day,kind)` |
| 2 | 高 | SQLite 锁 / 占满默认线程池 → amo 的 `to_thread(db.init)`、生图下载排队 | 独立池 + WAL + `busy_timeout` |
| 3 | 中高 | 同步 IO 误入 port/handler → **整个 loop 卡住**，其他插件一起停 | port 契约 O(1) + CI 禁止清单 + 看门狗 |
| 4 | 中高 | 异常从 port 冒进 amo handler → 该条消息中断（「她不回」） | 双层 guard + `wait_for` |
| 5 | 中 | 数据/日志撑满磁盘 → **amo 的 DB 写失败**，最坏损坏 | 上限 + 余量检查 + 只读降级 |
| 6 | 中 | 行情/资讯挂起、429 → 任务堆积 | 超时 + 熔断 + stale 缓存 |
| 7 | 中 | 升级后迁移失败/依赖缺失 → 插件加载失败（amo 正常），用户误判 amo 坏 | 迁移前备份 + 事务 + `/watch diag` 给原因 |
| 8 | 中低 | terminate 卡死 → 热重载卡住 | 所有 await 硬截止 |
| 9 | 中低 | 内存膨胀 → OOM 带走整个进程 | 节点/边上限 + LRU + 注入预算 |
| 10 | 低 | 取消语义错误（finally 里 await）→ 任务泄漏 | 只 catch Exception |
| 11 | 低 | Web API 异常 → 面板报错，聊天无影响 | 包装器 + 分页 |
| 12 | 低 | 硬崩溃（segfault/OOM/库损坏）→ 同进程带走 amo | 短期 WAL+快照；根治 Phase 2 进程外 worker |

## 3. 五层隔离

| 层 | 手段 | 落点 | 失效表现 |
|---|---|---|---|
| 异常 | 双层 guard + 限流 + trace_id | `guard.py` + amo 桥接 | 只丢一条信息，不丢回复 |
| 事件循环 | 独立线程池 + 只读快照 + 看门狗 | `runtime.py` | `loop_lag` 告警，可定位协程 |
| 存储 | 独立根 + 路径守卫 + 余量 + 归档 | `store.py` | 只读降级，amo 零接触 |
| 定时器 | 注册表 + 代际 + 幂等 terminate | `scheduler.py` | reload 后任务数回基线 |
| 依赖 | 单向 + 懒解析 + 版本握手 | `amo_social_bridge.py` | 版本不匹配 = 未装，不报错 |

## 4. watch 全挂，amo 还能对话吗

**能。** ① amo 顶层不 import watch（懒解析）；② 每个端口 0.3s 超时 + except；③ 社会块是提示词的可选段，None 就不拼。

| 级别 | 条件 | 用户感知 | amo 行为 |
|---|---|---|---|
| L0 | 正常 | 她有自己的社交生活 | 注入最新快照 |
| L1 | 慢/超时 | 社会内容旧一点 | ≤10 分钟缓存，标 stale |
| L2 | 异常/未加载/停用 | 聊天完全正常，只是没社会事件 | 不注入，日志限流 |
| L3 | 目录丢失/只读 | 社会功能从头开始/暂停 | watch 自建空库并只读，amo 无感 |

## 5. 风险最高的 3 条（ROI）

| 排名 | 风险 | 影响面 | 概率 | 成本 | 理由 |
|---|---|---|---|---|---|
| 1 | loop 阻塞（共用线程池/同步 IO 混进链路） | 整个进程 | 中高 | ~1 天 | 唯一日常能「一键全停」的故障，也是验收 B 判据 |
| 2 | 异常没在调用方收口 | 单条消息中断 | 中高 | ~0.5 天 | 最便宜的安全带 |
| 3 | 生命周期不幂等 | 数据错、不可逆 | 高 | ~1 天 | 不修就是每次改代码都在造脏数据 |

资源上限排第 4，但「现在写进 schema 几乎免费、事后补要迁移」，所以第一版就带。

## 6. 验收

**A**：`/watch_test raise port|handler|timer` → 连发 3 条都有回复；`grep -c "\[WATCH\] isolated"` ≥3；AstrBot 日志无 amo 归属 traceback。必须测 port 模式。

**B**：拆三个指标——loop 延迟 p99 <100ms、到达→handler <300ms、回复发出（不含模型）<1s。
跑 `/watch_test busy 10 good`；反例 `bad`（直接在 loop 上跑）**必须爆表**，否则看门狗是假的。

**C**：停 core → 目录改名 → 启动：core 正常起、有回复、`/watch diag` 返 `{degraded:true,reason:"no_db"}`、自建空库不抛错；再测只读目录进 L3。

**D**：reload ×3：`tasks.names` 数量每次相同、`generation` 每次不同、
`SELECT day,kind,count(*) FROM settlements GROUP BY 1,2` 全为 1、日志无 `Task was destroyed`、`all_tasks()` 回基线 ±1。

## 7. 不做

- 不注册 LLM tool；不订阅 amo 的同步 EventBus；不读写 amo 文件；不改 amo 默认值；不用 atexit / 全局 loop handler。
- 不做跨进程（Phase 2；端口纯 JSON，届时零协议改动）。

## 8. 目录结构

    Astrbot_amoupup_watch/
    ├── main.py            # 装载/卸载 + describe()（已存在，骨架）
    ├── guard.py           # 异常收口 + 限流日志        v0.1
    ├── runtime.py         # 线程池 + 看门狗            v0.1
    ├── store.py           # SQLite + 路径守卫          v0.1
    ├── scheduler.py       # 定时器注册表 + 代际        v0.1
    ├── world.py           # 社会模拟核心                v0.2+
    ├── docs/              # 本目录两份文档
    └── tests/             # 验收 A-D 的自动化
