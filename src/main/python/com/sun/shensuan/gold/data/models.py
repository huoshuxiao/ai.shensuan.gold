"""数据层共享模型与常量。

职责：定义抓取（`FetchResult`）、规范化（`NormalizeResult`）、质量评估（`DatasetQuality`）
与抓取编排（`IngestOutcome`）四类结果对象，以及质量/节点两个枚举和 PIT 列清单；供
adapters、normalize、store、pipeline、ingest 之间传递结构化结果。本模块不做计算、不做 I/O。

关键不变量：
- 质量只有 `GREEN/YELLOW/RED` 三档；`RED` 表示禁止加仓，只允许减仓或空仓建议。
- 节点状态是七态机 `PENDING/RUNNING/OK/FAILED/TIMEOUT/SKIPPED/DEGRADED`，进入终态后
  必须落盘；`PENDING` 被跳过时由 `SKIPPED` 表达，不得删节点。
- `PIT_COLUMNS` 的顺序就是规范化表尾部列顺序（见 `pit.apply_pit()` 的列重排）。
- 各 `as_dict()` 的键名是 `_quality.json`、链路报告与后续 UI 的对外契约；只允许追加键，
  不允许改名，否则既有报告与看板会静默读空。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd


class Quality(str, Enum):
    """数据集/节点级质量三档；继承 `str` 以便直接写进 JSON/CSV 而不做额外转换。

    规范化表的**行级** `quality` 列不是本枚举：它只有 `GREEN`/`DEGRADED`
    两值（见 `normalize.normalize_frame` 与 AGENTS §6.5），用于标记该行是否
    带行级降级标记；数据集三档用本枚举/`DatasetQuality.status`。

    - `GREEN`：核心列齐全、单位/时区/新鲜度正常，允许进入 LLM 与加仓链路。
    - `YELLOW`：非核心缺失、代理、轻微过期或发生过坏行剔除；允许 LLM，但决策卡必须显示。
    - `RED`：核心序列缺失/单位异常/最近坏点无法证明只是历史脏数据；禁止加仓。

    缺失策略（AGENTS §7.4）：`MISSING/DEGRADED/PROXY/REVISED_ONLY/MANUAL/STALE` 都
    比 `GREEN` 软，必须以 `flags` 或行级 `quality` 显式留下痕迹，不得静默当正常值。
    """

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class NodeStatus(str, Enum):
    """运行链路节点七态机；每个节点都必须落在一个终态上。

    取值直接写进 `data/decisions/<date>/pipeline_status.json` 并被链路页读取，
    属于外部契约：可以新增状态，不得改名或换语义。

    - `PENDING`：尚未开始，链路页显示上游依赖。
    - `RUNNING`：正在执行；超过阈值仍未结束会被 `load_status()` 改判 `TIMEOUT`。
    - `OK`：成功（可能带产物哈希）。
    - `FAILED`：失败且不再重试。
    - `TIMEOUT`：超时；包含进程崩溃后残留 `RUNNING` 的超期改判（`STALE_RUNNING`）。
    - `SKIPPED`：因上游失败/预算不足跳过，不是成功也不是失败。
    - `DEGRADED`：执行成功但数据质量降级，需要展示原因。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    OK = "OK"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    DEGRADED = "DEGRADED"


# PIT 字段清单与追加顺序：`pit.apply_pit()` 用它把规范化表重排为
# “业务列 + 这 8 列”。前 6 列回答“这条数据何时、从哪、在哪一批可被谁看到”，
# 后 2 列回答“能不能用、为什么不能用”。顺序变化会改变 CSV 列序与哈希。
PIT_COLUMNS = [
    "source",
    "as_of",
    "available_at",
    "fetched_at",
    "ingest_run_id",
    "vintage",
    "quality",
    "flags",
]


