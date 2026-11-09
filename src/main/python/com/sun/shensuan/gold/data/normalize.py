"""规范化：列名、类型、单位、排序、逐行校验与 PIT 字段。

职责：把适配器输出的原始表按 `DatasetConfig.kind` 分成四路（price/series/news/events）
做确定性清洗——统一列名与数值类型、按主键去重、按 `as_of` 排序、剔除坏行、补 PIT 字段、
给出数据集级质量；`assess_dataset()` 再对合并后的全量序列评估新鲜度、工作日缺口与最近坏点。

关键不变量：
- 输出列 = 业务列 + PIT 尾部 8 列（`models.PIT_COLUMNS` 顺序）。price/series/events 的
  `as_of` 与源 `date` 列都是 ISO 日期（`YYYY-MM-DD`，无时区）；news 的
  `as_of`/`available_at` 保留带 Asia/Shanghai 偏移的 ISO-8601 时间——新闻按分钟可见，
  不能压成日频。日期字符串不一致会让下游按字符串 join 时漏配，故在规范化层统一。
- 同一主键（`date`/`event_id`/`content_hash`）只保留最后一条；排序固定 `as_of` 升序
  （`mergesort` 保证稳定）。同日重复价不做平均，直接取最后一条并在留档里可查。
- 坏行必须剔除并留档（`rejected` + `reject_reason`），不得原地改值冒充好数据；
  剔除会让本次结果降为 `YELLOW`，但不会直接把数据集判 `RED`。
- `RED` 只留给两种确定不可用的情况：price/series/news 空源，事件 `severity` 非法。
- 缺失策略沿用 AGENTS §7.4：`revised_only` 数据集所有行 `quality=DEGRADED`；
  `zero_fields` 里的字段，真实 0 打 `ZERO_<COL>` 行级标记，缺失保持 NaN 并打
  `MISSING_<COL>`——禁止用 `fillna(0)` 把缺失洗成“真实零成交/零持仓”，
  两类标记都不构成加仓依据（AGENTS §2.1-5）。
- 抓取早于 `available_at` 的行打 `PROVISIONAL`，行质量 DEGRADED、数据集 YELLOW；
  同日后来已有有效值时，旧拒绝留档不再计入最近缺失（避免历史缺口永久降黄）。
- 两类「源缺值被写成 0」整行剔除并留档（2026-10-24，用户裁定甲/乙/丙中的乙）：
  `drop_all_zero_rows` 数据集的 series 行若**全部必需数值列都是 0**，按
  `ALL_ZERO_IN_REQUIRED` 剔除（CFTC 1997/2003 两行）；`positive_columns` 声明的列
  （AU0 的 `settlement`）出现 ≤0 时按 `NONPOSITIVE_<COL>` 剔除。两者都不是「补值」，
  原值留在 `rejected/` 里可查。
- 覆盖率低于 `coverage_floor`（来自 `application.acceptance.min_weekday_coverage`）
  的 price/series 数据集标 `LOW_COVERAGE` 并降为 YELLOW：这是**披露**而不是放行——
  验收判据 N1 只在质量报告已按同一标记披露时才不再判红（2026-10-24，D4 乙）。
- 单位不在此换算（CNY/g、USD/oz、%、吨各自由数据源决定），跨市场换算属特征层职责。
"""
from __future__ import annotations

import math
import statistics
from datetime import date

import pandas as pd

from ..common.errors import AdapterError
from ..common.timeutil import iso, now_shanghai, parse_date, parse_datetime, provisional_mask
from .trade_calendar import TradeCalendar, expected_days

# 覆盖报告连续性提示的阈值：只用于“第一屏可见性”，不改变数据集 RED/YELLOW 定档。
# 日频源工作日覆盖率低于 80% 时给出 warning；weekly 源按 40–60 行/年识别，
# 避免把 CFTC 这类周频源误报成“缺日”。阈值属报告口径，改动须同步 AGENTS §6.6。
CONTINUITY_WARN_COVERAGE = 0.80
WEEKLY_ROWS_PER_YEAR = (40.0, 60.0)
DAILY_MIN_ROWS_PER_YEAR = 100.0
from ..config.loader import DatasetConfig
from .models import NormalizeResult, Quality
from .pit import apply_pit

# 价格类数值列清单（契约参考）。当前 `normalize_frame()` 实际按 `spec.required_columns`
# 遍历转换，本常量未被直接引用；保留它用于说明“哪些列在 price 分支必须可转数值”。
PRICE_NUMERIC = ["open", "high", "low", "close", "volume", "open_interest", "amount"]
# 新闻类必填列清单（契约参考，同样由配置驱动，当前未直接引用）。
NEWS_COLUMNS = ["published_at", "title", "content_hash"]


def _empty_frame(spec: DatasetConfig) -> pd.DataFrame:
    """构造与正常输出同列的空表，保证空源也能走完 PIT 与下游 merge。

    列集合取 `required_columns` 去掉 PIT 列（`as_of`/`available_at` 由 `apply_pit()`
    统一补），并额外放一个空的 `as_of`。这样空表与正常表的列集合兼容，
    `NormalizedStore.merge()` 不会因为空表缺列而错位或丢历史行。

    Args:
        spec: 数据集配置，只用到 `required_columns`。

    Returns:
        全 object 列的空 DataFrame（没有任何行）。
    """
    columns = [column for column in spec.required_columns if column not in {"as_of", "available_at"}]
    if "as_of" not in columns:
        columns.append("as_of")
    return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})


