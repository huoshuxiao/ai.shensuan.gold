"""单元层：data.rates.select_usdcny 的四分支与同日差异闸。

覆盖场景：折算价优先、中间价兜底、双双不可用 MISSING、同日绝不混算、
差异超阈值标 DEGRADED、GREEN 但数值为空不冒充可用。
"""
from __future__ import annotations

import pandas as pd

from com.sun.shensuan.gold.data.rates import select_usdcny


def _frame(rows: list[tuple[str, float | None, str, str]]) -> pd.DataFrame:
    """构造规范化长表的最小列：date/usdcny/quality/flags。"""
    return pd.DataFrame(
        [
            {"date": d, "usdcny": v, "quality": q, "flags": f}
            for d, v, q, f in rows
        ]
    )


def test_prefers_boc_when_green():
    """折算价 GREEN 时优先折算价：不兜底、不算差异（中间价缺当日行）。"""
    boc = _frame([("2026-10-23", 6.7180, "GREEN", "")])
    mid = _frame([("2026-10-22", 6.7195, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid)
    assert result.value == 6.7180
    assert result.source == "akshare.currency_boc_sina"
    assert result.quality == "GREEN"
    assert result.fallback_reason is None
    assert result.divergence is None
    assert result.diverged is False


def test_falls_back_to_safe_when_boc_provisional():
    """折算价 PROVISIONAL 时用中间价兜底，并写明兜底原因。"""
    boc = _frame([("2026-10-23", 6.718, "DEGRADED", "PROVISIONAL")])
    mid = _frame([("2026-10-23", 6.718, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid)
    assert result.value == 6.718
    assert result.source == "safe.rmb_mid"
    assert result.quality == "GREEN"
    assert result.fallback_reason is not None
    assert "折算价" in result.fallback_reason


def test_falls_back_when_boc_row_missing():
    """折算价当天没有行（接口失败/缺数）时同样走中间价兜底。"""
    boc = _frame([("2026-10-22", 6.7195, "GREEN", "")])
    mid = _frame([("2026-10-23", 6.718, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid)
    assert result.value == 6.718
    assert result.source == "safe.rmb_mid"


def test_missing_when_both_unavailable():
    """两条都不 GREEN（或缺失）时 MISSING：value/source 为 None，不填 0、不插值。"""
    boc = _frame([("2026-10-23", 6.718, "DEGRADED", "PROVISIONAL")])
    mid = _frame([("2026-10-22", 6.7195, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid)
    assert result.value is None
    assert result.source is None
    assert result.quality == "MISSING"
    assert result.fallback_reason is not None


def test_green_row_with_null_value_not_usable():
    """GREEN 但数值为空的行不算可用值，直接走 MISSING（不填 0）。"""
    boc = _frame([("2026-10-23", None, "GREEN", "")])
    mid = _frame([("2026-10-22", 6.7195, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid)
    assert result.quality == "MISSING"
    assert result.value is None


def test_never_mixes_two_sources():
    """同日两条都有值时结果必须是其中一条的原值，绝不允许平均/拼接。"""
    boc = _frame([("2026-10-23", 6.7200, "GREEN", "")])
    mid = _frame([("2026-10-23", 6.7100, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid)
    assert result.value in (6.7200, 6.7100)
    assert result.value == 6.7200  # 优先折算价
    assert result.source == "akshare.currency_boc_sina"


def test_divergence_over_threshold_marks_degraded():
    """同日差异超过阈值时仍取折算价，但标 diverged 且质量 DEGRADED。"""
    boc = _frame([("2026-10-23", 6.7195, "GREEN", "")])
    mid = _frame([("2026-10-23", 6.7150, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid, divergence_warn=0.0005)
    # |6.7195-6.7150| / 6.7150 ≈ 0.00067 > 0.0005 → 必须被差异闸抓住。
    assert result.diverged is True
    assert result.quality == "DEGRADED"
    assert result.value == 6.7195
    assert result.source == "akshare.currency_boc_sina"


def test_divergence_below_threshold_not_diverged():
    """同日差异明显低于阈值时不标 diverged：闸不是恒真的。"""
    boc = _frame([("2026-10-23", 6.7195, "GREEN", "")])
    mid = _frame([("2026-10-23", 6.7180, "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid, divergence_warn=0.0005)
    # |6.7195-6.7180| / 6.7180 ≈ 0.00022 < 0.0005
    assert result.diverged is False
    assert result.quality == "GREEN"


def test_divergence_computed_on_string_input():
    """主表读回是全字符串（store.load 的 dtype=str），差异计算也要成立。"""
    boc = _frame([("2026-10-23", "6.7195", "GREEN", "")])
    mid = _frame([("2026-10-23", "6.7150", "GREEN", "")])
    result = select_usdcny("2026-10-23", boc, mid, divergence_warn=0.0005)
    assert result.value == 6.7195
    assert result.diverged is True