@dataclass
class FetchResult:
    """适配器抓取结果：原始表 + 溯源信息 + 可选的原始字节快照。

    `store.RawStore.save()` 用它落 append-only 原始层；`normalize.normalize_frame()`
    用它的 `fetched_at` 计算/填充 PIT 的 `available_at`。

    Attributes:
        frame: 适配器输出的原始表；列名须覆盖 `DatasetConfig.required_columns`，
            本层不做类型与单位校验（校验在 normalize 层）。
        source: 数据源标识（如 `akshare.fund_etf_hist_sina`），写入 PIT 的 `source` 列。
        fetched_at: 本次抓取时间，必须带时区；同时写入 PIT 的 `fetched_at`。
        status: 抓取节点状态，默认 `OK`；降级源可标 `DEGRADED`。
        notes: 透传到报告的中文说明（限流、末行零持仓等），不得写密钥或账号。
        raw_bytes: 需要原样留档的响应体（如 GPR 的 `.xls`）；为 None 时按 `frame` 转 CSV。
        raw_suffix: 原始快照后缀，默认 `.csv`；`save()` 会补前导点。
    """

    frame: pd.DataFrame
    source: str
    fetched_at: datetime
    status: NodeStatus = NodeStatus.OK
    notes: list[str] = field(default_factory=list)
    raw_bytes: bytes | None = None
    raw_suffix: str = ".csv"