def _to_number(series: pd.Series) -> pd.Series:
    """把列转为数值；无法解析的值变成 NaN，由调用方决定“剔除”还是“标记缺失”。

    用 `errors="coerce"` 而不是抛异常：适配器常给出 `'-'`、`''`、`'--'` 等占位符，
    逐格抛错会中断整批抓取。这里只负责转类型，可用性由行级校验判定；不得在这里
    `fillna(0)`，否则会把“缺失”洗成“真实零值”。

    Args:
        series: 原始列（可能是 object/str）。

    Returns:
        数值列；无法解析的位置为 NaN。
    """
    return pd.to_numeric(series, errors="coerce")


def _rejected_rows(frame: pd.DataFrame, mask: pd.Series, reason: str) -> pd.DataFrame:
    """保留被剔除行的原值与原因，供人工审计；不进入规范化主表。

    Args:
        frame: 剔除前的表（保留 `as_of`、价格等原值，便于复盘）。
        mask: 与 `frame.index` 对齐的布尔掩码，True 表示该行将被剔除。
        reason: 写入 `reject_reason` 列的默认错误码，取值
            `OHLC_INVALID`/`PRICE_OUT_OF_RANGE`/`NAN_IN_REQUIRED`/`SEVERITY_INVALID`。
            price 分支会再按行覆盖成更具体的原因。

    Returns:
        `reset_index(drop=True)` 后的剔除行；没有命中任何行时返回空表（不添加列），
        调用方需用 `.empty` 判断，不要假设 `reject_reason` 一定存在。
    """
    rejected = frame.loc[mask].copy()
    if rejected.empty:
        return rejected
    rejected["reject_reason"] = reason
    return rejected.reset_index(drop=True)


