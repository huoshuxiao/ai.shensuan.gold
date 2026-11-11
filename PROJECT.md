# 项目结构

> 状态：2026-10-20，P1 数据层骨架。
> 标记 `现状` 的目录/文件已存在；标记 `计划` 的尚未创建。
> 本文件是代码结构的可读索引：新增、移动、删除或重命名 Python 文件，必须同批更新本文件（规则见 `AGENTS.md` §15.14）。
> `__init__.py` 只作包标记，不要求写注释；其余 Python 文件必须有详细中文注释（§15.5）。
> `config/` 下四份 YAML 必须有文件头、顶层分节与关键字段的中文注释（§15.6），改值同批改注释。

## 1. 顶层目录与文件

| 路径 | 状态 | 说明 |
|---|---|---|
| `README.md` | 现状 | 用户需求与技术栈约束（19 条） |
| `AGENTS.md` | 现状 | 系统设计与 Agent 行为单一事实源 |
| `PROJECT.md` | 现状 | 本文件：代码结构、目录与文件作用索引 |
| `看板.md` | 现状 | 敏捷任务台账（TODO/PROCESS/DONE/ISSUE/PENDING/RISK） |
| `CHANGELOG.md` | 现状 | 变更记录 |
| `config/` | 现状 | 全局配置：application/data_sources/strategy/llm 四份 YAML，均带中文维护注释 |
| `docs/` | 现状 | 解释性文档，不参与程序设计 |
| `data/` | 现状 | 原始、规范化、手工、决策数据；`paper/`、`live/` 为计划 |
| `logs/` | 现状 | JSONL 运行日志，按日轮转，保留 7 天 |
| `src/main/python/` | 现状 | 生产代码根目录 |
| `src/test/python/` | 现状 | 测试代码根目录 |
| `plans/` | 现状 | 设计/实施计划草稿，不参与运行时 |
| `report/` | 计划 | 每日决策卡、研报与图表输出 |
| `requirements.txt` | 现状 | 唯一依赖清单，固定版本 |
| `pytest.ini` | 现状 | pytest 路径、标记与默认参数 |
| `.gitignore` | 现状 | 排除 `.env`、`data/live/`、原始数据、缓存等 |
| `Dockerfile` | 计划 | 容器运行入口（`docker run`，无 compose） |
| `opencode.json` | 计划 | opencode 指针配置，不复制第二套规则 |

## 2. 生产代码目录（现状）

```text
src/main/python/com/sun/shensuan/gold/
├── __init__.py                  # 包标记
├── _bootstrap.py                # sys.path 引导
├── cli.py                       # P1 命令行入口
├── common/                      # 通用基础能力
│   ├── __init__.py
│   ├── atomic.py                # 原子写
│   ├── errors.py                # 异常层级
│   ├── hashing.py               # 规范化 JSON 与哈希
│   ├── logging_setup.py         # JSONL 日志与轮转
│   ├── paths.py                 # 仓库路径推导
│   └── timeutil.py              # 时区与 PIT 时间规则
├── config/                      # 配置
│   ├── __init__.py
│   └── loader.py                # YAML 加载、校验、config_hash
├── acceptance/                  # 阶段验收闸（程序化判据，只读）
│   ├── __init__.py
│   └── p1.py                    # P1 质量/连续性/一致性判据与汇总
└── data/                        # 数据层
    ├── __init__.py
    ├── models.py                # 枚举与数据类
    ├── pit.py                   # PIT 字段
    ├── normalize.py             # 规范化与质量评估
    ├── manual.py                # 手工 CSV 校验与模板
    ├── store.py                 # 原始/规范化存储
    ├── pipeline.py              # 运行链路状态文件
    ├── trade_calendar.py        # 上交所交易日历抓取/落盘/读取（连续性分母）
    ├── news_events.py           # 快讯标题→地缘事件结构化行（规则表来自 strategy.yaml）
    ├── remediation.py           # 数据问题自动识别、安全处置与消费能力清单
    ├── ingest.py                # 抓取编排
    └── adapters/                # 数据源适配器
        ├── __init__.py
        ├── base.py              # 注册表与上下文
        ├── market.py            # ETF/SGE/COMEX/WTI/AU0
        ├── macro.py             # SPDR/美债/USDCNY/CFTC/手工序列
        └── geo.py               # GPR/GDELT/快讯/事件日历
```

