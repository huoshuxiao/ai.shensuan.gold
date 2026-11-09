"""P1 阶段验收闸：把「数据质量 / 连续性 / 一致性」全部编成程序判据。

职责：读取配置、规范化层、原始快照与 ``_quality.json``，逐条判定 P1 是否达标，
输出机器可读的验收报告与非 0 退出码依据。
不负责：判断策略好坏、生成决策卡、人工视觉复核（AGENTS §18.6：P1 不用人眼验收）。

约定：
- 每条判据都有稳定 `rule_id`，对应 AGENTS 章节；判据函数是纯函数，便于负对照。
- 判据返回值统一为 ``CheckResult(status, detail)``，status ∈ ``PASS/FAIL/SKIPPED``。
- ``SKIPPED`` 只用于「本轮无法判定的输入确实不存在」（如原始快照非 CSV），
  且必须写明原因；``SKIPPED`` 不参与 verdict，也不得冒充 PASS。
- 任一 ``FAIL`` ⇒ 整体 verdict=FAIL；`run_p1_acceptance` 的调用方据此决定退出码。
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..common.errors import GoldError
from ..config.loader import DatasetConfig, LoadedConfig
from ..data.normalize import cadence_of, normalize_frame
from ..data.store import format_cell
from ..data.trade_calendar import TradeCalendar, expected_days, load_all_calendars

PIT_COLUMNS = ["source", "as_of", "available_at", "fetched_at", "ingest_run_id", "quality", "flags"]
DATE_KINDS = ("price", "series", "events")


@dataclasses.dataclass
class CheckResult:
    """单条判据的结果。

    Args:
        status: ``PASS``/``FAIL``/``SKIPPED``。
        detail: 人类可读的读数与单位；FAIL 时必须含可定位的样例。

    Notes:
        不设「近似通过」档位：判据只有三态，避免把降级写成通过（AGENTS §0.1）。
    """

    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        """序列化为 JSON 友好字典。

        Returns:
            dict: ``{"status": ..., "detail": ...}``。
        """
        return {"status": self.status, "detail": self.detail}


def _blank_mask(series: pd.Series) -> pd.Series:
    """把「真没有值」统一成布尔掩码：NaN 与空白串都算空。

    Args:
        series: 任意列。

    Returns:
        pd.Series: True 表示该格为空。

    Notes:
        CSV 的空 `flags` 读回来是 NaN，`astype(str)` 会变成字符串 "nan"，
        直接判 `!= ""` 会把每一行都当成非空——判据就此恒真。所有空值判断都走这里。
    """
    return series.isna() | series.astype(str).str.strip().isin(["", "nan", "None"])


def _numeric_columns(spec: DatasetConfig, frame: pd.DataFrame) -> list[str]:
    """取该数据集中真正是数值的业务列，单位随数据集。

    Args:
        spec: 数据集配置。
        frame: 规范化表。

    Returns:
        list: 去掉标识/文本列后的数值列名；`content_hash`、`source_url` 之类
            字符串列不在其中，避免 `to_numeric` 把哈希判成缺失。
    """
    text = {"source", "as_of", "available_at", "fetched_at", "ingest_run_id", "vintage",
            "quality", "flags", "date", "published_at", "title", "content_hash",
            "event_id", "type", "region", "source_url"}
    return [c for c in frame.columns if c not in text]


def _price_columns(spec: DatasetConfig, frame: pd.DataFrame) -> list[str]:
    """取必须为正的价格列（开/高/低/收/结算），单位随数据集。

    Args:
        spec: 数据集配置。
        frame: 规范化表。

    Returns:
        list: 价格列名；成交量/持仓额不在其中——它们的 0 是源结构性事实，
            按 §6.6 标 `ZERO_*` 降级，而不是当伪造值剔除。
    """
    wanted = ("open", "high", "low", "close", "settlement")
    return [c for c in frame.columns if c in wanted]


def _frame_dates(frame: pd.DataFrame, spec: DatasetConfig) -> list[date]:
    """取数据集的 `as_of` 日期序列，单位：日历日。

    Args:
        frame: 规范化表。
        spec: 数据集配置（`kind` 决定时间列）。

    Returns:
        list: 升序 `date` 列表；无法解析的行被跳过（不填充）。

    Raises:
        GoldError: 时间列完全不可解析时抛出，由调用方记 FAIL。
    """
    column = "as_of" if "as_of" in frame.columns else None
    if column is None:
        raise GoldError(f"{spec.adapter} 缺 as_of 列")
    out: list[date] = []
    for value in frame[column]:
        text = str(value)[:10]
        try:
            out.append(date.fromisoformat(text))
        except ValueError:
            continue
    if not out:
        raise GoldError("as_of 全部不可解析")
    return sorted(out)


def _value_columns(spec: DatasetConfig) -> list[str]:
    """取该数据集用于数值一致性比较的列（不含 PIT 列）。

    Args:
        spec: 数据集配置。

    Returns:
        list: `required_columns` 中除 `date`/`event_id`/`published_at`/`title` 之外的列。
    """
    skip = {"date", "event_id", "published_at", "title", "source_url", "type", "region"}
    return [c for c in spec.required_columns if c not in skip]


def check_pit_columns(frame: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 C1：PIT 字段齐全且关键字段非空（AGENTS §6.5）。

    Args:
        frame: 规范化表。
        spec: 数据集配置。

    Returns:
        CheckResult: 缺列或关键字段有空值 → FAIL；空表 → FAIL（数据层不得静默缺源）。
    """
    missing = [c for c in PIT_COLUMNS if c not in frame.columns]
    if missing:
        return CheckResult("FAIL", f"缺 PIT 列 {missing}")
    if frame.empty:
        return CheckResult("FAIL", "规范化表为空；空表必须显式标 MISSING，不得静默当正常")
    empty = [c for c in ("source", "as_of", "available_at", "fetched_at", "ingest_run_id", "quality")
             if _blank_mask(frame[c]).any()]
    if empty:
        return CheckResult("FAIL", f"字段存在空值 {empty}")
    return CheckResult("PASS", f"{len(frame)} 行，PIT 字段齐全非空")


