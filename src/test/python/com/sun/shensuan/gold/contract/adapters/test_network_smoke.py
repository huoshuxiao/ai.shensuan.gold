"""联网冒烟层：每个启用的非手工适配器各一条真实抓取用例。

默认 ``pytest`` 由 ``pytest.ini`` 的 ``-m "not network"`` 排除本文件；需要联网时
显式执行：

```
/usr/bin/python3.10 -m pytest -m network src/test/python/com/sun/shensuan/gold/contract/adapters/test_network_smoke.py
```

覆盖范围：``config/data_sources.yaml`` 中 ``enabled: true`` 且适配器不属于手工导入
（``manual_geo_events``/``manual_series``）的数据集。手工源由离线单元测试覆盖，
不访问网络。用例按数据集名参数化，失败时可直接定位到源。

断言只做冒烟级检查：真实请求返回非空表、来源标识一致、抓取时间带时区；不校验
具体行数或价格，避免把上游正常修订误判成代码缺陷。坏时间行、限流、单位换算等
严格口径由离线契约测试负责。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from com.sun.shensuan.gold.config.loader import load_config
from com.sun.shensuan.gold.data.adapters.base import AdapterContext, get_adapter

pytestmark = pytest.mark.network

MANUAL_ADAPTERS = {"manual_geo_events", "manual_series"}
NEWS_ADAPTERS = {"news_cls", "news_sina", "news_shmet"}
WINDOW_DAYS = 45
REQUEST_TIMEOUT_S = 120


def pytest_generate_tests(metafunc) -> None:
    """按当前配置动态参数化 ``dataset_name``，新增启用源会自动纳入。

    Args:
        metafunc: pytest 收集期元对象；只有请求 ``dataset_name`` 的用例才注入参数。

    Returns:
        无；参数化结果由 pytest 保存。

    Notes:
        只覆盖非手工适配器：手工 CSV 的可用性取决于本地文件，不能由联网测试证明。
    """
    if "dataset_name" not in metafunc.fixturenames:
        return
    config = load_config()
    names = [
        name
        for name, spec in config.data_sources.datasets.items()
        if spec.enabled and spec.adapter not in MANUAL_ADAPTERS
    ]
    metafunc.parametrize("dataset_name", names)


def test_enabled_adapter_smoke(repo_config, dataset_name, tmp_path) -> None:
    """对单个启用数据集执行真实抓取，确认联网路径能返回非空结果。

    Args:
        repo_config: 仓库真实配置夹具，用于取出数据集规格。
        dataset_name: 参数化数据集名，同时作为失败定位标签。
        tmp_path: 临时手工目录；不落在仓库 ``data/manual``，防止网络失败时误用本地回退。

    Returns:
        无；断言失败时由 pytest 标记该数据集。

    Raises:
        Exception: 适配器自身的网络/解析异常原样上抛，pytest 输出会带数据集 id；
            不吞异常，否则会把断源伪装成通过。
    """
    spec = repo_config.data_sources.datasets[dataset_name].model_copy(deep=True)
    if spec.options:
        spec.options["download_timeout_s"] = REQUEST_TIMEOUT_S
    end = date.today()
    start = end - timedelta(days=WINDOW_DAYS)
    context = AdapterContext(manual_dir=tmp_path, request_timeout_s=REQUEST_TIMEOUT_S)

    result = get_adapter(spec.adapter)(spec, start, end, context)

    assert isinstance(result.frame, pd.DataFrame), f"{dataset_name} 未返回 DataFrame"
    assert not result.frame.empty, f"{dataset_name} 返回空表；源或窗口可能异常"
    assert result.source == spec.source, f"{dataset_name} 来源标识与配置不一致"
    assert result.fetched_at.tzinfo is not None, f"{dataset_name} fetched_at 缺少时区"

    if spec.adapter in NEWS_ADAPTERS:
        assert {"published_at", "title", "content_hash"} <= set(result.frame.columns), (
            f"{dataset_name} 快讯列契约不完整"
        )
    if spec.adapter == "gpr":
        assert any("REVISED_ONLY" in note for note in result.notes), (
            f"{dataset_name} 未标注 REVISED_ONLY"
        )


def test_cn_trade_calendar_smoke(tmp_path):
    """联网冒烟：上交所交易日历必须能抓到、能落盘、能读回，且覆盖到最近一个已发生的交易日。

    断言只到冒烟级：行数够长（1990 年至今 >8,000 个交易日）、区间自洽、
    2026 年国庆长假的周一至周五（10-01/02/05/06/07）都不是交易日，
    而 10-08、10-09 是交易日（2026-10-23 实测的上交所日历形状）。
    """
    from com.sun.shensuan.gold.data.trade_calendar import (
        BASIS_CN_SSE,
        fetch_cn_trade_days,
        load_trade_calendar,
        save_cn_trade_calendar,
    )

    days = fetch_cn_trade_days()
    assert len(days) > 8000, f"日历行数 {len(days)} 明显偏少，接口形状可能变了"
    assert days == sorted(days) and len(days) == len(set(days))
    meta = save_cn_trade_calendar(tmp_path, days, None)
    assert meta["rows"] == len(days)
    calendar = load_trade_calendar(tmp_path, BASIS_CN_SSE)
    assert calendar.available is True, calendar.note
    assert calendar.as_of_min < date(1995, 1, 1)
    assert calendar.as_of_max >= date(2026, 10, 23), "日历未覆盖到近期，跨年需重抓"
    holidays = [date(2026, 10, day) for day in (1, 2, 5, 6, 7)]
    assert not (set(holidays) & set(calendar.days)), "国庆长假的周一至周五被算成了交易日"
    assert date(2026, 10, 8) in calendar.days, "2026-10-08（长假后恢复交易首日）不在日历里"
    assert date(2026, 10, 9) in calendar.days, "2026-10-09 交易日不在日历里"