def normalize_frame(
    raw: pd.DataFrame,
    spec: DatasetConfig,
    fetched_at,
    ingest_run_id: str,
) -> NormalizeResult:
    """按数据集 kind 规范化原始表，并补齐 PIT 字段。

    四类分支的列契约与质量口径：
    - `price`：`date, open, high, low, close[, volume, open_interest, amount]`；
      价格单位随数据源（518880 为 CNY/份、SGE/AU0 为 CNY/g、GC 为 USD/oz、CL 为 USD/bbl），
      本函数只做量级区间校验（`price_min/price_max`），不换算单位。
      `low > min(open, close)`、`high < max(open, close)`、`high < low`、任一价 ≤0、
      `close` 为空或越界，都整行剔除；`reject_reason` 为 `OHLC_INVALID`（结构错）或
      `PRICE_OUT_OF_RANGE`（量级/空值）。price 分支要求**全部数值必需列**非空，
      成交量缺失也会整行剔除——宁可少一行，也不要一条没有量的价格。
      另：`positive_columns` 声明的列（当前只有 AU0 的 `settlement`）出现 ≤0 时按
      `NONPOSITIVE_<COL>` 整行剔除；缺值仍走 `NAN_IN_REQUIRED`，两条原因分开写，
      便于分辨「源给 0」与「源给空」。
    - `series`：`date + 主值列`（如 `us10y`、`gprd`、`holdings_tonnes`）；只要求首个
      数值必需列（主值）非空，附带列（如 CFTC 的 long/short）允许为空。
      主值为空整行剔除并记 `NAN_IN_REQUIRED`。声明了 `drop_all_zero_rows` 的数据集，
      **全部必需数值列同时为 0** 的行按 `ALL_ZERO_IN_REQUIRED` 整行剔除——多空净仓
      全 0 不是「空头为零」，而是源没给值；只对配置显式开启的数据集生效，
      因为 GPR 这类指数的 0 可以是真实的平静日（本库实测 2 行 `gprd=0`），
      全局强推会把真实读数删掉。
    - `news`：`published_at, title, content_hash`；`as_of` 与 `available_at` 都取
      `published_at` 的带时区 ISO-8601（新闻没有“收盘后统一可见”的概念），
      按 `content_hash` 去重（保留最后一条），标题空值补空串。
    - `events`：`event_id, date, type, severity, region, source_url, available_at`；
      `severity` 必须是 1/2/3，非法行剔除且本次结果整体 `RED`（严重度无法猜测，
      不能降级放行）；合法行 `flags` 标 `MANUAL`，表示人工维护、需人工复核。

    PIT 与质量传播：输出尾部固定追加
    `source/as_of/available_at/fetched_at/ingest_run_id/vintage/quality/flags`。
    行级 `quality` 只在 price/series 分支重算——`flags` 非空且不是 `MANUAL` 时
    `DEGRADED`；`revised_only` 数据集所有行强制 `DEGRADED`。抓取早于
    `available_at` 的行追加 `PROVISIONAL`，数据集判 YELLOW，事实包必须剔除。`zero_fields` 中的列
    保留原值：真实 0 标 `ZERO_<COL>`，缺失标 `MISSING_<COL>`（不填 0）。数据集级
    `status` 与行级独立：`price/series` 整批行被剔除（`frame` 剔空）判 `RED` +
    `ALL_ROWS_DROPPED`，只掉部分行判 `YELLOW`（附 `INVALID_ROWS_DROPPED`）；
    `RED` 另外留给空源与非法 severity。合并后的历史序列可能仍然新鲜，因此
    调用方（`run_ingest`）必须把这里的 `RED` 顶到数据集质量档上，不得被历史洗白。

    Args:
        raw: 适配器原始表；为 None 视为适配器违约。
        spec: 数据集配置（`kind`、`required_columns`、`key_columns` 语义、价格区间、
            `zero_fields`、`revised_only`、`available_at_rule`）。
        fetched_at: 抓取时间，必须带时区；写入 `fetched_at` 并参与 `available_at` 计算。
        ingest_run_id: 本次抓取批次号，写入 PIT，便于重放与对表。

    Returns:
        `NormalizeResult`：主表已排序、已去重、已补 PIT；`rejected` 为剔除行留档；
        `dropped_rows` 与 `rejected` 行数一致。

    Raises:
        AdapterError: `raw is None`，或缺少 `required_columns` 中的任一列
            （列契约不满足时不做“补空列继续跑”）。
        ValueError: `available_at_rule=explicit` 但某行 `available_at` 为空
            （由 `apply_pit()` 抛出，属人工数据错误）。
    """
    if raw is None:
        raise AdapterError(f"{spec.source} 返回 None")
    frame = raw.copy()
    notes: list[str] = []
    flags: list[str] = []
    invalid_rows = 0
    dropped = 0

    if frame.empty:
        result = apply_pit(_empty_frame(spec), spec, fetched_at, ingest_run_id)
        if spec.kind == "events":
            # 事件日历为空是“未登记”，不是“无风险”：只能黄灯，且必须让报告看见 EMPTY。
            return NormalizeResult(result, Quality.YELLOW, ["EMPTY"], 0, ["事件日历为空：表示未登记事件，不等于无风险"])
        # 行情/序列/新闻为空说明源坏了或未抓到，判 RED；不得用空表当“今天没变化”。
        return NormalizeResult(result, Quality.RED, ["EMPTY_SOURCE"], 0, ["数据源返回空表"])

    missing = [column for column in spec.required_columns if column not in frame.columns]
    if missing:
        raise AdapterError(f"{spec.source} 缺少必需列：{missing}")

    if spec.kind == "news":
        # 新闻按发布时间可见，不存在“收盘后统一可读”的问题；两条 PIT 时间戳取同一个值，
        # 后续 PIT 截断只要比较 available_at 即可，无需按 kind 特判。
        frame["as_of"] = frame["published_at"].map(lambda v: iso(parse_datetime(v)))
        frame["available_at"] = frame["as_of"]
        frame["title"] = frame["title"].fillna("").astype(str)
        frame["content_hash"] = frame["content_hash"].astype(str)
        frame = frame.drop_duplicates(subset=["content_hash"], keep="last")
        frame = frame.sort_values("as_of", kind="mergesort").reset_index(drop=True)
        frame["flags"] = ""
        result = apply_pit(frame, spec, fetched_at, ingest_run_id)
        return NormalizeResult(result, Quality.GREEN, [], 0, notes)

    if spec.kind == "events":
        frame["as_of"] = frame["date"].map(lambda v: parse_date(v).isoformat())
        # 与 as_of 用同一字符串：下游按 date/as_of 做字符串 join 时不会因
        # "2026-10-19" 与 "2026-10-19T00:00:00" 两种写法漏配。
        frame["date"] = frame["as_of"]
        # severity 先转数值：手工表里 "3.0" 也算 3；转不动的直接当非法，不猜等级。
        severity = _to_number(frame["severity"])
        bad_severity = severity.isna() | ~severity.isin([1, 2, 3])
        invalid_rows += int(bad_severity.sum())
        rejected = pd.DataFrame()
        if bad_severity.any():
            notes.append(f"丢弃 severity 非 1/2/3 的事件 {int(bad_severity.sum())} 行")
            rejected = _rejected_rows(frame, bad_severity, "SEVERITY_INVALID")
            frame = frame.loc[~bad_severity].copy()
            dropped += int(bad_severity.sum())
        # 用原 severity 序列里“合法位置”的值，保证与剔除后的 frame 行对齐。
        frame["severity"] = severity.loc[~bad_severity].astype(int)
        # 同 event_id 出现不同 severity 是人工登记冲突：不猜等级，整组剔除留档并判 RED
        # （评审 F6；旧 keep=last 会静默留最后一条，可能把 3 级降成 1 级）。
        distinct = frame.groupby("event_id")["severity"].nunique()
        conflict_ids = distinct[distinct > 1].index
        if len(conflict_ids):
            conflict_mask = frame["event_id"].isin(conflict_ids)
            notes.append(f"事件 severity 冲突整组剔除留档：{sorted(conflict_ids.tolist())}")
            rejected = pd.concat(
                [rejected, _rejected_rows(frame, conflict_mask, "SEVERITY_CONFLICT")], ignore_index=True
            )
            invalid_rows += int(conflict_mask.sum())
            dropped += int(conflict_mask.sum())
            frame = frame.loc[~conflict_mask].copy()
        frame = frame.drop_duplicates(subset=["event_id"], keep="last")
        frame = frame.sort_values("as_of", kind="mergesort").reset_index(drop=True)
        # 手工事件必须显式可辨：下游看到 MANUAL 才知道该字段需要人工复核。
        frame["flags"] = "MANUAL"
        result = apply_pit(frame, spec, fetched_at, ingest_run_id)
        # 只要出现过非法 severity 就整体 RED：严重度决定地缘风险档位，缺一行就可能低估风险。
        status = Quality.RED if invalid_rows else Quality.GREEN
        return NormalizeResult(result, status, flags, dropped, notes, rejected)

    # price / series
    frame["as_of"] = frame["date"].map(lambda v: parse_date(v).isoformat())
    # 源 date 列也统一成 ISO 日期；否则 COMEX/WTI/GPR 会留下 T00:00:00，
    # 与其它数据集的 YYYY-MM-DD 混用，字符串 join 时静默漏行。
    frame["date"] = frame["as_of"]
    # 只转 required_columns 里的业务列：适配器可能附带额外列（如 symbol），
    # 那些列保持原样，避免把文本列误转成 NaN 后又被当作缺失剔除。
    for column in spec.required_columns:
        if column in {"date", "as_of"}:
            continue
        if column not in frame.columns:
            continue
        frame[column] = _to_number(frame[column])

    required_numeric = [column for column in spec.required_columns if column not in {"date", "as_of", "severity"}]
    nan_required = frame[required_numeric].isna().any(axis=1)
    if spec.kind == "series":
        # 序列只要求主值列；附带列可以为空（CFTC 的 long/short、SPDR 的 change 等
        # 在部分历史行本就没有值，不能因此丢掉主值）。
        main = required_numeric[0] if required_numeric else None
        nan_required = frame[main].isna() if main else pd.Series(False, index=frame.index)
    # 全 0 与「某一列为 0」不同：多空与净仓同时为 0 说明源没给这一周的值，留着会被
    # 下游当成真实持仓读数。只对显式开启 `drop_all_zero_rows` 的数据集生效——GPR
    # 这类指数的 0 可以是真实的平静日，全局强推会把真实读数删掉。
    all_zero = pd.Series(False, index=frame.index, dtype=bool)
    if spec.kind == "series" and spec.drop_all_zero_rows:
        zero_columns = [column for column in required_numeric if column in frame.columns]
        if zero_columns:
            all_zero = frame[zero_columns].eq(0).all(axis=1) & ~nan_required
    rejected_parts: list[pd.DataFrame] = []
    if nan_required.any():
        count = int(nan_required.sum())
        notes.append(f"丢弃必需数值为空的行 {count} 行")
        rejected_parts.append(_rejected_rows(frame, nan_required, "NAN_IN_REQUIRED"))
        dropped += count
        flags.append("NAN_IN_REQUIRED")
        frame = frame.loc[~nan_required].copy()
    zero_mask = all_zero.reindex(frame.index, fill_value=False)
    if zero_mask.any():
        count = int(zero_mask.sum())
        notes.append(f"丢弃必需数值全为 0 的行 {count} 行（series：全 0 视为源缺值）")
        rejected_parts.append(_rejected_rows(frame, zero_mask, "ALL_ZERO_IN_REQUIRED"))
        dropped += count
        flags.append("ALL_ZERO_IN_REQUIRED")
        frame = frame.loc[~zero_mask].copy()

    row_flags = pd.Series([""] * len(frame), index=frame.index, dtype="object")

    if spec.kind == "price":
        for column in ("open", "high", "low", "close"):
            if column in frame.columns:
                frame[column] = _to_number(frame[column])
        close = frame["close"]
        # 收盘价是唯一被所有下游（均线、收益、估值）依赖的列，先单独判空与量级：
        # 价格 ≤0 或越出配置区间通常意味着单位错了（分/元、g/oz 混用）。
        bad_price = close.isna() | (close <= 0)
        if spec.price_min is not None:
            bad_price = bad_price | (close < spec.price_min)
        if spec.price_max is not None:
            bad_price = bad_price | (close > spec.price_max)
        if {"open", "high", "low"}.issubset(frame.columns):
            # OHLC 不自洽的行（假 K 线）整行剔除；任一价 ≤0 也按结构错误处理。
            bad_ohlc = (
                (frame["low"] > frame[["open", "close"]].min(axis=1))
                | (frame["high"] < frame[["open", "close"]].max(axis=1))
                | (frame["high"] < frame["low"])
                | (frame[["open", "high", "low"]].le(0).any(axis=1))
            )
        else:
            bad_ohlc = pd.Series(False, index=frame.index)
        invalid = bad_price | bad_ohlc
        if invalid.any():
            count = int(invalid.sum())
            invalid_rows += count
            dropped += count
            notes.append(f"丢弃价格/OHLC 非法行 {count} 行")
            reason = pd.Series("OHLC_INVALID", index=frame.index, dtype="object")
            reason.loc[bad_price] = "PRICE_OUT_OF_RANGE"
            # 先用统一 reason 建留档，再把具体原因按位置覆盖上去：_rejected_rows 已
            # reset_index，所以 `reason.loc[invalid].values` 与留档行严格同序。
            rejected_parts.append(_rejected_rows(frame, invalid, "OHLC_INVALID").assign(
                reject_reason=reason.loc[invalid].values
            ))
            row_flags.loc[bad_ohlc & ~bad_price] = "OHLC_INVALID"
            row_flags.loc[bad_price] = "PRICE_OUT_OF_RANGE"
            frame = frame.loc[~invalid].copy()
            row_flags = row_flags.loc[frame.index]
            flags.append("OHLC_OR_PRICE_INVALID")
        # 声明为「必须为正」的列（AU0 的 settlement）：源把缺值写成 0 时整行剔除，
        # 不猜一个结算价出来（AGENTS §2.1-2）。原因带列名，便于按列统计缺口；
        # 缺值（NaN）走上面的 NAN_IN_REQUIRED，两条原因分开，能分辨「源给 0」与「源给空」。
        for column in spec.positive_columns:
            if column not in frame.columns:
                continue
            values = frame[column]
            nonpositive = values.notna() & (values <= 0)
            if not nonpositive.any():
                continue
            count = int(nonpositive.sum())
            label = f"NONPOSITIVE_{column.upper()}"
            notes.append(f"丢弃 {column}<=0 的行 {count} 行")
            invalid_rows += count
            dropped += count
            if label not in flags:
                flags.append(label)
            rejected_parts.append(_rejected_rows(frame, nonpositive, label))
            frame = frame.loc[~nonpositive].copy()
            row_flags = row_flags.loc[frame.index]
        for column in spec.zero_fields:
            if column not in frame.columns:
                continue
            # 真实 0 与缺失必须分开：前者是源缺陷（零成交/零持仓）但要保留原值，
            # 后者是“没抓到”，只能标 MISSING_ 并保持 NaN，禁止 fillna(0) 洗成真实零值。
            values = frame[column]
            zeros = values == 0
            if zeros.any():
                label = f"ZERO_{column.upper()}"
                row_flags.loc[zeros] = row_flags.loc[zeros].map(
                    lambda existing: ";".join(part for part in (existing, label) if part)
                )
                if label not in flags:
                    flags.append(label)
            missing = values.isna()
            if missing.any():
                label = f"MISSING_{column.upper()}"
                row_flags.loc[missing] = row_flags.loc[missing].map(
                    lambda existing: ";".join(part for part in (existing, label) if part)
                )
                if label not in flags:
                    flags.append(label)

    frame = frame.drop_duplicates(subset=["as_of"], keep="last")
    frame = frame.sort_values("as_of", kind="mergesort").reset_index(drop=True)
    row_flags = pd.Series([""] * len(frame), index=frame.index, dtype="object")
    # 重新按最终索引标注零值与缺失：去重与排序已改变索引，且坏行已整行剔除，
    # 沿用剔除前的 row_flags 会把标记贴到错误的行上；行级质量必须与最终行一一对应。
    for column in spec.zero_fields:
        if column in frame.columns:
            values = frame[column]
            for mask, label in (
                (values == 0, f"ZERO_{column.upper()}"),
                (values.isna(), f"MISSING_{column.upper()}"),
            ):
                if mask.any():
                    row_flags.loc[mask] = row_flags.loc[mask].map(
                        lambda existing: ";".join(part for part in (existing, label) if part)
                    )
                    if label not in flags:
                        flags.append(label)
    frame["flags"] = row_flags.values
    result = apply_pit(frame, spec, fetched_at, ingest_run_id)
    if result["flags"].astype(str).str.contains("PROVISIONAL", regex=False).any():
        # 盘中抓取早于 available_at：本次快照可能不是最终值，数据集必须降黄。
        if "PROVISIONAL" not in flags:
            flags.append("PROVISIONAL")
    # 行级质量只允许 GREEN/DEGRADED：空标记与 MANUAL 才是 GREEN；任何 ZERO_/OHLC
    # 标记都 DEGRADED。数据集三档（GREEN/YELLOW/RED）见 DatasetQuality.status，禁止写进行级列。
    # `revised_only` 数据集整列强制 DEGRADED，防止修订后数据被当当时可见值使用。
    row_quality = [
        "DEGRADED" if str(value).strip() not in {"", "MANUAL"} else "GREEN"
        for value in result["flags"]
    ]
    result["quality"] = row_quality if not spec.revised_only else "DEGRADED"

    # 数据集级：整批被剔空说明错在列契约/单位口径这一层，是整体性故障，判 RED；
    # 只掉几行才是空洞，降黄留档。零值与修订数据同样是“可用但不可加仓”，只降黄。
    if frame.empty and dropped > 0:
        status = Quality.RED
        if "ALL_ROWS_DROPPED" not in flags:
            flags.append("ALL_ROWS_DROPPED")
    elif invalid_rows:
        status = Quality.YELLOW
        if "INVALID_ROWS_DROPPED" not in flags:
            flags.append("INVALID_ROWS_DROPPED")
    elif any(
        flag.startswith(("ZERO_", "MISSING_"))
        or flag in {"NAN_IN_REQUIRED", "ALL_ZERO_IN_REQUIRED", "PROVISIONAL"}
        for flag in flags
    ) or spec.revised_only:
        status = Quality.YELLOW
    else:
        status = Quality.GREEN
    rejected = pd.concat(rejected_parts, ignore_index=True) if rejected_parts else pd.DataFrame()
    return NormalizeResult(result, status, flags, dropped, notes, rejected)


