# P1 验收卡（第 5 版：评审修复后重跑 · 数据接到 2026-11-09 · Q3/Q4 双闸通过）

> 判据来源：AGENTS.md §18.6（2026-10-23 用户裁定「数据问题由程序标准化验收，不靠人眼」）。
> 本卡取代第 4 版（`acceptance.md`，10-24 落盘、HEAD 中已提交，保留作旧卡）：旧卡证据作废理由——①配置改了（单模型裁决 + C4 汇率量级闸 + Q4 新判据），`config_hash` 由 `9e87c4e8…` 变为 `0946643d…`；②数据重跑接到 2026-11-09（§18.1 纪律 1、§18.5 重开验收）。第 4 版逐数据集明细表由本次 accept 输出 JSON 取代，不沿用旧读数。
> 本版相对第 4 版的变化：T-035 设计裁决（丙，单模型 qwen2.5:7b）；T-036 gitignore 修复与源码补交（克隆可跑）；T-037 尺子修复（C1/C2/C4/F2/F3/F6，新增 Q4）；全量 ingest 重跑。

阶段：P1 数据层
结论：**程序判 `verdict=PASS`**（159 条判据 = 142 PASS / 0 FAIL / 17 SKIPPED，15 项），机器闸已过；**用户 2026-11-09 已签收 P1 通过（甲）**，里程碑转 `DONE`。第 7 节签字项 2（4 事件 severity 复核）与 3（R-006 五偏差确认）仍待办，不阻塞 P1，转入 P2 前置。
对应版本：
- 代码：HEAD `4df5b71`（分支 `feature/v1`），AGENTS v1.3；工作区干净
- 配置：`config_hash = sha256:0946643de3d6dbdbde742e1735f81df52e7b58cd73542f4a7ee8f9c333f4cd06`（accept 输出与 `_quality.json` 两处同值）
- 数据：accept 输出 `quality_run_id = p1-t037-20261109`、`quality_scope = full`、`reference_date = 2026-11-09`；`_quality.json` 的 `generated_at = 2026-11-09T22:27:41.270743+08:00`、`global_status = OK`、`red = []`、`core_not_green = []`

---

## 1. 机器闸：一条命令判阶段

```
命令：/usr/bin/python3.10 src/main/python/com/sun/shensuan/gold/cli.py accept --stage P1
退出码：ACCEPT_EXIT=0（0=PASS，1=FAIL，2=参数/配置/质量报告缺失）
读数：verdict=PASS，counts={"FAIL":0,"PASS":142,"SKIPPED":17}，reference_date=2026-11-09
阈值（config/application.yaml 的 acceptance 段）：min_weekday_coverage=0.8、max_stale_days=5、weekly_rows_per_year=[40,60]
日历（accept 输出 calendar 节点）：cn_sse integrity=OK，8,797 个交易日，1990-12-19..2026-12-31，
      fetched_at=2026-10-23T22:13:55+08:00，source=akshare.tool_trade_date_hist_sina；
      weekday 口径 integrity=APPROX（美国/外汇源用自然工作日近似）
Q4（本版新增）：PASS，批次基准日 2026-11-09 距验收日 2026-11-09 0 天 ≤ 5
```

15 项判据分组小计（159 = 142+0+17）：一致性 112 PASS / 0 FAIL / 12 SKIPPED；连续性 26 PASS / 0 FAIL / 5 SKIPPED；质量（Q1/Q2/Q3/Q4）4 PASS。
17 条 SKIPPED 都是「输入确实不存在」：C4 非价格源未声明区间、C6 快讯无数值列、N1 news×3+events×1、N2 events×1、S1 gpr 原始件 .xls——不计入通过。

配套机器证据：
- 全量离线测试：`/usr/bin/python3.10 -m pytest` → `224 passed, 16 deselected in 24.67s`，EXIT=0。
- 克隆完整性闸（T-036）：`git archive HEAD` 解到临时目录跑 pytest → `215 passed, 16 deselected`，EXIT=0（当时 HEAD ff05d24；本批又 +9 测试，磁盘 224）。
- 入库：`cli.py ingest --run-id p1-t037-20261109` → INGEST_EXIT=0，scope=full，global_status=OK。

## 2. 负对照（尺子有牙的证据）

- Q4 墙钟闸：修前现跑抓住「批次 2026-10-23 距 2026-11-09 已 17 天 > 5」判红；重跑入库后转 PASS（0 天）。
- C1 导入闸：负对照 5 种真实测试包导入形状全部被抓、4 种正常导入放行（改前坏正则全漏）。
- F6 事件 severity 冲突：同 id 先 3 后 1 → 改前静默留 1 判 GREEN（红），改后整组剔除留档判 RED。
- C4 汇率量级闸：两条汇率源 1424+8071 行落在 [5,10]；未除 100 的 600–870 会被抓（SKIPPED 19→17）。

## 3. 数据问题自动诊断（doctor，与 accept 分工不同）

