"""行情类适配器：ETF、SGE 现货、COMEX/WTI、沪金主力连续 AU0。

职责：把各行情接口统一成 ``date/open/high/low/close/volume/...`` 的 DataFrame，
列名统一小写英文、数值列强制转 float，并按日期闭区间过滤。

关键不变量：
- 适配器不做最终质量裁决，但必须把可判定的降级事实写进 notes/status：
  COMEX/WTI 的 volume/position 全为 0 时标 DEGRADED，只允许当收盘价使用。
- AU0 是新浪主力连续，不是官方 AU 指数；换月跳空属数据固有特性，本层不做复权。
- 价格单位保持源口径：境内 ETF/SGE 为 CNY，COMEX/WTI 为 USD。
- 空表不抛异常：交给规范化层统一标 RED（``EMPTY_SOURCE``），避免各适配器
  各写一套判定。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ...common.errors import AdapterError
from ...common.timeutil import now_shanghai
from ...config.loader import DatasetConfig
from ..models import FetchResult, NodeStatus
from .base import AdapterContext, register


def _filter_dates(frame: pd.DataFrame, start: date | None, end: date | None, column: str = "date") -> pd.DataFrame:
    """按日期列做闭区间过滤，并顺手剔除日期无法解析的行。

    两次 ``to_datetime`` 是刻意的：第一次剔除 NaT 行并保留原索引，第二次把
    同一列转成 ``date`` 对象供比较；``errors="coerce"`` 保证脏值变 NaT 而不是
    抛错，最终在 mask 中被过滤掉。

    Args:
        frame: 待过滤 DataFrame；空表原样返回。
        start: 起始日期（含），None 表示不设下限。
        end: 结束日期（含），None 表示不设上限。
        column: 日期列名，默认 ``"date"``。

    Returns:
        过滤后的副本；索引保持原样，调用方通常再做 ``reset_index``。
    """
    if frame.empty:
        return frame
    parsed = pd.to_datetime(frame[column], errors="coerce")
    frame = frame.loc[parsed.notna()].copy()
    if frame.empty:
        return frame
    parsed = pd.to_datetime(frame[column], errors="coerce").dt.date
    mask = pd.Series(True, index=frame.index)
    if start is not None:
        mask &= parsed >= start
    if end is not None:
        mask &= parsed <= end
    return frame.loc[mask].copy()


def _require(frame: pd.DataFrame, columns: list[str], source: str) -> None:
    """校验必需列齐全，缺列时一次性列出全部缺失项。

    Args:
        frame: 待校验 DataFrame。
        columns: 必需列名清单。
        source: 数据源名称，仅用于错误信息定位。

    Raises:
        AdapterError: 任一必需列缺失；错误信息同时给出实际列，便于对照接口变更。
    """
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise AdapterError(f"{source} 缺少列：{missing}；实际列：{list(frame.columns)}")


@register("etf")
def fetch_etf(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取境内黄金 ETF 日线（AkShare ``fund_etf_hist_sina``）。

    接口字段：``date/open/high/low/close/volume``（可能含 ``amount``）。
    ``amount`` 缺失时补 NA 列而不是 0，避免把“没有成交额”伪装成“零成交额”。
    单位：价格为 CNY/份，volume 为份，amount 为元。

    Args:
        spec: 数据集配置；``symbol`` 形如 ``sh518880`` / ``sz159934``。
        start / end: 按 ``date`` 闭区间过滤。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为 ``date/open/high/low/close/volume/amount``。

    Raises:
        AdapterError: 接口返回缺少 OHLCV 任一列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.fund_etf_hist_sina(symbol=spec.symbol)
    _require(raw, ["date", "open", "high", "low", "close", "volume"], spec.source)
    frame = raw.copy()
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "amount" not in frame.columns:
        frame["amount"] = pd.NA
    frame = _filter_dates(frame[["date", "open", "high", "low", "close", "volume", "amount"]], start, end)
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at)


@register("sge")
def fetch_sge(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取上海黄金交易所 Au99.99 现货日线（``ak.spot_hist_sge``）。

    接口字段：``date/open/high/low/close``；单位 CNY/g，无成交量列。
    该序列是人民币金价基准，只做研究展示，不进入默认交易清单。

    Args:
        spec: 数据集配置；``symbol`` 例如 ``Au99.99``。
        start / end: 按 ``date`` 闭区间过滤。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为 ``date/open/high/low/close``。

    Raises:
        AdapterError: 接口返回缺少 OHLC 任一列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.spot_hist_sge(symbol=spec.symbol)
    _require(raw, ["date", "open", "high", "low", "close"], spec.source)
    frame = raw.copy()
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = _filter_dates(frame[["date", "open", "high", "low", "close"]], start, end)
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at)


@register("foreign_futures")
def fetch_foreign_futures(
    spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext
) -> FetchResult:
    """抓取境外期货日线（COMEX ``GC`` / WTI ``CL``）。

    接口：``ak.futures_foreign_hist(symbol)``；``position`` 列映射为
    ``open_interest``。已知坑：末行 volume/position 常为 0（当日未结算），
    若整列全 0 就把节点标 DEGRADED 并写 notes，下游只允许当收盘价使用。

    Args:
        spec: 数据集配置；``symbol`` 例如 ``GC``、``CL``。
        start / end: 按 ``date`` 闭区间过滤。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``；status 为 OK 或 DEGRADED（全零成交/持仓时）。

    Raises:
        AdapterError: 接口返回缺少 ``date`` 或 OHLC 任一列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.futures_foreign_hist(symbol=spec.symbol)
    _require(raw, ["date", "open", "high", "low", "close"], spec.source)
    frame = raw.copy()
    if "position" in frame.columns:
        frame["open_interest"] = pd.to_numeric(frame["position"], errors="coerce")
    else:
        frame["open_interest"] = pd.NA
    for column in ("open", "high", "low", "close", "volume", "open_interest"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    keep = [column for column in ["date", "open", "high", "low", "close", "volume", "open_interest"] if column in frame.columns]
    notes = []
    # NaN 按 0 参与“全零”判定：缺值同样不能当作真实流动性
    zero = frame[keep].fillna(0)
    if "volume" in zero.columns and (zero["volume"] == 0).all():
        notes.append("volume 全为 0：标 DEGRADED，只当收盘价")
    if "open_interest" in zero.columns and (zero["open_interest"] == 0).all():
        notes.append("open_interest 全为 0：标 DEGRADED，只当收盘价")
    frame = _filter_dates(frame[keep], start, end)
    return FetchResult(
        frame=frame.reset_index(drop=True),
        source=spec.source,
        fetched_at=fetched_at,
        status=NodeStatus.DEGRADED if notes else NodeStatus.OK,
        notes=notes,
    )


@register("au0")
def fetch_au0(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取沪金主力连续 AU0 日线（``ak.futures_main_sina``）。

    中文字段映射：``日期/开盘价/最高价/最低价/收盘价/成交量/持仓量/动态结算价``。
    注意：AU0 是新浪拼接的主力连续，不是官方 AU 指数；换月处存在跳空，
    本层不做复权或拼接修正，按源数据原样输出。单位 CNY/g。

    Args:
        spec: 数据集配置；``symbol`` 为 ``AU0``。
        start / end: 缺省起点 1990-01-01、终点今天，按闭区间过滤。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``，列为
        ``date/open/high/low/close/volume/open_interest/settlement``
        （缺失列自动省略）。

    Raises:
        AdapterError: 重命名后仍缺 ``date`` 或 OHLC 任一列。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    start_text = (start or date(1990, 1, 1)).strftime("%Y%m%d")
    end_text = (end or date.today()).strftime("%Y%m%d")
    raw = ak.futures_main_sina(symbol=spec.symbol, start_date=start_text, end_date=end_text)
    mapping = {
        "日期": "date",
        "开盘价": "open",
        "最高价": "high",
        "最低价": "low",
        "收盘价": "close",
        "成交量": "volume",
        "持仓量": "open_interest",
        "动态结算价": "settlement",
    }
    frame = raw.rename(columns={key: value for key, value in mapping.items() if key in raw.columns})
    _require(frame, ["date", "open", "high", "low", "close"], spec.source)
    for column in ("open", "high", "low", "close", "volume", "open_interest", "settlement"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    keep = [column for column in ["date", "open", "high", "low", "close", "volume", "open_interest", "settlement"] if column in frame.columns]
    frame = _filter_dates(frame[keep], start, end)
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at)
