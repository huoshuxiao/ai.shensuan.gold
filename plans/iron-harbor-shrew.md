# 黄金量化研调与投资决策系统 — AGENTS.md 设计计划

## Context

仓库 `/home/sunwenkun/Developer/agent-workspace/ai.shensuan.gold.git` 目前没有实现代码，只有 `README.md`、`PROJECT.md`、`.gitignore` 以及 `docs/`、`plans/` 下的设计资料。用户要求先参照 README 中的 8 个 GitHub 参考项目，输出系统设计并生成根目录 `AGENTS.md`；本轮**只交付这一份设计文档**，不写业务代码、不建数据目录、不改 README/PROJECT。

目标系统：跟踪上期所黄金 AU、上海金 SGE、COMEX 黄金 GC、境内外黄金 ETF，输出可审计的研报、仓位建议与人工下单清单。用户明确表示缺乏金融专业知识、人工审查逻辑困难，因此“每个数字可追溯、每条规则可复核、LLM 不得编数”是硬要求。

## 已确认的用户决策

1. **投资风格：核心+卫星**。长期核心仓吃金价 beta，卫星仓 0–30% 做战术择时；日频评估、周频调仓。
2. **交易边界：“1+2”**。研究 + 纸面跟踪 + **人工下单清单**；绝不接券商 API、绝不自动下单、绝不保存交易凭证。用户手工执行并回录成交。
3. **AI 深度：完整多智能体每日辩论**。
4. **AI 权限：带内选择**。确定性规则先算出卫星仓区间 `[L,H]`，9 个角色辩论后在区间内选目标；风控经理一票否决；LLM 不得越出区间。

## 关键事实（本轮现读/实测，必须写进文档并标注日期）

### 环境
- `/usr/bin/python3.10` = Python 3.10.17；akshare 1.18.81，pandas/numpy/streamlit/fastapi/pytest/matplotlib/statsmodels/scipy/sklearn/plotly/sqlalchemy/pydantic/dotenv/requests 已装；duckdb、apscheduler 未装。
- Docker 26.1.3 可用；`docker compose` 子命令不存在，`docker-compose` 也未安装 ⇒ 设计用 `Dockerfile + docker run`，compose 仅作未来可选。
- Ollama 正在运行，已有 `qwen2.5:7b`（4.7GB Q4）、`qwen3.5:9b`（6.59GB Q4）、`deepseek-r1:8b`（5.23GB Q4）、`qwen3.5:4b`（3.4GB Q4）。15Gi 内存，检查时约 10.0Gi 可用；GTX 965M 2GB。单机只允许一个本地 LLM 实例。
- `~/miniconda3`；RD-Agent 在 `~/miniconda3/envs/rdagent`。conda 路径做成可配置项。
- Git 远端 `https://github.com/huoshuxiao/ai.shensuan.gold.git`，分支 `feature/v1`；用户自己 push，系统/Agent 不执行 push。

