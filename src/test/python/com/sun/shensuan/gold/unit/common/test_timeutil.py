"""单元层：common.timeutil 的 PIT 时点规则、日期解析与可用性过滤。

覆盖场景：available_at 各规则、非法规则/朴素时间拒绝、日期格式兼容、
cutoff 截断未来数据。
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from com.sun.shensuan.gold.common.timeutil import (
    SHANGHAI,
    available_at_for,
    filter_available,
    filter_fact_pack,
    parse_date,
    parse_datetime,
)


def test_same_day_rule():
    """验证 same_day_1530 规则把 available_at 固定为当日 15:30 上海时间，供收盘后数据做 PIT 标注。"""
    value = available_at_for(date(2026, 10, 16), "same_day_1530", datetime(2026, 10, 16, 16, tzinfo=SHANGHAI))
    assert value.isoformat() == "2026-10-16T15:30:00+08:00"


def test_same_day_0915_rule():
    """验证 same_day_0915 规则把 available_at 固定为当日 09:15（人民币中间价发布口径）。"""
    value = available_at_for(date(2026, 10, 16), "same_day_0915", datetime(2026, 10, 16, 9, 20, tzinfo=SHANGHAI))
    assert value.isoformat() == "2026-10-16T09:15:00+08:00"


def test_next_day_rule_crosses_weekend_naively():
    """验证 next_day_0600 只按自然日加一天、不跳周末；周末修正应由上层交易日历负责。"""
    value = available_at_for(date(2026, 10, 16), "next_day_0600", datetime(2026, 10, 17, tzinfo=SHANGHAI))
    assert value.isoformat() == "2026-10-17T06:00:00+08:00"


def test_cftc_lag_rule():
    """验证 as_of_plus_3d 规则把周频持仓的可用时点推迟三天到 06:00，模拟 CFTC 发布滞后。"""
    value = available_at_for(date(2026, 10, 13), "as_of_plus_3d", datetime(2026, 10, 19, tzinfo=SHANGHAI))
    assert value.isoformat() == "2026-10-16T06:00:00+08:00"


def test_fetched_at_rule_uses_fetch_time():
    """验证 fetched_at 规则原样返回抓取时刻（用于 GPR 等无 vintage 数据），不做额外偏移。"""
    fetched = datetime(2026, 10, 19, 23, 30, tzinfo=SHANGHAI)
    value = available_at_for(date(1985, 1, 1), "fetched_at", fetched)
    assert value == fetched


def test_unknown_rule_raises():
    """负向验证未知 available_at 规则抛 ValueError：静默放行会让未来数据冒充 PIT 数据。"""
    with pytest.raises(ValueError):
        available_at_for(date(2026, 1, 1), "whenever", datetime(2026, 1, 1, tzinfo=SHANGHAI))


def test_naive_datetime_rejected():
    """负向验证无时区 datetime 被拒绝：朴素时间无法判定 cutoff，必须显式带时区。"""
    with pytest.raises(ValueError):
        available_at_for(date(2026, 1, 1), "fetched_at", datetime(2026, 1, 1))


def test_parse_date_formats():
    """验证 parse_date 兼容斜杠、带时间字符串与 pandas Timestamp，统一收敛为 date。"""
    assert parse_date("2026/10/16") == date(2026, 10, 16)
    assert parse_date("2026-10-16 15:00:00") == date(2026, 10, 16)
    assert parse_date(pd.Timestamp("2026-10-16")) == date(2026, 10, 16)


def test_parse_datetime_defaults_to_shanghai():
    """验证 parse_datetime 对无时区输入补上 +08:00，避免跨时区比较错位。"""
    value = parse_datetime("2026-10-16 15:30")
    assert value.tzinfo is not None
    assert value.utcoffset().total_seconds() == 8 * 3600


def test_filter_available_excludes_future():
    """验证 filter_available 按 cutoff 截断：available_at 晚于 07:00 的行必须被剔除。"""
    frame = pd.DataFrame(
        {
            "as_of": ["2026-10-16", "2026-10-17"],
            "available_at": ["2026-10-16T15:30:00+08:00", "2026-10-18T06:00:00+08:00"],
        }
    )
    cutoff = datetime(2026, 10, 17, 7, 0, tzinfo=SHANGHAI)
    filtered = filter_available(frame, cutoff)
    assert filtered["as_of"].tolist() == ["2026-10-16"]


def test_filter_available_requires_timezone():
    """负向验证 cutoff 缺时区时抛 ValueError：与带时区数据比较前必须显式声明基准时区。"""
    frame = pd.DataFrame({"available_at": ["2026-10-16T15:30:00+08:00"]})
    with pytest.raises(ValueError):
        filter_available(frame, datetime(2026, 10, 17, 7, 0))


def test_filter_fact_pack_excludes_provisional_rows():
    """验证事实包过滤会剔除“抓取早于 available_at”的行，即使该行已满足 cutoff。"""
    frame = pd.DataFrame(
        {
            "as_of": ["2026-10-19", "2026-10-20"],
            "available_at": ["2026-10-20T06:00:00+08:00", "2026-10-21T06:00:00+08:00"],
            "fetched_at": ["2026-10-20T05:00:00+08:00", "2026-10-21T07:00:00+08:00"],
        }
    )
    cutoff = datetime(2026, 10, 21, 8, 0, tzinfo=SHANGHAI)
    filtered = filter_fact_pack(frame, cutoff)
    assert filtered["as_of"].tolist() == ["2026-10-20"]
