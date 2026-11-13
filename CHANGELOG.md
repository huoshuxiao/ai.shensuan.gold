# CHANGELOG

> 追加式记录。日期使用 `Asia/Shanghai`。仅记录可复核的变化；不做收益/有效性声明。

## [数据层+运维] 2026-10-25 23:40（T-034：数据问题自动识别与安全处置）

关联任务：`看板.md` T-034、R-001/R-002/R-005/R-013；判据出处 `AGENTS.md` §6.9（v1.1）。
用户指令：「问题解决：WTI 是 DEGRADED/QUALITY_RED，另有 COMEX、SPDR、US10Y、GPR 黄灯……给解决方案及程序化验证，目标自动识别问题，自动解决问题」。

### 新增

- `data/remediation.py`：读 `_quality.json`、规范化主表、`rejected/` 留档、GPR 原始快照、手工事件表，产出 `_remediation.json`。每条问题含 `issue_id/<dataset>/code/status/action/detail/evidence`；每个数据集含 `display_eligible/feature_eligible/score_eligible/promotion_eligible/price_only/blocked_fields/blocked_features/blocked_promotions/calendar_basis/coverage_is_lower_bound/reasons`。
- `NormalizedStore.write_remediation_report()/read_remediation_report()`：原子写/读取 `data/normalized/_remediation.json`。
- `cli.py doctor [--write]`：退出码 0=`CLEAN/AUTO_CONTAINED`，1=`NEEDS_HUMAN/ERROR`，2=`_quality.json` 不存在；`--write` 才落盘，默认只打印。
- 单元测试 `unit/data/test_remediation.py` 12 条：WTI 近期坏行封分数并算 `clear_after_valid_rows`、个别历史零值不封字段、SPDR 低覆盖禁流量特征、GPR 有/无原始快照两种判决、未复核事件进队列并在 `severity_reviewed=1` 后解锁、日历近似/官方日历回落、配置哈希不符全封锁、干净报告不误报、`doctor` 写盘与退出码、报告哈希不随生成时间漂移、规范化表读坏判 `READ_ERROR`。
- 集成测试 `integration/data/test_ingest_offline.py::test_ingest_writes_remediation_report`：日链自动写 `_remediation.json`，`_quality.json` 摘要哈希与独立报告一致。
- 集成测试 `integration/cli/test_cli.py::test_coverage_includes_remediation_summary`：`coverage` 有诊断文件时附带摘要，没有时不伪造该字段。

### 变更

- `ingest` 在写 `_quality.json` 前自动生成诊断并落盘，摘要写进 `_quality.json` 的 `remediation` 节点；诊断自身抛错时摘要记 `overall=ERROR` + `errors`，不打死日链。
- `coverage` 输出若存在 `_remediation.json`，附带同一份 `remediation` 摘要。
- `report_hash` 改为**内容哈希**，排除 `generated_at`：同一质量报告与同一能力状态跨次运行哈希一致，便于对表；`generated_at` 只表示生成时刻。
- 规范化表/剔除留档/手工事件表读不出来时新增 `READ_ERROR`：四项能力全关、`NEEDS_HUMAN`，不再让异常逃出诊断。
- `AGENTS.md`：新增 §6.9（问题矩阵、三种状态、能力清单、P2 下游硬约束、不变量、现状读数与边界）；§18.6 增「与 `doctor` 的分工」；§19.2 按现读更新已实现/未实现命令；版本 v1.0→v1.1，头部状态与附录 H 同批更新。
- `PROJECT.md`：登记 `data/remediation.py`、`unit/data/test_remediation.py`、`_remediation.json` 的 store 职责（同批结构回写）。

### 现状读数（2026-10-25 23:28–23:40 CST 现跑）

```text
cli.py doctor --write → DOCTOR_EXIT=1；overall=NEEDS_HUMAN
counts={"AUTO_RESOLVED":2,"CONTAINED":9,"NEEDS_HUMAN":1}（12 条）
blocked_datasets=[comex_gc,geo_events,gpr,spdr_gold,us10y,wti_cl]
generated_at=2026-10-25T23:28:38.043653+08:00
report_hash=sha256:37b88a942e7282f5c07f570e5e9e78dbff2543714fb92e675a7e80c3dfb4a86e
review_queue=4（XGE- 事件 severity 未复核）

cli.py accept --stage P1 → ACCEPT_EXIT=0；verdict=PASS
counts={"FAIL":0,"PASS":139,"SKIPPED":19}；quality_run_id=p1-fix-20261024-e
config_hash=sha256:9e87c4e8ee099f9c4720bde3e0e455c874ac197644161d96f720eb83995b778c

/usr/bin/python3.10 -m pytest -m "not network" → 215 passed, 16 deselected in 21.31s，EXIT=0
/usr/bin/python3.10 -m compileall -q src/main/python src/test/python → EXIT=0
git diff --check → EXIT=0
```

逐项处置（真实数据）：

| issue | 状态 | 自动动作 | 关键读数 |
|---|---|---|---|
| `wti_cl:RECENT_BAD_ROWS` | CONTAINED | 隔离坏行 + 封分数 | 坏日 2026-10-22、`OHLC_INVALID`、`clear_after_valid_rows=4` |
| `comex_gc:PROVISIONAL`、`wti_cl:PROVISIONAL` | CONTAINED | 到期后重抓转正 | 各 1 行、2026-10-23、`available_at=2026-10-24T06:00+08:00` 已过 |
| `comex_gc:ZERO_FLOW`、`wti_cl:ZERO_FLOW` | CONTAINED | 价格-only、封成交量/持仓量 | `zero_rows` 5,106 / 15,282 |
| `etf_159934:HISTORICAL_ZERO_FLOW`、`shfe_au0:HISTORICAL_ZERO_FLOW` | AUTO_RESOLVED | 保留行、逐行 DEGRADED | 零行 4 / 5，不封字段 |
| `spdr_gold:LOW_COVERAGE` | CONTAINED | 只展示、封流量特征 | `weekday_gaps=2829`；`FLOW_SPDR_CHG_1W/4W` 禁入分数 |
| `gpr:REVISED_ONLY` | CONTAINED | 研究/展示、禁晋升 | 17 份快照，`2026-10-19T23:43:55+08:00..2026-10-24T00:53:35+08:00` |
| `us10y:RECENT_MISSING_ROWS` | CONTAINED | 恢复新鲜前封分数 | `recent_missing_rows=1`、`stale_days=1` |
| `*:CALENDAR_APPROX` | CONTAINED | 标注保守下界 | 8 个数据集仍按周一至周五近似 |
| `geo_events:UNREVIEWED_EVENT_SEVERITY` | NEEDS_HUMAN | 进复核队列、封 `GEO_EVENT_SEVERITY` | 4 条 `XGE-` 事件待人工复核 |

### 未做

- 未修源站缺陷：WTI/COMEX 零成交量与零持仓、SPDR 历史空洞、GPR 历史无 vintage、美国/外汇官方日历缺失均保持原状；本批只做隔离与能力封锁。
- 未补任何缺失值、未插值、未猜事件 severity、未替用户复核事件。
- `doctor` 不自动触发 `ingest`：`PROVISIONAL` 需下一次抓取转正；`wti_cl` 需 4 个新的有效行才离开最近窗口。
- 未改任何阈值/配置，`config_hash` 未变；P1 `accept` 判决仍为 PASS，P1 里程碑仍 `PENDING` 待用户签收。
- `-m network` 的 16 条未跑；未联网重抓；未提交、未 push。

## [数据层+验收] 2026-10-24 01:01（T-032/T-033：三条数据口径（乙）、剔除不复活、删除误报事件）

关联任务：`看板.md` T-032/T-033/T-028、§18、R-009/R-011/R-012/R-013；判据出处 `AGENTS.md` §6.6（2026-10-24 补）、§18.6 N1（v0.9→v1.0）。
用户指令：「D1乙，D3乙，D4乙，家乐福那条删」。

### 新增

- `DatasetConfig.drop_all_zero_rows`（默认 `false`）：series 数据集**全部必需数值列同时为 0** 时整行剔除，`reject_reason=ALL_ZERO_IN_REQUIRED`。只 `cftc_gold` 开启——GPR 的 `gprd=0` 是真实平静日（本库 2 行），全局强推会删掉真实读数。
- `DatasetConfig.positive_columns`（默认空）：声明的列出现 ≤0 时整行剔除，`reject_reason=NONPOSITIVE_<COL>`；缺值仍走 `NAN_IN_REQUIRED`，两条原因分开。当前只有 `shfe_au0.settlement`。加载期校验该列必须属于 `required_columns` 且不得重复。
- `DatasetConfig.authoritative_snapshot`（默认 `false`）：该数据集按**整表替换**合并（不做并集），当前只有 `geo_events`。
- `normalize.cadence_of()`：相邻日期中位间隔 ≥5 天判周频，供 `assess_dataset` 与验收 N1 共用（两处不同算法会给出两个覆盖率）。
- `normalize` 的 `LOW_COVERAGE` 标记：日频源覆盖率低于 `acceptance.min_weekday_coverage` 时打标并降 YELLOW；周频源不参与。
- `store.merge(..., removed=..., authoritative=...)`：前者把被剔除的主键从主表删除，后者按整表替换并从旧表取 `new_rows` 计数。
- `geo.news_events.review_exclusions`（`strategy.yaml`）：按 `content_hash` 点名人工判死的误报，附 `reason/decided_by/decided_at`；加载期校验 `sha256:`+64 位小写十六进制、理由非空、无重复。`news_events.excluded_rows()` 与实际跳过共用同一张表，CLI 单独输出 `review_excluded_rows`。
- 测试 9 条（normalize 4／store 2／news_events 2／acceptance 1）：`test_series_all_zero_rows_dropped_when_enabled`、`test_all_zero_drop_is_opt_in_so_gpr_zero_days_survive`、`test_price_nonpositive_declared_column_dropped`、`test_low_coverage_marks_dataset_degraded_only_when_floor_given`、`test_merge_removes_rejected_keys_from_main_table`、`test_merge_authoritative_replaces_whole_table`、`test_review_exclusions_drop_only_the_named_rows`、`test_review_exclusions_reject_malformed_entries`、`test_coverage_below_floor_passes_only_when_low_coverage_disclosed`（四格：已披露 PASS／未披露 FAIL／只有 `STALE` FAIL／`LOW_COVERAGE`+`GREEN` FAIL）。

### 变更

- 规范化层 `merge` 调用改为 `merge(name, frame, key_columns, removed=rejected, authoritative=spec.authoritative_snapshot)`。修前剔除只进 `rejected/`，主表仍按并集把被剔行原样复活（实跑：cftc 总数 1,940、au0 4,575、geo_events 5）。
- `ingest` 的 RED 顶格条件从「非 RED 才合并 flags」改为「规范化层判 RED 就合并」，使整批剔空时 `ALL_ROWS_DROPPED` 不被既有的 RED 状态吃掉。
- 验收 N1 在低于下限时读质量报告的 `flags/quality`：`LOW_COVERAGE` 且 quality 非 GREEN 判 PASS（读数写「已披露」）；否则 FAIL。这条豁免只认覆盖率，`STALE` 等其他黄标借不到。
- `shfe_au0` 新增 `settlement` 为必需列并声明 `positive_columns: [settlement]`；`cftc_gold` 开启 `drop_all_zero_rows`；`geo_events` 声明 `authoritative_snapshot: true`；三处 YAML 均带同批注释。
- `AGENTS.md`：§6.6 补两条（典型「源缺值被写成 0」与覆盖率披露口径）、§18.6 的 N1 行加「已披露才放行」、版本 v0.9→v1.0、头部状态改判 PASS（139/0/19）。

### 数据变化（现读 `data/normalized/`）

| 数据集 | 变化 | 现读 |
|---|---|---|
| `cftc_gold` | 剔除 2 行（1997-12-19、2003-02-18，`ALL_ZERO_IN_REQUIRED`） | 1,940→1,938 行、`quality=GREEN`、`zero_rows=0` |
| `shfe_au0` | 剔除 618 行 `settlement<=0`（`NONPOSITIVE_SETTLEMENT`） | 4,575→3,957 行、首日 2008-01-09→2010-06-08（覆盖 18.8 年→16.4 年）、`quality=GREEN` |
| `spdr_gold` | 无剔除，改按覆盖率披露 | 2,894 行、`quality=YELLOW`、`notes=[LOW_COVERAGE]`、`weekday_gaps=2829` |
| `geo_events` | 删除 2026-10-20 家乐福误报（同一则公告 3 条快讯整组排除） | 5→4 行、`sha256=b197ed4d6aace26cc1d3a2d0c0fcab13b5f3b1cd11ad38d592a43ee0bb25bb92` |

### 判决（第 4 版卡）

```
cli.py accept --stage P1 → ACCEPT_EXIT=0；verdict=PASS
counts={"FAIL":0,"PASS":139,"SKIPPED":19}（158 条 / 14 项判据）
quality_run_id=p1-fix-20261024-e；quality_scope=full
generated_at=2026-10-24T00:55:20.470800+08:00（accept 输出不含该字段，读自 data/normalized/_quality.json）
config_hash=sha256:9e87c4e8ee099f9c4720bde3e0e455c874ac197644161d96f720eb83995b778c
cli.py ingest --run-id p1-fix-20261024-e → INGEST_EXIT=0，scope=full，global_status=DEGRADED，red=[wti_cl]，core_not_green=[]
cli.py extract-events → EXTRACT_EXIT=0，events_extracted=4，review_excluded_rows=3，输入 2,463 行（shmet 2,134／cls 140／sina 189），rules_version=ge-2，dry-run
/usr/bin/python3.10 -m pytest → 201 passed, 16 deselected in 26.21s，EXIT=0（01:03:38 现跑）
```

判据读数（`/tmp/accept_fix.json`）：C6·cftc `数值列 ['long','short','net'] 无空值；非正价格 0 行；结构性零均已标记`；C7·cftc `剔除 2 行留档，原因 ['ALL_ZERO_IN_REQUIRED']`；C7·au0 `剔除 618 行留档，原因 ['NONPOSITIVE_SETTLEMENT']`；C6·au0 七列无空值；N1·au0 `唯一日=3957 应交易日=3975 命中=3951 覆盖率=99.4% 口径=cn_sse(...) 分母外日=6`；N1·spdr `覆盖率 50.5% < 下限 80%，质量报告已按 LOW_COVERAGE 披露（quality=YELLOW）`；`geo_events` 4 行 C1/C3/C5/C6/C7 全 PASS。

### 未做

- 未改任何阈值（80% 覆盖率下限、`stale_days`、价格区间未动）。
- 第 2、3 条排除条目（`b92aeedc…`、`a93db66c…`）是我按「同一则公告」补的，`decided_by=Agent` 已写明；用户只要留第一条说一声即改回。
- `-m network` 的 16 条未跑；直连 akshare 的汇率/AU0 对表未重跑（旧读数，未重读）。
- 事件 severity 仍未经用户复核；4 条事件无 3 级事件。
- P1 里程碑仍 `PENDING`：`verdict=PASS` 只证明机器闸，阶段放行待用户签收。
- 未提交、未 push。