@dataclass
class NormalizeResult:
    """规范化结果：主表 + 数据集级质量 + 被剔除行留档。

    质量是两层的：`status` 是数据集级判定，由“是否发生过剔除/零值/修订数据”决定；
    行级 `frame["quality"]` 由 `normalize_frame()` 单独写入。两者不可互相替代——
    数据集 `YELLOW` 不代表每一行都坏，行级 `DEGRADED` 也不等于整个数据集 `RED`。

    Attributes:
        frame: 规范化后的主表：业务列 + PIT 尾部 8 列，已按 `as_of` 升序、已按主键去重。
        status: 三档质量；`RED` 表示本次结果不可用于加仓。
        flags: 数据集级标记（`OHLC_OR_PRICE_INVALID`/`ZERO_VOLUME`/`REVISED_ONLY` 等），
            会被 `assess_dataset()` 与 `_quality.json` 继续沿用。
        dropped_rows: 本次剔除行数，应与 `rejected` 行数一致（空源时为 0）。
        notes: 中文说明，供 CLI/报告直接展示，不得含密钥。
        rejected: 被剔除行原值 + `reject_reason`；由 `NormalizedStore.write_rejected()`
            单独留档供审计，不进入主表。
    """

    frame: pd.DataFrame
    status: Quality
    flags: list[str]
    dropped_rows: int
    notes: list[str] = field(default_factory=list)
    rejected: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class DatasetQuality:
    """单个数据集的可见质量快照；`as_dict()` 直接写入 `_quality.json` 的 `datasets` 节点。

    判定口径（细节见 `normalize.assess_dataset()`）：
    - 最近 5 行内出现非缺失类坏点 → `RED`（无法证明只是历史脏数据）；
    - `as_of_max` 距参考日超过 `stale_days` → `YELLOW` + `STALE`；
    - 最近 5 行内有因主值为空被剔除的行 → `YELLOW` + `RECENT_MISSING_ROWS`；
    - 最近 5 行带 `REVISED_ONLY`/`NAN_IN_REQUIRED`/`ZERO_*`/`MISSING_*` → `YELLOW`；
    - 其余 `GREEN`。历史坏行保留在 `flags`/`rejected_rows` 中可见，但不会把当前
      可用序列永久判黄——否则一次三年前的脏数据会让数据集永远无法加仓。

    Attributes:
        dataset: 数据集标识，取 `DatasetConfig.source`（不是配置里的 dataset key）。
        status: `GREEN/YELLOW/RED`。
        flags: 全量（含历史）标记集合，已排序。
        rows: 主表行数（不含被剔除行）。
        as_of_min / as_of_max: 主表 `as_of` 的 ISO 日期边界；空表为 None。
        invalid_rows: 调用方传入的剔除行数（本次运行或累计由调用方决定），本类不重算。
        zero_rows: 带 `ZERO_*` 标记的行次数；按标记逐个计数，同一行两种零值会计两次。
        stale_days: 参考日与 `as_of_max` 的自然日差；空表为 None。
        weekday_gaps: `price/series` 在两端日期之间缺失的应交易日数；其它 kind 恒为 0。
        weekday_gap_sample: 缺口日期样例（最多 10 个），用于报告定位。
        continuity_basis: 缺口分母口径标签，如 `cn_sse(8797 日, …)` 或
            `weekday_approx`；`weekday_approx(cn_sse 日历未落盘)` 说明声明了官方
            口径但回落到了近似，必须让读数自己说清（AGENTS §6.6）。
        rejected_rows: 剔除留档总行数；无留档时退化为 `invalid_rows`。
        recent_invalid_rows: 最近 5 行日期范围内被剔除的非缺失类坏点数。
        recent_missing_rows: 同一范围内的缺失类坏点数（`reject_reason=NAN_IN_REQUIRED`）。
        recent_flags: 最近 5 行出现的标记集合，已排序。
    """

    dataset: str
    status: Quality
    flags: list[str]
    rows: int
    as_of_min: str | None
    as_of_max: str | None
    invalid_rows: int
    zero_rows: int
    stale_days: int | None
    weekday_gaps: int
    weekday_gap_sample: list[str]
    continuity_basis: str = "weekday_approx"
    rejected_rows: int = 0
    recent_invalid_rows: int = 0
    recent_missing_rows: int = 0
    recent_flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好字典，键名固定并直接进入 `_quality.json`。

        `status` 取枚举 `value`（字符串），其余字段原样透传；本方法只换壳、不做裁剪，
        因此新增字段必须同批出现在这里，否则报告会静默漏字段。

        Returns:
            `{dataset, status, flags, rows, as_of_min, as_of_max, invalid_rows,
            rejected_rows, recent_invalid_rows, recent_missing_rows, zero_rows,
            recent_flags, stale_days, weekday_gaps, weekday_gap_sample,
            continuity_basis}`。
        """
        return {
            "dataset": self.dataset,
            "status": self.status.value,
            "flags": self.flags,
            "rows": self.rows,
            "as_of_min": self.as_of_min,
            "as_of_max": self.as_of_max,
            "invalid_rows": self.invalid_rows,
            "rejected_rows": self.rejected_rows,
            "recent_invalid_rows": self.recent_invalid_rows,
            "recent_missing_rows": self.recent_missing_rows,
            "zero_rows": self.zero_rows,
            "recent_flags": self.recent_flags,
            "stale_days": self.stale_days,
            "weekday_gaps": self.weekday_gaps,
            "weekday_gap_sample": self.weekday_gap_sample,
            "continuity_basis": self.continuity_basis,
        }


@dataclass
class IngestOutcome:
    """单个数据集一次抓取编排的结果汇总。

    两个行数不可混用：`rows` 是本次规范化后进入主表的行数（增量视图），`total_rows`
    是合并后规范化文件的总行数（含历史）；`new_rows` 是主键首次出现的行数。

    Attributes:
        dataset: 配置中的 dataset key（如 `etf_518880`）；与 `DatasetQuality.dataset`
            的 `source` 口径不同，报告中两者并存，禁止互换。
        status: 节点状态；`DEGRADED` 表示抓取成功但质量为 YELLOW/RED。
        quality: 三档质量，来自 `assess_dataset()`。
        rows: 本次规范化后主表行数。
        total_rows: 合并后规范化文件总行数。
        new_rows: 主键首次出现的行数（不含覆盖更新）。
        notes: 抓取 + 规范化 + 质量标记的合并说明。
        normalized_path / raw_path: 产物路径；抓取失败时为 None。
        error_code: 失败/降级错误码（`SOURCE_TIMEOUT`、`QUALITY_YELLOW` 等）。
        quality_detail: `DatasetQuality.as_dict()`，供 `_quality.json` 直接复用。
    """

    dataset: str
    status: NodeStatus
    quality: Quality
    rows: int
    total_rows: int
    new_rows: int
    notes: list[str]
    normalized_path: str | None
    raw_path: str | None
    error_code: str | None = None
    quality_detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好字典，供 `_quality.json` 的 `datasets` 节点直接消费。

        `status`/`quality` 都取枚举 `value`；路径字段保持字符串（调用方已转 str），
        空值写 `null` 而不是空串，便于上游区分“没有产物”和“产物路径为空”。

        Returns:
            `{dataset, status, quality, rows, total_rows, new_rows, notes,
            normalized_path, raw_path, error_code, quality_detail}`。
        """
        return {
            "dataset": self.dataset,
            "status": self.status.value,
            "quality": self.quality.value,
            "rows": self.rows,
            "total_rows": self.total_rows,
            "new_rows": self.new_rows,
            "notes": self.notes,
            "normalized_path": self.normalized_path,
            "raw_path": self.raw_path,
            "error_code": self.error_code,
            "quality_detail": self.quality_detail,
        }
