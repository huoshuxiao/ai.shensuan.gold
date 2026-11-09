"""契约层：data.normalize.assess_dataset 的数据质量分级契约。

覆盖场景：新鲜 GREEN、过期 YELLOW、零成交 YELLOW、工作日缺口计数、
GPR 仅修订版 YELLOW、空事件 YELLOW、无效行 RED、历史/近期拒绝行区分、
近期缺失值 YELLOW。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from com.sun.shensuan.gold.data.models import Quality
from com.sun.shensuan.gold.data.normalize import assess_dataset, normalize_frame, summarize_continuity


def _normalized(repo_config, frame, spec_name, fetched="2024-01-08 16:00"):
    """测试辅助：按数据集规格规范化给定 DataFrame，返回 NormalizeResult 供质量评估。"""
    spec = repo_config.data_sources.datasets[spec_name]
    return normalize_frame(frame, spec, pd.Timestamp(fetched, tz="Asia/Shanghai"), "run-q")


def test_fresh_price_is_green(repo_config, etf_frame):
    """验证参考日当天的新鲜价格数据评为 GREEN，stale_days 与工作日缺口均为 0。"""
    result = _normalized(repo_config, etf_frame, "etf_518880")
    spec = repo_config.data_sources.datasets["etf_518880"]
    quality = assess_dataset(result.frame, spec, reference_date=date(2024, 1, 8))
    assert quality.status == Quality.GREEN
    assert quality.as_of_max == "2024-01-08"
    assert quality.stale_days == 0
    assert quality.weekday_gaps == 0


def test_stale_is_yellow(repo_config, etf_frame):
    """验证参考日远晚于最后数据日时评为 YELLOW 并带 STALE 标记。"""
    result = _normalized(repo_config, etf_frame, "etf_518880")
    spec = repo_config.data_sources.datasets["etf_518880"]
    quality = assess_dataset(result.frame, spec, reference_date=date(2024, 2, 1))
    assert quality.status == Quality.YELLOW
    assert "STALE" in quality.flags


def test_zero_volume_is_yellow(repo_config, etf_frame):
    """验证零成交行使整体评为 YELLOW、zero_rows=1；零成交不能当真实流动性。"""
    frame = etf_frame.copy()
    frame.loc[2, "volume"] = 0
    result = _normalized(repo_config, frame, "etf_518880")
    spec = repo_config.data_sources.datasets["etf_518880"]
    quality = assess_dataset(result.frame, spec, reference_date=date(2024, 1, 8))
    assert quality.status == Quality.YELLOW
    assert "ZERO_VOLUME" in quality.flags
    assert quality.zero_rows == 1


def test_weekday_gaps_are_counted(repo_config, etf_frame):
    """验证缺失工作日被计数（2 天）并给出缺口样例，缺口必须可见而非静默补行。"""
    frame = etf_frame.loc[[0, 3, 4]].copy()
    result = _normalized(repo_config, frame, "etf_518880")
    spec = repo_config.data_sources.datasets["etf_518880"]
    quality = assess_dataset(result.frame, spec, reference_date=date(2024, 1, 8))
    assert quality.weekday_gaps == 2
    assert quality.weekday_gap_sample == ["2024-01-03", "2024-01-04"]


def test_gpr_is_revised_only_yellow(repo_config):
    """验证仅修订版 GPR 整表 vintage=REVISED_ONLY、quality=DEGRADED，数据集评为 YELLOW。"""
    spec = repo_config.data_sources.datasets["gpr"]
    raw = pd.DataFrame({"date": ["2026-10-12", "2026-10-13"], "gprd": [100.0, 110.0]})
    result = normalize_frame(raw, spec, pd.Timestamp("2026-10-19 23:00", tz="Asia/Shanghai"), "run-g")
    quality = assess_dataset(result.frame, spec, reference_date=date(2026, 10, 19))
    assert result.status == Quality.YELLOW
    assert quality.status == Quality.YELLOW
    assert "REVISED_ONLY" in quality.flags
    assert set(result.frame["vintage"]) == {"REVISED_ONLY"}
    assert set(result.frame["quality"]) == {"DEGRADED"}


def test_empty_events_is_yellow(repo_config):
    """验证空事件表评为 YELLOW/EMPTY；空表示未识别到事件，不等于无地缘风险。"""
    spec = repo_config.data_sources.datasets["geo_events"]
    result = normalize_frame(pd.DataFrame(), spec, pd.Timestamp("2026-10-19 08:00", tz="Asia/Shanghai"), "run-e")
    quality = assess_dataset(result.frame, spec)
    assert quality.status == Quality.YELLOW
    assert "EMPTY" in quality.flags


def test_invalid_rows_make_red(repo_config, etf_frame):
    """验证存在无效行（invalid_rows=1）时质量评为 RED，触发只减不加。"""
    result = _normalized(repo_config, etf_frame, "etf_518880")
    spec = repo_config.data_sources.datasets["etf_518880"]
    quality = assess_dataset(result.frame, spec, reference_date=date(2024, 1, 8), invalid_rows=1)
    assert quality.status == Quality.RED


def test_historical_rejected_rows_stay_visible(repo_config, etf_frame):
    """验证历史拒绝行只计数、不影响近期质量：仍为 GREEN，但 rejected_rows 与标记可见。"""
    result = _normalized(repo_config, etf_frame, "etf_518880")
    spec = repo_config.data_sources.datasets["etf_518880"]
    rejected = pd.DataFrame({"as_of": ["2023-01-03"], "reject_reason": ["OHLC_INVALID"]})
    quality = assess_dataset(
        result.frame,
        spec,
        reference_date=date(2024, 1, 8),
        invalid_rows=1,
        rejected=rejected,
    )
    assert quality.status == Quality.GREEN
    assert quality.rejected_rows == 1
    assert quality.recent_invalid_rows == 0
    assert "INVALID_ROWS_DROPPED" in quality.flags


def test_recent_rejected_rows_make_red(repo_config, etf_frame):
    """验证参考日当天的拒绝行评为 RED 并带 RECENT_INVALID_ROWS，近期污染必须阻断加仓。"""
    result = _normalized(repo_config, etf_frame, "etf_518880")
    spec = repo_config.data_sources.datasets["etf_518880"]
    rejected = pd.DataFrame({"as_of": ["2024-01-08"], "reject_reason": ["OHLC_INVALID"]})
    quality = assess_dataset(
        result.frame,
        spec,
        reference_date=date(2024, 1, 8),
        invalid_rows=1,
        rejected=rejected,
    )
    assert quality.status == Quality.RED
    assert quality.recent_invalid_rows == 1
    assert "RECENT_INVALID_ROWS" in quality.flags


def test_rejected_row_between_recent_valid_dates_is_red(repo_config):
    """验证夹在最近有效行之间的拒绝日仍算近期坏点，不得因主表缺该日而漏判。

    负对照对象：``assess_dataset`` 的“最近”窗口。若仍用尾部有效行的日期集合，
    被剔除的 2026-10-22（夹在 10-21 与 10-23 之间）不在集合内，会得到
    recent_invalid_rows=0、状态 YELLOW；改为日期区间后应计为 1 并判 RED。
    """
    spec = repo_config.data_sources.datasets["wti_cl"]
    raw = pd.DataFrame(
        {
            "date": [
                "2026-10-19",
                "2026-10-20",
                "2026-10-21",
                "2026-10-22",
                "2026-10-23",
            ],
            "open": [99.03, 98.23, 98.16, 97.69, 99.08],
            "high": [99.71, 101.37, 100.40, 100.17, 100.97],
            "low": [96.72, 95.25, 96.26, 97.72, 98.84],
            "close": [98.23, 98.16, 97.69, 99.08, 100.35],
            "volume": [0, 0, 0, 0, 0],
            "open_interest": [0, 0, 0, 0, 0],
        }
    )
    result = _normalized(repo_config, raw, "wti_cl", fetched="2026-10-23 13:28:09")
    quality = assess_dataset(
        result.frame,
        spec,
        reference_date=date(2026, 10, 23),
        invalid_rows=result.dropped_rows,
        rejected=result.rejected,
    )
    assert quality.status == Quality.RED
    assert quality.recent_invalid_rows == 1
    assert "RECENT_INVALID_ROWS" in quality.flags


def test_superseded_rejected_date_is_not_recent_missing(repo_config):
    """验证同一日期后来已有有效值时，旧拒绝留档不再计入 recent_missing_rows。

    负对照对象：``assess_dataset`` 对拒绝日的过滤；若不剔除已存在于主表的日期，
    2026-10-19 的旧 NAN_IN_REQUIRED 会让 recent_missing_rows 从 1 变成 2。
    """
    spec = repo_config.data_sources.datasets["us10y"]
    raw = pd.DataFrame({"date": ["2026-10-16", "2026-10-19"], "us10y": [5.0, 5.15]})
    result = normalize_frame(
        raw, spec, pd.Timestamp("2026-10-21 06:30", tz="Asia/Shanghai"), "run-q"
    )
    rejected = pd.DataFrame(
        {
            "as_of": ["2026-10-19", "2026-10-20"],
            "reject_reason": ["NAN_IN_REQUIRED", "NAN_IN_REQUIRED"],
        }
    )
    quality = assess_dataset(
        result.frame,
        spec,
        reference_date=date(2026, 10, 21),
        invalid_rows=2,
        rejected=rejected,
    )
    assert quality.rejected_rows == 2
    assert quality.recent_missing_rows == 1
    assert "RECENT_MISSING_ROWS" in quality.flags
    assert quality.status == Quality.YELLOW


def test_existing_provisional_row_is_flagged_by_quality(repo_config):
    """验证旧 CSV 没有 PROVISIONAL 标记时，质量闸仍按 fetched_at < available_at 动态识别。"""
    spec = repo_config.data_sources.datasets["us10y"]
    frame = pd.DataFrame(
        {
            "as_of": ["2026-10-20"],
            "us10y": ["5.1"],
            "fetched_at": ["2026-10-21T00:40:00+08:00"],
            "available_at": ["2026-10-21T06:00:00+08:00"],
            "quality": ["GREEN"],
            "flags": [""],
        }
    )
    quality = assess_dataset(frame, spec, reference_date=date(2026, 10, 21))
    assert quality.status == Quality.YELLOW
    assert "PROVISIONAL" in quality.flags
    assert "PROVISIONAL" in quality.recent_flags


def test_recent_missing_row_is_yellow(repo_config):
    """验证参考日主值缺失（NaN 被剔除）评为 YELLOW，recent_missing_rows=1 且标记可见。"""
    spec = repo_config.data_sources.datasets["us10y"]
    raw = pd.DataFrame(
        {
            "date": ["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
            "us10y": [4.0, 4.1, 4.2, None],
        }
    )
    result = normalize_frame(raw, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-q")
    quality = assess_dataset(
        result.frame,
        spec,
        reference_date=date(2024, 1, 8),
        invalid_rows=result.dropped_rows,
        rejected=result.rejected,
    )
    assert quality.status == Quality.YELLOW
    assert quality.recent_missing_rows == 1
    assert "RECENT_MISSING_ROWS" in quality.flags


def test_continuity_summary_flags_sparse_daily_and_ignores_weekly():
    """验证连续性摘要：SPDR 这类日频稀疏源给 warning，CFTC 周频源不误报。"""
    report = {
        "spdr_gold": {
            "quality_detail": {
                "rows": 2894,
                "as_of_min": "2004-11-19",
                "as_of_max": "2026-10-22",
                "weekday_gaps": 2829,
                "weekday_gap_sample": ["2005-08-03"],
            }
        },
        "cftc_gold": {
            "quality_detail": {
                "rows": 1940,
                "as_of_min": "1986-01-15",
                "as_of_max": "2026-10-13",
                "weekday_gaps": 8690,
                "weekday_gap_sample": ["1986-01-16"],
            }
        },
    }
    summary = summarize_continuity(report)
    by_name = {item["dataset"]: item for item in summary}
    assert by_name["spdr_gold"]["cadence_hint"] == "daily"
    assert by_name["spdr_gold"]["warning"]
    assert by_name["cftc_gold"]["cadence_hint"] == "weekly"
    assert by_name["cftc_gold"]["warning"] is None