## [数据层+验收] 2026-10-23 23:14（T-029/T-030/T-031：交易日历、事件抽取、判据自纠）

关联任务：`看板.md` T-029/T-030/T-031、§17、R-005/R-007/R-010；判据出处 `AGENTS.md` §6.2 补、§6.4、§6.6、§18.6（v0.8→v0.9）。
用户指令：①「先接交易日历再决数据质量问题」；②「第 2 问：通过新闻接口获取事件」。

### 新增

- `data/trade_calendar.py`：抓取/落盘/读取上交所交易日历。落 `data/reference/trade_calendar_cn_sse.csv` + 同名 `.meta.json`（`source`、`source_url`、`fetched_at`、`as_of_min/as_of_max`、`rows`、`sha256`），原子写；读盘校验 sha256，不符即不可用。抓取失败抛 `AdapterError`，空日历拒绝落盘——空日历会把「缺口」冒充成「无缺口」。
- `common/timeutil.py`：新增 `CALENDAR_BASE_CN_SSE/CALENDAR_BASE_WEEKDAY/CALENDAR_BASES`（只命名口径，日历本体在 data 层，与 §15.4「交易日历集中在 common」的口径一致，timeutil 仍不维护日历数据）。
- `DatasetConfig.calendar`（默认 `weekday`，加载期校验取值）、`paths.reference`（`PathsConfig` 键数 9→10）、`config/application.yaml` 与 `config/data_sources.yaml` 对应注释；境内四源（`etf_518880`/`etf_159934`/`sge_au9999`/`shfe_au0`）声明 `calendar: cn_sse`。
- `data/news_events.py`（T-030，第 2 问的落地）：快讯标题→地缘事件结构化行。规则表在 `config/strategy.yaml` 的 `geo.news_events`（`rules_version=ge-2`、`min_title_chars=8`、`dedupe_window_days=7`、5 个地区关键词表、6 类事件规则、`excludes`）；`event_id = XGE-<版本>-<日期>-<类型地区>-<来源哈希前 8 位>`；`merge_with_manual` 只替换 `XGE-` 前缀行。按 §15.13 先在 `experimental/news_events/` 验证（EXIT=0）再迁入 main，原型三文件已删除、不留两份。
- CLI `calendar [--date] [--refresh]`（0=可用且覆盖基准日、1=不可用/未覆盖、2=抓取失败）与 `extract-events [--write] [--news …]`（默认 dry-run，不写盘）。
- 判据 Q3「验收与数据同版本」：`_quality.json` 的 `config_hash` 必须等于本次验收加载的配置哈希，不一致或无记录即 FAIL——把 §18.1 纪律 1 变成程序。判据数 13 项→14 项。
- 测试：`unit/data/test_trade_calendar.py`（10 格）、`unit/data/test_news_events.py`（10 格）、`unit/acceptance/test_p1.py` 新增/改写 5 格、`contract/adapters/test_network_smoke.py` 新增日历冒烟 1 条（网络用例总数 15→16，默认仍不跑）。

### 变更（判据自身缺陷，四处；均为未提交内容就地改，无旧句可作废）

| 判据 | 改前 | 现场证据（真数据） | 改后 |
|---|---|---|---|
| N1 | 分子=行数 | `news_cls` 覆盖率 2240%、`gpr` 140%、`usdcny` 107.8% | 分子=命中的唯一日；读数并列 `唯一日/应交易日/命中/覆盖率/口径/分母外日`；与 `_quality.json` 的 `weekday_gaps` 同算法（AU0 曾同时出现 100.2% 与「缺 4 天」两个数） |
| N1/N2/N3 | 对 news/events 硬套节奏 | `geo_events` 5 行被判「38 行/年」判红 | news 的 N1、events 的 N1/N2 判 `SKIPPED` 并写原因；events 不进入节奏判定 |
| C3 | 主键硬编码 `as_of`/`content_hash` | 事件日历 2026-10-19 两个不同事件被判重复 | 按 `spec.key_columns` 判，读数报出用了哪几列 |
| C5 | 「带任何 flags 却 GREEN」即判红 | 事件行带 `MANUAL`（§7.4：披露而非降级）被判红 | 与 `normalize` 对齐：除 `MANUAL` 外的标记要求 `DEGRADED` |

另：`data/normalize.py` 的 `assess_dataset()/summarize_continuity()` 接 calendars；`DatasetQuality` 新增 `continuity_basis` 并进 `as_dict()`；`_quality.json` 新增 `calendar` 节点；连续性 warning 文案由「工作日覆盖率」改为带口径标签的「覆盖率」。`_available_at_rule_allowed` 的旧注释「六种合法规则」按现读改为「7 种」（`same_day_0915` 是 T-024 加的，注释当时没跟上）。

### 判决（第 3 版卡）

```
cli.py accept --stage P1 → ACCEPT_EXIT=1；verdict=FAIL
counts={"FAIL":3,"PASS":136,"SKIPPED":19}（158 条 / 14 项）
quality_run_id=p1-events-20261023-a；scope=full（accept 输出的 quality_run_id/quality_scope 两字段）；generated_at=2026-10-23T22:44:38.988133+08:00（不在 accept 输出里，读自 `data/normalized/_quality.json` 的 generated_at 字段）
config_hash=sha256:56397ced946cf3e8c10f8af03d01a9eb181a8455f33fd22d96a42b07df62bac1（accept 输出与 _quality.json 两处同值）
```

- 4 条判红 → 3 条：R-010（事件日历空）由 T-030 关闭；R-009（CFTC 两行全 0）、R-011（AU0 618 行 `settlement=0`）、R-012（SPDR 覆盖率 50.5%）仍在，等用户选口径（T-028）。
- 交易日历接入后的读数：518880 100.0%、159934 100.0%、SGE 99.2%（缺 20 天）、AU0 99.9%（缺 4 天）；外汇/美国源仍是 `weekday_approx` 近似（`usdcny` 99.9%、`usdcny_mid` 93.3%、COMEX 97.7%、WTI 97.6%、US10Y 95.9%）；SPDR 50.5%（换日历救不回来：近 5 年 68.0%、近 3 年 71.3%）。
- 事件抽取：2,389 行快讯 → 5 条事件（sanctions 1、trade_friction 2、regional_conflict 1、local_incident 1，无 3 级）；10-23 23:42 用 2,417 行（`news_rows_read`：SHMET 2,134／CLS 121／Sina 162）重跑 dry-run，仍 5 条且 `csv_sha256` 与已落盘文件相同；`extract-events --write` → `csv_sha256=2d2ffeb0…`、`manual_rows_kept=0`；`validate-manual` → `geo_events.csv rows=5 status=OK`。

### 未做

- 未修剩余 3 条判红、未改任何阈值；P1 里程碑保持 `PENDING`。
- 抽取事件的 severity/地区未经用户复核（2026-10-20 家乐福那条是误报候选：主体是公司报道）；未由 Agent 自行加规则去「修噪音」。
- `-m network` 的 16 条本轮未跑；直连 akshare 的汇率/AU0 对表未重跑（旧读数，未重读）。
- 本批全部改动（含 `data/reference/` 日历与 `data/manual/geo_events.csv`）未提交、未 push。

### 证据

```
/usr/bin/python3.10 -m pytest → 192 passed, 16 deselected in 18.26s，EXIT=0（10-24 00:05:49 现跑＝本批最后一次；本批共五次跑 21.19s（约 23:10）／30.35s（23:26）／27.49s（00:01:19）／22.91s（00:04:46）／18.26s（00:05:49），条数每次都是 192 passed、16 deselected）
cli.py calendar --refresh → CAL_EXIT=0（8,797 交易日，1990-12-19..2026-12-31，integrity=OK，covers_reference=true）
cli.py accept --stage P1（23:29:44 重跑，落 /tmp 后整份解析）→ ACCEPT_EXIT=1；verdict=FAIL；counts={FAIL:3,PASS:136,SKIPPED:19}；len(checks)=158；distinct rule_id=14
cli.py ingest --run-id p1-events-20261023-a → INGEST_EXIT=0（scope=full，global_status=DEGRADED，red=[wti_cl]，core_not_green=[]）
cli.py extract-events → EXIT=0（dry_run=true）；--write → EXIT=0
cli.py validate-manual → EXIT=0
安全边界静态扫描（src/main/**/*.py + config/*.yaml）→ 命中 0 行
grep -c -F 'v0.9' AGENTS.md → 3；grep -c -F '设计蓝图 v0.8' AGENTS.md → 0
wc -l（本行之后不再改这四份文档，末次现测）：AGENTS.md → 2699；看板.md → 693；CHANGELOG.md → 752；report/acceptance/P1/acceptance.md → 168；PROJECT.md → 209
本证据块内 `wc -l` 四个数（曾写 2703/700/762/165）与 pytest 用时（曾只写单次读数 21.19s）落笔时是算术猜测，非命令输出；已在 10-23 23:26–10-24 00:01 就地改掉、无旧句可作废，现测值见本块上方两行。
（`src/`、`config/`、`data/`、`report/`、`看板.md`、`CHANGELOG.md` 未入库，无 HEAD 基线，增删行数不可现量，故只报 `wc -l` 现读）
```

## [验收+数据] 2026-10-23 21:16（T-026/T-027/T-028：P1 程序化验收闸与首轮判决）

关联任务：`看板.md` T-026/T-027/T-028、§16、R-009–R-012；判据出处 `AGENTS.md` §18.6（v0.6→v0.7）。
用户裁定原文：「数据问题，以数据质量，连续性，一致性为标准，用程序进行标准化验收，不通过人工人眼验证」。

### 新增

- `acceptance/p1.py`（691 行）：13 条判据 C1–C7、S1（一致性）、N1–N3（连续性）、Q1–Q2（质量报告与原始层只增）。每条判据按数据集逐条出 `PASS/FAIL/SKIPPED` 与读数；`SKIPPED` 必须写原因且不计入通过。
- CLI 子命令 `accept --stage P1 [--date YYYY-MM-DD]`：退出码 `0=verdict PASS`、`1=verdict FAIL`、`2=参数/配置/质量报告缺失`；`--stage` 只接受 `P1`。
- `config/application.yaml` 的 `acceptance` 段 + `config/loader.py::AcceptanceConfig`（`extra="forbid"`，带 `min_weekday_coverage∈[0,1]`、`max_stale_days≥1`、周频窗口 `max≥min`、`replay_kinds⊆{price,series}` 校验）：`min_weekday_coverage=0.8`、`max_stale_days=5`、`weekly_rows_per_year=[40.0,60.0]`、`replay_kinds=["price","series"]`。改这些值即改判据，会进 `config_hash` 并使旧验收卡作废（§18.1 纪律 1）。
- `unit/acceptance/test_p1.py`（384 行）：每条判据配放行格与判红格，判红格只动被试数据。
- `AGENTS.md` §18.6「P1 数据验收程序化：不用人工人眼验证」（第 2012–2057 行，追加 46 行）：入口与退出码、13 条判据表、判据口径（节奏自算、快照按 `mtime`、浮点按落盘格式对表、`SKIPPED` 语义）、验收卡必须引用 `accept` 输出、`verdict=FAIL` 时里程碑不得标 `DONE`。
- `report/acceptance/P1/acceptance.md`（133 行）：机器闸一节改为引用程序判决；4 条判红逐条现读复核；D1–D4 处理选项与各自代价。

### 变更（就地改，均为未提交内容，无旧句可作废）

- `AGENTS.md` 首行状态 `v0.6`→`v0.7` 并写明「首轮程序化验收判 `verdict=FAIL`，阶段未通过」；§0.6 版本行追加 `v0.7` 一句；附录 H 追加 v0.7 行。
- `看板.md`：T-020 由 `PENDING`→`DONE`（19:22 全量重抓满足其验收标准）；T-015/§15.4 与 T-023 行里「折算价 10-23 仍 `PROVISIONAL`／`_quality.json` 仍是 run c」两句按现读改写；新增 T-026/T-027/T-028、§16、R-009–R-012；P1 里程碑说明与 DoD 的「离线测试」读数更新。
- `report/acceptance/P1/acceptance.md` 第 1 版的「机器闸通过（8/8）」作废：该结论出自人工目视 8 项核对，与本轮程序判决冲突（同一批数据现判 4 条 FAIL）。旧文件未提交、HEAD 中不存在，故就地重写并声明。

### 首轮判决与 4 条判红（数据缺陷，不是尺子读错）

```
命令：/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py accept --stage P1
退出码：1
读数：verdict=FAIL；counts={"FAIL":4,"PASS":132,"SKIPPED":13}；quality_run_id=p1-accept-20261023-a；scope=full
config_hash=sha256:4518785e8eda6abac0ff05e3798a5a3f0da99b330310444aec26ced2a6ef71fa
```

| 判据 | 判红内容 | 现读复核（同一轮） |
|---|---|---|
| C6·`cftc_gold` | `long/short/net` 各 2 个 0 未标 `ZERO_*/MISSING_*` | 1,940 行中 `1997-12-19`、`2003-02-18` 三列全 0，`quality=GREEN`、`flags` 空；`data_sources.yaml:218` 为 `zero_fields: []` |
| C1·`geo_events` | 规范化表为空却被当正常 | 文件 119 字节、0 行；`_quality.json` 的 `missing=[]` |
| C6·`shfe_au0` | `settlement` 618 个非正值 | 4,575 行中 `<=0` 618 行（`2008-01-09..2024-09-25`）；`>0` 最早 `2010-06-08`；`quality` 分布 GREEN 4,569 / DEGRADED 6 |
| N1·`spdr_gold` | 覆盖率 50.6% < 80% | 手算 2,894/5,720=50.6%；近 5 年 887/1,304=68.0%、近 3 年 559/784=71.3%，只看近年仍不达标 |

4 条各给 3 个处理选项与代价（D1–D4），等用户逐条选口径（T-028）；Agent 不自行放宽判据。

### 校准过程中被抓出的 7 处尺子缺陷（都已同批改掉并留回归格）

空 `flags/vintage` 回读成 NaN 致 C1/C5/C6/C7 恒红；C3 对 news 要求 `as_of` 唯一（真实 `news_shmet` 有 210 个重复时间戳但 `content_hash` 唯一）；C6 把结构性 0 一律要求披露并把 `content_hash` 当数值比；C7 把「早先被拒、后来有值」（US10Y 2026-10-19/10-20）当漏档；`_latest_raw_csv` 按文件名取到 13:28 旧批次致 S1 假判红；浮点用 `==` 比出 `6.4854` vs `6.485399999999999` 的假差异；周频节奏从 `_quality.json`（当时无内容）读，会让 CFTC 周频被 20.1% 覆盖率假判红——改为 `classify_cadence` 自算并加 N3。

### 数据侧同批变化（T-020 的落地）

19:22 全量重抓 `p1-accept-20261023-a`（`scope=full`、`refreshed` 含 16 个启用数据集、`inherited=[]`）：`usdcny` 2026-10-23 = `6.718`，`available_at=2026-10-23T18:00+08:00`、`fetched_at=19:22:36`、`quality=GREEN`、`flags` 空——T-020 的「18:00 后最终值」条件已满足。COMEX/WTI 的 10-23 行仍 `PROVISIONAL`（`available_at=2026-10-24T06:00+08:00`），07:00 事实包各剔 1 行。