### 数据接口（2026-10-18／19 探针，行情数据多到 2026-10-16，GPR 至 2026-10-13）
| 数据 | 接口 | 实测 | 口径/风险 |
|---|---|---|---|
| 沪金主力连续 AU0 | `ak.futures_main_sina("AU0", start_date, end_date)` | 2026-01-01 起探针返回 188 行，至 10-16 | **不是官方 AU 指数**；全历史长度待 P1 实测；合约换月有跳空 |
| 上海金 Au99.99 | `ak.spot_hist_sge("Au99.99")` | 2382 行 | CNY/g；现货基准 |
| SGE 报告 | `ak.macro_china_au_report()` | 38267 行 | 含 Au(T+D)/Ag(T+D)、成交量、持仓量 |
| COMEX GC | `ak.futures_foreign_hist("GC")` | 2588 行 | 最后一行 volume/position=0；实时接口持仓量也为 0 ⇒ 只能当收盘价用，标 degraded |
| 518880 | `ak.fund_etf_hist_sina("sh518880")` | 3212 行 | 场内主代理；开盘/收盘/量/额 |
| 159934 | `ak.fund_etf_hist_sina("sz159934")` | 3119 行 | 备用 ETF |
| CFTC 黄金持仓 | `ak.macro_usa_cftc_c_holding()` / `macro_usa_cftc_merchant_goods_holding()` | 周频至 2026-10-13，含黄金多/空/净仓 | 非商业=投机、商业=套保；发布时间滞后，PIT 要按发布日 |
| SPDR 黄金 ETF 持仓 | `ak.macro_cons_gold()` | 日频至 2026-10-16 | 总库存（吨）/增减/总价值；流量指标 |
| 美债 10Y 名义 | `ak.bond_zh_us_rate(start_date)` | 至 2026-10-16 | **无 TIPS 实际利率**；需手工 CSV 或代理 |
| USD/CNY | `ak.currency_boc_sina("美元", start, end)` | 至 2026-10-17 | 数值是“每 100 外币”的人民币价，必须 /100；固定用央行中间价或中行折算价之一 |
| DXY | 无直接可用接口（东财不稳定） | — | 用 BOC 六币种交叉汇率按 ICE 公式构造代理，标注 proxy |
| 地缘政治风险指数 GPR | `pandas.read_excel("https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls")`；备用为官网 CSV 手工导入 | 15,261 行，1985-01-01 至 2026-10-13；日期列用 `date`（`GPRD` 是指数值，不是日期）；字段 `GPRD/GPRD_ACT/GPRD_THREAT/GPRD_MA7/GPRD_MA30`；读 `.xls` 需 `xlrd`（本机已装） | 日频；**有修订与发布滞后**，`available_at` 以实际抓取快照为准；历史回测只认当时已存在的 vintage，无 vintage 的历史段标 `REVISED_ONLY`，不得用于晋升；衡量报纸报道强度，不是金价因果 |
| GDELT 2.0 DOC API | `https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=artlist&maxrecords=250&format=json` | 探针返回 429（限流） | 仅可选增强；必须退避 + 缓存；限流/失败即降级，不作核心依赖 |
| 财经快讯 | `ak.stock_info_global_cls("全部")` / `ak.stock_info_global_sina()` / `ak.futures_news_shmet("贵金属")`（或 `"全部"`） | 三源均 OK；每源约 20–2000 条，带发布时间 | 原文只做**确定性关键词/事件抽取**；v1 不把原文喂给 LLM；去重、时限、来源留痕 |
| WTI 原油 | `ak.futures_foreign_hist("CL")` / `ak.futures_foreign_commodity_realtime("CL")` | 7,688 行至 2026-10-16；最后一行 volume/position=0，实时持仓量=0 | 只当收盘价与地缘风险确认；标 degraded，不把零持仓当真实 |
| 地缘事件日历 | `data/manual/geo_events.csv`（手工维护） | — | 战争/制裁/大选/OPEC/央行会议等；字段 `event_id/date/type/severity/region/source_url/available_at` |
| 黄金已实现波动 | 由 GC/AU0/518880 收盘价计算 | — | 不依赖 VIX；作为“地缘冲击是否已被市场定价”的确认量 |
| 手工导入 | `data/manual/*.csv` | — | TIPS、官方 AU 指数、GLD/IAU 持仓、GPR 备用档案、地缘事件日历、新闻档案 |

> 东财系接口在本机多次连接失败；Sina/SGE/BOC 可用。设计必须以“主源+降级+手工导入”三层处理，禁止把缺失值静默填 0。

> **地缘政治进入模型分三层**（地缘政治/快讯/油价各行于 2026-10-19 探针；GPR 数据至 2026-10-13）：① GPR 定量层（水平、z 分、5 日变化、`ACT/THREAT` 拆分）；② 事件严重度层（快讯与事件日历的结构化标签，不读原文；severity 1=局部事件、2=区域冲突/制裁、3=全球性战争/大国直接冲突，必须人工复核）；③ 市场确认层（黄金已实现波动、WTI、USD/CNY、美债 10Y）。三层都进入事实包，但**不默认“地缘冲突=金价上涨”**；任何“地缘→加仓”规则必须先预注册、后样本外验证。`geo_score` 的权重、z 窗口、阈值全部进 `strategy.yaml`，代码不写魔法数。新闻原文在 v1 只做确定性关键词/事件抽取，不直接进 LLM，避免提示注入与不可复现。

## AGENTS.md 内容大纲（目标约 1000–1400 行，中文，表格优先）

