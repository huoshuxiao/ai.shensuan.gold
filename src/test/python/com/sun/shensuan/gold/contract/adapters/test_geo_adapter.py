"""契约层：快讯与 GDELT 适配器的容错契约。

覆盖场景：CLS/Sina/SHMET 单行坏时间戳只丢该行并计数、空内容与重复正文去重、
GDELT 限流（429）与超时统一降级为 DegradedSourceError。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from com.sun.shensuan.gold.common.errors import DegradedSourceError
from com.sun.shensuan.gold.data.adapters.base import AdapterContext
from com.sun.shensuan.gold.data.adapters.geo import (
    fetch_gdelt,
    fetch_news_cls,
    fetch_news_shmet,
    fetch_news_sina,
)


def _fake_akshare(monkeypatch, **functions) -> None:
    """测试辅助：用轻量假模块顶替 akshare，避免导入真实依赖与联网。"""
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(**functions))


def test_sina_drops_only_bad_timestamp_row(repo_config, monkeypatch, tmp_path):
    """验证新浪快讯单行坏时间只丢弃该行，其余行保留并在 notes 记录丢弃数。"""
    raw = pd.DataFrame(
        {
            "时间": ["2026-10-19 10:00:00", "not-a-time", "2026-10-19 11:00:00"],
            "内容": ["a", "b", "c"],
        }
    )
    _fake_akshare(monkeypatch, stock_info_global_sina=lambda: raw)
    result = fetch_news_sina(
        repo_config.data_sources.datasets["news_sina"],
        None,
        None,
        AdapterContext(manual_dir=tmp_path),
    )
    assert len(result.frame) == 2
    assert set(result.frame["title"]) == {"a", "c"}
    assert any("丢弃发布时间非法 1 行" in note for note in result.notes)


def test_cls_drops_only_bad_timestamp_row(repo_config, monkeypatch, tmp_path):
    """验证财联社快讯日期/时间拼接失败时只丢该行，不影响同批其他快讯。"""
    raw = pd.DataFrame(
        {
            "发布日期": ["2026-10-19", "2026-10-19", "2026-10-19"],
            "发布时间": ["10:00:00", "broken", "11:00:00"],
            "标题": ["a", "b", "c"],
            "内容": ["a-body", "b-body", "c-body"],
        }
    )
    _fake_akshare(monkeypatch, stock_info_global_cls=lambda symbol: raw)
    result = fetch_news_cls(
        repo_config.data_sources.datasets["news_cls"],
        None,
        None,
        AdapterContext(manual_dir=tmp_path),
    )
    assert len(result.frame) == 2
    assert set(result.frame["title"]) == {"a", "c"}
    assert any("丢弃发布时间非法 1 行" in note for note in result.notes)


def test_shmet_drops_only_bad_timestamp_row(repo_config, monkeypatch, tmp_path):
    """验证上海金属网快讯坏时间只丢该行，并保持正文哈希去重。"""
    raw = pd.DataFrame(
        {
            "发布时间": ["2026-10-19 10:00:00", "not-a-time", "2026-10-19 10:00:00"],
            "内容": ["same", "bad", "same"],
        }
    )
    _fake_akshare(monkeypatch, futures_news_shmet=lambda symbol: raw)
    result = fetch_news_shmet(
        repo_config.data_sources.datasets["news_shmet"],
        None,
        None,
        AdapterContext(manual_dir=tmp_path),
    )
    assert len(result.frame) == 1
    assert result.frame.iloc[0]["title"] == "same"
    assert any("丢弃发布时间非法 1 行" in note for note in result.notes)


def test_gdelt_429_is_degraded(repo_config, monkeypatch, tmp_path):
    """验证 GDELT 429 限流转 DegradedSourceError：可选源降级不得拖垮主链。"""
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: SimpleNamespace(status_code=429, json=lambda: {}),
    )
    with pytest.raises(DegradedSourceError, match="429"):
        fetch_gdelt(
            repo_config.data_sources.datasets["gdelt_doc"],
            None,
            None,
            AdapterContext(manual_dir=tmp_path),
        )


def test_gdelt_timeout_is_degraded(repo_config, monkeypatch, tmp_path):
    """验证 GDELT 请求超时转 DegradedSourceError，而不是让异常逃出适配器层。"""
    import requests

    def _timeout(*args, **kwargs):
        """测试替身：模拟 requests.get 读超时，供降级契约断言使用。"""
        raise requests.Timeout("读超时")

    monkeypatch.setattr("requests.get", _timeout)
    with pytest.raises(DegradedSourceError, match="请求失败"):
        fetch_gdelt(
            repo_config.data_sources.datasets["gdelt_doc"],
            None,
            None,
            AdapterContext(manual_dir=tmp_path),
        )