### 未做

- 未修任何一条判红数据；未改任何阈值。P1 里程碑保持 `PENDING`。
- `-m network` 的 15 条用例本轮未跑；直连 akshare 的汇率/AU0 全历史对表本轮未重跑（旧读数，未重读）。
- 交易日历仍未接入，N1 覆盖率把国内外节假日都算成缺口，数字偏保守。
- 本批全部改动未提交、未 push。

### 证据

```
/usr/bin/python3.10 -m pytest
→ 166 passed, 15 deselected in 24.04s，PYTEST_EXIT=0

/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py accept --stage P1 > /tmp/accept_p1.json
→ ACCEPT_EXIT=1；stdout 34,163 B、stderr 0 B

grep -n '^### 18\.6' AGENTS.md → 2012
grep -c -F 'v0.7' AGENTS.md → 3（首行状态、§0.6 版本行、附录 H）
grep -c -F '设计蓝图 v0.6' AGENTS.md → 0
git show HEAD:AGENTS.md | grep -c -F '设计蓝图 v0.6' → 0（版本行本就是未提交内容）

wc -l AGENTS.md → 2679；看板.md → 631；report/acceptance/P1/acceptance.md → 133
（`src/`、`config/`、`CHANGELOG.md`、`看板.md`、`report/` 未入库，无 HEAD 基线，增删行数不可现量，故只报 `wc -l` 现读）
```

## [口径] 2026-10-23（T-024：汇率同日兜底，折算价优先、中间价兜底）

关联任务：`看板.md` T-024/T-025、§15。用户方案「当日能取到折算价用折算价，不能取到用中间价」两步中的第一步落地；同日两条序列实测同源（1,223 天仅 1 天差 0.019%），数值上无损。

### 变更

- `common/timeutil.py`：`AVAILABLE_AT_RULES` 新增 `same_day_0915`（当日 09:15 可见，人民币中间价发布口径），实现与 docstring 同批。
- `config/data_sources.yaml`：`usdcny_mid.available_at_rule` 由 `next_day_0600` 改为 `same_day_0915`，注释同批（含观察期降回 `same_day_1530` 的预案）。
- `data/rates.py`（新增）：`select_usdcny` 确定性选择——折算价 GREEN 且无 `PROVISIONAL` 优先；否则中间价兜底（fallback_reason 留痕）；两条都不可用标 `MISSING`；同日两条都可用时算相对差异 |boc−mid|/mid，>0.05% 标 `diverged` 且 quality 压 `DEGRADED`；盘中临时值不参与差异比较；绝不平均/拼接。
- `cli.py`：新增 `rate --date YYYY-MM-DD` 只读探针（选择函数的消费入口，不抓取不写盘）。
- `AGENTS.md` §5.2 追加同日兜底条款；版本 v0.5→v0.6（§0.6 与附录 H 同批）。
- `PROJECT.md`：登记 `data/rates.py`、`unit/data/test_rates.py`；更新 `cli.py` 职责行。

### 数据

- `cli.py ingest --datasets usdcny_mid --run-id p1-t023-20261023` → EXIT=0；`usdcny_mid` 10-23 行 `available_at=2026-10-23T09:15:00+08:00`、`quality=GREEN`、flags 空（同日转正）；`_quality.json` 首次写入 `scope=subset`、`refreshed=[usdcny_mid]`、`inherited=[其余 15 个]`（顺带验证 T-023 合并逻辑）。
- `cli.py rate --date 2026-10-23` → `value=6.718 source=safe.rmb_mid quality=GREEN fallback_reason=折算价不可用（缺失或 PROVISIONAL）…`；`--date 2026-10-22` → `source=akshare.currency_boc_sina value=6.7195 quality=GREEN`（折算价优先分支）。

### 测试

```
/usr/bin/python3.10 -m pytest → tests=135 failures=0 errors=0 skipped=0 time=21.729，EXIT=0（15 条 network 默认排除）
```

新增回归：`unit/common/test_timeutil.py::test_same_day_0915_rule`；`unit/data/test_rates.py`（9 条：优先/兜底/MISSING/不混算/差异闸正负对照/GREEN 空值/字符串输入）；`unit/config/test_config.py` 追加 `available_at_rule == "same_day_0915"` 断言；`integration/cli/test_cli.py::test_rate_command`。


## [修复+口径] 2026-10-23（T-023：三项口径统一）

关联任务：`看板.md` T-023、§14。本批统一 T-019 文档矛盾、子集质量报告覆盖风险、行级 `quality` 定义。

### 变更

- `data/ingest.py`：新增 `_summarize_quality()`，全量与子集报告共用同一套 `global_status/missing/degraded/red/core_not_green/revised_only` 计算；新增 `_merge_quality_report()`：全量运行写 `scope=full`；`ingest --datasets` 子集运行与上一份 `_quality.json` 合并，未刷新数据集标 `inherited=true`、`inherited_run_id`、`inherited_generated_at`，`refreshed` 列本次刷新；没有旧报告时仍写子集报告但显式标 `scope=subset`，不冒充全量。
- `data/normalize.py`、`data/models.py`：口径注释/文档串统一为「行级 `quality` 只有 `GREEN/DEGRADED`；数据集三档 `GREEN/YELLOW/RED` 属 `DatasetQuality.status`」；不改任何运行行为。
- `AGENTS.md` §6.5、§6.6：追加行级 quality 口径段与质量报告范围段，均为追加并注明以新段为准。
- `PROJECT.md`：`data/ingest.py`、`data/normalize.py`、`unit/data/test_normalize.py`、`integration/data/test_ingest_offline.py` 的职责描述同步。
- `看板.md`：T-019 验收改为「WTI 10-22 源行 OHLC 不自洽已剔除并判 RED」；T-020 风险补充子集报告已合并；新增 T-023 与 §14。

### 测试

- `integration/data/test_ingest_offline.py`：`test_subset_ingest_merges_previous_quality_report`——首次 ETF+COMEX（COMEX 抛 `DegradedSourceError` 判 RED），第二次仅 ETF；断言 `scope=subset`、`refreshed=["etf_518880"]`、`inherited=["comex_gc"]`、`inherited_run_id="run-first"`、COMEX 仍在 `red`、`global_status=DEGRADED`，且落盘 `_quality.json` 与返回值一致。
- `unit/data/test_normalize.py`：`test_row_quality_is_green_or_degraded_only`——零成交行行级为 `DEGRADED`，行级集合只含 `GREEN/DEGRADED`，数据集状态仍可为三档。

### 证据

```
/usr/bin/python3.10 -m pytest -q --junitxml=/tmp/pytest_t023.xml
→ tests=124 failures=0 errors=0 skipped=0 time=16.676；EXIT=0

/usr/bin/python3.10 -m pytest -o addopts="" --collect-only -q
→ 139 tests collected，EXIT=0（124 执行 + 15 条 network 默认排除）

/usr/bin/python3.10 -m compileall -q src/main/python src/test/python
→ EXIT=0

git diff --check
→ EXIT=0
```

### 未做 / 仍缺

- 当前 `data/normalized/_quality.json` 仍是 `run_id=p1-refresh-20261023-c`（13:36），不含本次新增的 `scope/refreshed/inherited` 字段；下一次全量 ingest（T-020，18:00 后）才会写入新口径。旧报告的质量结论不变。
- 未重写已落盘的规范化 CSV；行级 `quality` 只统一文字与测试口径，数据行不动。
- 未提交、未 push。

## [P1 修复] 2026-10-23（T-022：验收项 3/5）

### 变更

- `src/main/python/com/sun/shensuan/gold/data/normalize.py`：price/series/events 的 `date` 与 `as_of` 统一为 `YYYY-MM-DD`；新增 `summarize_continuity()`，从质量明细生成工作日缺口/覆盖率/样例摘要；日频源覆盖率 <80% 给 warning，周频源（40–60 行/年）不误报。
- `src/main/python/com/sun/shensuan/gold/cli.py`：`coverage` 输出新增 `continuity` 第一屏摘要；只展示，不改变数据集状态，避免把节假日误判成 RED。
- `src/test/python/com/sun/shensuan/gold/unit/data/test_normalize.py`：新增 `test_date_column_is_iso_date`（`T00:00:00` → `YYYY-MM-DD`）。
- `src/test/python/com/sun/shensuan/gold/contract/data/test_quality.py`：新增 `test_continuity_summary_flags_sparse_daily_and_ignores_weekly`（SPDR 日频稀疏给 warning、CFTC 周频不误报）。
- `AGENTS.md` v0.5：§6.4 规范化层日期格式；§6.6 新增工作日缺口行；§18.3 P1 的“质量缺口可见/单位与时区”同步；§0.6 与附录 H 记录版本。
- `PROJECT.md`：`cli.py`、`normalize.py` 与两条测试的职责描述同步；无新增/移动/删除 Python 文件。
- 数据迁移：`data/normalized/comex_gc.csv`（2,553 行）、`gpr.csv`（15,267 行）、`wti_cl.csv`（7,641 行）及 `rejected/comex_gc.csv`（40 行）、`rejected/wti_cl.csv`（52 行）的 `date` 由 `YYYY-MM-DDT00:00:00` 改为 `YYYY-MM-DD`；共 25,553 个日期字符串，行数与所有业务值未变；`data/raw` 未动。

### 证据

```
/usr/bin/python3.10 -m pytest（JUnit）
→ tests=122 failures=0 errors=0 skipped=0，17.031s，EXIT=0

cli.py coverage
→ EXIT=0；continuity 11 条；spdr_gold gaps=2829 coverage=0.5054 warning；cftc_gold cadence_hint=weekly 无 warning

迁移后 date 格式扫描
→ 12 个含 date 列的规范化文件 bad_T00=0；5 个受影响文件行数不变（2553/15267/7641/40/52）

git diff --check
→ EXIT=0
```

### 未做

- T-020（BOC 18:00 后最终重抓）仍未执行；P1 不签收。
- 未创建 `report/acceptance/P1/acceptance.md`。
- 未解决 159934 水平跳变、BOC/SAFE 两序列几乎相同、SPDR 源发布口径三项一致性风险。
- 未提交、未 push。

## [设计文档] 2026-10-23（T-021：阶段路线图验收标准）

### 新增

- `AGENTS.md` §18.1：三层闸（机器闸/非专业人工闸/签字闸）与五条判定纪律（同版本证据、负对照、独立复算、通过≠盈利、证据可回溯）。
- `AGENTS.md` §18.2：非专业四问（数字来源、缺口可见、不越权、结论可作废）与“看不懂＝不通过”规则；明确用户不需要判断金融逻辑，只需判断可观察项。
- `AGENTS.md` §18.3：P0–P8 逐阶段验收表（9 个阶段、59 条验收项），每阶段列机器证据、非专业怎么看、不通过条件；覆盖数据覆盖/PIT/单位时区/质量闸、事实包确定性、regime 滞回与仓位不变量、回测会计与样本外、LLM 引用审计与留痕、UI、运维与 shadow。
- `AGENTS.md` §18.4：一页验收卡模板（`report/acceptance/P<n>/acceptance.md`，计划目录）与证据要求；§18.5：与 §16.4 DoR/DoD、§17 测试层、§12.7 晋升闸的关系。

### 变更

- `AGENTS.md` 版本 v0.3→v0.4；§0.6 版本行与附录 H 同步。
- 自 P1 验收起，阶段里程碑标 `DONE` 除既有 DoD 外还需一页验收卡；P0 与已完成的历史任务不追溯。

### 未做

- 未执行任何阶段验收，未创建 `report/acceptance/` 卡片；本批只有判据、模板与文档，不代表任何阶段已通过。
- 未改代码、配置、数据；未提交、未 push。

### 证据

```
grep -n '^### 18\.[1-5]' AGENTS.md
→ 1826/1846/1863/1971/1998

grep -n '^### 0\.6\|^| v0\.4' AGENTS.md
→ 84:### 0.6 本文件维护；2619:| v0.4 | 2026-10-23 | …

/usr/bin/python3.10 -m pytest
→ 120 passed, 15 deselected in 17.44s，EXIT=0

git diff --check
→ EXIT=0
```

## [修复+数据] 2026-10-23（T-019：P1 验收数据刷新与 PROVISIONAL 门）

### 新增

- `common/timeutil.py`：`provisional_mask()` 与 `filter_fact_pack()`；事实包过滤同时要求 `available_at <= cutoff` 与 `fetched_at >= available_at`，`PROVISIONAL` 行不得进入。
- `data/pit.py`：`fetched_at < available_at` 的行标 `PROVISIONAL`、`quality=DEGRADED`；`data/normalize.py` 对旧 CSV 无标记的行也能动态识别。
- 测试：`unit/common/test_timeutil.py`（事实包剔除临时行）、`unit/data/test_normalize.py`（临时/正式两态）、`unit/data/test_store.py`（临时行同值升级）、`contract/data/test_quality.py`（旧拒绝覆盖、动态 PROVISIONAL、夹在有效行之间的坏行判 RED）、`integration/data/test_ingest_offline.py`（非核心 RED 触发质量闸降级）。

### 变更

- `data/normalize.py`：同日后来已有有效值的 `NAN_IN_REQUIRED` 旧拒绝不再计最近缺失（修复 US10Y 2026-10-19 被重复计数）；近期坏行窗口改为“最近 5 个有效行最早 `as_of` 起至 `as_of_max`（含）”，被剔除日期夹在有效行之间也算（WTI 2026-10-22）。
- `data/store.py`：旧盘中临时行即使业务值相同，也可被收盘后同值快照升级（更新 `fetched_at`/`ingest_run_id`/`flags`）。
- `data/ingest.py`：`quality_gate` 与 `_quality.json` 的 `global_status` 同口径；任一数据集 RED 时节点为 `DEGRADED`、`error_code=DATASET_RED`。
- `AGENTS.md` §6.5/§6.6/§7.4/§7.5/§22 补 `PROVISIONAL` 与近期坏行窗口口径（未提交阶段就地修正，无旧句可作废）；`PROJECT.md` 同步文件职责与测试描述；`看板.md` 增 T-019/T-020 与 §13。

### 数据

- `cli.py ingest --run-id p1-refresh-20261023-c` → `EXIT=0`；`_quality.json`：`global_status=DEGRADED`、`red=[wti_cl]`、`missing=[]`、`core_not_green=[]`。
- 中国行情补齐 2026-10-21/10-22：`etf_518880` 3,216 行、`etf_159934` 3,123 行、`sge_au9999` 2,369 行、`shfe_au0` 4,574 行，均至 2026-10-22。
- COMEX/WTI 10-20 已用收盘后快照更新（COMEX `4139.4→4142.6`，WTI `98.12→98.16`），并补 10-21/10-22；WTI 10-22 源行 `low=97.72 > open=97.69` 判 `OHLC_INVALID` 剔除留档，数据集 RED。
- `usdcny` 10-21/10-22、`usdcny_mid` 10-20..10-22 为最终值；二者 2026-10-23 行仍为 `PROVISIONAL`（BOC 18:00/次日 06:00 才可见）。US10Y 补 10-20/21/22，`status=OK`、`recent_missing_rows=0`。
- 事实包过滤实测（cutoff `2026-10-24T07:00+08:00`）：COMEX/WTI/USDCNY/USDCNY_MID 各剔除 1 条 2026-10-23 临时行；其余数据集剔 0 条。

