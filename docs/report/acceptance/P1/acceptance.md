# P1 验收卡（第 4 版：程序化验收 `verdict=PASS` · 三条数据口径按用户裁定落地 · 误报事件已删）

> 判据来源：AGENTS.md §18.6（你 2026-10-23 裁定「数据问题以数据质量、连续性、一致性为标准，由程序标准化验收，不通过人工人眼验证」）。
> 本卡替换第 3 版（10-23 落盘的「3 条判红」版）：旧版本文件未提交、HEAD 中不存在，**已就地改掉、无旧句可作废**。
> 两版之间的变化：①你裁定「D1乙、D3乙、D4乙」三条数据口径；②删除 2026-10-20 家乐福误报；③修掉「剔除行从历史并集复活」的合并语义缺陷。
> 旧证据作废理由：`config_hash` 由 `sha256:56397ced…` 变为 `sha256:9e87c4e8…`（配置改了、数据重跑了，§18.1 纪律 1）。

阶段：P1 数据层
结论：**程序判 `verdict=PASS`**（158 条判据 = 139 PASS / 0 FAIL / 19 SKIPPED），机器闸已过；**阶段未签收**，里程碑保持 `PENDING`，等你看完第 6 节四问后签字。
对应版本：
- 代码：工作区未提交（HEAD `7f45934` + 未提交改动），分支 `feature/v1`，AGENTS v1.0
- 配置：`config_hash = sha256:9e87c4e8ee099f9c4720bde3e0e455c874ac197644161d96f720eb83995b778c`（accept 输出与 `_quality.json` 两处同值）
- 数据：accept 输出 `quality_run_id = p1-fix-20261024-e`、`quality_scope = full`；`data/normalized/_quality.json` 的 `generated_at = 2026-10-24T00:55:20.470800+08:00`（accept 输出不含该字段）

---

## 1. 机器闸：一条命令判阶段

```
命令：/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py accept --stage P1
退出码：ACCEPT_EXIT=0（0=PASS，1=FAIL，2=参数/配置/质量报告缺失）
读数：verdict=PASS，counts={"FAIL":0,"PASS":139,"SKIPPED":19}，reference_date=2026-10-23
阈值（config/application.yaml 的 acceptance 段）：min_weekday_coverage=0.8、max_stale_days=5、weekly_rows_per_year=[40.0,60.0]
日历（accept 输出的 calendar 节点）：cn_sse integrity=OK，8,797 个交易日，1990-12-19..2026-12-31，
      fetched_at=2026-10-23T22:13:55.929468+08:00，source=akshare.tool_trade_date_hist_sina；
      weekday 口径 integrity=APPROX（自然工作日近似，美国/外汇源用）
```

14 条判据各自的结果（按数据集逐条累加，158 = 139+0+19）：

| 判据 | 它挡什么 | PASS | FAIL | SKIPPED |
|---|---|---:|---:|---:|
| C1 PIT 字段 | 每行缺 PIT 列之一；空表当正常 | 16 | 0 | 0 |
| C2 时间口径 | `available_at < as_of`、日期格式混用 | 16 | 0 | 0 |
| C3 主键唯一 | 按 `key_columns` 声明判重复 | 16 | 0 | 0 |
| C4 数值区间 | 价格/汇率量级错 | 6 | 0 | 10（配置未声明区间的非价格源） |
| C5 行级质量可复现 | DEGRADED 说不出哪个标记；`MANUAL` 被误当降级 | 16 | 0 | 0 |
| C6 缺失未被冒充 | NaN、非正价格、结构性 0 未披露 | 13 | 0 | 3（纯文本快讯源无数值列） |
| C7 剔除留档有效 | 坏行静默消失、`rejected/` 无原因 | 16 | 0 | 0 |
| N1 日历覆盖率 | 日频源缺日（分子按唯一日）；低覆盖未披露 | 11 | 0 | 4（news×3 + events×1） |
| N2 最近不断供 | 核心源断供；events 不适用 | 14 | 0 | 1 |
| N3 周频节奏 | 周频被误报 / 日频形状误标周频 | 1 | 0 | 0 |
| Q1 质量报告自洽 | 报告与规范化表不一致 | 1 | 0 | 0 |
| Q2 原始层只增不改 | 覆盖历史原始快照 | 1 | 0 | 0 |
| Q3 验收与数据同版本 | 旧数据配新判据 | 1 | 0 | 0 |
| S1 原始重放对表 | 规范化结果与原始快照逐格不符 | 11 | 0 | 1（gpr 原始件是 `.xls`） |

