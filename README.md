# 我能永远守护你 · Astrbot_amoupup_watch

> **我能永远守护你**（独立社会模拟插件，AstrBot v4）。她有自己的社交世界：谁找她、她跟谁熟、哪些事会传开。
> **与 amo（Astrbot_amoupup_forever）只做握手接入，不进入 amo 代码库。**

- 当前版本：**0.2.0（金融/生活/职场全链路）**：guard / runtime / store / scheduler + 15 项单测；业务逻辑（world.py）还没写
- 跑测试：`pytest tests -q`（插件根目录放 PYTHONPATH）
- 设计文档：[docs/ISOLATION-DESIGN.md](docs/ISOLATION-DESIGN.md)
- 握手契约：[docs/HANDSHAKE.md](docs/HANDSHAKE.md)
- 作者：彩虹upup

## 目录结构（2026-11-13 分层重构，依赖单向）

    Astrbot_amoupup_watch/
    ├── main.py           入口：插件类 + 生命周期 + 指令 + LLM 工具 + 路由登记（≤480 行，有测试护栏）
    ├── models/           数据层：db（连接/迁移）、ledger（资产/贷款/流水）、memory（记忆索引）
    ├── services/         逻辑层：trade / market / candidates / life / daily / consume /
    │                     expense / work / work_events / property / credit
    ├── core/             决策层：agent_loop（三次自主决策）、llm_decider、amo_bridge、
    │                     memory_link、memory_tier、scheduler、runtime
    ├── utils/            工具层：城市系数、房价房租、金额取整、节假日、交易时段、稳定随机
    ├── ui/               展示层：cards（面板字段）、views/守护/index.html（Web 面板）
    ├── api/              接口层：handlers（面板/接口处理，ApiMixin）
    └── guard.py          异常收口（所有层共用）

依赖方向：main → api/ui → services/core → models → utils，禁止反向 import。
根目录残留的 `finance_*.py` / `store.py` / `scheduler.py` 是兼容 shim（sys.modules 别名，
指向真实实现），老代码与老测试零改动可用。

## 为什么单独开仓库

amo 已经是 6.9.x、1300+ 测试、每天在线跑的主插件。社会模拟是一个**会自己长数据**的子系统，
把它塞进 amo 等于把「最会积累状态的部分」和「最不能出事的聊天主链」绑在一起。所以：

| | amo | 本插件 |
|---|---|---|
| 职责 | 对话、日程、记忆、生图 | 社会世界模拟、事件、关系 |
| 数据库 | amo_forever.db | **自己的** plugin_data/Astrbot_amoupup_watch/social.db |
| 定时器 | 自己的 | **自己的** |
| 崩了会怎样 | 她不能聊天 | amo 只多一个 None 判断，照常聊天 |

amo 侧只加一个约 25 行的桥接：发现插件 → 调 `describe()` → 记一条日志。没有别的。

## 五条硬边界（详见设计文档）

1. **异常隔离**：5 类入口全部在 `guard.py` 收口，日志统一 `[WATCH] isolated`，绝不冒泡进 amo 派发层。
2. **不阻塞事件循环**：自有 `ThreadPoolExecutor`（4 IO + 1 DB），**不用** `asyncio.to_thread`（那个池子 amo 也在用）。
3. **存储独立**：`plugin_data/Astrbot_amoupup_watch/`，路径不可配置 + 路径守卫，代码里不出现 amo 任何文件名。
4. **定时器自管**：注册表 + 代际 tag + 幂等 terminate（3 秒硬截止）。
5. **资源有上限**：事件/关系/日志/轮询全部有上限，且每条上限都写明「触顶怎么办」。

## 本地目录

    D:\dafeiyu\work\watch\          ← 开发副本（就是仓库根）
    D:\dafeiyu\astrbot\data\plugins\Astrbot_amoupup_watch\   ← 部署位置（后续同步）

## 安装（等 v0.2 有功能之后）

1. 把整个目录复制到 `astrbot/data/plugins/Astrbot_amoupup_watch/`
2. 重启 core（或重载插件）
3. 日志出现 `[WATCH] loaded 0.1.0 api=1.0` 即加载成功
4. amo 侧会自己握手，日志出现 `[amo] watch handshake ok`

## 开发顺序（不许跳）

1. `guard.py`（异常收口）→ 2. `runtime.py`（线程池 + 看门狗）→ 3. `store.py`（库 + 路径守卫）
→ 4. `scheduler.py`（定时器）→ 5. 跑通验收 A/C/D → 6. 才写 `world.py` 的社会逻辑

先写功能再补边界，等于把五条硬边界变成事后追认。

## 明确不做

- 不注册 LLM tool（工具调用是第三个入口，边界稳了再说）
- 不订阅 amo 的同步 EventBus（会拖慢 amo）
- 不读 / 不写 amo 的任何文件
- 不上架插件市场