### 证据

```
/usr/bin/python3.10 -m pytest
→ 120 passed, 15 deselected in 14.68s，EXIT=0

/usr/bin/python3.10 -m compileall -q src/main/python src/test/python
→ EXIT=0

/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py ingest --run-id p1-refresh-20261023-c
→ EXIT=0；quality_gate=DEGRADED、error_code=DATASET_RED、message=数据集质量为 RED：['wti_cl']
```

### 未做 / 仍缺

- T-020：截至 2026-10-23 13:39 CST，BOC 18:00 未到；`usdcny`/`usdcny_mid` 的 10-23 行仍 `PROVISIONAL`，18:00 后需用新 run_id 跑**全量** ingest 并复核 `usdcny` 10-23 行转正；`usdcny_mid` 与 COMEX/WTI 的 10-23 行按 `next_day_0600` 要等 2026-10-24 06:00 后才转正。BOC 未转正前 P1 不签收。
- P1 尚无事实包构建器（P2 才产出事实包）；本次落的是 `filter_fact_pack` 过滤入口与回归测试，P2 构建事实包时必须调用它，否则 `PROVISIONAL` 门不会自动生效。
- 2026-10-23 中国行情要等 15:00 收盘；18:00 后全量重抓会顺带纳入。
- 未提交、未 push。

## [测试] 2026-10-23（T-013/T-016：逆向格言复核、网络冒烟）

### 新增

- `src/test/python/com/sun/shensuan/gold/contract/adapters/test_network_smoke.py`：动态读取启用配置，对 15 个非手工适配器各执行一条真实联网冒烟；标记 `network`，默认 `pytest` 排除。
- `PROJECT.md` 登记该测试文件。

### 变更

- `看板.md`：T-013 转 `DONE`；T-016 由 `ISSUE` 转 `DONE`；新增 §12 证据；顶部阶段说明更新。
- `AGENTS.md` 未改：T-013 只复核既有正文，T-016 只补测试。

### 证据

```
/usr/bin/python3.10 -m pytest
→ 112 passed, 15 deselected in 16.14s，EXIT=0

/usr/bin/python3.10 -m pytest -m network src/test/python/com/sun/shensuan/gold/contract/adapters/test_network_smoke.py
→ 15 passed in 78.29s (0:01:18)，EXIT=0

/usr/bin/python3.10 -m pytest -m network --collect-only src/test/python/com/sun/shensuan/gold/contract/adapters/test_network_smoke.py
→ 15 条，EXIT=0

grep -c "别人贪婪时恐惧，别人恐惧时贪婪" AGENTS.md → 5
grep -n "别人恐惧时贪婪" AGENTS.md → 7 行
```

### 未做 / 仍缺

- 网络用例默认不跑，受源限流与可用性影响；失败时按数据集名定位。
- 本次只证明 15 条联网路径可用，未逐字段核对返回值；未提交、未 push。

## [数据层] 2026-10-21（T-018：指标池缺失数据逻辑）

### 新增

- `config/data_sources.yaml` 新增 `usdcny_mid`：SAFE 人民币汇率中间价，1994-01-01 起；`ak.currency_boc_safe()` 的 `美元` 列按每 100 外币除以 100。与 `usdcny`（BOC 中行折算价，2021-10 起）是两套报价，禁止拼接混用。
- `data/adapters/macro.py` 新增 `safe_usdcny` 适配器；`data/vintage.py` 新增 `parse_gpr_payload()` 与 `load_gpr_vintage()`：只读 GPR 原始快照，按 `fetched_at <= available_at` 且 `0 <= fetched_at.date() - as_of <= 7` 天选取，校验 sha256；历史段返回 `None`，不反推修订值。
- 测试：`contract/adapters/test_macro_adapter.py` 增 2 条 SAFE 汇率契约；`unit/data/test_vintage.py` 增 6 条 PIT/完整性用例；配置数据集计数 19→20。

### 变更

- `data/adapters/geo.py` 的 GPR 解析抽到 `data/vintage.py`，解析行为与报错类型不变（既有 GPR 契约测试通过）。
- `AGENTS.md` §5.2 增两条汇率序列不得混用的说明；§6.1 增 SAFE 中间价探针行；§12.8 增 `load_gpr_vintage()` 现状。
- `PROJECT.md` 登记 `data/vintage.py` 与 `unit/data/test_vintage.py`；`看板.md` 增 T-018 与 §11。

### 证据

```
/usr/bin/python3.10 -m pytest
→ junit tests=112 failures=0 errors=0 skipped=0 time=14.317，EXIT=0

/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py ingest --run-id p1-t018-20261021
→ EXIT=0；global_status=OK；usdcny_mid 8,068 行 1994-01-01..2026-10-20 GREEN，new_rows=8,068

load_gpr_vintage(2026-10-19, available_at=2026-10-21T01:00+08:00)
→ gprd=248.66458129882812, vintage=2026-10-21T00:41:31.009290+08:00
load_gpr_vintage(2025-01-01, available_at=2026-10-21T01:00+08:00) → None
```

### 未做 / 仍缺

- 历史快讯（CLS/Sina 约一两天、SHMET 约 3 个月）与事件日历（`geo_events.csv` 空）无法由现有接口回补；不伪造。
- GPR 历史段仍无 vintage，`load_gpr_vintage` 对 2026-10-19 之前返回 `None`；只能从现有快照起逐日累积。
- `GEO_SCORE` 权重、`contrarian.stats.*`、RSI/分位/价差/事件统计均未实现，仍属 P2/P3。
- 未提交、未 push。

## [设计文档] 2026-10-21（T-017：README 第 19 条，逆向格言统计要求）

### 新增

- `README.md` 功能需求新增第 19 条：用两个或更多独立指数/指标的高低点交叉、重叠等组合信号，统计买入低点/卖出高点的命中率与盈利/亏损概率；结论必须带样本量、时间区间、成本后结果与置信区间，用数据说话。
- `AGENTS.md` 新增 §8.4.2：事件定义（`CROSS`/`OVERLAP`/`DIVERGENCE`、独立性、去重）、统计口径（N、Wilson CI、bootstrap、基准对照、FDR/Bonferroni、重叠样本、walk-forward、PBO）、PIT/执行纪律、`INSUFFICIENT_EVIDENCE` 与使用边界。
- `AGENTS.md` §12.7 增加“统计报告是晋升必交证据”；§12.6 增多重检验诊断；§12.10 增组合搜索试验计数；§17.2 增逆向统计测试层；§17.4 增事件 PIT 规则；§18 P2/P3 增统计原型/报告验收；§21.3 增多重检验风险；§22 增“高低点交叉/重叠”“命中率”术语。
- `AGENTS.md` §15.11 增第 19 条映射；§18 P0 行与 `PROJECT.md` 的约束计数同步为 19 条。

### 变更

- `README.md` 第 28 行由注释态 `# -` 改为正式功能需求条目；README 约束计数 18→19（技术栈 5 + 需求 14）。
- `AGENTS.md` 版本 v0.2→v0.3；§2.2 决定 3 写全格言两半并指向 §8.4.2，其下说明改为“决定 3 的引文与统计口径”；§8.4.1 首句改为“§2.2 已补全、§8.4 标题仍只引前半句”，并指向 §8.4.2。

### 未做

- 统计原型、真实数据事件统计、命中率/盈亏概率读数均未运行；按 §18 在 P2/P3 落地。本批只有口径与验收闸，不声称任何策略有效或已回测。
- 未改代码、配置、数据；未提交、未 push。

### 证据

```
grep -n '别人贪婪时恐惧，别人恐惧时贪婪' README.md
→ 28:正式功能需求（另第 3 行为目标句）

grep -n '^### 8.4.2' AGENTS.md
→ 695:### 8.4.2 多指标高低点交叉/重叠的统计验证（数据说话）

grep -n 'README 19 条约束映射' AGENTS.md
→ 1457:### 15.11 README 19 条约束映射

grep -n '^\| 19 \|' AGENTS.md
→ 1479:| 19 | 逆向格言：两个或更多独立指标的高低点交叉/重叠统计… |

grep -n '（19 条）' PROJECT.md
→ 13:| `README.md` | 现状 | 用户需求与技术栈约束（19 条） |

awk 计数（技术栈+需求，排除 Links）
→ tech=5 functional=7 nonfunctional=7 requirements=14 total=19

/usr/bin/python3.10 -m pytest
→ 104 passed in 13.15s，EXIT=0

git diff --check
→ EXIT=0
```

## [P1] 2026-10-19 / 2026-10-20

### 新增

- 工程骨架：`src/main/python/com/sun/shensuan/gold/`（`_bootstrap`、`common/`、`config/`、`data/`、`cli.py`）。
- 配置模板：`config/application.yaml`、`config/data_sources.yaml`、`config/strategy.yaml`、`config/llm.yaml`。
  - 19 个数据集定义；9 个委员会角色；四文件规范化哈希 `config_hash`。
- 数据适配器：ETF、SGE、COMEX/WTI、AU0、SPDR、US10Y、USD/CNY、CFTC、GPR、CLS/Sina/SHMET 快讯、GDELT（默认关闭）、手工序列、手工事件日历。
- 数据层：原始快照 append-only；规范化 CSV 幂等合并；PIT 字段（`source/as_of/available_at/fetched_at/ingest_run_id/vintage/quality/flags`）；质量闸；运行链路 `pipeline_status.json`。
- 被剔除行审计：`data/normalized/rejected/<dataset>.csv`，保留原值与 `reject_reason`。
- CLI：`preflight`、`ingest`、`normalize`、`status`、`coverage`、`list-datasets`、`validate-manual`。
- 手工 CSV 模板：`data/manual/geo_events.csv`、`tips10y.csv`、`au_index.csv`、`gld_holdings.csv`（仅表头）。
- 测试：`src/test/python/com/sun/shensuan/gold/`，离线默认；`pytest.ini` 排除 `network` 标记。
- `看板.md`：P1 任务、证据、偏差、RISK/PENDING。

### 变更

- `comex_gc.core: true → false`：源长期 `volume/open_interest=0`，按 AGENTS 只当收盘价；避免已知源缺陷把每日全局质量永久压成 DEGRADED。核心集变为 `etf_518880`、`sge_au9999`、`shfe_au0`。
- 数据集级质量判定按「最近窗口」：最近 5 个有效交易日内有坏值 → RED；最近缺值 → YELLOW；历史坏行剔除并留档，不把整条序列永久判 RED。行级拒绝原因仍逐条保留。
- 全局质量：仅 `FAILED`、`RED`、核心未达 `OK` 时判 `DEGRADED`；非核心源降级保留在 `degraded` 列表，不压全局。该口径比字面「任一源降级即 DEGRADED」更窄，已在 `看板.md` 标注。
- `us10y`：源内 98 行必需值为空（含当日盘前）剔除留档；最近缺失记 YELLOW，不记 RED。

### 修复

- `parse_datetime` 支持 ISO-8601 带时区字符串；新闻发布时间按 `Asia/Shanghai` 本地化。
- `to_jsonable` 先处理 NumPy 浮点/整型，避免 NaN 写入 JSON。
- 日志保留按「严格早于截止日」删除，边界日保留。
- 原始/规范化测试中的人造 OHLC 夹具修正，确保测的是既定分支而不是被 OHLC 校验提前拦截。

### 验收证据

```
/usr/bin/python3.10 -m pytest
→ 75 passed in 7.55s，EXIT=0（2026-10-20 00:02 复核跑）

/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py ingest --run-id p1-acceptance-20261019-d
→ EXIT=0；质量层 global_status=OK；missing=[]；red=[]；core_not_green=[]

/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py coverage
→ EXIT=0；degraded=[comex_gc,wti_cl,us10y,gpr,geo_events]；revised_only=[gpr]
→ 链路层 pipeline.overall=DEGRADED（任一节点 DEGRADED 即 DEGRADED）
注意：质量层 `global_status` 与链路层 `pipeline.overall` 是两个字段，口径不同，不合并、不互相替代。

/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py preflight
→ EXIT=0；overall=GREEN
```

真实数据覆盖（最终 run）：

- `etf_518880`：3,213 行，2013-07-29..2026-10-19。
- `sge_au9999`：2,366 行，2016-12-19..2026-10-19。
- `comex_gc`：2,549 行，2016-10-19..2026-10-19。
- `shfe_au0`：4,571 行，2008-01-09..2026-10-19。
- `gpr`：15,267 行，1985-01-01..2026-10-19；`vintage=REVISED_ONLY`。
- 剔除留档：SGE 17、GC 40、WTI 51（OHLC_INVALID）；US10Y 98（NAN_IN_REQUIRED）。

### 风险

- GPR 无 vintage，不能用于 H-GEO 晋升。
- COMEX/WTI 零成交/零持仓，只能当收盘价。
- 无 TIPS、无 DXY 代理；美债仅名义 10Y。
- 手工事件日历为空；空事件不等于无风险。
- 交易日历尚未接入，`weekday_gaps` 会把节假日计入。

### 未做

- P2 特征注册表、事实包、regime、仓位带、风控引擎。
- P3 回测、walk-forward、PBO/DSR。
- P4 日链、决策卡、纸面账本。
- P5 LLM 委员会、引用审计。
- P6 Streamlit、P7 cron/Docker/运行手册、P8 评估。
- 网络测试默认不跑；未做 LLM/UI/回测性能预算。

### 提交

- 本次 P1 产物**未提交**；等待用户明确指令。Agent 不执行 `git push`。

## [结构整改] 2026-10-20

### 新增

- 全仓中文 docstring：`src/main` 与 `src/test` 中 34 个非 `__init__.py` 模块、27 个类、206 个函数全部补齐模块级/类级/函数级注释；`__init__.py` 按要求豁免。
- `PROJECT.md` 重写为代码结构索引：顶层目录、生产/测试目录树、逐文件作用、计划包、结构维护规则。
- 测试分层：`unit/`、`contract/`、`integration/`、`architecture/`；测试根目录只保留 `conftest.py` 与 `fixtures/`。
- 结构静态测试 3 条：docstring 齐全、测试分层、每个非 `__init__.py` 文件在 `PROJECT.md` 登记。
- `AGENTS.md`：快速契约由 16 条扩为 19 条；新增 §15.14「代码结构回写 PROJECT.md」；§15.5 增加注释硬要求；DoD、测试层次、P1 验收闸、任务协议同步。

### 变更

- 测试文件从测试根目录迁入分层子目录；`test_config.py` 的仓库根推导由 `parents[7]` 改为 `parents[9]`。
- 既有 75 条测试逻辑未改；新增 3 条结构测试后总数为 78。

### 验收证据

```
/usr/bin/python3.10 -m pytest
→ 78 passed in 7.31s，EXIT=0

/usr/bin/python3.10 -m compileall -q src/main/python src/test/python
→ EXIT=0

AST 审计（不含 __init__.py）
→ modules=34 classes=27 funcs=206 missing=0

AST 去 docstring 对比 /tmp/src.before-20261020
→ main 26 个文件 logic/signature differences=0
```