分组小计（accept 输出的 `group` 字段）：一致性 110 PASS / 14 SKIPPED；连续性 26 PASS / 5 SKIPPED；质量 3 PASS；FAIL 合计 0。

`SKIPPED` 的 19 条都是「输入确实不存在」，不计入通过：C4 的 10 条（非价格源未声明区间）、C6 的 3 条（快讯无数值列）、N1 的 4 条（news×3 是事件流、events 一天多条没有「该有一条」的概念）、N2 的 1 条（events 无断供概念）、S1 的 1 条（GPR 原始件是 `.xls`，无 CSV 可重放）。

## 2. 三条裁定落地（你选的「乙」）

### D1 乙：`cftc_gold` 两行多空净仓全 0 → 整行剔除

- 规则：series 数据集**全部必需数值列同时为 0** ⇒ 源没给这一周的值，整行剔除、留档 `ALL_ZERO_IN_REQUIRED`。
- 现读：C6 `数值列 ['long','short','net'] 无空值；非正价格 0 行；结构性零均已标记`；C7 `剔除 2 行留档，原因 ['ALL_ZERO_IN_REQUIRED']`；主表 1,940→1,938 行、`quality=GREEN`、`zero_rows=0`；N3 `1986-01-15..2026-10-13 行数=1938 行数/年=47.6`（周频口径 40–60 内）。
- 代价：历史少 2 行（1997-12-19、2003-02-18）。
- **没做成全局开关**：GPR 有 2 行 `gprd=0` 是真实的平静日，全局强推会把真实读数删掉，所以这是 per-dataset 开关、只有 `cftc_gold` 打开。

### D3 乙：`shfe_au0` 的 `settlement<=0` → 整行剔除

- 规则：`positive_columns: [settlement]`，值 ≤0 整行剔除、留档 `NONPOSITIVE_SETTLEMENT`；缺值仍走 `NAN_IN_REQUIRED`（「源给 0」与「源给空」分开记）。
- 现读：C7 `剔除 618 行留档，原因 ['NONPOSITIVE_SETTLEMENT']`；主表 4,575→3,957 行；C6 七列（`open/high/low/close/volume/open_interest/settlement`）无空值、非正价格 0 行；N1 `2010-06-08..2026-10-23 唯一日=3957 应交易日=3975 命中=3951 覆盖率=99.4% 口径=cn_sse(...) 分母外日=6`。
- 代价：首日由 2008-01-09 退到 2010-06-08，覆盖 18.8 年→16.4 年。仍满足 P1「主标的 ≥5 年」，但 2008–2010 段在 P2 不能拿结算价做特征。

### D4 乙：`spdr_gold` 覆盖率按披露放行，P2 只展示

- 规则：日频源覆盖率低于 80% ⇒ 数据集标 `LOW_COVERAGE` 降 YELLOW；N1 只在质量报告**已按同一标记披露且 quality 非 GREEN** 时放行，读数写明「已披露」。
- 现读：`spdr_gold quality=YELLOW、status=DEGRADED、notes=[LOW_COVERAGE]、weekday_gaps=2829`；N1 `覆盖率 50.5% < 下限 80%，质量报告已按 LOW_COVERAGE 披露（quality=YELLOW；D4 乙：只展示、不进分数）；2004-11-19..2026-10-22 唯一日=2894 应交易日=5720 命中=2891 覆盖率=50.5% 口径=weekday_approx 分母外日=3`；N2 滞后 1 天（非核心，只披露）。
- 这条豁免只认覆盖率：`STALE` 等其他黄标借不到（负对照 `test_coverage_below_floor_passes_only_when_low_coverage_disclosed` 四格：已披露 PASS／未披露 FAIL／只有 `STALE` FAIL／`LOW_COVERAGE`+`GREEN` FAIL）。
- P2 约束：`FLOW_SPDR_CHG_1W/4W` **只展示、不进分数、不作加仓依据**（空洞上的 4 周窗口会算出假流量）。该约束登记在 `看板.md` R-013，P2 实现时由你确认是否仍保留。

## 3. 删除 2026-10-20 家乐福误报（「家乐福那条删」）

```
命令：/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py extract-events（dry-run）
读数：EXTRACT_EXIT=0；events_extracted=4（原 5）；review_excluded_rows=3；rows_after_merge=4
      输入 2,463 行快讯（shmet 2,134／cls 140／sina 189）；rules_version=ge-2
      csv_sha256=b197ed4d6aace26cc1d3a2d0c0fcab13b5f3b1cd11ad38d592a43ee0bb25bb92
```