**卷首**：`设计蓝图 v0.1（仓库尚无实现代码）` 状态标记；15 行“Agent 快速契约”与阅读顺序；优先级：用户指令 > AGENTS.md > README/PROJECT > 工具默认。

- **§0 文档治理**：状态/版本/修订规则；“规范 / 现状 / TODO” 标签；说明 README 的 `PANDING` 规范化为 `PENDING`（保留原词备注，不静默改）。
- **§1 定位与边界**：目标、成功标准；“核心+卫星”中“金边组合（gold sleeve）”的定义；1+2 交易边界；不构成投资建议。
- **§2 设计原则**：确定性内核；LLM 只做事实约束的论证；PIT 时点纪律；失败关闭/降级；可复现哈希；对非专业用户可解释；缺口不静默填。
- **§3 参考项目映射**：8 个 GitHub 项目逐条列“借鉴/不照搬”。借鉴 TradingAgents 的辩论形状、AI Hedge Fund 的哈希台账/PIT、Alpha Desk 的合规门禁/公开预测台账、TradingAgents-astock 的 T+1/手数、a-share-agents 的多源降级、aiagents-stock 的 Streamlit/Docker；不照搬 LangGraph、MongoDB/Redis/Vue、19 个投资大师人格、并发 LLM、自动交易。
- **§4 系统架构**：数据 → 事实包（fact pack）→ 确定性规则 → 多智能体委员会（带内）→ 风控 → 决策卡/纸面台账/UI；进程模型 CLI + Streamlit + cron，无数据库、无端口服务；四种模式 `LLM_IN_BAND / DETERMINISTIC / DEGRADED / SHADOW`；每次决策记录 config/data/model 哈希。
- **§5 资产、基准与口径**：AU0、SGE Au99.99、COMEX GC、518880（主）、159934；官方 AU 指数用适配器占位；基准 B0 现金、B1 100% 518880、B2 静态 70% 518880 + 30% 现金（月度再平衡）、B3 确定性规则、B4 LLM 叠加（仅前瞻）；CNY/g 与 USD/oz 换算（1 oz=31.1034768 g）；黄金ETF支持T+0、100 份整手、费用。
- **§6 数据层**：上面的实测源表（含 GPR/快讯/油价/事件日历）；原始层/规范化层/手工层；`available_at` 与 PIT 过滤；质量闸（重复、跳空、零量、过期、单位、时区、GPR 修订滞后、GDELT 限流、快讯空行）；降级规则；COMEX 零持仓、AU0 非官方指数、TIPS/DXY 代理、GPR 滞后、新闻原文未入模型必须显示在 UI/报告。
- **§7 特征字典**：`fact_id/名称/分组/公式/窗口/单位/方向/来源/as_of/缺失策略`；趋势、动量、波动率、跨市场价差、持仓/ETF 流量、宏观/汇率、**地缘政治/避险**七组；地缘组至少含 `geo_gpr_level`、`geo_gpr_z_1y`、`geo_gpr_chg_5d`、`geo_gpr_act`、`geo_gpr_threat`、`geo_event_severity`、`geo_news_count`、`geo_market_stress`、`geo_score`；所有 z-score 窗口与阈值进配置，不在代码写魔法数。
- **§8 状态、信号与逆向叠加**：MA200/MA60 斜率、波动率分位、实际利率/美元代理做 regime（带滞回）；增加 `GEO_SHOCK`（GPR z≥1.5 且黄金波动分位≥80%）与 `GEO_CALM`（GPR z≤0）；分数 −100..+100 映射卫星仓区间；**逆向叠加**用 COT 分位、SPDR 4 周流量、RSI、内外价差极值、地缘情绪极值，需 2 个信号 + 价格确认，最大 ±10pp；v1 默认只读/只减不加，样本外通过后才允许加仓。地缘政治预注册两条假设：H-GEO-1“冲击后 1–5 日避险动量”、H-GEO-2“冲击峰值 + RSI 超买后的反向衰减”；未过门槛前只展示、只允许风险下降方向。
- **§9 组合与风控**：金边组合内 `core + satellite + cash = 100%`；核心目标 70%（战略仓，允许漂移到 60–80% 后季度再平衡；核心超过 70% 时卫星上限相应压缩），卫星 0–30%，现金为剩余且不得为负；12% 年化波动目标；回撤 10%/15% 分级降险；周度最大变动 10pp、3pp 噪声带、卫星月换手 ≤40%；95/99% VaR/CVaR、压力情景、ADV 5% 上限；只做多 ETF、默认不用杠杆/不做空；硬闸失败不许加仓。
- **§10 决策卡与研报**：一句话结论；动作/目标/变动；3 条证据（数值、日期、`fact_id`）；最强反方观点；可证伪的失效条件；我该做什么/不该做什么；不行动选项；数据质量横幅；模式徽章；决策哈希与模型元数据；大白话风险提示。
- **§11 多智能体委员会（本次重点），可以用时间换结论**：**9 角色** = 宏观利率、**地缘政治/避险**、跨市场价差、流量/持仓、技术/状态、多方研究员、空方研究员、风控经理、组合经理；2 轮；单实例顺序调用 `qwen3.5:9b`，`think=false`、`temperature=0.1`、`num_ctx=8192`、`num_gpu=0`、`num_thread=4`，第 1 轮 9 角色各 ≤50s、第 2 轮 4 角色各 ≤45s，总预算 **12min**（07:05–07:17，07:17 硬截止）；只读同一事实包，无联网、无工具；地缘角色只读 GPR/事件严重度/市场确认等结构化字段，**不读新闻原文**；JSON schema；聚合 `T_final = clamp(T_PM, L, H)`，风控否决→0，质量 RED→只减不加，解析/超时/幻觉→`T_det`；数字引用审计；冷启动预热与失败降级；B3/B4 平行纸面账本。若时间预算不足，可把地缘角色并入宏观角色，但事实包中的地缘字段不得删除。明确声明：LLM 文本不保证逐位可复现，但确定性事实包、规则与决策哈希可复现；每次保存模型 digest、提示词哈希、原始输出与耗时。
- **§12 回测与验证**：事件驱动日频；PIT；次日开盘成交；费用/滑点/T+1/整手（黄金 ETF 支持 T+0，但日频策略仍按次日开盘执行）；B0–B4；walk-forward + purge/embargo；CAGR/波动/Sharpe/Sortino/MaxDD/Calmar/换手/VaR/CVaR；PBO、Deflated Sharpe、参数敏感性；逆向叠加与地缘 H-GEO-1/H-GEO-2 的预注册门槛（≥5 年 walk-forward、较 B3 Sharpe 提升 ≥0.2、MaxDD 不恶化 >2pp、含成本）；GPR 历史从 1985 起，但当前下载文件是修订后序列：只认上线后逐日快照作为 vintage；没有 vintage 的历史段标 `REVISED_ONLY`，不得用于 H-GEO 晋升，保守滞后不能替代 vintage；LLM 无法历史回测，只能 8–12 周前瞻 shadow。
- **§13 纸面账本与人工成交**：append-only 哈希链 JSONL，事件 `DECISION/ORDER_INTENT/FILL/MARK/CORRECTION`；次日开盘纸面成交；人工清单字段（标的、方向、价格带、数量/手数、最大滑点、有效期、不行动替代）；实际成交写 `data/live/actual_fills.csv`（gitignored）并与纸面账本对账。
- **§14 Streamlit 界面**：今日决策、数据健康、**地缘政治与避险**、辩论与证据、组合/纸面台账、回测、规则追溯/What-if、词表/设置；除“录成交”和“手动触发”外只读；What-if 只重算确定性规则；缺口始终可见；绑定 127.0.0.1；无下单按钮。手动交易；交易数据手动录入系统。
- **§15 工程规范**：目录树；`/usr/bin/python3.10`；只改 requirements.txt，失败用国内源；pytest；`_bootstrap` + `sys.path` + 平铺导入；中文注释/docstring、英文标识符、类型注解；配置优先级 CLI > env > YAML > 默认；JSONL 日志；Dockerfile + `docker run`；密钥只进 `.env`；不提交数据缓存。
- **§16 敏捷看板、DoD 与自动提交**：`看板.md` 列 = ID/状态/优先级/任务/验收标准/证据/负责人/日期/关联提交/风险；状态 `TODO/PROCESS/DONE/ISSUE/PENDING/RISK`；WIP=1；DoR/DoD；自动提交只在 DONE 且测试+数据闸+密钥扫描通过后，按显式路径清单 `git add`，中文消息 `[gold][T-xxx] ...`；**只 commit 不 push**；日常决策产物由每周归档任务提交，避免每日噪声；写明与兄弟项目“未经要求不得 commit”的冲突及本 README 的显式授权。
- **§17 测试与验收**：默认离线、无网络/无 LLM/无真实时钟；fixtures 合成 OHLCV；数据契约（含 GPR 列/日期/滞后/修订、快讯去重与空行、GDELT 429 降级）；PIT 截断不变性；确定性哈希；回测会计恒等式；LLM 负向测试（编数、无引用、越界、否决、坏 JSON、超时、提示注入、**试图引用新闻原文**）；账本篡改检测；Streamlit AppTest；自动提交 dry-run；性能预算（pytest <120s、日链确定性部分 <3min、LLM 委员会 <12min、端到端热启动 <20min、UI <3s、峰值 <9.5GB）。
- **§18 分阶段路线图**：P0–P8（见下）。
- **§19 运行手册**：每日时间线、命令、cron、模型预热、故障恢复（含 GPR/快讯源不可达）、备份、日志轮转、Docker、事故清单。
- **§20 Qoder / opencode 规则**：先读顺序；任务协议；允许/禁止命令；必须停手询问的触发条件；证据格式；AGENTS.md 单一事实源；opencode 的 Ollama 配置只作指针，不复制两份规则。
- **§21 风险、合规与开放问题**：数据缺口、模型风险、过拟合、执行缺口、税务/法律、硬件、未决问题；**地缘政治专项**：GPR 修订/滞后、快讯噪声与重复、GDELT 限流、战争/制裁尾部风险、把“报道强度”误当“金价因果”、新闻原文提示注入、事件日历维护责任。
- **§22 术语表**：alpha/beta、核心/卫星、回撤、Sharpe/Sortino/Calmar、VaR/CVaR、PIT、walk-forward、purged CV、COT、基差/溢价、DXY、实际利率、ATR、RSI、regime、滞回、滑点、跟踪误差、哈希链、事实包、否决权、**GPR 与地缘风险三分层、GPRD_ACT/GPRD_THREAT**。