### 2.1 生产 Python 文件作用

路径均相对 `src/main/python/`。

| 文件 | 作用 | 关键约定 |
|---|---|---|
| `com/sun/shensuan/gold/__init__.py` | 包标记 | 不要求注释 |
| `com/sun/shensuan/gold/_bootstrap.py` | 把 `src/main/python` 挂到 `sys.path`，供直接运行的脚本使用 | 返回仓库根；不写死绝对路径 |
| `com/sun/shensuan/gold/cli.py` | CLI 入口：`preflight`、`ingest`、`normalize`、`status`、`coverage`、`rate`、`calendar`、`list-datasets`、`validate-manual`、`extract-events`、`accept`；`coverage` 输出附连续性摘要；`rate --date` 打印某一天的 USD/CNY 汇率选择结果（折算价优先、中间价兜底）；`calendar [--date] [--refresh]` 读取或刷新上交所交易日历；`extract-events [--write] [--news …]` 从快讯标题抽地缘事件；`accept --stage P1` 跑阶段验收闸；`daily [--run-id …]` 一键顺序跑 preflight→ingest→accept→doctor→rate（默认 run-id 含时分秒 `daily-YYYYMMDD-HHMMSS`，避免同日重跑撞原始快照 append-only 致全源误判 FAILED） | 输出 JSON；退出码 `0` 正常/验收通过、`1` 链路失败/验收判据存在 FAIL/日历不可用或未覆盖基准日、`2` 配置/输入错误；`continuity` 只展示，不改数据集状态；`rate`/`accept` 只读，不抓取不写盘；`calendar` 只在 `--refresh` 时联网并写 `data/reference/`；`extract-events` 默认 dry-run 不落盘，`--write` 才原子写 `data/manual/geo_events.csv` 且只替换 `XGE-` 前缀行 |
| `com/sun/shensuan/gold/common/atomic.py` | 原子写文本/字节/JSON：临时文件 + `fsync` + `os.replace` | 失败清理临时文件；不产生半截文件 |
| `com/sun/shensuan/gold/common/errors.py` | 项目异常层级：配置、适配器、降级、手工数据、质量闸 | 失败显式，禁止静默吞异常 |
| `com/sun/shensuan/gold/common/hashing.py` | 规范化 JSON 与 SHA-256（文本/字节/文件/对象） | 键排序、UTF-8、NaN/Inf→`null`；跨运行可复现 |
| `com/sun/shensuan/gold/common/logging_setup.py` | 结构化 JSONL 日志，按日一个文件，清理超期日志 | 保留天数来自配置；日志不含密钥 |
| `com/sun/shensuan/gold/common/paths.py` | 仓库根路径推导与环境变量覆盖 | 所有路径从仓库根派生 |
| `com/sun/shensuan/gold/common/timeutil.py` | 上海时区、ISO 时间、日期解析、`available_at` 规则、PIT 过滤、事实包临时行剔除 | 存储带时区；`available_at > cutoff` 与 `PROVISIONAL` 行不得进入事实包 |
| `com/sun/shensuan/gold/config/loader.py` | 四份 YAML 的加载、schema 校验、`config_hash` | 缺键/类型错即失败；哈希进入决策元数据 |
| `com/sun/shensuan/gold/acceptance/p1.py` | P1 阶段验收闸：一致性 C1–C7+重放 S1、连续性 N1–N3、质量 Q1–Q3（共 14 项），逐条 PASS/FAIL/SKIPPED 并汇总 verdict | 只读，不写盘；阈值全部来自 `application.yaml` 的 `acceptance`；任一 FAIL ⇒ verdict FAIL 且 CLI 退出码 1；无质量报告抛 `GoldError` 由 CLI 返回 2；SKIPPED 只用于输入确实不存在，不冒充 PASS；C3 按 `spec.key_columns` 判唯一；C5 中 `MANUAL` 只披露不降档（与 `normalize` 同规则）；N1 分子按命中的唯一日、分母按数据集 `calendar` 口径，news/events 判 SKIPPED 并写原因；Q3 要求 `_quality.json` 的 `config_hash` 与当前配置一致 |
| `com/sun/shensuan/gold/acceptance/__init__.py` | 包标记，导出 `CheckResult`、`run_p1_acceptance` | 不写业务逻辑 |
| `com/sun/shensuan/gold/data/models.py` | `Quality`/`NodeStatus` 枚举；抓取、规范化、质量、摄取结果数据类 | 状态词与 AGENTS §4.5 一致 |
| `com/sun/shensuan/gold/data/pit.py` | 为每行补 `source/as_of/available_at/fetched_at/ingest_run_id/vintage/quality/flags` | 无 vintage 的修订数据标 `REVISED_ONLY`；抓取早于可见时点标 `PROVISIONAL` |
| `com/sun/shensuan/gold/data/vintage.py` | 解析 GPR 原始快照，按 `available_at` 与最大滞后挑出可见 vintage | 只读原始层；历史段超 7 天返回 `None`，不反推修订值 |
| `com/sun/shensuan/gold/data/normalize.py` | 统一列名/类型/单位/排序与 ISO 日期，去重，剔除非法行，生成质量评估与连续性摘要 | 非法行进入 rejected；缺失不填 0；`PROVISIONAL` 行降黄；同日后来有效的旧拒绝不计最近缺失；近期坏行按最近 5 个有效行的日期区间判定；`summarize_continuity()` 只作覆盖展示；行级 `quality` 只允许 `GREEN/DEGRADED`，数据集三档见 `DatasetQuality.status` |
| `com/sun/shensuan/gold/data/rates.py` | `select_usdcny`：折算价优先、中间价兜底、同日不混算、同日差异 >0.05% 标 `DEGRADED` | 不抓取不写盘；MISSING 时 `value=None`，不填 0；盘中临时值（`PROVISIONAL`）不参与差异比较；P2 事实包只从这里取汇率 |
| `com/sun/shensuan/gold/data/trade_calendar.py` | 抓取/落盘/读取上交所交易日历，给出连续性判据的分母：`cn_sse` 用官方交易日，`weekday` 用周一至周五近似 | 抓取失败抛 `AdapterError`，不返回空日历；空日历拒绝落盘；读盘校验 sha256，不符即视为不可用；日历不可用时 `expected_days()` 回落近似并把原因写进口径标签，读数不得冒充官方；`source_url` 取自 akshare 函数 docstring |
| `com/sun/shensuan/gold/data/news_events.py` | 从已入库快讯**标题**按 `strategy.yaml` 的 `geo.news_events` 关键词表抽出资金地缘事件行（`event_id/date/type/severity/region/source_url/available_at`），并与人工登记行合并 | 不读正文、不调用 LLM、不联网；`rules_version` 内嵌进 `event_id`，改关键词必须同批改版本号并重跑历史对表（§6.2）；`excludes` 按命中位置抵消（「黄金爆炸性上涨」不算爆炸事件）且必须包含本规则关键词，否则加载期拒绝；同 `(type, 地区)` 在冷却期内只算一条，未点明地区的报道并入具体地区事件；`event_id` 不以 `XGE-` 开头的人工行原样保留；severity 未经人工复核不得作为加仓依据（§8.5） |
| `com/sun/shensuan/gold/data/manual.py` | 手工 CSV 必填列、PIT 字段、严重度与时间校验；生成模板 | 缺来源/时间戳不得进入事实包；模板不覆盖已有文件 |
| `com/sun/shensuan/gold/data/store.py` | `RawStore` 原始快照 append-only；`NormalizedStore` 幂等合并、rejected 落盘、质量报告与自动诊断报告 | 原始字节不改写；规范化可重复执行；盘中临时行可被收盘后同值快照升级；`_remediation.json` 与 `_quality.json` 同批原子写 |
| `com/sun/shensuan/gold/data/remediation.py` | 读质量报告/留档/原始快照/手工事件，自动识别已知问题，产出问题清单与每数据集消费能力（`display/feature/score/promotion_eligible`、`price_only`、禁入字段/特征） | 只隔离与降能力，不补值、不猜 severity、不把 RED 洗绿；需人判断的标 `NEEDS_HUMAN`；`AUTO_RESOLVED` 只用于坏行留档/历史零值等确定性动作 |
| `com/sun/shensuan/gold/data/pipeline.py` | 运行链路节点状态写入与读取；残留 `RUNNING` 超时判定 | 原子写 `pipeline_status.json`；失败向下游传播 |
| `com/sun/shensuan/gold/data/ingest.py` | 适配器调用、超时/重试、原始快照、规范化、质量闸、报告 | 单源失败不阻塞其他源；缺失/降级显式；`quality_gate` 与 `_quality.json` 的 `global_status` 同口径，任一数据集 RED 即降级；子集运行与旧报告合并并标 `scope/refreshed/inherited` |
| `com/sun/shensuan/gold/data/adapters/base.py` | 适配器注册表、`AdapterContext`、可用适配器枚举 | 新适配器通过注册表接入 |
| `com/sun/shensuan/gold/data/adapters/market.py` | ETF `518880`/`159934`、SGE `Au99.99`、COMEX/WTI、沪金主力 `AU0` | COMEX/WTI 零持仓标降级；AU0 非官方指数 |
| `com/sun/shensuan/gold/data/adapters/macro.py` | SPDR 持仓、美债 10Y、BOC `USD/CNY`、SAFE 中间价、CFTC、手工序列 | 两条汇率序列都是每 100 外币价 /100；禁止拼接混用；TIPS 缺失标 `MISSING/DEGRADED` |
| `com/sun/shensuan/gold/data/adapters/geo.py` | GPR、GDELT、CLS/Sina/SHMET 快讯、手工事件日历 | GPR 日期列用 `date`；无 vintage→`REVISED_ONLY`；GDELT 429 降级 |
| `com/sun/shensuan/gold/data/adapters/__init__.py` | 导入并注册各适配器 | 不要求注释 |