def check_pit_order(frame: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 C2：时间口径正确——`available_at >= as_of`，日期格式按 kind 统一（§6.4/§6.5）。

    Args:
        frame: 规范化表。
        spec: 数据集配置。

    Returns:
        CheckResult: 出现未来可见时点早于数据自身时点、或日期格式串用 → FAIL。
    """
    if frame.empty:
        return CheckResult("SKIPPED", "空表，无时间口径可判")
    bad_order = 0
    bad_format = 0
    samples: list[str] = []
    for _, row in frame.iterrows():
        as_of = str(row["as_of"])
        avail = str(row["available_at"])
        if as_of[:10] > avail[:10]:
            bad_order += 1
            if len(samples) < 3:
                samples.append(f"as_of{as_of}>avail{avail}")
        if spec.kind in DATE_KINDS:
            if "T" in as_of:
                bad_format += 1
                if len(samples) < 3:
                    samples.append(f"as_of 含时间 {as_of}")
        elif "T" not in as_of:
            bad_format += 1
            if len(samples) < 3:
                samples.append(f"news as_of 非 ISO {as_of}")
    if bad_order:
        return CheckResult("FAIL", f"{bad_order} 行 available_at 早于 as_of；样例 {samples}")
    if bad_format:
        return CheckResult("FAIL", f"{bad_format} 行日期格式与 kind={spec.kind} 不符；样例 {samples}")
    return CheckResult("PASS", f"kind={spec.kind}：{len(frame)} 行时序与格式一致")


def check_key_unique(frame: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 C3：主键唯一（§6.6 去重）。

    Args:
        frame: 规范化表。
        spec: 数据集配置。

    Returns:
        CheckResult: 重复主键 → FAIL 并给出重复值。

    Notes:
        键按 kind 选：price/series/events 一个时点只能有一条；news 的同一时刻
        可以有多条不同快讯，去重口径是 `content_hash`（`normalize` 就是按它去重），
        拿 `as_of` 要求新闻唯一会把正常世界状态判成缺陷。
    """
    if frame.empty:
        return CheckResult("SKIPPED", "空表")
    keys = [column for column in (spec.key_columns or []) if column] or ["as_of"]
    missing = [column for column in keys if column not in frame.columns]
    if missing:
        return CheckResult("FAIL", f"缺主键列 {missing}，无法判唯一")
    dup = int(frame.duplicated(subset=keys, keep=False).sum())
    if dup:
        values = frame.loc[frame.duplicated(subset=keys), keys].astype(str).agg(" | ".join, axis=1)
        return CheckResult("FAIL", f"{dup} 行主键 {keys} 重复；样例 {sorted(set(values))[:5]}")
    return CheckResult("PASS", f"{len(frame)} 行主键 {keys} 唯一")


def check_price_range(frame: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 C4：数值落在配置声明的合理区间（单位随数据集，§6.6 单位异常）。

    Args:
        frame: 规范化表。
        spec: 数据集配置（`price_min`/`price_max` 为空则跳过）。

    Returns:
        CheckResult: 越界或不可解析 → FAIL。
    """
    if spec.price_min is None or spec.price_max is None:
        return CheckResult("SKIPPED", "配置未声明价格区间")
    column = "close" if "close" in frame.columns else next(iter(_value_columns(spec)), None)
    if column is None or frame.empty:
        return CheckResult("SKIPPED", "无数值列可判")
    series = pd.to_numeric(frame[column], errors="coerce")
    bad = series.isna() | (series < spec.price_min) | (series > spec.price_max)
    if bad.any():
        return CheckResult("FAIL", f"{int(bad.sum())} 行 {column} 不在 [{spec.price_min}, {spec.price_max}]")
    return CheckResult("PASS", f"{len(frame)} 行 {column} 在 [{spec.price_min}, {spec.price_max}]")


def check_row_quality(frame: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 C5：行级 `quality` 必须能由同一行 `flags` 复现（§6.5 两值口径）。

    复现规则与 `data/pit.py`/`normalize` 完全一致：`flags` 按分号拆开，
    **除 `MANUAL` 之外**的任一标记（`ZERO_*`/`MISSING_*`/`REVISED_ONLY`/
    `PROVISIONAL`/`NAN_IN_REQUIRED` 等）都要求该行 `DEGRADED`；只有 `MANUAL`
    或空标记的行要求 `GREEN`。`MANUAL` 表示「人工维护/需人工复核」，是披露而不是
    降级（§7.4）——把它当降级会把事件日历这类正常世界状态判成缺陷。

    Args:
        frame: 规范化表。
        spec: 数据集配置。

    Returns:
        CheckResult: 无降级标记却 DEGRADED，或带降级标记却 GREEN → FAIL。
    """
    if frame.empty:
        return CheckResult("SKIPPED", "空表")
    quality = frame["quality"].astype(str).str.strip()
    illegal_q = sorted(set(quality) - {"GREEN", "DEGRADED"})
    if illegal_q:
        return CheckResult("FAIL", f"行级 quality 出现三档值 {illegal_q}（行级只允许 GREEN/DEGRADED）")
    parts = frame["flags"].fillna("").astype(str).str.split(";")
    degrade_flag = parts.map(lambda items: any(
        item.strip() and item.strip() != "MANUAL" for item in items
    ))
    manual_only = int((~degrade_flag & parts.map(lambda items: any(i.strip() == "MANUAL" for i in items))).sum())
    degraded_with_no_flag = int(((quality == "DEGRADED") & ~degrade_flag).sum())
    green_with_flag = int(((quality == "GREEN") & degrade_flag).sum())
    if degraded_with_no_flag:
        return CheckResult("FAIL", f"{degraded_with_no_flag} 行 DEGRADED 但没有可复现的降级标记（flags 只有空值或 MANUAL）")
    if green_with_flag:
        return CheckResult("FAIL", f"{green_with_flag} 行带 ZERO_/MISSING_/REVISED_ONLY 等降级标记却标 GREEN")
    return CheckResult(
        "PASS",
        f"DEGRADED={int((quality == 'DEGRADED').sum())} 行，flags 可复现；MANUAL 仅披露不降档 {manual_only} 行",
    )


def check_no_fabricated_values(frame: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 C6：缺失不得被 0/NaN 冒充，结构性零必须带标记（§2.1-5、§6.6、§6.7）。

    Args:
        frame: 规范化表。
        spec: 数据集配置。

    Returns:
        CheckResult: 数值列出现 NaN；价格列出现 ≤0；量/持仓为 0 却没带 `ZERO_*`
            或 `MISSING_*` 标记 → 均 FAIL。

    Notes:
        只查数值列：`content_hash` 之类字符串列走 `to_numeric` 会全变 NaN，
        把判据变成「只要有文本列就恒红」。成交量/持仓的 0 是源事实（COMEX/WTI 全零），
        §6.6 的口径是标降级而不是剔行或判伪造，所以这里要求的是「0 必须被披露」。
    """
    if frame.empty:
        return CheckResult("SKIPPED", "空表")
    columns = _numeric_columns(spec, frame)
    if not columns:
        return CheckResult("SKIPPED", "没有数值列可判")
    price_columns = set(_price_columns(spec, frame))
    problems: list[str] = []
    blank_flags = _blank_mask(frame["flags"])
    for column in columns:
        series = pd.to_numeric(frame[column], errors="coerce")
        original_blank = _blank_mask(frame[column])
        nan_count = int((series.isna() & ~original_blank).sum()) + int(original_blank.sum())
        if nan_count:
            problems.append(f"{column} 有 {nan_count} 个空值")
        zeros = int((series == 0).sum())
        negatives = int((series < 0).sum())
        if column in price_columns and (zeros or negatives):
            problems.append(f"{column} 有 {zeros + negatives} 个非正值（价格类列不得为 0/负；0 一般是源缺值被填成 0）")
        if zeros and column not in price_columns:
            undisclosed = int(((series == 0) & blank_flags).sum())
            if undisclosed:
                problems.append(f"{column} 有 {undisclosed} 个 0 未标 ZERO_*/MISSING_*（降级未披露）")
    if problems:
        return CheckResult("FAIL", "；".join(problems))
    return CheckResult("PASS", f"数值列 {columns} 无空值；非正价格 0 行；结构性零均已标记")


def check_rejected_archive(frame: pd.DataFrame, rejected: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 C7：坏行必须留档、有原因，且不得以空值形态混回规范化表（§6.6）。

    Args:
        frame: 规范化表。
        rejected: `data/normalized/rejected/<dataset>.csv`；不存在时传空表。
        spec: 数据集配置。

    Returns:
        CheckResult: 缺原因 → FAIL；同一时点既被剔除、表内该时点的主值又是空值 → FAIL。

    Notes:
        「同一天先被剔、后被更新批次补齐」是允许的世界状态（US10Y 盘前空值当晚出最终值），
        留档必须继续可见，所以这里判的不是日期重叠本身，而是重叠后表内值是否为真空值——
        空值意味着剔除没生效或被 0 冒充。
    """
    if rejected.empty:
        return CheckResult("PASS", "无剔除记录")
    if "reject_reason" not in rejected.columns:
        return CheckResult("FAIL", "剔除表缺 reject_reason 列")
    blank_reason = int(_blank_mask(rejected["reject_reason"]).sum())
    if blank_reason:
        return CheckResult("FAIL", f"{blank_reason} 行剔除记录没有原因")
    key = "as_of" if "as_of" in rejected.columns else "date"
    overlap = set(rejected[key].astype(str)) & set(frame[key].astype(str)) if key in frame.columns else set()
    columns = _price_columns(spec, frame) or _numeric_columns(spec, frame)
    leaked: list[str] = []
    if overlap and columns:
        subset = frame[frame[key].astype(str).isin(overlap)]
        for column in columns:
            series = pd.to_numeric(subset[column], errors="coerce")
            if bool(series.isna().any()):
                leaked.append(f"{column}@{subset[key].iloc[0]}")
    if leaked:
        return CheckResult("FAIL", f"{len(overlap)} 个被剔除时点仍在表内且主值为空，剔除未生效；样例 {leaked[:3]}")
    reasons = sorted(set(rejected["reject_reason"].astype(str)))
    return CheckResult("PASS", f"剔除 {len(rejected)} 行留档，原因 {reasons}；其中 {len(overlap)} 个时点已被后续批次补出有效值")


def check_weekday_coverage(
    frame: pd.DataFrame,
    spec: DatasetConfig,
    min_coverage: float,
    calendars: dict[str, TradeCalendar] | None = None,
    disclosed: dict[str, Any] | None = None,
) -> CheckResult:
    """判据 N1：日频源覆盖率下限（§6.6 连续性）。

    分子是「命中的应交易日数（唯一日，非行数）」，分母是「区间内应交易日数」，
    与 `normalize.assess_dataset()` 的 `weekday_gaps` 同一口径——两处若用不同算法，
    同一个源会在验收卡和质量报告里给出两个覆盖率。行数直接当分子会出两种假象：
    快讯一天几十条把覆盖率推到几千percent，以及日历外的日期把结果抬过 100%。

    低于下限时不是一律判红：质量报告若已按**同一标记** `LOW_COVERAGE` 把该数据集
    判成非 GREEN，说明缺口已经披露、P2 只能展示不能进分数（2026-10-24 D4 乙），
    此时判 PASS 并在读数里写明「已披露」。**未披露**（报告里没有 `LOW_COVERAGE`
    ——比如有人把数据集标黄却是别的原因）仍然判红：这条豁免只认覆盖率这一件事。

    Args:
        frame: 规范化表。
        spec: 数据集配置（提供 `calendar` 口径）。
        min_coverage: 覆盖率下限（0–1）；来自 `application.yaml` 的 `acceptance`。
        calendars: 已加载日历；None 时按 weekday 近似（读数会写 `weekday_approx`）。
        disclosed: 该数据集在 `_quality.json` 里的披露信息，形如
            `{"flags": [...], "quality": "YELLOW"}`；None 表示报告里没有这一条。

    Returns:
        CheckResult: 覆盖率低于下限且未披露 → FAIL；已按 `LOW_COVERAGE` 披露 → PASS；
            区间内没有应交易日 → SKIPPED 并写原因。读数含唯一日数、应交易日数、
            命中数、覆盖率、口径与「分母外日数」。
    """
    dates = _frame_dates(frame, spec)
    expected, basis = expected_days(spec.calendar, dates[0], dates[-1], calendars)
    if not expected:
        return CheckResult("SKIPPED", f"{dates[0]}..{dates[-1]} 区间内 {basis} 没有应交易日，覆盖率无从谈起")
    observed = set(dates)
    expected_set = set(expected)
    hit = len(observed & expected_set)
    coverage = hit / len(expected_set)
    outside = len(observed - expected_set)
    detail = (
        f"{dates[0]}..{dates[-1]} 唯一日={len(observed)} 应交易日={len(expected_set)} 命中={hit} "
        f"覆盖率={coverage:.1%} 口径={basis} 分母外日={outside}"
    )
    if coverage < min_coverage:
        info = disclosed or {}
        flags = set(info.get("flags") or [])
        quality = info.get("quality")
        if "LOW_COVERAGE" in flags and quality not in (None, "GREEN"):
            return CheckResult(
                "PASS",
                f"覆盖率 {coverage:.1%} < 下限 {min_coverage:.0%}，质量报告已按 LOW_COVERAGE 披露"
                f"（quality={quality}；D4 乙：只展示、不进分数）；{detail}",
            )
        return CheckResult(
            "FAIL",
            f"覆盖率 {coverage:.1%} < 下限 {min_coverage:.0%}，且质量报告未按 LOW_COVERAGE 披露；{detail}",
        )
    return CheckResult("PASS", detail)


def check_recent_daily(frame: pd.DataFrame, spec: DatasetConfig, reference: date, max_stale_days: int) -> CheckResult:
    """判据 N2：核心日频源最近不得断供（§6.6 过期；单位：自然日）。

    Args:
        frame: 规范化表。
        spec: 数据集配置。
        reference: 验收基准日。
        max_stale_days: 允许的最大滞后天数。

    Returns:
        CheckResult: 核心源滞后超阈值 → FAIL；非核心只报读数。
    """
    latest = _frame_dates(frame, spec)[-1]
    lag = (reference - latest).days
    detail = f"最新 as_of={latest}，相对 {reference} 滞后 {lag} 天（core={spec.core}）"
    if spec.core and lag > max_stale_days:
        return CheckResult("FAIL", f"核心源滞后 {lag} 天 > {max_stale_days}；{detail}")
    if lag > max_stale_days:
        return CheckResult("PASS", f"非核心源滞后 {lag} 天，已在质量报告披露；{detail}")
    return CheckResult("PASS", detail)


def check_weekly_cadence(frame: pd.DataFrame, spec: DatasetConfig, low: float, high: float) -> CheckResult:
    """判据 N3：周频源按每年 40–60 行识别，不把周频误报成缺口（§6.6）。

    Args:
        frame: 规范化表。
        spec: 数据集配置。
        low: 每年行数下限。
        high: 每年行数上限。

    Returns:
        CheckResult: 行数/年落在区间外 → FAIL。
    """
    dates = _frame_dates(frame, spec)
    years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    per_year = len(dates) / years
    detail = f"{dates[0]}..{dates[-1]} 行数={len(dates)} 年数={years:.2f} 行数/年={per_year:.1f}"
    if not low <= per_year <= high:
        return CheckResult("FAIL", f"行数/年 {per_year:.1f} 不在 [{low}, {high}]；{detail}")
    return CheckResult("PASS", detail)


def check_replay_matches_source(normalized: pd.DataFrame, raw: pd.DataFrame, spec: DatasetConfig) -> CheckResult:
    """判据 S1：用原始快照重算规范化，与磁盘上的规范化表逐值对表（§17.4 确定性）。

    Args:
        normalized: 磁盘规范化表。
        raw: 最新原始快照（适配器产出的原始帧）。
        spec: 数据集配置。

    Returns:
        CheckResult: 同一时点数值不一致 → FAIL 并列出样例；原始快照无法重放 → SKIPPED。

    Notes:
        比较只用 `required_columns` 的数值列；PIT 列（fetched_at/run_id）不参与，
        因为它们本来就该随批次变化。
    """
    fetched = str(normalized["fetched_at"].iloc[-1]) if not normalized.empty else None
    run_id = str(normalized["ingest_run_id"].iloc[-1]) if not normalized.empty else None
    if not fetched or not run_id:
        return CheckResult("SKIPPED", "规范化表无 PIT 时间，无法定批重放")
    try:
        replay = normalize_frame(raw.copy(), spec, fetched_at=pd.Timestamp(fetched).to_pydatetime(),
                                 ingest_run_id=run_id).frame
    except Exception as exc:
        return CheckResult("SKIPPED", f"重放不可行：{exc}")
    if replay.empty:
        return CheckResult("SKIPPED", "重放结果为空表")
    columns = _value_columns(spec)
    left_frame = replay.drop_duplicates(subset=["as_of"], keep="last").set_index("as_of")
    right_frame = normalized.drop_duplicates(subset=["as_of"], keep="last").set_index("as_of")
    left = left_frame[columns]
    right = right_frame[columns]
    common = left.index.intersection(right.index)
    if common.empty:
        return CheckResult("FAIL", "重放结果与规范化表没有共同时点，无法对表")
    diff: list[str] = []
    for column in columns:
        a = left.loc[common, column].map(format_cell)
        b = right.loc[common, column].map(format_cell)
        mismatch = a != b
        for as_of in list(mismatch[mismatch].index)[:3]:
            diff.append(f"{as_of}/{column}: 重放={a.loc[as_of] or '空'} 表内={b.loc[as_of] or '空'}")
    if diff:
        return CheckResult("FAIL", f"重放对表不一致（共同时点 {len(common)}）；样例 {diff[:5]}")
    return CheckResult("PASS", f"重放与规范化表在 {len(common)} 个共同时点上逐格一致（按落盘格式 `format_cell`）")


def check_report_config_version(report: dict[str, Any], config_hash: str) -> CheckResult:
    """判据 Q3：验收用的配置与产出数据的配置必须是同一版本（§18.1 纪律 1）。

    Args:
        report: `_quality.json` 解析结果（内含产出它的 `config_hash`）。
        config_hash: 本次验收加载的配置哈希。

    Returns:
        CheckResult: 两个哈希不一致 → FAIL，说明在拿旧数据配新判据（或相反）；
            报告里没有 `config_hash` 也判红——版本不明即证据不明。
    """
    recorded = str(report.get("config_hash") or "")
    if not recorded:
        return CheckResult("FAIL", f"质量报告未记录 config_hash，无法证明与当前配置同版本（当前 {config_hash[:20]}…）")
    if recorded != config_hash:
        return CheckResult(
            "FAIL",
            f"配置版本不一致：数据产出用 {recorded[:20]}…，本次验收用 {config_hash[:20]}…；必须重跑 ingest 再验收",
        )
    return CheckResult("PASS", f"config_hash 一致（{recorded[:20]}…）")


def check_quality_report(report: dict[str, Any], specs: dict[str, DatasetConfig]) -> CheckResult:
    """判据 Q1：质量报告自身必须自洽（§6.6 质量闸口径）。

    Args:
        report: `_quality.json` 解析结果。
        specs: 数据集配置表。

    Returns:
        CheckResult: 核心源非 GREEN、RED 未列、降级未列、修订未标 → FAIL。
    """
    problems: list[str] = []
    datasets = report.get("datasets") or {}
    core = [n for n, s in specs.items() if s.core and n in datasets]
    for name in core:
        status = datasets[name].get("quality")
        if status != "GREEN":
            problems.append(f"核心源 {name} quality={status}")
    red = set(report.get("red") or [])
    for name, item in datasets.items():
        if item.get("quality") == "RED" and name not in red:
            problems.append(f"{name} 判 RED 但未进 red 列表")
        if item.get("status") in ("DEGRADED", "RED") and name not in set(report.get("degraded") or []) | red:
            problems.append(f"{name} 状态 {item.get('status')} 但未在 degraded/red 列表披露")
    for name, spec in specs.items():
        if spec.revised_only and name not in set(report.get("revised_only") or []):
            problems.append(f"{name} 配置 revised_only=true 但未进 revised_only 列表")
    empty = [n for n, item in datasets.items() if item.get("rows") == 0 and item.get("quality") == "GREEN"]
    if empty:
        problems.append(f"空表被判 GREEN：{empty}")
    if problems:
        return CheckResult("FAIL", "；".join(problems))
    return CheckResult("PASS", f"报告自洽：global={report.get('global_status')} red={sorted(red)} 核心={core}")


def check_raw_append_only(raw_root: Path, reference: date, run_started: datetime) -> CheckResult:
    """判据 Q2：原始快照只增不改（§6.4）——非基准日的文件不得在本次批次被写入。

    Args:
        raw_root: `data/raw` 根目录。
        reference: 验收基准日。
        run_started: 本次验收所用抓取批次的开始时间（带时区）。

    Returns:
        CheckResult: 历史日期目录里出现晚于批次开始的写入 → FAIL。
    """
    if not raw_root.exists():
        return CheckResult("FAIL", f"原始层根目录不存在：{raw_root}")
    violations: list[str] = []
    touched = 0
    for source in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        for day in sorted(p for p in source.iterdir() if p.is_dir()):
            is_reference = day.name == reference.isoformat()
            for path in day.rglob("*"):
                if not path.is_file():
                    continue
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=run_started.tzinfo)
                if not is_reference and mtime >= run_started:
                    violations.append(f"{path.relative_to(raw_root)} @{mtime.isoformat()}")
                elif is_reference:
                    touched += 1
    if violations:
        return CheckResult("FAIL", f"{len(violations)} 个历史快照在本批次被改写；样例 {violations[:3]}")
    return CheckResult("PASS", f"基准日 {reference} 目录内 {touched} 个新快照，历史目录未被改写")


def classify_cadence(frame: pd.DataFrame, spec: DatasetConfig) -> str:
    """按相邻行日期间隔自行判定节奏，返回 ``daily``/``weekly``。

    Args:
        frame: 规范化表。
        spec: 数据集配置（仅用于报错信息）。

    Returns:
        str: 中位间隔 ≥5 天判 `weekly`，否则 `daily`；空表返回 `daily`。

    Notes:
        不依赖质量报告的 `cadence_hint`：`_quality.json` 里根本没有 continuity 节点，
        照抄报告会让周频源（CFTC）被套上日频覆盖率下限——这是假判红。判据要能自己算；
        算的时候调 `normalize.cadence_of`，与覆盖率下限的判定共用同一把尺。
    """
    dates = _frame_dates(frame, spec)
    return cadence_of(sorted(set(dates)))


def _latest_raw_csv(raw_root: Path, source: str, dataset: str, reference: date) -> pd.DataFrame | None:
    """取某数据集在基准日的最新原始 CSV 快照。

    Args:
        raw_root: `data/raw` 根目录。
        source: 源标识目录名（配置 `source`）。
        dataset: 数据集名（文件名前缀）。
        reference: 验收基准日。

    Returns:
        pd.DataFrame | None: 原始帧；目录或 CSV 不存在（如 GPR 的 `.xls`）时返回 None。

    Notes:
        返回 None 只表示「本轮没有可比对的输入」，调用方必须记 SKIPPED，不得记 PASS。
    """
    day = raw_root / source / reference.isoformat()
    if not day.is_dir():
        return None
    files = sorted((p for p in day.glob(f"{dataset}.*.csv") if p.is_file()),
                   key=lambda p: (p.stat().st_mtime, p.name))
    if not files:
        return None
    return pd.read_csv(files[-1])


def run_p1_acceptance(
    config: LoadedConfig,
    reference: str | None = None,
    calendars: dict[str, TradeCalendar] | None = None,
) -> dict[str, Any]:
    """执行 P1 全部程序化判据并汇总验收报告。

    Args:
        config: 已加载配置（含 `acceptance` 阈值与数据集契约）。
        reference: 验收基准日 `YYYY-MM-DD`；None 时取质量报告的 `reference_date`。
        calendars: 缺口分母日历；None 时从 `paths.reference` 读盘。读不到官方日历
            时 N1 回落工作日近似，读数会写明口径，不冒充官方交易日。

    Returns:
        dict: ``{"stage","verdict","checks","counts","metrics","config_hash","run_id"}``；
        `verdict` ∈ ``PASS/FAIL``；`checks[].status` 含 `PASS/FAIL/SKIPPED`。
        另含 ``calendar`` 节点：每种口径的完整性、行数、首末日与抓取时间。

    Raises:
        GoldError: 质量报告缺失或不可读（没有报告就无法判质量，不允许蒙混）。

    Notes:
        判据分三组：一致性（C1–C7、S1）、连续性（N1–N3）、质量（Q1–Q2）。
        阈值全部来自配置，代码里不写魔法数（§15.5）。
    """
    store_root = config.path("normalized")
    report_path = store_root / "_quality.json"
    if not report_path.exists():
        raise GoldError(f"缺质量报告 {report_path}；先跑 ingest 再验收")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ref_text = reference or report.get("reference_date") or date.today().isoformat()
    ref_date = date.fromisoformat(ref_text)
    acceptance = config.application.acceptance
    raw_root = config.path("raw")
    if calendars is None:
        calendars = load_all_calendars(config.path("reference"))
    started = datetime.fromisoformat(str(report.get("generated_at")))
    checks: list[dict[str, str]] = []

    def add(group: str, rule_id: str, name: str, result: CheckResult) -> None:
        """追加一条判据读数。

        Args:
            group: 判据组（一致性/连续性/质量）。
            rule_id: 稳定编号，供 AGENTS 与看板引用。
            name: 判据中文名。
            result: 判据结果。
        """
        checks.append({"group": group, "rule_id": rule_id, "name": name, **result.as_dict()})

    specs = {n: s for n, s in config.data_sources.datasets.items() if s.enabled}
    for name, spec in sorted(specs.items()):
        frame = pd.read_csv(store_root / f"{name}.csv") if (store_root / f"{name}.csv").exists() else pd.DataFrame()
        rejected_path = store_root / "rejected" / f"{name}.csv"
        rejected = pd.read_csv(rejected_path) if rejected_path.exists() else pd.DataFrame()
        add("一致性", "C1", f"{name}·PIT 字段", check_pit_columns(frame, spec))
        if not frame.empty:
            add("一致性", "C2", f"{name}·时间口径", check_pit_order(frame, spec))
            add("一致性", "C3", f"{name}·主键唯一", check_key_unique(frame, spec))
            add("一致性", "C4", f"{name}·数值区间", check_price_range(frame, spec))
            add("一致性", "C5", f"{name}·行级质量可复现", check_row_quality(frame, spec))
            add("一致性", "C6", f"{name}·缺失未被冒充", check_no_fabricated_values(frame, spec))
            add("一致性", "C7", f"{name}·剔除留档有效", check_rejected_archive(frame, rejected, spec))
        if not frame.empty and name in (report.get("datasets") or {}):
            if spec.kind == "events":
                # 事件日历是「有事件才有行」的事件流，既不是日频也不是周期序列：
                # 覆盖率、断供、行数/年 三种节奏判据都不适用，硬套会把正常的
                # 稀疏事件判成缺陷（§7.3：GEO_EVENT_SEVERITY=0 只表示未识别到事件）。
                add("连续性", "N1", f"{name}·日历覆盖率", CheckResult(
                    "SKIPPED", "events 是事件流，覆盖率分母不适用；空日历由 C1 要求显式标 MISSING"))
                add("连续性", "N2", f"{name}·最近不断供", CheckResult(
                    "SKIPPED", "events 无「断供」概念：没有新事件不等于源故障，陈旧度由质量闸按 stale_days 判"))
            elif classify_cadence(frame, spec) == "weekly":
                add("连续性", "N3", f"{name}·周频节奏", check_weekly_cadence(
                    frame, spec, acceptance.weekly_rows_per_year_min, acceptance.weekly_rows_per_year_max))
            else:
                if spec.kind == "news":
                    add("连续性", "N1", f"{name}·日历覆盖率", CheckResult(
                        "SKIPPED", "news 是事件流（一天多条），覆盖率分母不适用；时效由 N2 判"))
                else:
                    payload = (report.get("datasets") or {}).get(name) or {}
                    disclosed = {
                        "flags": list(((payload.get("quality_detail") or {}).get("flags")) or []),
                        "quality": payload.get("quality"),
                    }
                    add("连续性", "N1", f"{name}·日历覆盖率", check_weekday_coverage(
                        frame, spec, acceptance.min_weekday_coverage, calendars, disclosed))
                add("连续性", "N2", f"{name}·最近不断供", check_recent_daily(frame, spec, ref_date, acceptance.max_stale_days))
        if spec.kind in ("price", "series") and not frame.empty:
            raw = _latest_raw_csv(raw_root, spec.source, name, ref_date)
            if raw is None:
                add("一致性", "S1", f"{name}·原始重放对表", CheckResult("SKIPPED", f"{spec.source}/{ref_date} 无 CSV 原始快照"))
            else:
                add("一致性", "S1", f"{name}·原始重放对表", check_replay_matches_source(frame, raw, spec))
    add("质量", "Q1", "质量报告自洽", check_quality_report(report, specs))
    add("质量", "Q2", "原始层只增不改", check_raw_append_only(raw_root, ref_date, started))
    add("质量", "Q3", "验收与数据同版本", check_report_config_version(report, config.config_hash))

    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "FAIL", "SKIPPED")}
    verdict = "FAIL" if counts["FAIL"] else "PASS"
    return {
        "stage": "P1",
        "verdict": verdict,
        "reference_date": ref_text,
        "quality_run_id": report.get("run_id"),
        "quality_scope": report.get("scope"),
        "config_hash": config.config_hash,
        "thresholds": {
            "min_weekday_coverage": acceptance.min_weekday_coverage,
            "max_stale_days": acceptance.max_stale_days,
            "weekly_rows_per_year": [acceptance.weekly_rows_per_year_min, acceptance.weekly_rows_per_year_max],
        },
        "counts": counts,
        "calendar": {
            basis: {
                "integrity": cal.integrity,
                "available": cal.available,
                "rows": cal.rows,
                "as_of_min": cal.as_of_min.isoformat() if cal.as_of_min else None,
                "as_of_max": cal.as_of_max.isoformat() if cal.as_of_max else None,
                "fetched_at": cal.fetched_at,
                "source": cal.source,
                "note": cal.note,
            }
            for basis, cal in calendars.items()
        },
        "failed": [c for c in checks if c["status"] == "FAIL"],
        "skipped": [c for c in checks if c["status"] == "SKIPPED"],
        "checks": checks,
    }