## 关键设计决定（写进文档）

1. **确定性内核 + LLM 带内选择**：所有数字、仓位区间、风控闸由 Python 规则计算；LLM 只接收事实包，输出结构化观点；最终卫星仓被 clamp 在 `[L,H]`，风控可降到 0；LLM 失败回退 `T_det`。这样既满足“完整多智能体每日辩论”，又能历史回测与追责。
2. **事实包与引用审计**：每个 `fact_id` 有来源、`as_of`、公式、缺失策略；LLM 任何数字必须引用存在的 `fact_id` 且值在容差内，否则整条观点作废；提示词内不放新闻原文，避免注入；v1 不联网。
3. **逆向格言可证伪**：`别人贪婪时恐惧` 作为观察维度全量展示，但变成加仓动作必须先过预注册样本外门槛；未通过前只允许“极端情绪 + 风控触发”的减仓，不允许加仓。
4. **ETF 优先、只做多、无杠杆**：人工清单默认 518880/159934，100 份整手；期货只做研究展示，不进入默认清单；不做空、不加杠杆。
5. **金边组合口径**：仓位建议是“用户自定 gold sleeve 的百分比”，不是总资产百分比；系统不知道用户总财富与税况，必须在决策卡上写明。
6. **数据缺口显性化**：TIPS、DXY、官方 AU 指数、GLD/IAU 流量、新闻若无源，显示 `DEGRADED/MISSING`，不许用 0 或 LLM 猜测替代。
7. **自动提交只服务于开发任务**：DONE 时自动 commit 开发产物；每日决策 JSON/报告按周归档提交；任何情况下不 push、不提交 `.env`、`data/live/`、原始缓存。
8. **地缘政治三分层、可证伪**：GPR 定量层 + 事件严重度层 + 市场确认层；新闻原文 v1 不进 LLM；地缘规则先预注册、后样本外验证；未通过前只读/只减不加；“战争→买金”的直觉不得写成硬规则。