## 3. 测试代码目录（现状）

测试按层次分目录；测试根目录只保留 `conftest.py`、`fixtures/`、`experimental/`，禁止平铺 `test_*.py`。

```text
src/test/python/com/sun/shensuan/gold/
├── conftest.py                  # 全局离线夹具与路径引导
├── fixtures/                    # 固定夹具数据（当前为空，后续放 CSV/JSON）
├── experimental/                # 非正式代码验证区（计划；验证后迁入 main）
├── unit/                        # 单元测试
│   ├── common/
│   │   ├── test_hashing.py
│   │   ├── test_logging.py
│   │   └── test_timeutil.py
│   ├── config/
│   │   └── test_config.py
│   ├── acceptance/
│   │   └── test_p1.py
│   └── data/
│       ├── test_manual.py
│       ├── test_news_events.py
│       ├── test_normalize.py
│       ├── test_rates.py
│       ├── test_remediation.py
│       ├── test_store.py
│       ├── test_trade_calendar.py
│       └── test_vintage.py
├── contract/                    # 数据契约/质量闸测试
│   ├── adapters/
│   │   ├── test_geo_adapter.py
│   │   ├── test_gpr_adapter.py
│   │   ├── test_macro_adapter.py
│   │   └── test_network_smoke.py
│   └── data/
│       └── test_quality.py
├── integration/                 # 集成测试
│   ├── cli/
│   │   └── test_cli.py
│   └── data/
│       ├── test_ingest_offline.py
│       └── test_pipeline.py
└── architecture/                # 架构与工程约束测试
    └── test_architecture.py
```

