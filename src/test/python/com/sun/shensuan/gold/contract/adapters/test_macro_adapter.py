"""契约层：data.adapters.macro.fetch_boc_usdcny 的单位与日期契约。

覆盖场景：BOC「每 100 外币」报价除以 100 后的量级、按 date 闭区间过滤。
akshare 接口用 monkeypatch 顶替，不联网。

口径来源：AGENTS §5.2 / §17.3——USD/CNY 必须固定一列并除以 100，缺这一步
整条汇率序列会放大 100 倍，跨市场价差与 DXY 代理全部失真。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from com.sun.shensuan.gold.data.adapters.base import AdapterContext
from com.sun.shensuan.gold.data.adapters.macro import fetch_boc_usdcny, fetch_safe_usdcny


def test_usdcny_divides_boc_per_100(monkeypatch, repo_config, tmp_path):
    """验证「每 100 外币」报价除以 100：711 元/100 美元必须落成 7.11 元/美元。

    负对照对象：``fetch_boc_usdcny`` 里的 ``/ 100.0``；删掉它本条必红，
    而真实数据量级（`data/normalized/usdcny.csv` 为 6.4–6.8）也会同步放大 100 倍。
    """
    raw = pd.DataFrame(
        {
            "日期": ["2026-10-16", "2026-10-17"],
            "中行折算价": [711.0, 720.0],
            "中行现钞买入价": [700.0, 709.0],
        }
    )
    monkeypatch.setattr("akshare.currency_boc_sina", lambda **kwargs: raw)
    spec = repo_config.data_sources.datasets["usdcny"]
    result = fetch_boc_usdcny(
        spec,
        date(2026, 10, 16),
        date(2026, 10, 17),
        AdapterContext(manual_dir=tmp_path),
    )
    assert result.frame["usdcny"].tolist() == pytest.approx([7.11, 7.2], abs=1e-12)
    assert list(result.frame["date"]) == ["2026-10-16", "2026-10-17"]
    assert any("除以 100" in note for note in result.notes)


def test_usdcny_filters_by_date_range(monkeypatch, repo_config, tmp_path):
    """验证按 start/end 闭区间过滤，且只取配置指定的报价列（不与其他列混用）。"""
    raw = pd.DataFrame(
        {
            "日期": ["2026-10-14", "2026-10-16", "2026-10-18"],
            "中行折算价": [710.0, 711.0, 712.0],
            "中行现钞买入价": [699.0, 700.0, 701.0],
        }
    )
    monkeypatch.setattr("akshare.currency_boc_sina", lambda **kwargs: raw)
    spec = repo_config.data_sources.datasets["usdcny"]
    result = fetch_boc_usdcny(
        spec,
        date(2026, 10, 15),
        date(2026, 10, 17),
        AdapterContext(manual_dir=tmp_path),
    )
    assert list(result.frame["date"]) == ["2026-10-16"]
    assert result.frame["usdcny"].tolist() == pytest.approx([7.11], abs=1e-12)


def test_safe_usdcny_divides_per_100(monkeypatch, repo_config, tmp_path):
    """验证 SAFE 中间价同样按“每 100 外币”除以 100，且注明与折算价不得混用。

    负对照对象：``fetch_safe_usdcny`` 里的 ``/ 100.0``；删掉后 870.0 会落成
    870 元/美元，量级错误会直接污染跨市场价差。
    """
    raw = pd.DataFrame(
        {
            "日期": ["1994-01-01", "2026-10-20"],
            "美元": [870.0, 672.0],
            "欧元": [None, 751.59],
        }
    )
    monkeypatch.setattr("akshare.currency_boc_safe", lambda: raw)
    spec = repo_config.data_sources.datasets["usdcny_mid"]
    result = fetch_safe_usdcny(
        spec,
        date(1994, 1, 1),
        date(2026, 10, 20),
        AdapterContext(manual_dir=tmp_path),
    )
    assert result.frame["usdcny"].tolist() == pytest.approx([8.7, 6.72], abs=1e-12)
    assert any("不得拼接混用" in note for note in result.notes)


def test_safe_usdcny_filters_by_date_range(monkeypatch, repo_config, tmp_path):
    """验证 SAFE 中间价按 start/end 闭区间过滤，不把全历史一次性带进目标区间。"""
    raw = pd.DataFrame(
        {
            "日期": ["1994-01-01", "2026-10-19", "2026-10-20"],
            "美元": [870.0, 672.08, 672.0],
        }
    )
    monkeypatch.setattr("akshare.currency_boc_safe", lambda: raw)
    spec = repo_config.data_sources.datasets["usdcny_mid"]
    result = fetch_safe_usdcny(
        spec,
        date(2026, 10, 19),
        date(2026, 10, 20),
        AdapterContext(manual_dir=tmp_path),
    )
    assert list(result.frame["date"]) == ["2026-10-19", "2026-10-20"]
    assert result.frame["usdcny"].tolist() == pytest.approx([6.7208, 6.72], abs=1e-12)