## 未来目录与配置（本轮不创建，仅写入设计）

```
AGENTS.md  
README.md  
PROJECT.md  
看板.md  
CHANGELOG.md
requirements.txt  
pytest.ini  
Dockerfile  
.gitignore  
opencode.json(可选指针)
config/{application,data_sources,strategy,llm}.yaml
data/{raw,normalized,manual,decisions,paper,live}/
logs/  
temp/  
report/daily/YYYY-MM-DD/
src/main/python/com/sun/shensuan/gold/{_bootstrap,common,config,data,features,regime,strategy,risk,llm,decision,backtest,paper,report,ui,cli}/
src/test/python/com/sun/shensuan/gold/{conftest.py,fixtures,...}
```

关键配置：`strategy.yaml` 存所有阈值/仓位带/费用；`data_sources.yaml` 存源、参数、发布滞后、陈旧阈值、手工回退；`llm.yaml` 存模型、超时、角色、权重、`mode: influence|shadow|off`；配置哈希写入每次决策记录。

## 每日流水线（Asia/Shanghai）

1. 06:20 预检（磁盘/内存/锁/配置）；可用内存 <9.5GB 时跳过 LLM，走 `DETERMINISTIC`；可选预热 Ollama（`keep_alive=45m`）。
2. 06:30 顺序抓取（美股收盘、SPDR、中国前收盘、GPR、财经快讯、WTI 等），重试 2 次，写原始快照与 `fetched_at`。
3. 06:45 规范化/去重/质量闸（GREEN/YELLOW/RED），缺口显式标记；GPR 滞后、GDELT 限流、快讯空行/重复单独标记。
4. 07:00 截止，按 `available_at <= cutoff` 冻结事实包（含地缘三层字段），算 `fact_pack_hash`。
5. 07:05 确定性规则：regime（含 `GEO_SHOCK/GEO_CALM`）、分数、区间 `[L,H]`、风控闸、B3 目标。
6. 07:05–07:17 LLM 委员会：第 1 轮 9 角色（各 ≤50s），第 2 轮 4 角色（多方/空方/风控/组合经理，各 ≤45s），顺序调用，总预算 12min，07:17 硬截止；冷启动未就绪则跳过。
7. 07:17–07:22 引用审计、JSON 校验、聚合、clamp、风控否决；写最终目标与完整 trace（含地缘角色引用审计）。
8. 07:22–07:35 决策卡/研报、哈希链追加、纸面 order intent；不执行任何真实交易。
9. 08:00 人工复核窗口；09:30–14:30 用户按清单手工执行。
10. 15:30 回录成交；15:45 纸面/实际对账、算 NAV；失败开 `ISSUE` 并在 UI 顶栏报警。