### 未做

- 未提交、未 push；等待用户明确指令。
- 未运行网络测试；LLM/UI/回测不在本批范围。

## [P1] 2026-10-20（T-010：代码评审阻断级修复）

关联任务：`看板.md` T-010。

### 修复

- `data/pit.py`：`available_at_rule=explicit` 分支改用 `available_at_for(..., explicit=...)`，与手工层校验器共用 `parse_datetime`。此前 `geo_events.csv` 里把 `available_at` 写成裸日期（`2026-10-16`）会通过手工校验器（`status=OK`），却在 `run_ingest` 里抛 `ValueError: naive datetime 不允许`，把整条日链打断；现在按 Asia/Shanghai 零点补齐。缺值时的报错带上数据源、第几条记录与该行的 `as_of`。
- `data/ingest.py`：把「规范化 → 写原始/规范化层 → 质量评估」整段纳入 try。此前这段在重试循环之外，`normalize_frame` 的列契约错（`AdapterError`）或落盘 `OSError` 会逃出 `run_ingest`，链路状态停在 `ingest.<dataset> = RUNNING`、下游 `normalize/quality_gate = PENDING`、`_quality.json` 不写，与 §4.5「终态必写」和模块自述的「单个数据集失败不中断整条链」相矛盾。现在该数据集落 `FAILED` + `PIPELINE_<异常类型>`，其余数据集继续抓。
- 质量口径（变更，需用户确认可回退）：规范化层判 `RED`（适配器返回空表、或必需列全空导致整批剔空）时，`run_ingest` 取「本次规范化」与「合并后历史」中较差的一档。此前质量只由合并后的历史序列决定，历史仍新鲜就会把今天已经坏掉的源显示成 `OK/GREEN`。方向是向 §2.1-4「失败关闭」收紧，比改动前更硬。

### 新增

- `data/normalize.py`：`ALL_ROWS_DROPPED` 标记——`price/series` 整批被剔空时数据集判 `RED`（部分剔行仍只 `YELLOW` + `INVALID_ROWS_DROPPED`）。
- 测试 3 条（`integration/data/test_ingest_offline.py`）：手工表裸日期可见时点、规范化环节失败后的终态与续跑、整批剔空不被历史洗白。

### 验收证据

```
改前（只加测试，未改实现）
/usr/bin/python3.10 -m pytest -m "not network" src/test/python/com/sun/shensuan/gold/integration/data/test_ingest_offline.py -q
→ 3 failed，EXIT=1
  ValueError: naive datetime 不允许；必须带时区（common/timeutil.py:62）
  AdapterError: akshare.fund_etf_hist_sina 缺少必需列：['volume', 'amount']（data/normalize.py:165 逃出 run_ingest）
  assert <Quality.YELLOW> == <Quality.RED>

改后（全量离线）
/usr/bin/python3.10 -m pytest -m "not network"
→ 81 passed in 8.45s，EXIT=0
```

### 未做

- 评审第 3–8 条未动（`wti_cl price_min: -100.0` 死档、`key_columns ⊆ required_columns` 校验、`available_at_rule` 取值校验、USD/CNY ÷100 契约测试、非主线程未装 SIGALRM 的可见性、`cli.py`/`manual.py` 绕过原子写与 `logging_setup` 未 `close()` handler）。
- 未提交、未 push；真实联网日链未重跑；`data/` 下 2026-10-19 验收产物未重算。

## [P1] 2026-10-20（T-011：代码评审剩余条目修复）

关联任务：`看板.md` T-011。顺序按评审第 3–8 条执行。

### 变更（配置口径，方向为收紧）

- `common/timeutil.py`：新增 `AVAILABLE_AT_RULES`，作为六种 PIT 规则的唯一权威清单；`available_at_for` 与之共用，常量收录但分支未实现时兜底报错。
- `config/loader.py`：`DatasetConfig` 在加载期新增三项校验——`available_at_rule` 必须在六值清单内；`price_min >= 0`；`key_columns ⊆ required_columns ∪ {"as_of"}` 且 `price_min <= price_max`。此前非法规则要等生成 PIT 才炸、主键列拼错会让合并退化为整表覆盖。
- `config/data_sources.yaml`：`wti_cl.price_min: -100.0 → 0.01`，与 AGENTS §6.6「价格 ≤0 必须剔除」一致。现读 `data/normalized/wti_cl.csv` 的 `2020-04-20` 收盘为 `21.22`（非负），本批不改任何已验收读数；原 -100 本就是死档（`normalize.py` 独立执行 `close <= 0`）。

### 新增

- `contract/adapters/test_macro_adapter.py`（新文件，已登记 `PROJECT.md`）：BOC「每 100 外币」价 ÷100 的算术契约与日期区间过滤。生产 `macro.py` 的 `÷100.0` 早已实现，本项只补测试，两条用例在改前即为绿。
- `data/ingest.py`：`_call_with_timeout(..., notes)` 在保护装不上（非主线程/平台无 SIGALRM/`timeout<=0`）时追加固定告警，并入节点 `message` 与 `IngestOutcome.notes`；不改变节点状态。
- `cli.py`：`normalize` 子命令的质量复核报告改用 `atomic_write_json`；`data/manual.py`：手工模板改用 `atomic_write_text`（半截模板会被下次启动误认为“用户已有文件”）。
- `common/logging_setup.py`：重复初始化先逐个 `close()` 再 `clear()`，不再泄漏文件描述符。
- 测试 8 条：4 条配置负向用例（负价格下界、区间反向、主键列拼错、未知 PIT 规则）、2 条 USD/CNY 契约、1 条超时保护告警、1 条重复初始化关闭旧句柄。

### 验收证据

```
改前（只加测试，未改实现）
/usr/bin/python3.10 -m pytest -m "not network"
→ 8 failed，EXIT=1
  DID NOT RAISE ValidationError（4 条配置负向用例）
  TypeError: _call_with_timeout() takes 2 positional arguments but 3 were given
  旧 FileHandler.stream 仍非 None
  assert -100.0 > 0（wti_cl 价格下界）
  新测试文件未登记 PROJECT.md

改后（全量离线）
/usr/bin/python3.10 -m pytest -m "not network"
→ 89 passed in 11.66s，EXIT=0
/usr/bin/python3.10 -m compileall -q src/main/python src/test/python
→ EXIT=0
AST 审计：modules=35 classes=27 funcs=225 missing=0

终验复跑（文档回写后，2026-10-20 01:42）
/usr/bin/python3.10 -m pytest -m "not network"
→ 89 passed in 11.01s，EXIT=0
/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py preflight
→ overall=GREEN，EXIT=0
```

### 未做

- 未提交、未 push；真实联网日链未重跑；`data/` 下 2026-10-19 验收产物未重算。
- WTI 源的合约滚动与负价处理未核实（本次只确认现读数据 `2020-04-20` 行非负）。

## [P1] 2026-10-21（T-012：README 第 16 条，代码与配置文件注释）

关联任务：`看板.md` T-012。代码侧 docstring 已由 T-009 完成（AST 缺失 0）；本批补齐配置文件一侧并加静态检查。

### 变更（仅注释与检查，不改任何配置取值）

- `config/application.yaml`、`config/strategy.yaml`、`config/llm.yaml`、`config/data_sources.yaml`：逐份补文件头说明、顶层分节说明与关键字段口径（单位、PIT、降级、生效阶段）；键与取值未变。
- `pytest.ini`：补文件头注释，说明测试路径、导入根与默认排除联网用例。
- `architecture/test_architecture.py`：新增 `test_config_files_have_maintenance_comments`，检查四份 YAML 的文件头、每个顶层键、`data_sources.yaml` 每个数据集条目前是否有注释；内置两条合成负对照证明检查器会报缺失。
- `AGENTS.md`：§15.5 增配置文件注释硬要求；§15.6 增改值同批改注释；§15.11 标题与 P0 行由 15 条改为 18 条并补第 16–18 条映射；§16.4 DoD 增配置注释检查；§16.7 增配置值/单位/阶段变更同步注释；§17.2「结构/注释」增 YAML 检查；§20.2 增配置注释同批要求。
- `PROJECT.md`：README 计数 15→18，顶层说明、`config/` 说明、架构测试职责与结构维护规则补配置注释要求。

### 验收证据

```
/usr/bin/python3.10 -m pytest
→ 90 passed in 13.13s，EXIT=0（T-011 时 89 条 + 新增配置注释检查 1 条）
终验复跑（文档回写后）→ 90 passed in 12.77s，EXIT=0；compileall EXIT=0

真实配置负对照（内存中删注释，不写盘）
基线→缺注释后：
[] → ['application.yaml:10 顶层键 mode 前缺少注释']
（删除 mode 前的完整注释块；证明检查器能看见真实文件的缺失）

/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py preflight
→ EXIT=0；config_hash=sha256:308880e93880cf749d2db9624f0a00612bb13b297b62c25d45764ee4935d4ac3；
  overall=YELLOW（MemAvailable=7.72 GiB < 9.5 GiB，P1 不启 LLM），disk 145.99 GiB free
```

`config_hash` 与加注释前一致，因为加载器只哈希 `yaml.safe_load` 后的文档，注释不进入哈希。本批现场复核：把四份文档 `safe_dump` 成无注释文本再 `safe_load`，哈希仍为 `sha256:3088...`，与加载器值相等。

### 未做

- 未提交、未 push；真实联网日链未重跑；`data/` 下验收产物未重算。

## [设计文档] 2026-10-21（T-013：补全逆向格言两半）

关联任务：`看板.md` T-013。README 第 3 行原句为“别人贪婪时恐惧，别人恐惧时贪婪”；`AGENTS.md` §2.2 决定 3、§8.4 标题与正文此前只引了前半句。

### 变更（纯追加，不改旧句）

- `AGENTS.md` 新增 §8.4.1：前半句“别人贪婪时恐惧”映射为降险（减仓/不做多/降低卫星上限）；后半句“别人恐惧时贪婪”映射为逆向加仓，v1 只展示、不执行，任何加仓须过 §12.7 全部 7 条 + 用户书面签字。
- §2.2 表后追加“决定 3 的引文补全”，指向 §8.4.1；§12.7 末尾追加“门槛只约束加仓方向，降险不受此限”；§22 术语表追加“逆向格言”一行。
- 更正：§8.4 末段与 §8.5 第二条把晋升门槛误写成 §12.8；现读 §12.8 是 GPR vintage 规则，正确为 §12.7。旧句保留，以 §8.4.1 的引用更正为准。

### 验收证据

```
grep -c "别人贪婪时恐惧，别人恐惧时贪婪" AGENTS.md
→ 3

grep -n "别人恐惧时贪婪" AGENTS.md
→ 5 行（§2.2 补全、§8.4.1 引言、§8.4.1 表格、§12.7 补充、§22 词表）

/usr/bin/python3.10 -m pytest .../architecture/test_architecture.py -k config_files_have_maintenance_comments
→ 1 passed，EXIT=0
```

全量 `pytest` 当前为 `2 failed, 102 passed` in 13.62s，EXIT=1；两个红项都指向 2026-10-21 00:30 新出现的 `contract/adapters/test_geo_adapter.py`（`_timeout` 缺 docstring、文件未登记 `PROJECT.md`），不是本笔改动。已按 §20.2 开 `T-014 ISSUE`，未夹带修复；`看板.md` 中 T-013 因全量闸未过标为 `PENDING`（内容已完成）。

### 未做

- 未提交、未 push；未改并发新增的 `test_geo_adapter.py` 与 `plans/iron-harbor-shrew.md`（后者仍是旧简写，以 `AGENTS.md` 为准）。

## [P1] 2026-10-21（T-015：AGENTS.md 二轮评审修复）

关联任务：`看板.md` T-015。按评审优先级修复 H1–H3、M1–M4、L1–L2，共 9 条。

### 修复

- **H1 缺失值被填 0**：`data/normalize.py` 删除零成交/零持仓字段的两处 `fillna(0)`。真实 0 标 `ZERO_<COL>`，缺失保持 NaN 并标 `MISSING_<COL>`；两者都使数据集降级，但不再把“没抓到”伪装成“真实零”。`config/loader.py` 的 `zero_fields` docstring 与 `config/data_sources.yaml` 注释同批改口径。
- **H2 `cli.py normalize` 质量误报**：复核质量时传入 `rejected` 与 `invalid_rows`，与 `run_ingest` 同口径；近期坏行不会被历史好数据洗成 GREEN。
- **M1 LLM 配置缺硬校验**：`config/loader.py` 新增四个加载期校验——`base_url` 只允许本机回环、`think` 必须为 false、`num_gpu` 必须为 0、`mode` 只允许 influence/shadow/off。非法配置在加载期直接失败。
- **M2 快讯单行坏时间打整源**：`data/adapters/geo.py` 对 CLS/Sina/SHMET 的发布时间逐行解析；非法行只丢该行并在 `notes` 记“丢弃发布时间非法 N 行”，不再让整源失败。
- **M3 规范化失败丢原始证据**：`data/ingest.py` 把原始快照落盘移到规范化之前；规范化抛错时仍保留本次抓取的 `raw_path`，便于复盘。
- **T-014 并发文件登记**：`contract/adapters/test_geo_adapter.py` 已登记 `PROJECT.md` 目录树与作用表，嵌套替身 `_timeout` 补中文 docstring。

### 新增

- `contract/adapters/test_geo_adapter.py`：快讯坏时间逐源 3 条、GDELT 429/超时 2 条，共 5 条。
- `unit/config/test_config.py`：5 条非法 LLM 配置参数化用例。
- `integration/data/test_ingest_offline.py`：规范化失败后 `raw_path` 仍存在。
- `integration/cli/test_cli.py`：`ingest` 汇总输出。
- `contract/adapters/test_gpr_adapter.py`：GPR 15,261 行日频规模夹具。

### 变更（文档）

- `AGENTS.md`：头部状态改 v0.2；§0.6/§18/附录 H 标 P1 待验收；§§5.1/6.1/21.1 的 COMEX/WTI 由“末行零持仓 / 2,588 / 7,688 行”改为“全部行零持仓”，行数按 2026-10-19 快照现读（COMEX 2,589、WTI 7,689）。未提交状态下的就地修正，无旧句可作废。
- `看板.md`：P1 里程碑 `DONE → PENDING`（等待用户验收）；新增 T-015；T-014 `ISSUE → DONE`；DoD 同步。
- `PROJECT.md`：登记 `test_geo_adapter.py`。

### 验收证据

```
/usr/bin/python3.10 -m pytest -m "not network" --junitxml=/tmp/pytest_p1_fix.xml
→ EXIT=0；tests=104 failures=0 errors=0 skipped=0 time=12.659
（相关 7 个测试文件：tests=57 failures=0 errors=0，EXIT=0）

/usr/bin/python3.10 -m compileall -q src/main/python src/test/python
→ EXIT=0
```

pytest 终端本次未输出汇总行，条数取自 junit `testsuite` 属性。

### 未做