def cadence_of(dates: list[date]) -> str:
    """按相邻日期的中位间隔判定序列节奏，供覆盖率类判据共用。

    Args:
        dates: 升序日期列表（同一日可重复，调用方应先取唯一日）。

    Returns:
        str: 中位间隔 ≥5 天为 ``weekly``，否则 ``daily``；少于 3 个日期时按
            ``daily`` 处理（样本不足不能证明它是周频）。

    Notes:
        normalize 的覆盖率下限与验收 N1/N3 必须用同一把尺：两处若各写一套，
        同一个源会在一处按日频判缺口、另一处按周频放行。
    """
    if len(dates) < 3:
        return "daily"
    gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:])]
    # 用真中位数而不是「排序后取中间那一个」：相邻间隔常在 3/5 天之间交替
    # （周五→周三＝5），取单个元素会把日频序列判成周频。
    return "weekly" if statistics.median(gaps) >= 5 else "daily"


def assess_dataset(
    frame: pd.DataFrame,
    spec: DatasetConfig,
    reference_date: date | None = None,
    invalid_rows: int = 0,
    rejected: pd.DataFrame | None = None,
    calendars: dict[str, TradeCalendar] | None = None,
    coverage_floor: float | None = None,
) -> "DatasetQuality":
    """对合并后的全量规范化序列做新鲜度、缺口与最近坏点评估。

    判定顺序（首个命中即定档，见函数末尾）：最近可用行出现非缺失类坏点 → `RED`；
    超期 → `YELLOW` + `STALE`；最近有缺失类坏点 → `YELLOW`；最近 5 行带
    `REVISED_ONLY`/`NAN_IN_REQUIRED`/`PROVISIONAL`/`ZERO_*` → `YELLOW`；日频源全区间
    覆盖率低于 `coverage_floor` → `YELLOW` + `LOW_COVERAGE`（周频源按节奏判，
    不参与覆盖率下限）；其余 `GREEN`。
    历史坏行仍留在 `flags`/`rejected_rows` 中可见，但不把当前可用序列永久判黄。
    同一日期后来已有有效值时，旧拒绝留档只保留审计计数，不再计入最近缺失。

    口径边界：
    - “最近”= 尾部 5 个有效行的最早 `as_of` 起、到 `as_of_max`（含）为止的日期
      区间，外加晚于 `as_of_max` 的剔除行；不是严格的 5 个交易日，也不读交易日历。
      用日期区间而非尾行日期集合，是为了让夹在最近有效行之间的被剔除日期仍被计入。
    - `weekday_gaps` 的分母由 `spec.calendar` 决定：`cn_sse` 且日历可用时按上交所
      官方交易日（节假日不再算缺口）；`weekday` 口径、或声明了 `cn_sse` 但日历
      未落盘/校验不符时，按周一至周五近似，节假日会被算成缺口（保守口径，宁可
      多报）。到底用了哪种口径写进 `continuity_basis`，读数自己必须说清。
      news/events 不计算缺口。
    - 空表：events 为 `YELLOW`（未登记不等于无风险），其余 kind 为 `RED`。

    Args:
        frame: 合并后的规范化表，必须含 `as_of`，可选 `flags`；通常来自
            `NormalizedStore.load()` 的合并结果。
        spec: 数据集配置，提供 `kind`、`stale_days`、`revised_only`。
        reference_date: 质量评估参考日（通常是 `--end` 或当天）；缺省取当前上海日期。
        invalid_rows: 调用方已知的剔除行数。当 `rejected` 缺失或不含 `as_of` 时用它
            保守估算最近坏点（无法区分历史/近期，只能按“近期坏点”处理）。
        rejected: 被剔除行留档（`NormalizedStore.read_rejected()`），需含 `as_of`
            与可选 `reject_reason`；有它才能把“历史坏点”和“最近坏点”分开。
        calendars: 已加载日历 `{basis: TradeCalendar}`；None 表示全部按
            `weekday` 近似。只有 `spec.calendar == "cn_sse"` 且该日历
            `available` 时，分母才换成上交所官方交易日。
        coverage_floor: 覆盖率下限（0–1），来自 `application.acceptance.min_weekday_coverage`；
            None 表示不按覆盖率定档。低于下限时数据集标 `LOW_COVERAGE` 并降为
            YELLOW——这是披露，不是放行：验收 N1 只在质量报告已按同一标记披露时
            才不再判红（D4 乙，2026-10-24）。

    Returns:
        `DatasetQuality`，可直接 `as_dict()` 写入 `_quality.json`。

    Raises:
        KeyError: `frame` 不含 `as_of` 列（契约破坏；不做“补空列继续算”）。
    """
    # 局部导入：本函数是唯一直接构造 DatasetQuality 的地方，返回值类型注解用字符串
    # 前向引用，避免顶层再多一个名字。
    from .models import DatasetQuality

    reference = reference_date or now_shanghai().date()
    if frame is None or frame.empty:
        status = Quality.YELLOW if spec.kind == "events" else Quality.RED
        rejected_rows = len(rejected) if rejected is not None else invalid_rows
        return DatasetQuality(
            spec.source, status, ["EMPTY"], 0, None, None, invalid_rows, 0, None, 0, [],
            rejected_rows=rejected_rows,
        )

    # as_of 既有纯日期（price/series/events），也有带偏移的时间戳（news）。统一先按
    # UTC 解析再转上海：纯日期取 UTC 0 点 → 上海 08:00，仍是同一天，不会前后移日；
    # 带时区的时间戳则保持真实本地时间。errors="coerce" 让脏日期变成 NaT 后不参与边界。
    as_of = pd.to_datetime(frame["as_of"], errors="coerce", utc=True).dt.tz_convert("Asia/Shanghai")
    as_of_date = as_of.dt.date
    as_of_min = as_of_date.min()
    as_of_max = as_of_date.max()
    flags = set()
    if "flags" in frame.columns:
        for value in frame["flags"].fillna(""):
            for flag in str(value).split(";"):
                if flag:
                    flags.add(flag)
    if spec.revised_only:
        flags.add("REVISED_ONLY")

    # 只看尾部 5 行：一次三年前的脏数据不应让数据集永远无法加仓；但历史标记仍保留在
    # 全量 flags 里，报告与人工审计看得到。注意 frame 已按 as_of 升序，尾行即最新。
    recent = frame.tail(5)
    recent_flags: set[str] = set()
    if "flags" in recent.columns:
        for value in recent["flags"].fillna(""):
            for flag in str(value).split(";"):
                if flag:
                    recent_flags.add(flag)
    if spec.revised_only:
        recent_flags.add("REVISED_ONLY")
    if {"fetched_at", "available_at"} <= set(frame.columns):
        provisional = provisional_mask(frame)
        if provisional.any():
            flags.add("PROVISIONAL")
            if provisional.tail(5).any():
                recent_flags.add("PROVISIONAL")

    zero_rows = 0
    # 按标记逐个统计：同一行若同时零成交量与零持仓，会被两个标记各计一次，
    # 这是“异常次数”而不是“异常行数”，报告里不要与 rows 相减。
    for flag in list(flags):
        if flag.startswith("ZERO_"):
            zero_rows += int(frame["flags"].fillna("").str.contains(flag, regex=False).sum())

    rejected_rows = len(rejected) if rejected is not None else invalid_rows
    recent_invalid_rows = 0
    recent_missing_rows = 0
    if rejected_rows:
        flags.add("INVALID_ROWS_DROPPED")
        if rejected is not None and not rejected.empty and "as_of" in rejected.columns:
            # 能逐行留档时，用日期区间把坏点分成“最近”和“历史”：最近 = 从尾部
            # 5 个有效行里最早日期起、到最新可用日（含）为止；外加晚于最新可用日
            # 的剔除行。不能只用尾行日期集合——被剔除的日期正好夹在最近两个有效行
            # 之间时（如 WTI 2026-10-22），集合判定会把它误当历史。
            rejected_date = pd.to_datetime(rejected["as_of"], errors="coerce").dt.date
            recent_valid = as_of_date.dropna().tail(5)
            if recent_valid.empty:
                in_recent = pd.Series(False, index=rejected.index)
            else:
                in_recent = rejected_date.ge(recent_valid.min())
                if as_of_max is not None and not pd.isna(as_of_max):
                    in_recent &= rejected_date.le(as_of_max)
            if as_of_max is not None and not pd.isna(as_of_max):
                in_recent = in_recent | (rejected_date > as_of_max)
            reasons = rejected.get("reject_reason", pd.Series("", index=rejected.index)).fillna("")
            missing_mask = reasons.astype(str).eq("NAN_IN_REQUIRED")
            # 同日后来已有有效值时，旧的“缺失”拒绝不再代表当前缺口（US10Y 2026-10-19）；
            # 结构类坏点（OHLC_INVALID）仍算近期污染，不能因为是同日就放过。
            valid_dates = set(as_of_date.dropna().tolist())
            superseded_missing = missing_mask & rejected_date.isin(valid_dates)
            recent_invalid_rows = int((in_recent & ~missing_mask).sum())
            recent_missing_rows = int((in_recent & missing_mask & ~superseded_missing).sum())
        elif invalid_rows:
            # 没有逐行留档时无法证明是历史坏点，按最近坏点保守处理
            recent_invalid_rows = invalid_rows
    if recent_invalid_rows:
        flags.add("RECENT_INVALID_ROWS")
    if recent_missing_rows:
        flags.add("RECENT_MISSING_ROWS")

    stale_days = None
    if as_of_max is not None and not pd.isna(as_of_max):
        stale_days = (reference - as_of_max).days
        if stale_days > spec.stale_days:
            flags.add("STALE")

    weekday_gaps = 0
    gap_sample: list[str] = []
    continuity_basis = "weekday_approx"
    if spec.kind in {"price", "series"} and as_of_min is not None and not pd.isna(as_of_min):
        # 分母口径由配置声明：cn_sse 用上交所官方交易日（节假日不再算缺口），
        # weekday 只剔周末。日历没落盘/校验不符时 expected_days 会回落近似，
        # 并把回落原因写进 continuity_basis，读数不会冒充官方口径。
        observed = {value for value in as_of_date.dropna().tolist()}
        expected, continuity_basis = expected_days(spec.calendar, as_of_min, as_of_max, calendars)
        missing = [value for value in expected if value not in observed]
        weekday_gaps = len(missing)
        gap_sample = [value.isoformat() for value in missing[:10]]
        # 覆盖率按「命中应交易日/应交易日」算，与验收 N1 同一算法；低于下限只降级
        # 并打标记，不判 RED——缺口是知道了就能用的信息，不是「源坏了」。
        # 周频源不参与：CFTC 一年只有约 52 条，按交易日算必然「覆盖率 3%」，
        # 那是节奏不是缺口（与验收 N3 共用 cadence_of 的判据）。
        weekly_source = cadence_of(sorted(observed)) == "weekly"
        if (
            expected
            and coverage_floor is not None
            and not weekly_source
            and (1 - weekday_gaps / len(expected)) < coverage_floor
        ):
            flags.add("LOW_COVERAGE")

    # 定档顺序即优先级：最近坏点最硬（RED），其次过期、最近缺失、最近降级标记，
    # 最后才 GREEN。这里只查 recent_*，历史标记不参与定档。
    if recent_invalid_rows:
        status = Quality.RED
    elif stale_days is not None and stale_days > spec.stale_days:
        status = Quality.YELLOW
    elif recent_missing_rows:
        status = Quality.YELLOW
    elif recent_flags & {"REVISED_ONLY", "NAN_IN_REQUIRED", "PROVISIONAL"} or any(
        flag.startswith(("ZERO_", "MISSING_")) for flag in recent_flags
    ):
        status = Quality.YELLOW
    elif "LOW_COVERAGE" in flags:
        # 覆盖率低于下限：数据集「可用但降级」，由 N1 按同一标记判断是否已披露。
        status = Quality.YELLOW
    else:
        # 历史坏行保留在 flags / rejected_rows 中可见，但不把当前可用序列永久判黄
        status = Quality.GREEN

    return DatasetQuality(
        dataset=spec.source,
        status=status,
        flags=sorted(flags),
        rows=len(frame),
        as_of_min=as_of_min.isoformat() if as_of_min is not None and not pd.isna(as_of_min) else None,
        as_of_max=as_of_max.isoformat() if as_of_max is not None and not pd.isna(as_of_max) else None,
        invalid_rows=invalid_rows,
        zero_rows=zero_rows,
        stale_days=stale_days,
        weekday_gaps=weekday_gaps,
        weekday_gap_sample=gap_sample,
        continuity_basis=continuity_basis,
        rejected_rows=rejected_rows,
        recent_invalid_rows=recent_invalid_rows,
        recent_missing_rows=recent_missing_rows,
        recent_flags=sorted(recent_flags),
    )