失败行为：核心序列缺失 → `DEGRADED` 且不许加仓；LLM 失败 → `DETERMINISTIC` 卡并公示；08:00 前未出报告 → UI 醒目警告，绝不阻塞人工。

## 分阶段路线图（写进 AGENTS.md）

| 阶段 | 交付 | 验收闸 |
|---|---|---|
| **P0 本轮** | 仅根目录 `AGENTS.md` 设计蓝图 | 11 条 README 约束、8 个参考项目、4 项用户决策、地缘政治数据接口全覆盖；无代码/无数据目录改动 |
| P1 | 工程骨架、`看板.md`、requirements、pytest.ini、配置模板；数据适配器（含 GPR/快讯/油价/事件日历）、原始/规范化层、PIT、质量闸、手工 CSV | 5 年 518880/SGE/GC + AU0 全历史实测；GPR 至 2026-10-13 入库；缺口可见；离线测试绿 |
| P2 | 特征注册表（含地缘组）、事实包、regime（含 `GEO_SHOCK`）、仓位带、风控引擎、规则 trace | 黄金回放逐位一致；边界/无未来函数测试；带与否决不变量；地缘字段缺失时只读/只减 |
| P3 | 事件驱动回测、费用、B0–B4、walk-forward、指标报告 | 会计恒等式；基准可复现；样本外报告；逆向叠加与 H-GEO-1/2 启用/关闭结论 |
| P4 | 确定性日链、决策卡/研报、纸面账本、人工成交回录 | 20 日回放；账本零断链；卡片字段完整；缺口可见 |
| P5 | LLM 客户端、提示词、**9 角色** 2 轮委员会、引用审计、clamp/降级 | 5 个真实交易日；schema 或降级 100%；零越界；地缘角色零原文引用；端到端热启动 <20min；峰值 <9.5GB |
| P6 | Streamlit 全部页面、What-if、审计查看器、数据健康、成交录入 | AppTest 全页通过；只读行为；加载 <3s；无副作用 |
| P7 | cron、Dockerfile/`docker run`、运行手册、20 日无人值守纸面运行 | 20/20 次运行；恢复演练；仅本地绑定；备份可恢复 |
| P8 | 8–12 周评估、合规审计、术语表、v1.0 | 预注册指标达标；LLM/逆向叠加/地缘 go-no-go；用户签字；零真实订单 |