排的是 3 条、不是 1 条：同一则家乐福公告被三家快讯转述，只删 10-20 那条，10-21 的转述会立刻顶上来成一条新事件。

| # | `content_hash` 前 12 位 | 日期 | 为什么排 | 谁定的 |
|---|---|---|---|---|
| 1 | `sha256:ee8f816189ca…` | 2026-10-20 | 家乐福下调财务目标、顺带提到骚乱：主体是公司财报，属转述误报 | 用户 |
| 2 | `sha256:b92aeedc5e96…` | 2026-10-21 | 同一则公告的另一篇（谈消费压力） | Agent |
| 3 | `sha256:a93db66c87b4…` | 2026-10-21 | 同一则公告的第三篇（谈 2 亿欧元影响） | Agent |

- 机制：`strategy.yaml` 的 `geo.news_events.review_exclusions` 按 `content_hash` 点名，附 `reason/decided_by/decided_at`；加载期校验哈希格式、理由非空、无重复。它记的是**复核结论**、不是关键词规则，所以不改 `rules_version`（改了会让所有历史事件号整批变化）。
- 判据：`geo_events` 4 行，C1/C3/C5/C6/C7 全 PASS（C5 `MANUAL 仅披露不降档 4 行`），N1/N2 按事件流口径 `SKIPPED`。
- **报备**：第 2、3 条是我按「同一则公告」补的（`decided_by=Agent` 已写明）。你若只要第一条，说一声即改回。

## 4. 顺带修掉的合并语义缺陷（不加判据，改代码）

三条乙落地后的一次实跑暴露：剔除只写进 `rejected/`，主表仍按并集合并，被剔的行第二天又复活（cftc 总数 1,940、au0 4,575、geo_events 5）。

- `store.merge(..., removed=...)`：规范化层剔掉的键从主表删除（按 `key_columns` 规范化后的元组比对）。
- `store.merge(..., authoritative=...)`：`geo_events` 声明 `authoritative_snapshot: true`，整表以本次抽取结果为准。否则「删除 10-20 那条」永远删不掉——它比现存最新事件还新，按日期窗口保护会把它留下来。
- 回归格：`test_merge_removes_rejected_keys_from_main_table`、`test_merge_authoritative_replaces_whole_table`。

## 5. 交易日历接入后的覆盖率读数（同一口径两处对得上）

| 数据集 | 分母口径 | 唯一日/应交易日 | 覆盖率 | 判定 |
|---|---|---|---:|---|
| `etf_518880` | cn_sse 官方交易日 | 3,217 / 3,217 | 100.0% | PASS |
| `etf_159934` | cn_sse | 3,124 / 3,124 | 100.0% | PASS |
| `sge_au9999` | cn_sse | 2,369 命中 / 2,389 | 99.2%（缺 20 天，1 行在日历外） | PASS |
| `shfe_au0` | cn_sse | 3,951 命中 / 3,975 | 99.4%（缺 24 天，6 行在日历外） | PASS |
| `usdcny` / `usdcny_mid` | weekday 近似（外汇报价日无官方日历） | 1,320 / 1,321、7,984 / 8,560 | 99.9% / 93.3%（104、87 行在周一至周五之外） | PASS |
| `comex_gc` / `wti_cl` / `us10y` | weekday 近似（美国交易日无官方日历） | 2,553 / 2,613、7,641 / 7,830、1,452 / 1,514 | 97.7% / 97.6% / 95.9% | PASS |
| `spdr_gold` | weekday 近似 | 2,891 命中 / 5,720 | **50.5%（已按 LOW_COVERAGE 披露）** | PASS（D4 乙） |

诚实披露：美国源没有可直连的官方日历（本机 akshare 1.18.81 只有 `tool_trade_date_hist_sina` 一个日历接口），它们的分母仍是周一至周五近似，美国假日会被算成缺口，数字**偏保守**；每条读数都写 `口径=weekday_approx`，不冒充官方。

## 6. 非专业四问（§18.2，你只需回答「能 / 不能 / 看不懂」）