### 3.1 测试 Python 文件作用

路径均相对 `src/test/python/`。

| 文件 | 层次 | 作用 |
|---|---|---|
| `com/sun/shensuan/gold/conftest.py` | 全局 | 配置、临时目录、ETF 夹具、抓取结果构造器 |
| `com/sun/shensuan/gold/unit/common/test_hashing.py` | 单元 | 规范化 JSON 键排序、哈希顺序无关、numpy/NaN 可序列化 |
| `com/sun/shensuan/gold/unit/common/test_logging.py` | 单元 | 日志保留窗口、JSONL 格式、保留天数校验、重复初始化关闭旧句柄 |
| `com/sun/shensuan/gold/unit/common/test_timeutil.py` | 单元 | `available_at` 各规则、时区、日期解析、未来数据过滤、事实包临时行剔除 |
| `com/sun/shensuan/gold/unit/config/test_config.py` | 单元 | 配置加载、哈希稳定性、缺键/未知键拒绝、价格区间/主键/PIT 规则校验 |
| `com/sun/shensuan/gold/unit/acceptance/test_p1.py` | 单元 | P1 每条验收判据的放行格与判红格：PIT 字段/时序/主键/区间/行级质量可复现/缺失不冒充/剔除不泄漏/覆盖率/核心不断供/周频不误报/原始重放逐值对表/质量报告自洽/原始层只增 |
| `com/sun/shensuan/gold/unit/data/test_manual.py` | 单元 | 手工 CSV 必填列、严重度、时间戳、重复 ID、模板不覆盖 |
| `com/sun/shensuan/gold/unit/data/test_normalize.py` | 单元 | PIT 字段、去重、NaN、快讯去重、事件空表、缺列、`PROVISIONAL` 标记、`date` 统一 ISO 日期、行级 `quality` 两值口径 |
| `com/sun/shensuan/gold/unit/data/test_rates.py` | 单元 | `select_usdcny` 四分支：折算价优先、中间价兜底（含折算价缺行）、双双不可用 MISSING、同日不混算；差异闸正负对照（超阈值 DEGRADED / 低于阈值不报）；GREEN 空值不冒充；字符串输入可算 |
| `com/sun/shensuan/gold/unit/data/test_remediation.py` | 单元 | 自动诊断放行/判红对照：近期坏行留档与离开窗口行数、系统性零流降价格-only、个别历史零值不封字段、SPDR 低覆盖禁流量特征、GPR 无 vintage 禁晋升、事件 severity 未复核进队列、日历近似/缺失、配置哈希不一致全封锁、干净报告不误报、`doctor` 写盘与退出码 |
| `com/sun/shensuan/gold/unit/data/test_store.py` | 单元 | 原子/稳定格式、幂等合并、原始快照 append-only、rejected 保留、临时行升级 |
| `com/sun/shensuan/gold/unit/data/test_vintage.py` | 单元 | GPR 快照 PIT 选择、修订 vintage 生效、历史段拒绝、sha256 与无时区参数 |
| `com/sun/shensuan/gold/unit/data/test_trade_calendar.py` | 单元 | 工作日近似排周末、日历存回读、四种「日历不可用」各带原因（缺文件/缺 meta/正文被改写/空文件）、空日历拒绝落盘、未知口径直接拒绝、`cn_sse` 不可用时回落近似并披露原因、抓取失败抛 `AdapterError` |
| `com/sun/shensuan/gold/unit/data/test_news_events.py` | 单元 | 事件抽取规则表校验（缺版本/空规则/severity 越界/关键词空/type 重复/excludes 不含本规则关键词）、标题命中与最短长度、`excludes` 按命中位置抵消（「爆炸性上涨」不算爆炸、真爆炸仍算）、同类型冷却期合并、未点明地区并入具体地区事件、一条标题命中两类型各算一次、坏 `published_at`（naive/不可解析）拒绝、人工行不被覆盖 |
| `com/sun/shensuan/gold/contract/adapters/test_geo_adapter.py` | 契约 | 快讯单行坏时间只丢该行并计数、正文去重、GDELT 429/超时降级 |
| `com/sun/shensuan/gold/contract/adapters/test_gpr_adapter.py` | 契约 | GPR 本地 CSV 解析、列名大写、缺 `GPRD` 报错、`REVISED_ONLY` |
| `com/sun/shensuan/gold/contract/adapters/test_macro_adapter.py` | 契约 | USD/CNY 每 100 外币价 ÷100、按日期区间过滤 |
| `com/sun/shensuan/gold/contract/adapters/test_network_smoke.py` | 契约 | 15 个启用非手工适配器的真实联网冒烟；按数据集参数化，默认 `network` 标记排除 |
| `com/sun/shensuan/gold/contract/data/test_quality.py` | 契约 | 新鲜度、过期、零成交量、工作日缺口、非法行、近期缺口（含夹在有效行之间的拒绝日）、旧拒绝覆盖、`PROVISIONAL` 识别、连续性摘要（日频稀疏源 warning、周频源不误报） |
| `com/sun/shensuan/gold/integration/cli/test_cli.py` | 集成 | CLI 命令输出、退出码、缺失状态文件、未知数据集 |
| `com/sun/shensuan/gold/integration/data/test_ingest_offline.py` | 集成 | 离线摄取状态、幂等、全源失败、超时、超时保护告警、降级、零成交量、非核心 RED 触发质量闸降级、子集报告与旧报告合并 |
| `com/sun/shensuan/gold/integration/data/test_pipeline.py` | 集成 | 节点状态流转、原子 JSON、失败传播、残留 `RUNNING`→`TIMEOUT` |
| `com/sun/shensuan/gold/architecture/test_architecture.py` | 架构 | main 不得导入 test、目录存在、gitignore、依赖固定版本、注释（含四份 YAML 配置注释）/测试布局/结构文档 |