- 未提交、未 push；真实联网日链未重跑；`data/` 下 2026-10-19 验收产物未重算。
- 快讯真实源与 GDELT 真实 429 未联网复测。
- T-013 状态仍为 `PENDING`，由该笔负责人复核后转 DONE。

## [验证] 2026-10-21（联网复核）

关联：`看板.md` §10、T-016。本批不改代码。

### 复核结果

- 仓库内 `@pytest.mark.network` 测试为 0 条：`grep -rn "mark.network" src/test` → 0 命中；`pytest -m network --collect-only` → EXIT=5。开 T-016 ISSUE。
- 只读探针：CLS/Sina/SHMET 实时 20/20/2,000 行；GDELT 真实 429 转 `DegradedSourceError`；COMEX 2,588 行、WTI 7,690 行，volume/open_interest 全 0；GPR 15,267 行至 2026-10-19。
- 真实日链 `network-recheck-20261021` EXIT=0；质量层 `global_status=OK`、`missing=[]`、`red=[]`、`core_not_green=[]`、`degraded=[comex_gc,wti_cl,us10y,gpr,geo_events]`；链路层 17 节点 OK 12 / DEGRADED 5，`overall=DEGRADED`。
- 写盘：`data/raw/<source>/2026-10-21/`、`data/normalized/*.csv`、`data/decisions/2026-10-21/pipeline_status.json`、`data/normalized/_quality.json`。

### 未做

- 未新增网络测试（T-016）；GDELT 非 429 成功路径未复测；未逐字段核对新行与交易所原始行情；未提交、未 push。

## [裁决] 2026-11-09（T-035：设计取丙 + 单模型 qwen2.5:7b）

### 变更

- 用户裁决（看板 T-035、AGENTS §0.7）：以 `AGENTS.md` 为行为单一事实源，先按其实现；`docs/国内黄金量化投资设计.md` 保留为参考文档，其「以本文件§9 为准/投资决策按本文件执行/替代对应设计」三处优先句作废（旧句保留留痕）。
- 模型收口：只用 `qwen2.5:7b` 一个模型。`config/llm.yaml` 的 `model` 由 `deepseek-r1:8b` 改为 `qwen2.5:7b`，`models` 由四模型清单改为单模型清单；§11.3 其余参数不变。
- 频率与动作空间：维持每日评审、每日决策；动作空间中性（不看多也不看空，可空仓/买入持有/卖出）。「可空仓」边界：核心 70% 战略仓政策不变，空仓落在卫星与风控/降级减仓层；金边组合整体空仓需用户另行书面裁决。
- `AGENTS.md` 追加 §0.7 与附录 H v1.2 行（追加优先，未删改已提交正文）；`unit/config/test_config.py:27-28` 断言同批更新。
- `config_hash`：`sha256:0b5d90c1…` → `sha256:4eefa29a02929e5304f4ee0789ecf66389147ce21c85938665e7a1f9af7ceeda`；按 §18.6 Q3，须重跑 ingest 后再验收（后续步骤落地）。

### 验证

- `cli.py preflight` → EXIT=0，overall=YELLOW（内存闸，P1 不启 LLM）。
- `pytest unit/config architecture` → `23 passed in 2.13s`，EXIT=0（改前 1 failed：旧模型断言；改后全绿）。

### 风险 / 未做

- 全量 pytest 本批未重跑（只跑 config+architecture 23 条）；gitignore 修复与源码补交、尺子修复、ingest/accept 重跑在后续步骤；本批未提交、未 push。

## [修复] 2026-11-09（T-036：gitignore 吞源码包修复与补交）

### 变更

- `.gitignore`：裸 `data/`（:232）改为 `/data/` 锚定根目录——旧规则把源码包 `src/main/.../gold/data/` 整体忽略，致 `ca3fbfe "P1 init"` 提交含 0 个数据层文件、克隆即 ImportError（评审 B2/F1）；同批删除对 3 个 `__init__.py` 的逐行忽略。
- 补交：`gold/data/` 源码包 18 个文件、3 个 `__init__.py`、10 个测试文件（contract/data、integration/data、unit/data）。

### 验证

- `git check-ignore -v` 现读：`normalize.py`/`__init__.py` 不再命中；根 `data/normalized/_quality.json` 仍由 `/data/` 忽略。
- 克隆完整性闸：首跑（`54aed79` 归档）`1 failed, 214 passed`——`test_validate_manual` 直读 gitignored 的 `data/manual`，新鲜克隆必 YELLOW（把生产数据当夹具，§17.1）；改为 `tmp_config` 隔离沙箱并补「抽掉模板→YELLOW」负对照（`ff05d24`）后，复跑克隆闸（HEAD 归档到 `/tmp/gold_clone_head`）→ `215 passed, 16 deselected in 23.77s`，EXIT=0。
- 覆盖面现读：HEAD `src/main` 32/32、`src/test` 23/23（含 conftest）；工作区干净。

### 风险 / 未做

- 不 push；历史提交 `ca3fbfe` 的不完整状态不可改写（不 amend），由本批补交修复。

## [修复] 2026-11-09（T-037：评审尺子缺陷 C1/C2/C4/F2/F3/F6）

### 变更

- **C1**（高）：`architecture/test_architecture.py` 的 main→test 导入闸由坏正则改 AST 判定（覆盖 `import x`/`from x import y`/`from gold import unit` 三形状），新增负对照测试证 5 种真实导入被抓、4 种正常导入放行。
- **C2**（高）：`acceptance/p1.py` 新增判据 **Q4 批次相对墙钟新鲜**——批次基准日距验收日 > `max_stale_days` 即 FAIL；判据 14→15 项（158→159 条），AGENTS §18.6 同批回写并注旧计数作废。
- **C4**（中）：`data_sources.yaml` 给 `usdcny`/`usdcny_mid` 声明 `price_min/max=5–10`（实测 6.09–8.71 在内），C4 由 SKIPPED 转 PASS，抓 /100 单位错。
- **F2**（高）：`news_events.py` 合并输出保留 `EVENT_COLUMNS` + 现有额外列（`severity_reviewed` 不再被静默截掉）。
- **F3**（中）：`trade_calendar.py` meta 缺 `sha256` 判 `SHA_MISMATCH`（不可用），与校验和不符同档。
- **F6**（中）：`normalize.py` 事件同 `event_id` 不同 severity → 整组剔除留档（`SEVERITY_CONFLICT`）判 RED，不静默取最后；同 severity 重复仍正常去重。

### 验证

- 每处先写失败测试跑红再修绿：C1 改前 NameError EXIT=1 → 9 passed；Q4 改前 ImportError EXIT=2 → 38 passed；F6 改前 GREEN≠RED EXIT=1 → 绿。
- 全量 `pytest` → `224 passed, 16 deselected in 24.67s`，EXIT=0（较修前 +9 条新测试）。
- `accept --stage P1` 现跑 → EXIT=1，`{FAIL:2, PASS:140, SKIPPED:17}`：Q3 配置漂移（`9e87c4e8…` vs `0946643d…`）+ Q4「批次 2026-10-23 距 2026-11-09 已 17 天 > 5」——两红均待步骤4/5 重跑 ingest 消解，属尺子正常 biting。
- F2/F3 由子代理实现（中途 interrupted），主代理复核 diff 并跑测试 28 passed；F6 由主代理补完。

### 风险 / 未做

- `config_hash` 变为 `sha256:0946643d…`；Q3/Q4 判红是预期，重跑 ingest 后复验；本批未提交、不 push。

## [验收/数据] 2026-11-09（T-038：重跑入库 + 第 5 版验收卡 + 恢复日更）

### 变更 / 读数

- 全量入库 `cli.py ingest --run-id p1-t037-20261109` → EXIT=0，`reference_date=2026-11-09`、`scope=full`、`global_status=OK`、`red=[]`、`core_not_green=[]`；数据由 10-23 接到当天（17 天缺口补齐）。
- `accept --stage P1` 复跑 → EXIT=0，`verdict=PASS`，`{FAIL:0,PASS:142,SKIPPED:17}`（159 条/15 项）；Q3（配置同版本 `0946643d…`）与 Q4（批次 0 天 ≤ 5）双闸通过。
- `doctor --write` → EXIT=1，`overall=NEEDS_HUMAN`，`{AUTO_RESOLVED:2,CONTAINED:8,NEEDS_HUMAN:1}`，`config_hash_match=True`，`blocked_datasets=[comex_gc,geo_events,gpr,spdr_gold,us10y,wti_cl]`，`review_queue=4`，`report_hash=sha256:3789187e…`。
- 第 5 版验收卡：`docs/report/acceptance/P1/acceptance_v5.md`（取代第 4 版 `acceptance.md`，旧卡保留）；四问全「能」，三签字项待用户。
- T-025 汇率同日兜底观察重新起算：第 1 天 `rate --date 2026-11-09` → GREEN，source=BOC 折算价 6.7188，diverged=false（需续观 3–5 交易日）。

### 风险 / 未做

- 阶段里程碑仍 `PENDING`：机器闸过 ≠ 签收，待用户签卡第 7 节三签字项（P1 签收 / 4 事件 severity 复核 / R-006 五偏差）。
- cron 日更监控未安装（属系统级改动，待用户授权；P7 前的最低方案：工作日定时 ingest + accept，判红写 ISSUE）。
- 网络冒烟 16 条本轮未跑；数据产物（data/）gitignored 不入库。

## [签收] 2026-11-09（T-039：用户签收 P1 通过 · cron 取手动）

### 变更

- 用户裁决：P1 阶段**签收通过（甲）**，里程碑 P1 转 `DONE`；cron 日更监控取**乙（先手动跑，不装监控）**。
- 验收卡 `docs/report/acceptance/P1/acceptance_v5.md` 结论与签字行落痕（用户甲，2026-11-09）；AGENTS 附录 H v1.4、看板里程碑与 T-039 同批。
- 签字项 2（4 条事件 severity 复核）与 3（R-006 五项设计偏差确认）仍待办，转 P2 前置，不阻塞 P1。

### 手动日更命令清单（cron 乙，工作日按 §19.1 时间线，CST）

```
/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py preflight
/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py ingest --run-id daily-$(date +%Y%m%d)
/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py accept --stage P1
/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py doctor --write
/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py rate --date $(date +%F)
```

- `accept` EXIT=1 即判红（重点看 Q3 配置同版本 / Q4 批次新鲜）；`doctor` EXIT=1 是 NEEDS_HUMAN 信号（事件未复核），两者退出码 1 是预期判据信号、不是命令崩溃。
- 改任何配置后必须重跑 ingest 再 accept（Q3）；数据停超过 5 天 accept 会判红（Q4）。

### 风险 / 未做

- 无自动监控：数据停了需人工发现（用户已知悉，取乙）；T-025 汇率观察需续观 3–5 交易日（第 1 天已 GREEN）。

## [功能] 2026-11-11（T-040：`cli.py daily` 一键日更 · 评审取「简化命令」甲）

### 变更

- 评审结论：日更两方案「简化命令 vs 装 cron」，用户选**甲（简化命令）**。理由：cron 属 P7（需节假日闸+失败写 ISSUE+告警），现在裸装是半成品；`daily` 命令把 5 条长命令压成 1 条、仍手动触发、不碰系统，且正是将来 cron 要调的载荷。
- 新增 `cli.py daily [--run-id …]`：顺序跑 preflight→ingest→accept→doctor→rate；preflight/ingest 非 0 硬停（没有新数据不验收），否则退出码取 accept；doctor 的 NEEDS_HUMAN 与 rate 读数只进汇总。
- **默认 run-id 含时分秒**（`daily-YYYYMMDD-HHMMSS`）：真跑首轮暴露——`daily-YYYYMMDD` 与当日 20:22 的手动 ingest 撞了原始快照 append-only，17 源全误判 FAILED 并覆写 `_quality.json`。加时间戳后消除碰撞。
- PROJECT.md `cli.py` 作用表、看板 T-040 同批。

### 验证

- 先写失败测试再实现：`command_daily` 不存在（4 failed）→ 实现后 14 passed；run-id 无时间戳（1 failed）→ 加 HHMMSS 后转绿。
- 全量 `pytest` → `229 passed, 16 deselected`，EXIT=0。
- 真实端到端：`cli.py daily` → DAILY_EXIT=0，汇总 `overall=PASS`；`accept` 复跑 → EXIT=0 `verdict=PASS`（142/0/17），`quality_run_id=daily-20261111-221721`（含时间戳，无碰撞），`_quality.json` 恢复健康全量。

### 风险 / 未做

- cron 仍不装（乙）；`daily` 是 P7 自动化的前置积木，尚未接节假日判断与失败写 ISSUE。
- 桩测试覆盖编排逻辑，但「真跑才暴露的 run-id 碰撞」教训说明后续对 daily 的任何改动仍需一次真实跑兜底。

## [文档] 2026-11-11 23:2x（T-041：同步两处过期读数 · 看板头部与 AGENTS §6.9）

关联任务：`看板.md` T-041、里程碑 P1 行；判据出处 `AGENTS.md` §6.9、§18.6、附录H v1.5。
用户指令：「修复看板头部和 AGENTS §6.9 的过期读数」。

### 变更（纯追加，旧句一字未动）

- `看板.md` 头部：第 3–4 行（2026-10-25 23:30 落笔）保留留痕，新增第 6–7 行「头部读数同步」，作废其中随批次漂移的八项读数（①–⑧）——更新时间、`accept` 判决（`139/0/19`、`158 条 / 14 项` → `142/0/17`、`159 条 / 15 项`）、`reference_date`/`run_id`、P1 里程碑（「待用户签收（标 PENDING）」→ DONE，用户 11-09 签收甲）、全量 pytest 条数（`215 passed`→`229 passed`）、`doctor` 问题条数（12 条 2/9/1 → 11 条 2/8/1）、`coverage` 五项读数、提交状态（「本地未提交」→ T-035–T-040 已 commit、HEAD `d117c68`、仍未 push）；⑨ 单独声明「只同步读数、不改索引」，第 3 行各节 §编号指向未逐条重验。
- `AGENTS.md` §6.9：在 2026-10-25「现状读数」块与「逐项读数」之后追加「现状读数同步」段。作废三处：`counts` 12 条 → 11 条（少的是 `wti_cl:RECENT_BAD_ROWS`，`2026-10-22` 坏日已滑出 §6.6 近期坏行窗口；本批 `wti_cl` 现读 `DEGRADED`、`red=[]`，**滑出窗口不等于源缺陷被修好**）、`report_hash` `37b88a94…` → `5ec7f396…`、`PROVISIONAL` 由「各 1 行 `2026-10-23`、`RERUN_INGEST_TO_FINALIZE`」改为「各 1 行 `2026-11-11`、`available_at=2026-11-12T06:00+08:00` 未到期、`WAIT_UNTIL_AVAILABLE_AT`」；逐项更新 `ZERO_FLOW` 行数（2,565/5,130 与 7,652/15,304）、`spdr_gold` `weekday_gaps=2830`、`gpr` 快照 21 份（至 `2026-11-11T22:18:19`）、`us10y` `as_of_max=2026-11-10`。未变读数照抄：`overall=NEEDS_HUMAN`、封锁 6 个数据集、`review_queue` 4 条 `XGE-`（severity 全 2）、`*:CALENDAR_APPROX` 8 个数据集、`HISTORICAL_ZERO_FLOW` 零行 4/5。同时把 §6.9「边界」段旧句「§18.6 的 14 条判据」按 §18.6 的 11-09 计数注对到 15 项 / 159 条。
- `AGENTS.md` 附录H 新增 v1.5 一行（沿 T-035/T-037/T-039 既有做法：版本号只进附录H）。
- 本批未动：`README.md`、`PROJECT.md`、`config/*.yaml`、`src/**`、`data/**`（无代码/判据/阈值/数据源变化，故不回写结构索引）。