| # | 问题 | 在哪看 | 本卡证据 |
|---|---|---|---|
| 1 | 数字有来源吗 | 本卡每节 | 所有读数都指到某一次命令输出（`/tmp/accept_fix.json`、`_quality.json`、extract 的 dry-run 输出）；`accept` 的 Q3 证明数据与判据同版本 |
| 2 | 缺口显眼吗 | 第 2、5 节 | `spdr_gold` 50.5% 黄灯披露；WTI 10-22 坏行判 RED 并进 `red`；GPR `REVISED_ONLY`；`PROVISIONAL` 不进事实包；19 条 SKIPPED 逐条写了原因 |
| 3 | 能确认不越权吗 | 第 8 节 | 静态扫描 0 命中；`.env` 不存在且被忽略；`data/live/` 未创建且被忽略；全仓无券商路径 |
| 4 | 结论能作废吗 | 第 7 节 | 每条失效条件可观察（`config_hash` 变即作废、日历失效回落近似、severity 未经复核不得加仓） |

## 7. 结论的失效条件（第 4 问）

- 任何代码/配置/数据变化 ⇒ `config_hash` 变 ⇒ 本卡作废，Q3 会把旧数据配新尺子的做法直接判红，必须重跑 `ingest` + `accept` 出新卡。
- GPR 41.8 年（15,267 行）是修订后序列（`revised_only=[gpr]`）：只可研究展示，不得用于 H-GEO 晋升（§12.8）。
- `PROVISIONAL` 行不进事实包：COMEX/WTI 的 2026-10-23 行 `available_at=2026-10-24 06:00`，07:00 前的事实包会剔除它们。
- 事件日历为空 ≠ 无地缘风险；现在有 4 条抽取事件，`GEO_EVENT_SEVERITY` 仍只表示「识别到/未识别到」，不表示风险大小（§7.3）。
- 抽取事件的 severity 未经人工复核 ⇒ 不得作为加仓依据（§6.2、§8.5）。
- COMEX/WTI 全量行 `volume/open_interest=0`：只当收盘价，不当真实流动性。
- 交易日历只覆盖境内口径：美国/外汇源仍按周一至周五近似，覆盖率数字偏保守；日历文件被改写、sha256 不符或为空时自动回落近似并披露原因。
- AU0 覆盖只到 2010-06-08：2008–2010 段因 `settlement=0` 已整行剔除，不能拿那段做结算价特征。

## 8. 未查（不得折成通过）

- 抽取事件的 severity 与地区标注未经你复核（属人工复核范围）——未查。
- 快讯窗口只有 86 天（SHMET `2026-07-30..2026-10-23`）与 5 天（CLS/Sina）：5 年事件史无法回补，`GEO_EVENT_SEVERITY` 的历史序列仍不存在——未查。
- `-m network` 的 16 条用例（含日历冒烟 1 条）本轮未执行；直连 akshare 的汇率/AU0 全历史对表未重跑（旧读数，未重读）——未查。
- 美国/外汇交易日历没有官方来源，N1 对它们只是近似口径——未查。
- C4 的 10 条 SKIPPED：非价格源未声明数值区间，未查它们的合理量级闸。
- GPR 的 S1 重放（原始件是 `.xls`）——未查。
- 备份恢复演练、账本链、回测：属 P4/P7/P3 闸——未做。
- 本批改动（含 `data/normalized/*`、`data/manual/geo_events.csv` 4 行）均未提交、未 push。

## 9. 复核用的命令与读数（accept 00:55、extract 01:01:11、pytest 01:03:38 现跑）

```
/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py accept --stage P1 → ACCEPT_EXIT=0
    verdict=PASS；counts={"FAIL":0,"PASS":139,"SKIPPED":19}；quality_run_id=p1-fix-20261024-e；scope=full
/usr/bin/python3.10 -m pytest → 201 passed, 16 deselected in 26.21s，EXIT=0（01:03:38 现跑）
cli.py ingest --run-id p1-fix-20261024-e → INGEST_EXIT=0，scope=full，global_status=DEGRADED，red=[wti_cl]，core_not_green=[]
cli.py extract-events → EXIT=0，dry_run=true，events_extracted=4，review_excluded_rows=3
cli.py calendar --refresh → 上次 CAL_EXIT=0，8,797 交易日，covers_reference=true（本轮未重抓，接受旧日历）
安全边界静态扫描（src/main/**/*.py + config/*.yaml，正则含 broker/place_order/trade_api/券商/自动下单/password/secret/api_key）→ 命中 0 行
.env 不存在（test -e → 1）且被忽略（git check-ignore EXIT=0）；data/live 目录未创建且被忽略（git check-ignore data/live/ EXIT=0，规则 .gitignore:227）
git diff --check → EXIT=0
```

---

签字与日期：______