## 4. 计划中的代码包（尚未创建）

| 包 | 阶段 | 计划职责 |
|---|---|---|
| `features/` | P2 | 特征注册表与计算（趋势、动量、波动、跨市场、流量、宏观、地缘） |
| `regime/` | P2 | 市场状态与滞回切换 |
| `strategy/` | P2 | 综合分数、卫星仓区间 `[L,H]`、逆向叠加 |
| `risk/` | P2 | 风控闸、回撤分级、波动目标、换手约束 |
| `backtest/` | P3 | 事件驱动回测、费用、基准 B0–B4、walk-forward |
| `decision/` | P4 | 决策卡、规则 trace、人工下单清单 |
| `paper/` | P4 | 纸面账本、哈希链、对账 |
| `report/` | P4 | 每日报告与图表 |
| `llm/` | P5 | 本地模型客户端、9 角色委员会、引用审计 |
| `ui/` | P6 | Streamlit 只读界面与两个写操作 |

对应测试目录在实现时同步创建：`unit/<包>/`、`contract/<包>/`、`integration/<包>/`；非正式原型先放 `experimental/`，验证通过后迁入 `main`。

## 5. 结构维护规则

1. 新增、移动、删除、重命名 Python 文件或目录，必须在同一批更新本文件的目录树与文件作用表。
2. 每个非 `__init__.py` 的模块、类、函数必须有详细中文注释；公式写清输入、输出、单位、窗口/时区。
3. `config/` 下每份 YAML 必须有文件头、顶层分节与关键字段注释；改配置值时同批改注释，注释与取值不符视为未完成。
3. 测试文件按 `unit/contract/integration/architecture` 分层；不得把 `test_*.py` 平铺在测试根目录。
4. 非正式代码先在 `src/test/.../experimental/` 验证，通过后再迁入 `src/main/...`。
5. 计划中的目录不得写成现状；未实现的功能必须标 `计划`。

## Links

- [返回](README.md)
- [Agent 行为与系统设计](AGENTS.md)
