"""PIT（Point-in-Time）字段生成与截断。

抓取时刻早于 ``available_at`` 的行标 ``PROVISIONAL``：它来自盘中快照，可能还会被
收盘后正式值修正，不能作为最终读数进入事实包。
"""
from __future__ import annotations

import pandas as pd

from ..common.timeutil import available_at_for, iso, parse_date, to_shanghai
from ..config.loader import DatasetConfig
from .models import PIT_COLUMNS


def _append_flag(value: object, flag: str) -> str:
    """把 ``flag`` 追加到分号分隔的 flags 字符串，已存在则不重复。

    Args:
        value: 原 flags 单元格；NaN/None 视为空。
        flag: 要追加的标记，如 ``PROVISIONAL``。

    Returns:
        分号分隔的标记串；原值为空时只返回 ``flag``。
    """
    base = "" if pd.isna(value) else str(value)
    parts = [part for part in base.split(";") if part]
    if flag not in parts:
        parts.append(flag)
    return ";".join(parts)


def apply_pit(
    frame: pd.DataFrame,
    spec: DatasetConfig,
    fetched_at,
    ingest_run_id: str,
) -> pd.DataFrame:
    """为每一行补 source/as_of/available_at/fetched_at/ingest_run_id/vintage/quality/flags。

    输入：``frame`` 为规范化后的表，price/series/events 需含 ``as_of``（日期），
    news 需含 ``as_of``（带时区时间戳）；``explicit`` 规则额外要求 ``available_at`` 列。
    ``fetched_at`` 必须带时区，作为 ``fetched_at`` 列与 ``fetched_at`` 规则的落点。
    输出：列顺序为「业务列 + ``PIT_COLUMNS``」的副本，时间戳一律是 Asia/Shanghai
    的 ISO-8601 字符串（纯日期为 ``YYYY-MM-DD``），空表也补齐 PIT 列。

    ``available_at`` 口径：非 ``explicit`` 规则按 ``spec.available_at_rule`` 从
    ``as_of`` 推算（自然日，不查交易日历）；``explicit`` 规则（手工事件表、快讯）
    直接采用表里的 ``available_at``，并与手工层校验器共用 ``parse_datetime``——
    裸日期按 Asia/Shanghai 零点补齐，带偏移的值保留原偏移后换算到上海。

    Returns:
        pd.DataFrame: 带全套 PIT 列的副本；``revised_only`` 数据集整表
        ``quality=DEGRADED`` 且 ``flags`` 前缀 ``REVISED_ONLY``。

    Raises:
        ValueError: ``fetched_at`` 为 naive（由 ``to_shanghai`` 抛出）；
            ``available_at_rule=explicit`` 但某行 ``available_at`` 为空
            （消息含数据源、第几条记录与该行的 ``as_of``，便于定位手工行）；
            规则名未知或日期无法解析。

    Notes:
        行号按表内顺序从 1 起计数，与手工 CSV 的表头无关；本函数只生成 PIT 列，
        不做 ``available_at <= cutoff`` 截断（那是 ``filter_available`` 的职责）。
        ``fetched_at < available_at`` 的行会追加 ``PROVISIONAL`` 标记；下游事实包
        必须用 ``filter_fact_pack`` 同时剔除临时行。
    """
    if frame.empty:
        result = frame.copy()
        for column in PIT_COLUMNS:
            if column not in result.columns:
                result[column] = pd.Series(dtype="object")
        return result

    result = frame.copy()
    fetched = to_shanghai(fetched_at)
    result["fetched_at"] = iso(fetched)
    result["source"] = spec.source
    result["ingest_run_id"] = ingest_run_id
    result["vintage"] = "REVISED_ONLY" if spec.revised_only else ""

    if spec.kind == "news":
        as_of_values = result["as_of"].map(lambda v: iso(to_shanghai(pd.Timestamp(v).to_pydatetime())))
    else:
        as_of_values = result["as_of"].map(lambda v: parse_date(v).isoformat())
    result["as_of"] = as_of_values

    explicit = result["available_at"] if "available_at" in result.columns else pd.Series([None] * len(result))
    available = []
    provisional = []
    for index, (as_of_text, explicit_value) in enumerate(zip(as_of_values, explicit), start=1):
        if spec.available_at_rule == "explicit":
            # 缺失判定用 pd.isna 而不是 isinstance(float)：手工表经 pandas 解析后
            # 空值可能是 None/NaN/NaT 三种，任何一种都必须显式报错，不得当“看不见”。
            if pd.isna(explicit_value):
                raise ValueError(
                    f"{spec.source} 使用 explicit 规则但第 {index} 条记录缺少 available_at"
                    f"（as_of={as_of_text}）；手工 CSV 必须填写可见时点"
                )
            # 与 available_at_for/手工校验器共用 parse_datetime：裸日期按
            # Asia/Shanghai 补齐，不能一处放行、一处把整条日链打死。
            available_dt = available_at_for(
                parse_date(as_of_text), "explicit", fetched, explicit=explicit_value
            )
        else:
            as_of_date = parse_date(as_of_text)
            available_dt = available_at_for(as_of_date, spec.available_at_rule, fetched)
        available.append(iso(available_dt))
        # 盘中抓取：抓取时刻早于该行可见时点，数据可能还会被收盘后快照修正。
        provisional.append(fetched < available_dt)
    result["available_at"] = available
    if any(provisional):
        existing = result["flags"] if "flags" in result.columns else pd.Series([""] * len(result))
        result["flags"] = [
            _append_flag(value, "PROVISIONAL") if is_provisional else ("" if pd.isna(value) else str(value))
            for value, is_provisional in zip(existing, provisional)
        ]

    base_flags = "REVISED_ONLY" if spec.revised_only else ""
    result["quality"] = "DEGRADED" if spec.revised_only else "GREEN"
    if base_flags:
        existing = result["flags"] if "flags" in result.columns else pd.Series([""] * len(result))
        result["flags"] = [
            ";".join(part for part in (base_flags, str(value or "")) if part)
            for value in existing
        ]
    elif "flags" not in result.columns:
        result["flags"] = ""

    ordered = [column for column in result.columns if column not in PIT_COLUMNS] + PIT_COLUMNS
    return result[ordered]