顺序不可颠倒：确定性内核与测试必须先于 LLM 和 UI。

## 本轮验证清单（只针对文档，不虚构测试）

- [ ] `AGENTS.md` 位于仓库根目录；本次实现只新增这一份交付文件，不触碰 README.md/PROJECT.md 及既有 `docs/`、`plans/` 的未提交改动。
- [ ] 11 条 README 约束逐条可定位（Python 路径、requirements、分步流程、opencode/qoder 规则、看板、自动 commit、conda、docker、测试、硬件、rdagent）。
- [ ] 8 个参考项目均有“借鉴/不照搬”结论。
- [ ] 4 项用户决策（核心+卫星、1+2、完整辩论、带内选择）均有明确条款。
- [ ] 所有数据读数带探针日期与接口名；AU0 明确写“非官方指数，2026 探针 188 行”；COMEX 零持仓、TIPS/DXY 缺口、`PANDING` 规范化均有显式说明。
- [ ] 地缘政治接口逐条有探针日期与接口名：GPR 15,261 行至 2026-10-13；GDELT 429 仅可选；CLS/Sina/SHMET 快讯可用；WTI 零持仓标 degraded；新闻原文 v1 不进 LLM。
- [ ] §11 为 9 角色且含地缘政治/避险；地缘规则未过样本外前只读/只减不加；H-GEO-1/H-GEO-2 已预注册。
- [ ] 文档明确标注“设计蓝图，尚无实现”，没有把规划写成现状；没有“测试已通过”等无证据表述。
- [ ] 无真实下单路径、无券商 API、无密钥；自动提交仅 commit 不 push，且有失败闸。
- [ ] 用 `grep`/`wc -l` 回读标题与关键条款，人工核对无自相矛盾（尤其仓位区间、百分比、时间线）。

## 风险与待定（写进 §21）

- TIPS 实际利率、DXY、官方 AU 指数无稳定直连；代理指标可能失真，必须标 degraded。
- 9b 本地模型冷启动可能十余分钟，中文金融推理质量未知；确定性回退是唯一安全底线。
- 逆向叠加可能样本外失效；默认只减不加，通过预注册门槛才启用加仓。
- 决策 07:35 与人工执行 09:30 之间存在跳空风险；价格带、最大滑点、不行动选项必须保留。
- 用户总资产、税况、风险承受力、金边组合规模未知；需要上线前问卷与“金边组合”定义。
- 数据许可/交易所条款、节假日日历维护、模型 digest 升级流程、纸面/实际对账容差、是否加入期货与更多新闻源，均列为开放问题。
- GPR 是报纸报道强度，不是金价因果；有修订与发布滞后，历史回测只认当时已存在的 vintage，无 vintage 的历史段标 `REVISED_ONLY` 且不得用于晋升；保守滞后不能替代 vintage。
- 地缘快讯噪声大、重复/空行多；GDELT 探针 429 限流；新闻原文 v1 不进 LLM，只做确定性关键词/事件抽取，抽取规则要有版本号与回归测试。
- 战争/制裁等尾部事件可能超出历史样本；地缘加仓规则必须预注册、限制最大卫星仓，不能拿单次事件做故事化交易。

## 交付物

- 新增：`/home/sunwenkun/Developer/agent-workspace/ai.shensuan.gold.git/AGENTS.md`
- 不修改：`README.md`、`PROJECT.md`、`.gitignore`；不创建代码、配置、数据或台账文件（留待 P1）。