### 验证

- `/usr/bin/python3.10 -m pytest` → 改文档前 `229 passed, 16 deselected in 31.60s`、改文档后 `229 passed, 16 deselected in 28.06s`，均 EXIT=0。
- `/usr/bin/python3.10 .../cli.py accept --stage P1` → `ACCEPT_EXIT=0`、`verdict=PASS`、`counts={"FAIL":0,"PASS":142,"SKIPPED":17}`、`reference_date=2026-11-11`、`quality_run_id=daily-20261111-221721`、`n_checks=159`、`n_rule_ids=15`。
- `/usr/bin/python3.10 .../cli.py doctor`（不带 `--write`）→ `DOCTOR_EXIT=1`、`overall=NEEDS_HUMAN`、`counts={"AUTO_RESOLVED":2,"CONTAINED":8,"NEEDS_HUMAN":1}`（11 条）、`config_hash_match=True`、`review_queue=4`。
- `/usr/bin/python3.10 .../cli.py coverage` → `COV_EXIT=0`、`global_status=OK`、`red=[]`、`missing=[]`、`degraded=[comex_gc,wti_cl,spdr_gold,us10y,gpr]`、启用数据集 16。
- 纯追加自查：`git diff --numstat` → `AGENTS.md`/`看板.md`/`CHANGELOG.md` 三文件删除行数合计 0（新增行只落在追加段落内）。

### 风险 / 未做

- 未重跑克隆闸（`git archive` + 临时目录 pytest，读数属 T-036 `215 passed`，旧读数未重读）与网络冒烟 16 条。
- `AGENTS.md` §0.6 的版本号长行仍只列到 `v1.1`（`v1.2`–`v1.5` 只在附录H）——这是既有写法，本批未夹带修复；若要收口请另行指令。
- `看板.md` 第 3 行其余索引句（各节 §编号指向）未逐条重验，只同步了读数。
- T-025 汇率观察仍未推进：`daily` 里的 `rate` 在 22:17 跑，晚间必然取折算价，测不到「SAFE 中间价当日 09:15 后可见」那一腿。

## [评审] 2026-11-11 23:44（T-042：P2 设计评审 · 三阻断待裁决）

关联任务：`看板.md` T-042、R-015、里程碑 P2 行下的评审注；评审产物 `docs/P2评审.md`（155 行）；判据出处 `AGENTS.md` §6.9、§7、§8、§9、§18.3 P2 表。
用户指令：「P1已完成，P2评审」。

### 新增

- `docs/P2评审.md`：P2 能不能按字面实现的评审。结构为 0 一页结论 / 1 证据来源 / 2 阻断 B1–B3 / 3 重要 M1–M6 / 4 次要 L1–L5 / 5 实施顺序 / 6 待裁决表（甲乙丙＋推荐） / 7 未查。
- `看板.md`：T-042 任务行、R-015（PENDING，三阻断）、里程碑表下新增一条 P2 评审注（P2 里程碑保持 `TODO`：评审≠开工）。

### 三条阻断（本批只提出，不代用户选）

- **B1 封锁组与缺失组口径互斥**：§6.9 硬约束 2 的字面读法是「数据集 `score_eligible=false` → 其**整组**权重剔除」，§8.2 又是「缺失超过 2 组 → `DEGRADED`」。现读 `capabilities` 有 6 个数据集 `score_eligible=false`，分属 cross_market/macro/flows/geopolitics 四组 ⇒ 按字面 P2 上线第一天就常态 `DEGRADED`（只减不加），仓位带与 `T_det` 失去意义；按「组内特征级重归一」读法则四组都还有可用特征、可正常出分。另发现 `DEGRADED` 一名三义（§4.3 模式／§8.2 后果／§7.4 行级标记）。
- **B2 仓位闸的输入不存在**：§8.3 `min(H, 当前卫星仓)`、§9.3 需 `core/cash_floor`、§9.5 需历史高水位、§9.6 需 `Δsatellite` 序列，而账本/NAV 是 P4 交付 ⇒ P2 要么静默跳过风控，要么读一个不存在的账本（＝编数）。
- **B3 `T_det` 无定义**：§2.2/§4.3/§11.7 都把它当兜底交付物，§8 只算到 `[L,H]` 区间，没有任何取点规则 ⇒ P4 决策卡与 P5 降级回退悬空。

### 六项重要（要点）

- M1 `features/registry.yaml` 全盘不存在（`find` → 0 命中），且首批 fact_id 里 `MAC_TIPS10Y`/`MAC_DXY_PROXY`/`GEO_EVENT_SEVERITY` 先天无源或被封锁。
- M2 §8.2「每组标准化到 [-100,+100]」无公式无配置键，可复现闸测不到分数层。
- M3 单位冲突：`regime.hysteresis.band: 0.05`（比例）对 `low_pct/high_pct: 30/70`（百分数）；`VOL_PCT_1Y` 的分位窗口与 `target_vol_annual` 的年化系数（√252/√250）在 `strategy.yaml` 里没有键。
- M4 跨市场 PIT 错日：境外 `comex_gc/wti_cl` 现读各有 1 行 `2026-11-11` `PROVISIONAL`（`available_at=2026-11-12T06:00`），07:00 冻结时境外只能用 11-10 而境内可用 11-11 ⇒ `XMK_*` 的对齐规则必须先声明。
- M5 `contrarian.enabled` 一枚布尔管格言两半句（§8.4.1 前半句降险 v1 允许、后半句加仓须过 §12.7）⇒ 须拆 `reduce/add`。
- M6 P2 无验收闸：`accept --stage P2` 现跑 → EXIT=2 `verdict=ERROR`，而 §18.3 P2 表 8 行验收项全无程序判据。

### 验证

- `/usr/bin/python3.10 -m pytest` → `229 passed, 16 deselected`，EXIT=0（本批只加文档，未动代码）。
- `cli.py accept --stage P1` → EXIT=0 `verdict=PASS`（142/0/17，159 条/15 项）；`accept --stage P2` → EXIT=2 `verdict=ERROR`。
- `cli.py doctor` → EXIT=1 `overall=NEEDS_HUMAN`（11 条 2/8/1）；`_remediation.json` `capabilities` 逐数据集现读 `display/feature/score/promotion` 与 `blocked_*` 三项清单已抄进评审文档 §1。
- 静态现读：`loader.py:755`（strategy 只校验含 `weights`）、`remediation.py:310`（`severity_reviewed` 仅在列存在时读）、`common/timeutil.py:289`（`filter_fact_pack` 已存在）、`PROJECT.md:188-191`（四包标计划）、`ls src/main/.../gold` 无 `features/regime/strategy/risk`、`data/manual/geo_events.csv` 与 `data/normalized/geo_events.csv` 表头均无 `severity_reviewed`、`etf_518880.csv` 有 `amount`／`sge_au9999.csv` 无量额／`comex_gc.csv` 量额被 `price_only` 禁读。

### 风险 / 未做

- 「四组被剔→当天 DEGRADED」是**规则级推演**，不是程序输出（P2 无代码可跑）；评审文档 §7 已如此标注。
- 本批不实现任何 P2 代码、不改 `AGENTS.md`/`config`——B1/B2/B3 属口径选择，按 §0.2「不得自行选择更宽松的一侧」等用户裁决（R-015）。
- P2 前置仍未清：R-005（4 条事件 severity 复核）、R-006（五偏差确认）。
- 未重跑克隆闸与网络冒烟。

## [评审] 2026-11-11 23:49（T-043：P2 评审补录 M7 逆向指标池 ＋ 参考文档衔接）

关联任务：`看板.md` T-043、R-016（与 R-015 并列为 P2 开工前置）；评审产物 `docs/P2评审.md` §8/§9/§10（补录后全文 `wc -l` 现读 236 行、11 节，初版 155 行/7 节）。
用户指令：承「P1已完成，P2评审」——初版漏判了 §18 路线图里属 P2 交付物的「逆向多指标高低点统计原型」，本批补齐。

### 新增

- `docs/P2评审.md` §8：M7 三裁决（M7-a 独立性是否加「不同底层序列」闸／M7-b 极端分位窗口／M7-c 周频指标进日频事件窗）。
- `docs/P2评审.md` §9：与 `docs/国内黄金量化投资设计.md`（10-29 参考文档）判断 1 的衔接——用现读 ρ 矩阵证实「trend/momentum/volatility 三组同源于 518880 一条收盘价、合计权重 0.30+0.15+0.15=0.60」，但按 §0.7 裁决（丙）**不据此改权重表**，权重改动属 §12.7/P3 与用户书面裁决；P2 侧只加「有效独立性」判据。
- `docs/P2评审.md` §10：能力粒度表（现读 16 个数据集的 `display/feature/score/promotion/price_only/blocked_*` 逐列）与新增次要项 **L6**——`gpr` 是 `feature=true/score=false`，所以 `GEO_SHOCK` 在 P2 **可以触发**且后果属降险；若实现把 `score_eligible=false` 一刀切成「不可计算」，`GEO_SHOCK` 会静默永不触发且判据不报错。建议 registry 加 `use_in_score/use_in_regime` 两位，并配「`gpr` 封锁时 `GEO_SHOCK` 仍可被夹具触发」的正对照格。同节标注计数变更：次要项 L1–L5 → **L1–L6**，重要项 M1–M6 → **M1–M7**（初版标题与 §0 一字未动，以该句为准）。

### 探针读数（一次性内联执行，未在仓库留文件、未进 `experimental/`、未进 `main`）

- 独立性（`etf_518880.close` 派生，样本 2,959 行 `2014-09-04..2026-11-11`）：同组内 `MOM_RET_20D × MOM_RSI14 = 0.839`、`VOL_RV20 × VOL_PCT_1Y = 0.745` 超 §8.4.2 的 ρ≤0.7 ⇒ 判不独立；跨组 8 对在 0.026–0.543 之间 ⇒ 全部判独立——**即三组虽同源一条序列，ρ 闸抓不住**，这是 M7-a 的实据。
- 分位窗口敏感性（4 指标、持续≥2 日、5 日重叠窗、20 日冷却）：低（恐惧）方向 OVERLAP 去重事件数 滚动 252 日 `N=63`（原始同日极端 602 日）vs 全历史扩展 `N=35`（297 日），**差 28 个事件**；高（贪婪）方向 63 vs 65。`N<30 → INSUFFICIENT_EVIDENCE` 与 Wilson CI/`n_trials` 都随窗口变，而 §8.4.2 没写窗口——这是 M7-b。
- 可算性正面读数：四指标同时可判样本 3,031 行（`2014-05-26..2026-11-11`，12.5 年），满足 §8.4.2「至少 5 年」。
- 周频对齐：`cftc_gold` 现读 1,941 行（`_quality.json` 的 `rows/total_rows` 均为 1941），要进「N 个交易日内同时极端」的日频窗只能前值填充，而 §6.7-5 明写不得用前值替代缺失——**§8.4.2 的字面实现与 §6.7 直接冲突**，这是 M7-c。

### 验证

- `/usr/bin/python3.10 -m pytest` → `229 passed, 16 deselected`，EXIT=0（本批同样只动文档）。
- `wc -l data/normalized/cftc_gold.csv` → 1942（含表头）、`etf_518880.csv` → 3231；`_quality.json` 现读 `cftc_gold rows=1941`、`etf_518880 rows=3230`。
- 探针脚本以 heredoc 内联运行，跑后 `git status --short` 只有 `docs/P2评审.md` 与台账文档被改，无新增未跟踪文件。

### 风险 / 未做

- 探针里的 RSI/RV/分位公式是评审近似，P2 正式实现须按 `features/registry.yaml` 重算并对表；因此 §8.2 的事件数用于**证明口径缺口存在**，不作为 P2/P3 的统计结论。
- 未做事件后收益命中率与成本后统计（属 P2 原型/P3 报告范围，且须预注册后冻结）。
- M7-a/b/c 未裁决；R-015 的 B1/B2/B3 亦未裁决——P2 仍不得开工。

## [评审] 2026-11-11 23:54（T-044：P2 评审补录三 M8 规则 trace）

关联任务：`看板.md` T-044、R-017、里程碑下评审补录注；评审产物 `docs/P2评审.md` §11（补录后 `wc -l` 现读 266 行、12 节）。
用户指令：承「P1已完成，P2评审」——按 §18 路线图 P2 行的交付清单逐项核对，初版漏了最后一项「规则 trace」。

### 新增

- `docs/P2评审.md` §11：M8。规则 trace 在 `AGENTS.md` 有六处引用（`:1976` 路线图 P2 交付、`:457` 决策层落盘位置、`:1078` 报告第 6 节、`:1154` 时间线 07:22–07:35 写 trace、`:1431` 规则追溯/What-if 页、`:122` §1.2 目标 6），但：
  - 没有任何一处定义它的字段、粒度、顺序、落盘文件名与失败行为（现有文字只给「分数、regime、`[L,H]`、风控闸」四个主题词）；
  - §4.4 哈希清单（`:266-272` 现读七项：`fact_pack_hash`/`config_hash`/`data_snapshot_hash`/`prompt_hash`/`model_digest`/`decision_hash`/`prev_hash`）**不含 trace 哈希**，即「trace 逐位可复现」目前没有可比凭证；
  - §18.3 P2 验收表 8 行逐行核对，**没有 trace 一格** ⇒ 该交付物无法验收。
- 三处连锁后果与三档选项（甲＝P2 定义 `rule_trace.json` schema ＋两格判据「同输入两次 trace 逐字节相同」「改阈值不改事实包」；乙＝推到 P4，须作废路线图那一句；丙＝用 `pipeline_status.json` 冒充，不推荐——节点级状态回答不了「哪条阈值生效」）。

### 计数同步

- P2 评审待裁决合计 **10 项**：B1/B2/B3、M4/M5、M7-a/b/c、L4、M8；分布在 R-015（三阻断）／R-016（M7 三裁决）／R-017（M8）。
- 看板里程碑下的初版评审注「三阻断 B1/B2/B3 待用户裁决」为初版范围，已在其后追加补录注点名，旧句未动。

### 验证

- `/usr/bin/python3.10 -m pytest` → `229 passed, 16 deselected`，EXIT=0（本批仍只动文档）。
- 引用行号逐条来自现读：`grep -n "trace" AGENTS.md` → 1976/457/1078/1154/1431/122/257；`sed -n '263,274p' AGENTS.md` → 哈希清单七项无 trace。

### 风险 / 未做