```
命令：cli.py doctor --write → DOCTOR_EXIT=1，overall=NEEDS_HUMAN
counts={"AUTO_RESOLVED":2,"CONTAINED":8,"NEEDS_HUMAN":1}
config_hash_match=True；report_hash=sha256:3789187e90ffe993c81a2a78481d9d9eb13ceb9ad84b42faa14107f57feab03e
blocked_datasets=[comex_gc, geo_events, gpr, spdr_gold, us10y, wti_cl]（6 个）
review_queue=4（4 条 XGE- 抽取事件 severity 待你复核）
```

`doctor=NEEDS_HUMAN` 不改 `accept=PASS`（§18.6 分工）：accept 判「数据满足 P1 基线」，doctor 判「已知缺陷如何安全处置」。下游能力封锁清单（P2 必读 `_remediation.json`）：
- comex_gc/wti_cl：零成交量/零持仓系统性缺陷 → `price_only`，成交量/持仓量禁读。
- spdr_gold：覆盖率 50.6%（LOW_COVERAGE）→ 只展示，`FLOW_SPDR_CHG_1W/4W` 禁入分数。
- gpr：`REVISED_ONLY`（无 vintage）→ 禁 H-GEO 晋升。
- geo_events：4 条事件 severity 未复核 → `GEO_EVENT_SEVERITY` 禁入分数与加仓依据。
- us10y：能力以 `_remediation.json` 现值为准（本次入库后已新鲜）。

## 4. 用户已裁定的口径（沿用，不再问）

- D1/D3/D4 乙（2026-10-24）：cftc 全 0 行剔除（主表 1,938 行）、au0 settlement≤0 剔除（覆盖 2010-06-08 起）、spdr 低覆盖披露放行（只展示不进分数）。
- 设计取舍丙（2026-11-09，T-035）：AGENTS 单一事实源、单模型 qwen2.5:7b、每日决策、中性多维动作（「可空仓」落卫星/风控层，核心 70% 政策不变）。
- 家乐福误报排除（2026-10-24）：3 条同源快讯按 content_hash 排除，geo_events 4 行。

## 5. 非专业四问（§18.2）

| # | 问题 | 本卡证据 | 答 |
|---|---|---|---|
| 1 | 数字有来源吗 | C1/C2/S1：每行带 source/as_of/available_at；S1 用原始快照重放对表 | 能 |
| 2 | 缺口显眼吗 | C1/C6/C7/N1–N3/Q1/Q4 + 第 3 节封锁清单；零成交/低覆盖/修订均列出 | 能 |
| 3 | 能确认不越权吗 | 全仓静态扫描无券商 API/下单路径；只人工清单；C1 导入闸有牙 | 能 |
| 4 | 结论能作废吗 | 第 6 节失效条件均可观察（config_hash 变即作废、日历失效回落、severity 未复核禁加仓） | 能 |

## 6. 结论的失效条件

- `config_hash` 改变（改判据/阈值/模型/数据源声明）→ 本卡作废，须重跑 ingest + accept。
- 批次基准日距验收日 > `max_stale_days`（Q4）→ 数据陈旧，须重跑。
- 官方日历 sha256 不符或缺失（F3 修后缺 sha 也判不可用）→ 回落工作日近似并披露。
- 事件 severity 未经你复核 → `GEO_EVENT_SEVERITY` 不得进分数或作加仓依据。
- GPR 无 vintage → 不得用于 H-GEO 晋升。
- COMEX/WTI 全量行 volume/open_interest=0 → 只当收盘价，不当真实流动性。
- AU0 覆盖只到 2010-06-08：2008–2010 段因 settlement=0 已整行剔除，不能拿那段做结算价特征。

## 7. 签字项（仅业务/口径选择，不是数据目视）

1. **P1 阶段签收**：机器闸 PASS + 四问全「能」，是否签 P1 通过？**已签：用户甲（通过），2026-11-09**
2. **4 条抽取事件 severity 复核**（review_queue=4）：复核前地缘事件字段封锁；是否逐条复核？______
3. **五项设计偏差确认**（看板 R-006，含数据集 RED 窗口、comex 非核心、非核心 RED 压全局、空源顶 RED、wti 价格下界）：确认或要求改回？______

## 8. 未查（不得折成通过）

- 网络冒烟 16 条（`-m network`）本轮未跑。
- 4 条事件 severity 与地区未经你复核（属人工复核范围）。
- 快讯窗口短（SHMET 86 天量级），5 年事件史无法回补；GPR 历史无 vintage。
- 美国/外汇官方日历无来源，N1 只是近似口径。
- GPR 的 S1 重放（原始件 .xls）未覆盖；C4 对非价格源未声明区间者仍 SKIPPED。
- 备份恢复演练、账本链、回测：属 P4/P7/P3 闸——未做。

签字与日期：用户（P1 签收：甲，通过），2026-11-09。事件 severity 复核（项2）与 R-006 偏差确认（项3）仍待办。