def summarize_continuity(
    datasets: dict[str, dict],
    calendars: dict[str, TradeCalendar] | None = None,
) -> list[dict]:
    """把质量报告里的缺口整理成覆盖首屏可见的连续性摘要。

    只做展示，不修改数据集状态：`weekday_gaps` 的分母按数据集声明的口径取
    （`cn_sse` 用官方交易日，`weekday` 用周一至周五近似），近似口径仍会把交易所
    节假日算成缺口，所以不能直接据此判 RED；但 SPDR 这类日频源若覆盖率明显偏低，
    必须让非专业读者在第一屏看到，而不是只藏在 `_quality.json` 的明细里。

    Args:
        datasets: `_quality.json` 的 ``datasets`` 映射；每项可含
            ``quality_detail``（优先）或直接平铺质量字段，其中
            ``continuity_basis`` 决定本行用哪种分母。
        calendars: 已加载日历；缺失时 `cn_sse` 声明会回落近似，与
            `assess_dataset` 的回落一致，两处不会算出两个覆盖率。

    Returns:
        按覆盖率升序排列的摘要列表；每项含
        ``dataset/rows/as_of_min/as_of_max/weekday_gaps/weekday_coverage``、
        ``continuity_basis``（分母口径）、推断的 ``cadence_hint``、缺口样例与
        可选 ``warning``。无缺口、无日期边界或日期非法的数据集不进入列表。

    Raises:
        无；坏输入按“无法评估”跳过，不影响覆盖报告其他字段。
    """
    summary: list[dict] = []
    for name, entry in datasets.items():
        if not isinstance(entry, dict):
            continue
        detail = entry.get("quality_detail") if isinstance(entry.get("quality_detail"), dict) else entry
        gaps_raw = detail.get("weekday_gaps") or 0
        start_text = detail.get("as_of_min")
        end_text = detail.get("as_of_max")
        if not gaps_raw or not start_text or not end_text:
            continue
        try:
            start = date.fromisoformat(str(start_text)[:10])
            end = date.fromisoformat(str(end_text)[:10])
            gaps = int(gaps_raw)
        except (TypeError, ValueError):
            continue
        basis_label = str(detail.get("continuity_basis") or "weekday_approx")
        basis = "cn_sse" if basis_label.startswith("cn_sse") else "weekday"
        expected_days_list, used_basis = expected_days(basis, start, end, calendars)
        expected = len(expected_days_list)
        if expected <= 0:
            continue
        coverage = max(0.0, min(1.0, 1.0 - gaps / expected))
        total_rows = int(detail.get("rows") or entry.get("total_rows") or 0)
        years = max((end - start).days / 365.25, 1.0 / 365.25)
        rows_per_year = total_rows / years
        if WEEKLY_ROWS_PER_YEAR[0] <= rows_per_year <= WEEKLY_ROWS_PER_YEAR[1]:
            cadence = "weekly"
        elif rows_per_year >= DAILY_MIN_ROWS_PER_YEAR:
            cadence = "daily"
        else:
            cadence = "sparse/other"
        warning = None
        if cadence == "daily" and coverage < CONTINUITY_WARN_COVERAGE:
            warning = (
                f"覆盖率 {coverage:.1%}（缺 {gaps} 天；分母口径 {used_basis}）；"
                "若该源应为日频，缺口必须显式标 MISSING 或补数"
            )
        summary.append(
            {
                "dataset": name,
                "rows": total_rows,
                "as_of_min": start.isoformat(),
                "as_of_max": end.isoformat(),
                "weekday_gaps": gaps,
                "weekday_coverage": round(coverage, 4),
                "continuity_basis": used_basis,
                "rows_per_year": round(rows_per_year, 1),
                "cadence_hint": cadence,
                "gap_sample": list(detail.get("weekday_gap_sample") or [])[:10],
                "warning": warning,
            }
        )
    summary.sort(key=lambda item: (item["weekday_coverage"], -item["weekday_gaps"]))
    return summary
