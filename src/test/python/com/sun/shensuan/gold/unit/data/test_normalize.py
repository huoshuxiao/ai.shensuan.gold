"""单元层：data.normalize 的规范化、PIT 字段与质量标记。

覆盖场景：OHLC/零价/零成交剔除与标记、重复日期保留最新、NaN 主值剔除、
快讯去重、事件 severity 非法、空事件可见、缺必需列报错、行级 quality 两值口径。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from com.sun.shensuan.gold.data.models import Quality
from com.sun.shensuan.gold.data.normalize import assess_dataset, normalize_frame


def test_price_normalize_pit_and_flags(repo_config, etf_frame):
    """验证价格表规范化：非法 OHLC 与零价行剔除留痕，零成交行保留但标 DEGRADED，PIT 字段与 available_at 正确。"""
    spec = repo_config.data_sources.datasets["etf_518880"]
    raw = etf_frame.copy()
    raw.loc[1, "low"] = 5.00  # low > min(open, close)
    raw.loc[2, "volume"] = 0
    raw.loc[3, "close"] = 0.0
    result = normalize_frame(raw, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-1")
    frame = result.frame
    assert result.status == Quality.YELLOW
    assert result.dropped_rows == 2
    assert len(result.rejected) == 2
    assert set(result.rejected["reject_reason"]) == {"OHLC_INVALID", "PRICE_OUT_OF_RANGE"}
    assert "OHLC_OR_PRICE_INVALID" in result.flags
    assert "INVALID_ROWS_DROPPED" in result.flags
    assert "ZERO_VOLUME" in result.flags
    assert len(frame) == 3
    assert set(frame["as_of"]) == {"2024-01-02", "2024-01-04", "2024-01-08"}
    zero_row = frame.loc[frame["as_of"] == "2024-01-04"].iloc[0]
    assert "ZERO_VOLUME" in zero_row["flags"]
    assert zero_row["quality"] == "DEGRADED"
    assert frame.iloc[0]["available_at"] == "2024-01-02T15:30:00+08:00"
    for column in ("source", "as_of", "available_at", "fetched_at", "ingest_run_id", "vintage", "quality", "flags"):
        assert column in frame.columns


def test_row_quality_is_green_or_degraded_only(repo_config, etf_frame):
    """验证规范化表的行级 quality 只有 GREEN/DEGRADED 两值，数据集三档不写进行级列。

    行级 `quality` 是 PIT 的可用性标记，不是 `DatasetQuality.status` 的
    GREEN/YELLOW/RED；两者混用会让 P2 事实包按三档枚举误判合法行。
    """
    spec = repo_config.data_sources.datasets["etf_518880"]
    raw = etf_frame.copy()
    raw.loc[raw.index[-1], "volume"] = 0
    result = normalize_frame(
        raw, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-row-quality"
    )
    values = set(result.frame["quality"])
    assert values == {"GREEN", "DEGRADED"}
    assert result.status in {Quality.GREEN, Quality.YELLOW, Quality.RED}


def test_zero_fields_missing_stays_missing(repo_config):
    """验证零值字段缺失时保持空值并标 MISSING_*，不得填 0 冒充真实零成交/零持仓。"""
    spec = repo_config.data_sources.datasets["comex_gc"]
    raw = pd.DataFrame(
        {
            "date": ["2026-10-14", "2026-10-15", "2026-10-16"],
            "open": [4200.0, 4210.0, 4220.0],
            "high": [4220.0, 4230.0, 4240.0],
            "low": [4190.0, 4200.0, 4210.0],
            "close": [4210.0, 4220.0, 4230.0],
            "volume": [None, None, None],
            "open_interest": [None, None, None],
        }
    )
    result = normalize_frame(raw, spec, pd.Timestamp("2026-10-16 20:00", tz="Asia/Shanghai"), "run-1")
    frame = result.frame
    assert len(frame) == 3
    assert frame["volume"].isna().all()
    assert frame["open_interest"].isna().all()
    assert "MISSING_VOLUME" in result.flags
    assert "MISSING_OPEN_INTEREST" in result.flags
    assert not any(flag.startswith("ZERO_") for flag in result.flags)
    assert all("MISSING_VOLUME" in flags for flags in frame["flags"])
    assert result.status == Quality.YELLOW


def test_provisional_when_fetched_before_available(repo_config):
    """验证抓取早于 available_at 的行标 PROVISIONAL、行质量 DEGRADED、数据集 YELLOW。"""
    spec = repo_config.data_sources.datasets["us10y"]
    raw = pd.DataFrame({"date": ["2026-10-20"], "us10y": [5.1]})
    result = normalize_frame(
        raw, spec, pd.Timestamp("2026-10-21 00:40", tz="Asia/Shanghai"), "run-early"
    )
    row = result.frame.iloc[0]
    assert row["available_at"] == "2026-10-21T06:00:00+08:00"
    assert "PROVISIONAL" in row["flags"]
    assert row["quality"] == "DEGRADED"
    assert "PROVISIONAL" in result.flags
    assert result.status == Quality.YELLOW


def test_final_fetch_after_available_is_not_provisional(repo_config):
    """验证在 available_at 之后抓取的行不带 PROVISIONAL，行质量保持 GREEN。"""
    spec = repo_config.data_sources.datasets["us10y"]
    raw = pd.DataFrame({"date": ["2026-10-20"], "us10y": [5.1]})
    result = normalize_frame(
        raw, spec, pd.Timestamp("2026-10-21 06:30", tz="Asia/Shanghai"), "run-final"
    )
    row = result.frame.iloc[0]
    assert row["flags"] == ""
    assert row["quality"] == "GREEN"
    assert "PROVISIONAL" not in result.flags
    assert result.status == Quality.GREEN


def test_duplicate_date_keeps_last(repo_config, etf_frame):
    """验证同一日期重复行保留最后一条（close=4.85），避免旧快照覆盖新快照。"""
    spec = repo_config.data_sources.datasets["etf_518880"]
    raw = pd.concat([etf_frame, etf_frame.iloc[[0]].assign(close=4.85)], ignore_index=True)
    result = normalize_frame(raw, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-1")
    row = result.frame.loc[result.frame["as_of"] == "2024-01-02"].iloc[0]
    assert float(row["close"]) == pytest.approx(4.85)


def test_series_drops_nan_main_value(repo_config):
    """验证时间序列主值 NaN 的行被剔除并标 YELLOW/NAN_IN_REQUIRED，禁止 NaN 冒充真实读数。"""
    spec = repo_config.data_sources.datasets["us10y"]
    raw = pd.DataFrame({"date": ["2026-10-16", "2026-10-19"], "us10y": [5.09, None]})
    result = normalize_frame(raw, spec, pd.Timestamp("2026-10-19 08:00", tz="Asia/Shanghai"), "run-1")
    assert len(result.frame) == 1
    assert result.status == Quality.YELLOW
    assert "NAN_IN_REQUIRED" in result.flags


def test_news_dedup_and_available_at(repo_config):
    """验证快讯按内容哈希去重（3 行留 2 行），且 available_at 取发布时间而非抓取时间。"""
    spec = repo_config.data_sources.datasets["news_sina"]
    raw = pd.DataFrame(
        {
            "published_at": ["2026-10-19 10:00:00", "2026-10-19 10:00:00", "2026-10-19 11:00:00"],
            "title": ["a", "a", "b"],
            "content_hash": ["sha256:x", "sha256:x", "sha256:y"],
        }
    )
    result = normalize_frame(raw, spec, pd.Timestamp("2026-10-19 12:00", tz="Asia/Shanghai"), "run-1")
    assert len(result.frame) == 2
    assert result.frame["available_at"].tolist() == result.frame["as_of"].tolist()


def test_events_reject_bad_severity(repo_config):
    """验证事件表出现 severity=4 时整表 RED，仅保留合法行并标 MANUAL；非法严重度不得进入质量记录。"""
    spec = repo_config.data_sources.datasets["geo_events"]
    raw = pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "date": ["2026-10-01", "2026-10-02"],
            "type": ["war", "sanction"],
            "severity": ["3", "4"],
            "region": ["X", "Y"],
            "source_url": ["http://a", "http://b"],
            "available_at": ["2026-10-01T12:00:00+08:00", "2026-10-02T12:00:00+08:00"],
        }
    )
    result = normalize_frame(raw, spec, pd.Timestamp("2026-10-03 08:00", tz="Asia/Shanghai"), "run-1")
    assert result.status == Quality.RED
    assert len(result.frame) == 1
    assert result.frame.iloc[0]["event_id"] == "e1"
    assert result.frame.iloc[0]["flags"] == "MANUAL"


def test_empty_events_is_visible_not_fake(repo_config):
    """验证空事件表返回 YELLOW 且带 EMPTY 标记、DataFrame 为空；空不等于无风险，必须可见。"""
    spec = repo_config.data_sources.datasets["geo_events"]
    result = normalize_frame(pd.DataFrame(), spec, pd.Timestamp("2026-10-19 08:00", tz="Asia/Shanghai"), "run-1")
    assert result.status == Quality.YELLOW
    assert "EMPTY" in result.flags
    assert result.frame.empty


def test_missing_required_column_raises(repo_config, etf_frame):
    """负向验证价格表缺 close 列抛 AdapterError：缺列不能静默补 0 或跳过。"""
    from com.sun.shensuan.gold.common.errors import AdapterError

    spec = repo_config.data_sources.datasets["etf_518880"]
    raw = etf_frame.drop(columns=["close"])
    with pytest.raises(AdapterError):
        normalize_frame(raw, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-1")


def test_date_column_is_iso_date(repo_config):
    """验证源 date 列带 T00:00:00 时，规范化后 date/as_of 都统一为 YYYY-MM-DD。"""
    spec = repo_config.data_sources.datasets["gpr"]
    raw = pd.DataFrame({"date": ["2026-10-16T00:00:00"], "gprd": [100.0]})
    result = normalize_frame(
        raw, spec, pd.Timestamp("2026-10-19 08:00", tz="Asia/Shanghai"), "run-date"
    )
    row = result.frame.iloc[0]
    assert row["date"] == "2026-10-16"
    assert row["as_of"] == "2026-10-16"


def test_series_all_zero_rows_dropped_when_enabled(repo_config):
    """D1 乙：声明 `drop_all_zero_rows` 的 series 数据集，必需数值列全 0 的行整行剔除留档。

    放行格：只有 long=0、net/short 非 0 的行必须保留（「某一列为 0」是真实读数）。
    判红格（负对照）：全 0 行若留下，`check_no_fabricated_values` 会判它「0 未披露」。
    """
    spec = repo_config.data_sources.datasets["cftc_gold"]
    raw = pd.DataFrame(
        {
            "date": ["2003-02-11", "2003-02-18", "2003-02-25"],
            "net": [1000, 0, -500],
            "long": [0, 0, 1500],
            "short": [1000, 0, 2000],
        }
    )
    result = normalize_frame(
        raw, spec, pd.Timestamp("2026-10-24 06:00", tz="Asia/Shanghai"), "run-all-zero"
    )
    assert len(result.frame) == 2
    assert set(result.frame["as_of"]) == {"2003-02-11", "2003-02-25"}
    assert result.rejected["as_of"].tolist() == ["2003-02-18"]
    assert set(result.rejected["reject_reason"]) == {"ALL_ZERO_IN_REQUIRED"}
    assert "ALL_ZERO_IN_REQUIRED" in result.flags
    assert result.dropped_rows == 1
    assert result.status == Quality.YELLOW


def test_all_zero_drop_is_opt_in_so_gpr_zero_days_survive(repo_config):
    """全局强推会删掉真实读数：GPR 的 gprd=0 是平静日，未开启开关的数据集必须保留。"""
    gpr = repo_config.data_sources.datasets["gpr"]
    raw = pd.DataFrame({"date": ["2025-02-09"], "gprd": [0.0]})
    result = normalize_frame(raw, gpr, pd.Timestamp("2026-10-24 06:00", tz="Asia/Shanghai"), "run-gpr")
    assert len(result.frame) == 1
    assert result.rejected.empty
    assert gpr.drop_all_zero_rows is False


def test_price_nonpositive_declared_column_dropped(repo_config):
    """D3 乙：AU0 的 settlement<=0 整行剔除留档；>0 放行，缺值走 NAN_IN_REQUIRED。

    三条原因必须能分开：settlement=0 是「源给 0」、缺失是「源给空」、开高低收越界是结构错。
    """
    spec = repo_config.data_sources.datasets["shfe_au0"]
    raw = pd.DataFrame(
        {
            "date": ["2008-01-09", "2008-01-10", "2010-06-08", "2010-06-09"],
            "open": [200, 201, 260, 261],
            "high": [205, 206, 265, 266],
            "low": [198, 199, 258, 259],
            "close": [203, 204, 262, 263],
            "volume": [100, 100, 100, 100],
            "open_interest": [1000, 1000, 1000, 1000],
            "settlement": [0, None, 261, 262],
        }
    )
    result = normalize_frame(
        raw, spec, pd.Timestamp("2026-10-24 15:30", tz="Asia/Shanghai"), "run-settle"
    )
    assert set(result.frame["as_of"]) == {"2010-06-08", "2010-06-09"}
    assert set(result.rejected["as_of"]) == {"2008-01-09", "2008-01-10"}
    assert set(result.rejected["reject_reason"]) == {"NONPOSITIVE_SETTLEMENT", "NAN_IN_REQUIRED"}
    assert "NONPOSITIVE_SETTLEMENT" in result.flags
    assert result.dropped_rows == 2


def test_low_coverage_marks_dataset_degraded_only_when_floor_given(repo_config):
    """D4 乙：覆盖率低于下限时数据集标 LOW_COVERAGE 并降 YELLOW；不给下限则不定档。

    夹具是「每 2 个工作日一条」的日频序列（覆盖率约 50%）：给下限 → YELLOW +
    LOW_COVERAGE；不给下限 → GREEN。另一格用周频序列证明周频源不被这条下限误伤
    （它的「缺口」是节奏，不是缺数据；按周频判则不能出 LOW_COVERAGE）。
    """
    spec = repo_config.data_sources.datasets["spdr_gold"]
    sparse_daily = pd.bdate_range("2024-01-01", "2024-12-31", freq="2B")
    frame = pd.DataFrame(
        {
            "as_of": sparse_daily.strftime("%Y-%m-%d").tolist(),
            "holdings_tonnes": [1000.0] * len(sparse_daily),
            "flags": [""] * len(sparse_daily),
        }
    )
    floored = assess_dataset(frame, spec, reference_date=date(2025, 1, 2), coverage_floor=0.80)
    assert floored.status == Quality.YELLOW
    assert "LOW_COVERAGE" in floored.flags
    no_floor = assess_dataset(frame, spec, reference_date=date(2025, 1, 2))
    assert no_floor.status == Quality.GREEN
    assert "LOW_COVERAGE" not in no_floor.flags

    weekly = pd.date_range("2024-01-01", "2024-12-31", freq="7D")
    weekly_frame = pd.DataFrame(
        {
            "as_of": weekly.strftime("%Y-%m-%d").tolist(),
            "holdings_tonnes": [1000.0] * len(weekly),
            "flags": [""] * len(weekly),
        }
    )
    weekly_result = assess_dataset(weekly_frame, spec, reference_date=date(2025, 1, 2), coverage_floor=0.80)
    assert "LOW_COVERAGE" not in weekly_result.flags
    assert weekly_result.status == Quality.GREEN
