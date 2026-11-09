"""宏观与持仓类适配器：SPDR、美债 10Y、USD/CNY、CFTC、手工序列。

职责：把宏观/持仓类数据抓成统一 DataFrame（列名英文化、数值化、按日期过滤）；
PIT 字段与质量判定由规范化层统一补齐。

关键不变量：
- 单位在此层定死：BOC 报价是“每 100 外币”的人民币价，统一除以 100；
  CFTC 持仓保持接口原值（张），不做换算。
- 缺失值一律保留 NaN，禁止前值填充或填 0；10Y 收益率的空行只写 notes。
- 手工 CSV 只读，至少含 ``date/value``；缺 ``available_at`` 时按当日 18:00
  Asia/Shanghai 兜底，保证 PIT 字段非空。
"""
from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd

from ...common.errors import AdapterError
from ...common.timeutil import SHANGHAI, now_shanghai
from ...config.loader import DatasetConfig
from ..models import FetchResult
from .base import AdapterContext, register
from .market import _filter_dates, _require


@register("spdr")
def fetch_spdr(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取 SPDR 黄金 ETF 持仓（AkShare ``macro_cons_gold``）。

    接口字段：``日期/总库存/增持/减持/总价值``，返回可能含多种商品；若存在
    ``商品`` 列则只保留含“黄金”的行。单位：库存为吨，价值为美元。

    Args:
        spec: 数据集配置（本适配器不使用 symbol）。
        start / end: 按 ``date`` 闭区间过滤。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为
        ``date/holdings_tonnes/change_tonnes/total_value_usd``（缺失列自动省略）。

    Raises:
        AdapterError: 接口返回缺少 ``日期`` 或 ``总库存`` 列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.macro_cons_gold()
    _require(raw, ["日期", "总库存"], spec.source)
    frame = raw.copy()
    # 该接口混排多种商品；只在确实有“黄金”行时才过滤，避免误删整表
    if "商品" in frame.columns:
        gold = frame["商品"].astype(str).str.contains("黄金")
        if gold.any():
            frame = frame.loc[gold].copy()
    mapping = {"日期": "date", "总库存": "holdings_tonnes", "增持/减持": "change_tonnes", "总价值": "total_value_usd"}
    frame = frame.rename(columns={key: value for key, value in mapping.items() if key in frame.columns})
    for column in ("holdings_tonnes", "change_tonnes", "total_value_usd"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    keep = [column for column in ["date", "holdings_tonnes", "change_tonnes", "total_value_usd"] if column in frame.columns]
    frame = _filter_dates(frame[keep], start, end)
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at)


@register("us10y_bond")
def fetch_us10y(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取中美国债收益率表，只取美国 10Y 名义利率。

    接口：``ak.bond_zh_us_rate(start_date=YYYYMMDD)``；字段 ``日期`` 与
    ``美国国债收益率10年``。当日盘前该值可能为空，保留 NaN 并写 notes，
    禁止用前值填充（否则会制造未来函数）。单位：%。

    Args:
        spec: 数据集配置（未使用 symbol）。
        start / end: 起始缺省 2021-01-04（接口默认起点），结束为闭区间。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为 ``date/us10y``。

    Raises:
        AdapterError: 接口缺少 ``日期`` 或 ``美国国债收益率10年`` 列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    start_text = (start or date(2021, 1, 4)).strftime("%Y%m%d")
    raw = ak.bond_zh_us_rate(start_date=start_text)
    _require(raw, ["日期", "美国国债收益率10年"], spec.source)
    frame = raw.rename(columns={"日期": "date", "美国国债收益率10年": "us10y"})[["date", "us10y"]].copy()
    frame["us10y"] = pd.to_numeric(frame["us10y"], errors="coerce")
    frame = _filter_dates(frame, start, end)
    notes = []
    if frame["us10y"].isna().any():
        notes.append("部分行美国 10Y 收益率为空（含当日盘前）：按缺失处理，不填前值")
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at, notes=notes)


@register("boc_usdcny")
def fetch_boc_usdcny(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取中行美元牌价并换算成 ``USD/CNY``。

    坑：BOC 接口返回“每 100 外币”的人民币价，必须除以 100；价格列由
    ``options.price_column`` 固定（默认“中行折算价”），不得在不同列之间混用，
    否则同一序列口径不一致。

    Args:
        spec: 数据集配置；``symbol`` 缺省“美元”，``options.price_column``
            指定使用哪一列报价。
        start / end: 起始缺省 2021-10-01，结束缺省今天。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为 ``date/usdcny``，单位：CNY / 1 USD。

    Raises:
        AdapterError: 接口缺少 ``日期`` 或指定价格列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    start_text = (start or date(2021, 10, 1)).strftime("%Y%m%d")
    end_text = (end or date.today()).strftime("%Y%m%d")
    raw = ak.currency_boc_sina(symbol=spec.symbol or "美元", start_date=start_text, end_date=end_text)
    column = str(spec.options.get("price_column", "中行折算价"))
    _require(raw, ["日期", column], spec.source)
    frame = raw.rename(columns={"日期": "date", column: "usdcny"})[["date", "usdcny"]].copy()
    frame["usdcny"] = pd.to_numeric(frame["usdcny"], errors="coerce") / 100.0
    frame = _filter_dates(frame, start, end)
    # 固定使用同一列；不与其他 BOC 价格列混用
    return FetchResult(
        frame=frame.reset_index(drop=True),
        source=spec.source,
        fetched_at=fetched_at,
        notes=[f"BOC 每 100 外币价除以 100；固定使用「{column}」"],
    )


@register("safe_usdcny")
def fetch_safe_usdcny(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取 SAFE 人民币汇率中间价（1994 起），只取美元列并换算成 CNY/USD。

    口径：``ak.currency_boc_safe()`` 返回“每 100 外币”的人民币中间价，
    ``美元`` 列必须除以 100。该序列与 ``boc_usdcny`` 的中行折算价是两套
    报价：每次特征只能选一条，禁止拼接成一条混用（AGENTS §5.2）。

    Args:
        spec: 数据集配置（未使用 symbol/options）。
        start / end: 按 ``date`` 闭区间过滤；None 表示全历史。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为 ``date/usdcny``，单位：CNY / 1 USD。

    Raises:
        AdapterError: 接口缺少 ``日期`` 或 ``美元`` 列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.currency_boc_safe()
    _require(raw, ["日期", "美元"], spec.source)
    frame = raw.rename(columns={"日期": "date", "美元": "usdcny"})[["date", "usdcny"]].copy()
    frame["usdcny"] = pd.to_numeric(frame["usdcny"], errors="coerce") / 100.0
    frame = _filter_dates(frame, start, end)
    return FetchResult(
        frame=frame.reset_index(drop=True),
        source=spec.source,
        fetched_at=fetched_at,
        notes=["SAFE 人民币汇率中间价：每 100 美元除以 100；与 usdcny（中行折算价）不得拼接混用"],
    )


@register("cftc_gold")
def fetch_cftc_gold(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取 CFTC 黄金持仓（非商业/投机口径）。

    接口：``ak.macro_usa_cftc_c_holding()``，周频；字段
    ``日期/黄金-多头仓位/黄金-空头仓位/黄金-净仓位``，单位：张。
    发布滞后由 ``spec.available_at_rule``（``as_of_plus_3d``）在规范化层处理，
    本函数不自行平移日期，避免出现两套滞后口径。

    Args:
        spec: 数据集配置（未使用 symbol）。
        start / end: 按 ``date`` 闭区间过滤。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为 ``date/long/short/net``。

    Raises:
        AdapterError: 接口缺少日期或多空/净仓位列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.macro_usa_cftc_c_holding()
    _require(raw, ["日期", "黄金-多头仓位", "黄金-空头仓位", "黄金-净仓位"], spec.source)
    frame = raw.rename(
        columns={"日期": "date", "黄金-多头仓位": "long", "黄金-空头仓位": "short", "黄金-净仓位": "net"}
    )[["date", "long", "short", "net"]].copy()
    for column in ("long", "short", "net"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = _filter_dates(frame, start, end)
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at)


@register("manual_series")
def fetch_manual_series(
    spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext
) -> FetchResult:
    """读取通用手工序列 CSV（TIPS 实际利率、官方 AU 指数等无稳定直连的数据）。

    文件约定：至少含 ``date`` 与 ``value``；可选 ``available_at``。缺
    ``available_at`` 时按该日 18:00 Asia/Shanghai 兜底（国内数据当日收盘后
    可见的保守假设），保证 PIT 字段非空，绝不填 0 或前值。

    Args:
        spec: 数据集配置；``manual_path`` 相对 ``context.manual_dir``。
        start / end: 按 ``date`` 闭区间过滤。
        context: 适配器上下文，提供手工目录。

    Returns:
        ``FetchResult``，列为 ``date/value/available_at``；空文件返回空表并写 notes。

    Raises:
        AdapterError: 未配置 manual_path、文件不存在，或缺 ``date``/``value`` 列。
    """
    if not spec.manual_path:
        raise AdapterError(f"{spec.source} 未配置 manual_path")
    path = context.manual_dir / spec.manual_path
    if not path.exists():
        raise AdapterError(f"手工文件不存在：{path}")
    fetched_at = now_shanghai()
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    if raw.empty:
        frame = pd.DataFrame(columns=["date", "value", "available_at"])
        return FetchResult(frame=frame, source=spec.source, fetched_at=fetched_at, notes=["手工文件为空"])
    _require(raw, ["date", "value"], spec.source)
    frame = raw.copy()
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    if "available_at" not in frame.columns:
        frame["available_at"] = frame["date"].map(
            lambda value: datetime.combine(pd.Timestamp(value).date(), time(18, 0), tzinfo=SHANGHAI).isoformat()
        )
    keep = ["date", "value", "available_at"]
    frame = _filter_dates(frame[keep], start, end)
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at)
