"""单元层：acceptance.p1 的每条 P1 判据都要有「放行 1 格 + 判红 ≥1 格」。

覆盖场景：PIT 字段齐全、时序与格式、主键唯一、数值区间、行级质量可复现、
缺失不被 0/NaN 冒充、剔除留档有效、工作日覆盖率、核心不断供、周频不误报、
原始重放逐格对表、质量报告自洽、原始层只增不改、原始快照按修改时间取最新。
负对照一律动被试数据（注入错误值），不动判据本身；每条判据同时给一格真值放行，
防止「什么都没测」的恒绿。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from com.sun.shensuan.gold.acceptance.p1 import (
    CheckResult,
    _latest_raw_csv,
    check_batch_freshness,
    check_key_unique,
    classify_cadence,
    check_no_fabricated_values,
    check_pit_columns,
    check_pit_order,
    check_price_range,
    check_quality_report,
    check_report_config_version,
    check_raw_append_only,
    check_recent_daily,
    check_rejected_archive,
    check_replay_matches_source,
    check_row_quality,
    check_weekday_coverage,
    check_weekly_cadence,
)
from com.sun.shensuan.gold.common.errors import GoldError
from com.sun.shensuan.gold.config.loader import DatasetConfig
from com.sun.shensuan.gold.data.trade_calendar import TradeCalendar


def _spec(**over) -> DatasetConfig:
    """构造一个最小可用的数据集契约，默认日频价格型、核心源。

    Args:
        over: 覆盖字段；用于造出不同 kind/区间/核心的判据场景。

    Returns:
        DatasetConfig: 通过 schema 校验的配置对象。
    """
    base = {
        "source": "akshare.unit_test",
        "adapter": "unit_price",
        "kind": "price",
        "core": True,
        "required_columns": ["date", "open", "high", "low", "close"],
        "available_at_rule": "same_day_1530",
        "stale_days": 4,
        "key_columns": ["as_of"],
        "price_min": 1.0,
        "price_max": 2000.0,
    }
    base.update(over)
    return DatasetConfig(**base)


def _frame(dates: list[str], **over) -> pd.DataFrame:
    """构造带 PIT 列的规范化表；默认字段齐全、时序合法、无标记。

    Args:
        dates: `as_of` 与 `date` 同步使用的日期列表（YYYY-MM-DD）。
        over: 需要覆盖的列（如 quality/flags/close/volume）。

    Returns:
        pd.DataFrame: 可直接喂给各判据的合成夹具。
    """
    n = len(dates)
    fetched = over.pop("fetched_at", ["2026-10-23T19:22:36+08:00"] * n)
    available = over.pop("available_at", [d + "T15:30:00+08:00" for d in dates])
    data = {
        "date": dates,
        "open": [2.0] * n,
        "high": [3.0] * n,
        "low": [1.5] * n,
        "close": [2.5] * n,
        "source": ["akshare.unit_test"] * n,
        "as_of": dates,
        "available_at": available,
        "fetched_at": fetched,
        "ingest_run_id": ["unit-run"] * n,
        "vintage": [float("nan")] * n,
        "quality": ["GREEN"] * n,
        "flags": [float("nan")] * n,
    }
    data.update(over)
    return pd.DataFrame(data)


def test_pit_columns_pass_and_fail_missing_column():
    """PIT 齐全判据：完整表放行；缺 available_at 必须判红。"""
    ok = _frame(["2026-10-20", "2026-10-21"])
    assert check_pit_columns(ok, _spec()).status == "PASS"
    broken = ok.drop(columns=["available_at"])
    result = check_pit_columns(broken, _spec())
    assert result.status == "FAIL" and "available_at" in result.detail


def test_pit_columns_fail_on_blank_value_not_string_nan():
    """空值负对照：把 source 真的清空必须判红；flags 为空（NaN）不得被算成有值。"""
    blank = _frame(["2026-10-20"])
    blank.loc[0, "source"] = ""
    assert "空值" in check_pit_columns(blank, _spec()).detail
    assert check_pit_columns(_frame(["2026-10-20"]), _spec()).status == "PASS"


def test_pit_columns_fail_on_empty_frame():
    """空表不能静默当正常：必须判红并写出空表语义。"""
    result = check_pit_columns(_frame([]), _spec())
    assert result.status == "FAIL" and "空" in result.detail


def test_pit_order_fail_when_available_before_as_of():
    """未来数据混入的负对照：available_at 早于 as_of 必须判红。"""
    frame = _frame(["2026-10-20"], available_at=["2026-10-19T15:30:00+08:00"])
    result = check_pit_order(frame, _spec())
    assert result.status == "FAIL" and "available_at" in result.detail


def test_pit_order_fail_when_date_carries_time_for_price_kind():
    """price 分支的 `as_of` 必须是 YYYY-MM-DD；带时间即格式串用，判红。"""
    frame = _frame(["2026-10-20"])
    frame.loc[0, "as_of"] = "2026-10-20T00:00:00"
    assert check_pit_order(frame, _spec()).status == "FAIL"


def test_pit_order_news_kind_requires_iso_timezone():
    """news 分支必须用带时区 ISO-8601；裸日期判红，合法 ISO 放行。"""
    spec = _spec(kind="news", required_columns=["published_at", "title"], price_min=None, price_max=None)
    assert check_pit_order(_frame(["2026-10-20"]), spec).status == "FAIL"
    iso = _frame(["2026-10-20"])
    iso.loc[0, "as_of"] = "2026-10-20T09:30:00+08:00"
    iso.loc[0, "available_at"] = "2026-10-20T09:30:00+08:00"
    assert check_pit_order(iso, spec).status == "PASS"


def test_key_unique_fail_on_duplicate_as_of():
    """价格/序列同一时点两条记录＝重复；正常一行放行。"""
    assert check_key_unique(_frame(["2026-10-20", "2026-10-20"]), _spec()).status == "FAIL"
    assert check_key_unique(_frame(["2026-10-20", "2026-10-21"]), _spec()).status == "PASS"


def test_key_unique_uses_configured_key_columns():
    """主键按配置 `key_columns` 判，不硬编码：新闻按 (as_of, content_hash)、事件按 event_id。

    同一时刻两条不同 `content_hash` 的快讯必须放行；`content_hash` 相同才判红。
    反向负对照：事件日历同一天两条不同 `event_id` 是正常世界状态（10-19 既有
    胡塞空袭又有加美贸易谈判破裂），硬按 `as_of` 唯一会把真事件判成缺陷。
    """
    news_spec = _spec(
        kind="news",
        required_columns=["published_at", "title", "content_hash"],
        key_columns=["as_of", "content_hash"],
        price_min=None,
        price_max=None,
    )
    same_time = _frame(["2026-07-30T15:29:32+08:00", "2026-07-30T15:29:32+08:00"])
    same_time.loc[:, "as_of"] = same_time["date"]
    same_time.loc[:, "available_at"] = same_time["date"]
    same_time["content_hash"] = ["aaaa", "bbbb"]
    assert check_key_unique(same_time, news_spec).status == "PASS"
    same_time["content_hash"] = ["aaaa", "aaaa"]
    result = check_key_unique(same_time, news_spec)
    assert result.status == "FAIL" and "content_hash" in result.detail

    events_spec = _spec(
        kind="events",
        required_columns=["event_id", "date", "type", "severity", "region", "source_url"],
        key_columns=["event_id"],
        price_min=None,
        price_max=None,
    )
    two_events = _frame(["2026-10-19", "2026-10-19"])
    two_events["event_id"] = ["XGE-ge-2-a", "XGE-ge-2-b"]
    assert check_key_unique(two_events, events_spec).status == "PASS"
    two_events["event_id"] = ["XGE-ge-2-a", "XGE-ge-2-a"]
    assert check_key_unique(two_events, events_spec).status == "FAIL"


def test_row_quality_manual_flag_is_disclosure_not_degradation():
    """MANUAL 只表示需人工复核：带 MANUAL 且 GREEN 放行；只有 MANUAL 却 DEGRADED 判红。"""
    manual = _frame(["2026-10-19", "2026-10-20"], flags=["MANUAL", "MANUAL"])
    assert check_row_quality(manual, _spec()).status == "PASS"
    mislabeled = _frame(["2026-10-19"], flags=["MANUAL"], quality=["DEGRADED"])
    result = check_row_quality(mislabeled, _spec())
    assert result.status == "FAIL" and "MANUAL" in result.detail
    mixed = _frame(["2026-10-19"], flags=["MANUAL;ZERO_VOLUME"])
    assert check_row_quality(mixed, _spec()).status == "FAIL"


def test_price_range_fail_out_of_band():
    """单位异常负对照：把 6.718 写成 671.8（未除以 100 的形状）必须被判红。"""
    spec = _spec(price_min=1.0, price_max=20.0)
    assert check_price_range(_frame(["2026-10-20"], close=[671.8]), spec).status == "FAIL"
    assert check_price_range(_frame(["2026-10-20"], close=[6.718]), spec).status == "PASS"


def test_row_quality_passes_when_flags_absent_even_if_nan():
    """NaN 的 flags 是「没有标记」，不得被读成字符串 nan 而判红（恒红的旧 bug）。"""
    assert check_row_quality(_frame(["2026-10-20"]), _spec()).status == "PASS"


def test_row_quality_fail_when_degraded_without_flags():
    """行级 DEGRADED 必须能由 flags 复现：无标记却降级判红。"""
    assert check_row_quality(_frame(["2026-10-20"], quality=["DEGRADED"]), _spec()).status == "FAIL"


def test_row_quality_fail_when_flagged_but_green():
    """反向负对照：带 flags 却标 GREEN 同样判红。"""
    assert check_row_quality(_frame(["2026-10-20"], flags=["ZERO_VOLUME"]), _spec()).status == "FAIL"


def test_row_quality_fail_on_third_status_value():
    """行级 quality 只允许两值：出现 YELLOW（数据集三档值）即判红。"""
    assert check_row_quality(_frame(["2026-10-20"], quality=["YELLOW"]), _spec()).status == "FAIL"


def test_no_fabricated_values_fail_on_nan_and_zero_price():
    """缺失不得被 0 或 NaN 冒充：两种污染都要判红，干净表放行。"""
    nan_frame = _frame(["2026-10-20", "2026-10-21"], close=[2.5, float("nan")])
    assert check_no_fabricated_values(nan_frame, _spec()).status == "FAIL"
    assert check_no_fabricated_values(_frame(["2026-10-20"], close=[0.0]), _spec()).status == "FAIL"
    assert check_no_fabricated_values(_frame(["2026-10-20"]), _spec()).status == "PASS"


def test_no_fabricated_values_treats_structural_zero_as_disclosure_duty():
    """成交量为 0：带 ZERO_VOLUME 放行（源结构性事实），不带标记判红（降级未披露）。"""
    spec = _spec(required_columns=["date", "open", "high", "low", "close", "volume"])
    flagged = _frame(["2026-10-20"], volume=[0.0], flags=["ZERO_VOLUME"], quality=["DEGRADED"])
    assert check_no_fabricated_values(flagged, spec).status == "PASS"
    hidden = _frame(["2026-10-20"], volume=[0.0])
    result = check_no_fabricated_values(hidden, spec)
    assert result.status == "FAIL" and "未标" in result.detail


def test_no_fabricated_values_skips_text_columns():
    """字符串列不参与数值判据：content_hash 有值却全非数字，不得被 to_numeric 判成空值。"""
    spec = _spec(kind="news", required_columns=["published_at", "title"], price_min=None, price_max=None)
    news = _frame(["2026-10-20T09:30:00+08:00"])
    news.loc[:, "as_of"] = news["date"]
    news["content_hash"] = ["d41d8cd98f00b204"]
    assert check_no_fabricated_values(news, spec).status == "PASS"


def test_rejected_archive_fail_when_value_still_blank():
    """被剔除时点若又出现在表内且主值为空＝剔除没生效，判红；已补出有效值放行。"""
    frame = _frame(["2026-10-22"], close=[float("nan")])
    rejected = pd.DataFrame([{"as_of": "2026-10-22", "close": 2.5, "reject_reason": "OHLC_INVALID"}])
    assert check_rejected_archive(frame, rejected, _spec()).status == "FAIL"
    healed = _frame(["2026-10-22"])
    result = check_rejected_archive(healed, rejected, _spec())
    assert result.status == "PASS" and "已被后续批次补出" in result.detail


def test_rejected_archive_fail_on_blank_reason():
    """剔除必须有原因：空原因判红；有原因放行；无剔除记录放行。"""
    frame = _frame(["2026-10-20"])
    blank = pd.DataFrame([{"as_of": "2026-10-19", "close": 2.5, "reject_reason": ""}])
    assert check_rejected_archive(frame, blank, _spec()).status == "FAIL"
    good = pd.DataFrame([{"as_of": "2026-10-19", "close": 2.5, "reject_reason": "OHLC_INVALID"}])
    assert check_rejected_archive(frame, good, _spec()).status == "PASS"
    assert check_rejected_archive(frame, pd.DataFrame(), _spec()).status == "PASS"


def test_weekday_coverage_fail_below_floor():
    """覆盖率低于下限要判红：一周只有 1 行时 20% < 80%。"""
    dates = ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"]
    result = check_weekday_coverage(_frame(dates), _spec(), 0.80)
    assert result.status == "FAIL" and "覆盖率" in result.detail
    dense = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
    assert check_weekday_coverage(_frame(dense), _spec(), 0.80).status == "PASS"


def test_coverage_denominator_follows_declared_calendar():
    """同一份数据在两种分母下必须给出不同判决，否则日历是死代码。

    夹具形状（合成，不声称与 2026 年真实节假日一一对应）：一段 8 天「长假」
    落在周一至周五中间。按自然工作日算是 7/15=46.7%（判红）；按合成的
    上交所日历是 7/7=100%（放行）。第三格再从真交易日里删掉两天（5/7=71.4%），
    证明换成日历不是「一律放行」。
    """
    holidays = {date(2026, 10, day) for day in range(1, 9)}
    weekdays = [
        date(2026, 9, 30),
        *[date(2026, 10, day) for day in (1, 2, 5, 6, 7, 8, 9)],
        *[date(2026, 10, day) for day in range(12, 17)],
    ]
    sse_days = [day for day in weekdays if day not in holidays]
    calendar = TradeCalendar(
        basis="cn_sse",
        days=frozenset(sse_days),
        integrity="OK",
        note="unit fixture",
        rows=len(sse_days),
        as_of_min=min(sse_days),
        as_of_max=max(sse_days),
    )
    dates = [day.isoformat() for day in sse_days]
    frame = _frame(dates)

    weekday_spec = _spec(calendar="weekday")
    under_weekday = check_weekday_coverage(frame, weekday_spec, 0.80)
    assert under_weekday.status == "FAIL"
    assert "weekday_approx" in under_weekday.detail

    cn_spec = _spec(calendar="cn_sse")
    under_sse = check_weekday_coverage(frame, cn_spec, 0.80, {"cn_sse": calendar})
    assert under_sse.status == "PASS", under_sse.detail
    assert "口径=cn_sse" in under_sse.detail

    # 日历口径下真缺两天，仍要判红（日历不是免检章）。
    shorter = _frame([day for day in dates if day not in ("2026-10-14", "2026-10-15")])
    missing_two = check_weekday_coverage(shorter, cn_spec, 0.80, {"cn_sse": calendar})
    assert missing_two.status == "FAIL" and "应交易日=7" in missing_two.detail

    # 声明了 cn_sse 但日历没落盘：必须回落近似并在读数里写原因，不得冒充官方。
    fallback = check_weekday_coverage(frame, cn_spec, 0.80, None)
    assert "weekday_approx" in fallback.detail and "日历未加载" in fallback.detail


def test_coverage_counts_unique_dates_and_discloses_extra_days():
    """覆盖率分子按唯一日计，日历外的日期单列披露——一天多条不得把结果抬过 100%。

    夹具：2026-10-19（周一）重复两行、10-20 一行、10-24（周六）一行。
    区间内周一至周五有 5 天，命中 2 天 → 40% 判红；若按行数算会得出 80%「放行」，
    重复行与周六那条就被洗成了覆盖。
    """
    frame = _frame(["2026-10-19", "2026-10-19", "2026-10-20", "2026-10-24"])
    result = check_weekday_coverage(frame, _spec(), 0.80)
    assert result.status == "FAIL", result.detail
    assert "唯一日=3" in result.detail and "应交易日=5" in result.detail
    assert "命中=2" in result.detail and "覆盖率=40.0%" in result.detail
    assert "分母外日=1" in result.detail


def test_coverage_skipped_when_denominator_empty():
    """分母为空的区间必须 SKIPPED 并写原因，不能算成 0%（判红）也不能算成 100%。"""
    # 2026-10-17 周六、10-18 周日：周一至周五一个都没有。
    result = check_weekday_coverage(_frame(["2026-10-17"]), _spec(), 0.80)
    assert result.status == "SKIPPED" and "没有应交易日" in result.detail


def test_coverage_below_floor_passes_only_when_low_coverage_disclosed():
    """D4 乙：低于下限只在「质量报告已按 LOW_COVERAGE 披露」时放行；其余三种仍判红。

    四格：①已披露且判 YELLOW → PASS；②报告里没这条 → FAIL；
    ③标黄但原因是 STALE（借不到这条豁免）→ FAIL；④带 LOW_COVERAGE 却仍报 GREEN
    （自相矛盾）→ FAIL。最后两格是这条豁免的牙：它只认「覆盖率」这一件事。
    """
    dates = ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"]
    frame = _frame(dates)

    disclosed = check_weekday_coverage(
        frame, _spec(), 0.80, None, {"flags": ["LOW_COVERAGE"], "quality": "YELLOW"}
    )
    assert disclosed.status == "PASS" and "已按 LOW_COVERAGE 披露" in disclosed.detail

    undisclosed = check_weekday_coverage(frame, _spec(), 0.80, None, {"flags": [], "quality": "GREEN"})
    assert undisclosed.status == "FAIL" and "未按 LOW_COVERAGE 披露" in undisclosed.detail

    other_reason = check_weekday_coverage(
        frame, _spec(), 0.80, None, {"flags": ["STALE"], "quality": "YELLOW"}
    )
    assert other_reason.status == "FAIL"

    contradictory = check_weekday_coverage(
        frame, _spec(), 0.80, None, {"flags": ["LOW_COVERAGE"], "quality": "GREEN"}
    )
    assert contradictory.status == "FAIL"


def test_classify_cadence_separates_weekly_from_daily():
    """节奏自判负对照：周频夹具必须判 weekly，否则会被日频 80% 覆盖率下限假判红。"""
    spec = _spec(kind="series", required_columns=["date", "us10y"], core=False, price_min=None, price_max=None)
    weekly = [(date(2021, 1, 4) + timedelta(days=7 * i)).isoformat() for i in range(60)]
    assert classify_cadence(_frame(weekly), spec) == "weekly"
    daily = [(date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(20)]
    assert classify_cadence(_frame(daily), spec) == "daily"
    sparse_daily = [(date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(3)]
    assert classify_cadence(_frame(sparse_daily), spec) == "daily"


def test_recent_daily_core_stale_fails_noncore_only_discloses():
    """核心源断供判红；同样的滞后落在非核心源只披露不阻断。"""
    frame = _frame(["2026-10-01"])
    core = check_recent_daily(frame, _spec(core=True), date(2026, 10, 23), 5)
    assert core.status == "FAIL" and "核心源滞后" in core.detail
    noncore = check_recent_daily(frame, _spec(core=False), date(2026, 10, 23), 5)
    assert noncore.status == "PASS" and "非核心" in noncore.detail


def test_weekly_cadence_does_not_misreport_weekly_source():
    """周频不误报：40–60 行/年放行；日频形状误标周频要判红。"""
    weekly_dates = [(date(2021, 1, 4) + timedelta(days=7 * i)).isoformat() for i in range(260)]
    spec = _spec(kind="series", required_columns=["date", "us10y"], core=False, price_min=None, price_max=None)
    assert check_weekly_cadence(_frame(weekly_dates), spec, 40.0, 60.0).status == "PASS"
    too_many = [(date(2021, 1, 4) + timedelta(days=i)).isoformat() for i in range(730)]
    assert check_weekly_cadence(_frame(too_many), spec, 40.0, 60.0).status == "FAIL"


def test_replay_uses_stored_float_format_not_binary_equality():
    """重放对表按落盘格式比：648.54/100 的二进制形状与表内 6.4854 必须算一致。"""
    spec = _spec(kind="series", required_columns=["date", "us10y"], price_min=None, price_max=None)
    raw = pd.DataFrame([{"date": "2021-10-01", "us10y": 648.54 / 100}])
    stored = _frame(["2021-10-01"])
    stored["us10y"] = [6.4854]
    stored = stored[["date", "us10y", "source", "as_of", "available_at", "fetched_at",
                     "ingest_run_id", "vintage", "quality", "flags"]]
    assert check_replay_matches_source(stored, raw, spec).status == "PASS"


def test_replay_fail_when_stored_value_differs_from_raw():
    """重放对表：磁盘值被改过一个落盘档位也要被抓出来。"""
    spec = _spec()
    raw = pd.DataFrame([
        {"date": "2026-10-20", "open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5},
        {"date": "2026-10-21", "open": 2.0, "high": 3.0, "low": 1.5, "close": 2.6},
    ])
    stored = _frame(["2026-10-20", "2026-10-21"], close=[2.5, 2.6])
    assert check_replay_matches_source(stored, raw, spec).status == "PASS"
    tampered = stored.copy()
    tampered.loc[1, "close"] = 2.7
    result = check_replay_matches_source(tampered, raw, spec)
    assert result.status == "FAIL" and "close" in result.detail


def test_latest_raw_csv_picks_newest_by_mtime_not_name(tmp_path):
    """同日多批次快照：必须按修改时间取最新，文件名序会把旧批次当成最新（旧 bug）。"""
    day = date(2026, 10, 23)
    d = tmp_path / "akshare.unit_test" / day.isoformat()
    d.mkdir(parents=True)
    older = d / "etf.p1-zzz-old.csv"
    newer = d / "etf.p1-aaa-new.csv"
    older.write_text("date,open,high,low,close\n2026-10-20,2,3,1.5,2.5\n", encoding="utf-8")
    newer.write_text("date,open,high,low,close\n2026-10-20,2,3,1.5,9.9\n", encoding="utf-8")
    stamp_old = datetime(2026, 10, 23, 13, 0, 0).timestamp()
    stamp_new = datetime(2026, 10, 23, 19, 22, 0).timestamp()
    os.utime(older, (stamp_old, stamp_old))
    os.utime(newer, (stamp_new, stamp_new))
    frame = _latest_raw_csv(tmp_path, "akshare.unit_test", "etf", day)
    assert frame is not None and float(frame["close"].iloc[0]) == 9.9
    assert _latest_raw_csv(tmp_path, "akshare.unit_test", "missing_dataset", day) is None


def test_quality_report_fail_when_core_not_green_or_red_unlisted():
    """质量报告自洽判据：核心非 GREEN、RED 未列、空表判 GREEN 都要判红。"""
    specs = {"etf_518880": _spec(), "wti_cl": _spec(core=False)}
    good = {"global_status": "OK", "datasets": {
        "etf_518880": {"quality": "GREEN", "status": "OK", "rows": 10},
        "wti_cl": {"quality": "RED", "status": "DEGRADED", "rows": 9},
    }, "red": ["wti_cl"], "degraded": ["wti_cl"], "revised_only": []}
    assert check_quality_report(good, specs).status == "PASS"
    core_bad = {"datasets": {"etf_518880": {"quality": "YELLOW", "status": "DEGRADED", "rows": 10},
                             "wti_cl": {"quality": "GREEN", "status": "OK", "rows": 9}},
                "red": [], "degraded": ["etf_518880"], "revised_only": []}
    assert "核心源" in check_quality_report(core_bad, specs).detail
    hidden_red = {"datasets": {"etf_518880": {"quality": "GREEN", "status": "OK", "rows": 10},
                               "wti_cl": {"quality": "RED", "status": "DEGRADED", "rows": 9}},
                  "red": [], "degraded": [], "revised_only": []}
    assert check_quality_report(hidden_red, specs).status == "FAIL"
    empty_green = {"datasets": {"etf_518880": {"quality": "GREEN", "status": "OK", "rows": 0},
                                "wti_cl": {"quality": "GREEN", "status": "OK", "rows": 9}},
                   "red": [], "degraded": [], "revised_only": []}
    assert "空表" in check_quality_report(empty_green, specs).detail


def test_report_config_version_three_arms():
    """Q3 同版本闸：一致放行、不一致判红、报告没记哈希也判红。

    不一致这一格挡的是「拿旧数据配新判据」——改一次 `strategy.yaml` 或
    `data_sources.yaml` 后不重跑 ingest，旧 `_quality.json` 的结论就属于另一个配置。
    """
    assert check_report_config_version({"config_hash": "sha256:aaaa"}, "sha256:aaaa").status == "PASS"
    mismatch = check_report_config_version({"config_hash": "sha256:aaaa"}, "sha256:bbbb")
    assert mismatch.status == "FAIL" and "重跑 ingest" in mismatch.detail
    missing = check_report_config_version({}, "sha256:bbbb")
    assert missing.status == "FAIL" and "未记录" in missing.detail


def test_quality_report_fail_when_revised_only_not_disclosed():
    """配置声明 revised_only 却没进报告列表＝修订被洗白，判红。"""
    specs = {"gpr": _spec(kind="series", required_columns=["date", "gprd"], revised_only=True,
                          core=False, price_min=None, price_max=None)}
    report = {"global_status": "DEGRADED", "datasets": {"gpr": {"quality": "YELLOW", "status": "DEGRADED", "rows": 5}},
              "red": [], "degraded": ["gpr"], "revised_only": []}
    result = check_quality_report(report, specs)
    assert result.status == "FAIL" and "revised_only" in result.detail


def test_raw_append_only_fail_when_history_rewritten(tmp_path):
    """历史日期目录在本批次被改写＝覆盖原始快照，判红；只动基准日目录放行。"""
    started = datetime(2026, 10, 23, 19, 0, 0)
    old = tmp_path / "akshare.unit_test" / "2026-10-21"
    old.mkdir(parents=True)
    victim = old / "etf.p1-old.csv"
    victim.write_text("date,close\n2026-10-20,2.5\n", encoding="utf-8")
    stamp = datetime(2026, 10, 23, 19, 30, 0).timestamp()
    os.utime(victim, (stamp, stamp))
    result = check_raw_append_only(tmp_path, date(2026, 10, 23), started)
    assert result.status == "FAIL" and "改写" in result.detail

    fresh = tmp_path / "akshare.unit_test" / "2026-10-23"
    fresh.mkdir()
    (fresh / "etf.p1-new.csv").write_text("date,close\n2026-10-23,2.6\n", encoding="utf-8")
    os.utime(victim, (datetime(2026, 10, 21, 1, 0, 0).timestamp(),) * 2)
    assert check_raw_append_only(tmp_path, date(2026, 10, 23), started).status == "PASS"


def test_raw_append_only_fail_when_root_missing(tmp_path):
    """原始层根目录不存在＝无从判只增，判红而不是 SKIPPED。"""
    assert check_raw_append_only(tmp_path / "nope", date(2026, 10, 23), datetime(2026, 10, 23, 19, 0, 0)).status == "FAIL"


def test_frame_dates_rejects_unparseable_column():
    """连续性判据的时间列不可用时必须抛错，而不是当成 0 行放行。"""
    frame = _frame(["2026-10-20"])
    frame["as_of"] = ["not-a-date"]
    with pytest.raises(GoldError):
        check_weekday_coverage(frame, _spec(), 0.80)


def test_check_result_shape_is_stable():
    """判据输出契约：三态与 detail 是 UI/看板的对外键，不得漂移。"""
    assert CheckResult("PASS", "ok").as_dict() == {"status": "PASS", "detail": "ok"}


def test_batch_freshness_gate():
    """Q4：批次基准日距验收日超 max_stale_days 判红；当天/边界/未来批次放行。

    评审 C2：N2 只相对批次基准日算滞后，17 天前的批可读「滞后 0 天」冒充新鲜；
    Q4 用墙钟兜住。负对照动被试侧（批次日期），不动阈值。
    """
    today = date(2026, 11, 9)
    assert check_batch_freshness(today, today, 5).status == "PASS"
    assert check_batch_freshness(date(2026, 11, 4), today, 5).status == "PASS"
    assert check_batch_freshness(date(2026, 11, 3), today, 5).status == "FAIL"
    assert check_batch_freshness(date(2026, 10, 23), today, 5).status == "FAIL"
    assert check_batch_freshness(date(2026, 11, 10), today, 5).status == "PASS"