- §18.3 P2 表「8 行」是按该表现读行数计（注册表完整／事实包 PIT／确定性／仓位不变量／regime 与滞回／缺失策略／逆向原型只读／无 LLM 参与），未把 §18.1/§18.4 的通用闸重复计入。
- 若用户选甲，trace 是否需要独立哈希（`trace_hash` 进 §4.4 清单）属口径变化，须按 §18.5 重开验收并同批改 AGENTS，本批不预定结论。

## [运维] 2026-11-12 22:2x（T-045：`cli.py daily` 监控与优化 · 互斥锁＋耗时汇总）

关联任务：`看板.md` T-045；判据出处 `AGENTS.md` §19.11（新增）、附录H v1.6；结构索引 `PROJECT.md`（`cli.py` 行、`test_cli.py` 行）。
用户指令：「监控并优化：`/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py daily` 已执行」。

### 监控现场读数（改动前）

- 用户 22:03:39 触发的那次跑成功：`run_id=daily-20261112-220339`，18 节点 `OK=13/DEGRADED=5`，`overall=DEGRADED`，全局质量 `OK`、`red=[]`；起点 `22:03:40.434855` → `_quality.json` 落盘 `22:05:10.313130` ⇒ 抓取到质量闸约 **90 秒**。
- 慢在哪：**只有 `pipeline_status.json` 答得了**。`_quality.json` 现读无耗时键、数据集无 `duration`；`ingest.py` 只把耗时写进状态文件。旧样本 9 源合计 64,213 ms，其中 `spdr_gold` 27,804 ms、`etf_518880` 16,812 ms（两者 69%）。
- 日志答不了：`logs/app-2026-11-12.jsonl` 现读 16 行全是 `ingest outcome`，**0 行含 `duration_ms`**，没有 preflight/normalize/quality_gate/accept/doctor/rate 任何阶段行 ⇒ §15.7「阶段、状态、耗时、错误码齐全」不成立。
- 汇总只进 stdout：`daily` 结束仅打印 `{step, exit}`，**不落盘**；无人值守（P7）时读不到判决，也看不出耗时。
- 并发无保护：全仓 `grep lock` 零命中 ⇒ 两个 `daily` 可同时写同一批目录（2026-11-11 已发生过一次覆写 `_quality.json` 的事故，当时只加了 run-id 时间戳，没解决并发）。
- `rate` 腿在晚间必然取折算价 ⇒ 看板 T-025「SAFE 中间价当日 09:15 后可见」的观察**永远不会被推进**，而汇总里看不出来。

### 变更（`cli.py`，先写失败测试再改）

- 新增 `DAILY_LOCK_NAME`/`EXIT_BUSY=3`/`_pid_alive`/`_acquire_daily_lock`/`_release_daily_lock`/`_daily_summary_path`/`_rate_observation`/`_pending_finalization`/`_finish_daily`。
- `daily` 起跑前原子取 `data/.daily.lock`（内容 `"<pid> <ISO8601>"`）：活锁占用 → 退出码 **3**、一个子命令都不执行、不写任何产物；pid 不存在视为陈旧锁，删除后重取一次；`PermissionError` 算存在，不得当陈旧锁删；`--force` 可越权但**不取得所有权**，结束时绝不删别人的锁。
- 每步用 `time.perf_counter()` 记 `duration_ms`，并写一条 `message="daily step"`、`extra={run_id, stage, status:"exit_<码>", duration_ms}` 的结构化日志（`duration_ms` 本就在日志字段白名单里，以前没人写）。
- 结束（含硬停）原子写 `data/decisions/<date>/daily_summary.json`：`schema_version/run_id/date/started_at/ended_at/total_ms/daily[].{step,exit,duration_ms}/step_ms/overall/stopped_at/exit_code/scope/datasets/config_hash/lock/rate_observation/pending_finalization/artifacts{quality,remediation,pipeline_status,daily_summary,log_dir}`；落盘失败只写 `summary_error`，不打死日链。
- 新增 `--datasets`（透传 ingest，汇总标 `scope=subset`，禁止冒充全量 §6.6）与 `--force`；`--run-id` 帮助文本按字面改为 `daily-YYYYMMDD-HHMMSS`。
- `rate_observation`：`phase ∈ {pre_0930, intraday(09:30–18:00), post_1800}`，`observation_valid = (phase=="intraday")`——把 T-025 观察是否推进绑进汇总，晚间跑不再假装推进。
- `pending_finalization`：从 `_remediation.json` 只取 `PROVISIONAL` 类问题（`dataset/rows/dates/max_available_at/refresh_due/action`）并给下一步；文件不存在时 `items=[]` 且明写「不猜」，不伪造尾巴。
- 模块 docstring 退出码约定补 3=BUSY（旧文本只写 0/1/2）。

### 新增测试（10 条，`integration/cli/test_cli.py`，全部离线）

- 每步 `duration_ms` 与 `total_ms ≥ Σ`；`daily_summary.json` 落盘且含 `schema_version/run_id/config_hash/artifacts`；硬停（preflight=2）也落盘 `overall=STOPPED/stopped_at=preflight` 且只含已跑那一步。
- 活锁 → 退出码 3 ＋ 子命令调用列表为空 ＋ 汇总文件不存在（负对照）；陈旧锁（真跑一个已退出子进程的 pid）→ 清理后五步全跑完并释放锁（放行对照）。
- 5 条 `daily step` 日志逐条带 `duration_ms`/`run_id`；`--datasets` 透传且 `scope=subset`；`rate_observation` 在 22:30→`post_1800/false`、09:45→`intraday/true`；`pending_finalization` 只列 `PROVISIONAL`（夹具含 `ZERO_FLOW` 干扰项）；无诊断文件时不造尾巴。

### 验证

- 先红：新增前 7 条 `7 failed, 5 passed`（EXIT=1）；实现过程中 1 条 `NameError: atomic_write_json` 是我测试笔误（helper 未导入），改为写真文件后转绿——不是生产缺陷。
- 后绿：`daily` 选择集 `15 passed`；全量 `/usr/bin/python3.10 -m pytest` → **`239 passed, 16 deselected`，EXIT=0**（旧基线 229，同批 +10）。
- 真跑 A（22:15 全量）→ `DAILY_A_EXIT=0`，汇总 `overall=PASS`、`total_ms=86155`、`step_ms={preflight:16, ingest:75551, accept:10054, doctor:485, rate:46}`；日志出现 5 条 `daily step`（旧行为 0 条）；锁跑后已删除。
- 真跑并发负对照（A 在跑时启 B）→ `DAILY_B_EXIT=3`、`overall=BUSY`、`detail=another_daily_running:pid_36025`，`_quality.json` sha256 前后同为 `3f0b69b74c6657227601465c…` ⇒ **零执行且零写盘**。
- 真跑子集（22:20，`--datasets usdcny,usdcny_mid,comex_gc,wti_cl`）→ `SUBSET_EXIT=0`、`scope=subset`、`total_ms=32113`（ingest 21,460 / accept 10,004）、`accept` 仍 `PASS`；`pending_finalization` 现读 `comex_gc`/`wti_cl` 各 1 行 `2026-11-12`、`available_at=2026-11-13T06:00+08:00`、`refresh_due=false`、`action=WAIT_UNTIL_AVAILABLE_AT`。
- `config_hash` 仍是 `sha256:0946643d…`（本批没碰任何 YAML）⇒ 不触发 §18.6 的 Q3 重跑要求。

### 风险 / 未做

- 未做并发抓取：`ingest` 75.5s 里两个源占大头，但总耗时已在 §17.11「日链确定性部分 <3min」预算内，且 `PipelineStatusWriter` 非线程安全——要提速需单独授权并先给它加写入锁。
- 单次 `daily` 仍会重复抓全量（同日重跑代价 ≈86s 网络）；`--datasets` 提供了便宜的补抓手段，但没有自动「只补缺的那条腿」的调度（属 P7 cron）。
- 明早 06:00 后必须再跑一次（全量或境外/外汇子集）才把 `PROVISIONAL` 转正；`daily_summary.pending_finalization.next_action` 已把这句话写进产物。
- 子集验证把 `_quality.json` 一度写成 `scope=subset`，随即用一次全量跑恢复：`RESTORE_EXIT=0`、`run_id=daily-20261112-222101`、汇总 `scope=full`、`overall=PASS`、`total_ms=92637`（`ingest` 78,951 / `accept` 12,835 / `doctor` 728 / `rate` 94 / `preflight` 22），`_quality.json` 现读 `scope=full`、`global_status=OK`，锁已释放；尾巴仍是 `comex_gc`/`wti_cl` 各 1 行 `2026-11-12`、`refresh_due=false`。
- 同日三次全量/子集耗时对照：86,155 ms（22:15 全量）／32,113 ms（22:20 四源子集）／92,637 ms（22:21 全量恢复），`accept` 稳定 10.0–12.8 s、`ingest` 75.6–79.0 s ⇒ 全量已在 §17.11 `<3min` 预算内，子集约为全量的 37%。
- T-025 仍未有效推进：需要一次 09:30–18:00 之间的运行；现在汇总会如实标 `observation_valid=false` 而不是沉默。

## [验证] 2026-11-12 22:52（T-046：联网冒烟 16 条与克隆闸现跑＋有牙证据）

关联任务：`看板.md` T-046；规范出处 `AGENTS.md` §17.13（新增）、附录H v1.7。
用户指令：「跑，并验证——未跑网络冒烟 16 条与克隆闸（旧读数）」。

### 新增

- `AGENTS.md` §17.13：把两道闸的**可复制命令、退出码口径、覆盖范围、两条陷阱、两条负对照**固定下来；并明写克隆闸不能证明什么（不覆盖联网、不证明数据内容正确）。
- `看板.md` T-046 行：本批现跑读数与踩坑记录。

### 现跑读数（旧读数作废，旧句留痕）

- 联网冒烟：`/usr/bin/python3.10 -m pytest -m network src/test/python/com/sun/shensuan/gold/contract/adapters/test_network_smoke.py` → **`16 passed in 58.89s`，NETWORK_EXIT=0**（起场 `MemAvailable=5.18 GiB`）。条数构成现读为 15 条数据集冒烟（`list-datasets`：20 个数据集、16 个 enabled，其中 `geo_events` 走手工适配器不进联网层）＋ 1 条上交所日历冒烟；用例由 `pytest_generate_tests` 从配置动态参数化，故条数随「启用非手工源」数增长（T-016 旧读数 15 条 / 78.29s）。
- 克隆闸：`git archive HEAD`（HEAD=`8becd0b`）解到 `data/tmp/`（gitignored）→ 解包 **74 个文件**、克隆内无 `data/` → 在克隆里跑离线全量 → **`239 passed, 16 deselected in 42.19s`，CLONE_EXIT=0**（T-036 旧读数 215 passed）。

### 负对照（两条都动被试侧，不动判据）

- **NC3（克隆闸能看见「测试直读 gitignored 生产数据」）**：在 pristine 克隆里新增一条读 `data/normalized/etf_518880.csv` 的用例 → `2 failed, 238 passed`：一条红在该文件确实不存在（缺陷类被抓），另一红是架构闸 `test_all_python_files_listed_in_project_md` 顺手抓到该文件未登记 —— 两道闸各自真在咬。
- **NC4（克隆闸能看见源码漏提交）**：`rm src/main/python/com/sun/shensuan/gold/data/rates.py` → `ModuleNotFoundError: com.sun.shensuan.gold.data.rates`，3 个测试文件收集失败、`205 deselected, 3 errors`、EXIT=2（不是静默放行）。
- **踩到的两条陷阱（已写进 §17.13）**：①复用「已经跑过测试的克隆」时，`load_config()`/preflight 已把 `data/`、`logs/` 建出来，NC3 的前提断言 `assert not (REPO_ROOT/"data").exists()` 先失败——那不是缺陷被抓，是我的前提失效 ⇒ 规范改为「每枚对照用新解包的克隆」；②`parents[N]` 数错（`com/sun/shensuan/gold` 是三层）会让对照因为「找不到 AGENTS.md」这类别的原因失败，看起来像抓到缺陷 ⇒ 规范改为「先自证仓库根，再断言被试缺陷」。

### 环境与清理

- 验证只在一次性克隆内进行；主仓 `src/`、`config/`、`data/` 未被改动：跑完 `rm -rf` 临时克隆后 `git status --porcelain` 为空，`_quality.json` 仍是 `run_id=daily-20261112-222101`、`scope=full`、`global_status=OK`，`daily_summary.json` 未被本轮改动，`data/.daily.lock` 不存在。
- 本批未改代码/配置/判据 ⇒ `config_hash` 不变、不触发 Q3；离线全量在改动文档后复跑 `239 passed, 16 deselected` EXIT=0。

### 风险 / 未做

- 网络冒烟的绿只证明「15 个启用非手工源 ＋ 上交所日历当下可抓且形状不变」，不证明价格质量与阈值口径（后者归 `accept`/`doctor`），也不覆盖手工源（`geo_events`）。
- 克隆闸没有接入任何自动化（本仓库无 CI），仍是「要人跑」的一次性验证；§17.13 只是把命令固定下来，不等于它每天会跑。
- 未在克隆内验证 `accept --stage P1`（该判据依赖 `data/`，克隆里没有）；这属于设计边界而非缺陷。

## [运维] 2026-11-13 14:18（T-047：`daily` 待转正提示按 items 到期时间生成）

关联任务：`看板.md` T-047；规范出处 `AGENTS.md` §19.11（汇总落盘）。
用户指令：「修」（针对 2026-11-13 日更验证时提出的 `next_action` 文案瑕疵）。

### 变更

- `cli.py::_pending_finalization`：`next_action` 的到期时间改为按 `items[].max_available_at` 取最早/最晚生成，不再写死「境外 06:00 之后」。14:03 这类盘中跑会同时留下 `usdcny` 当日 18:00 与 `comex_gc`/`wti_cl` 次日 06:00 两种点，旧文案会把外汇那行说成「明早再说」。
- 新增集成测试 `integration/cli/test_cli.py::test_daily_summary_next_action_uses_item_due_times`：两行到期时间不同时，最早/最晚都必须出现在 `next_action`，旧固定文案不得再出现。

### 验收证据（2026-11-13 现跑）

- `/usr/bin/python3.10 -m pytest src/test/python/com/sun/shensuan/gold/integration/cli/test_cli.py -k daily` → `16 passed, 9 deselected in 7.07s`，EXIT=0。
- 负对照：把 `next_action` 临时还原成旧固定文案 → 同一条新用例 `1 failed, 24 deselected in 0.61s`（EXIT=1）；恢复后 `sha256sum cli.py` 回到 `7282d513bdccc65c3414fb7b7cc11e360cfc1a3f42dbe2e1068a4f151ccfe74d`。
- 全量 `/usr/bin/python3.10 -m pytest` → `240 passed, 16 deselected in 40.47s`，EXIT=0（旧读数 239 passed）。
- 本批未改阈值/数据源/交易口径 ⇒ `config_hash` 不变（仍为 `sha256:0946643d…`）。

### 边界

- 当日 `data/decisions/2026-11-13/daily_summary.json` 由改动前的代码生成，`next_action` 仍是旧文案；新逻辑在下一次 `daily` 运行时生效，本批不重跑抓取、不改写该产物。
