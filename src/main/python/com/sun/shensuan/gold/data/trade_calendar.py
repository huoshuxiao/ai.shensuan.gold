"""交易日历：抓取、落盘、读取「哪些天本该有数据」，供连续性判据取分母。

本模块只负责日历本身：不做质量判定、不改写行情表、不替代 `data/pit.py` 的
时间口径。它存在的理由是 AGENTS §6.6 的「工作日缺口」——只用周一至周五做分母会把
交易所节假日算成缺口，覆盖率数字偏保守，判据因此分不清「源没给」和「那天本来不开市」。

外汇报价日与美国交易日没有可直连的官方日历（本机 akshare 1.18.81 只有
`tool_trade_date_hist_sina` 一个日历接口），所以数据集必须显式声明口径：
`cn_sse` 用上交所官方交易日，`weekday` 用自然工作日近似；后者必须在读数里写明
「近似」，不得把近似口径冒充成官方日历。

失败口径：抓取失败抛 `AdapterError`（调用方决定降级，不静默返回空）；日历文件
未落盘、缺校验和或校验和不符时 `load_trade_calendar()` 返回 `integrity` 非 `OK` 的空日历，
由调用方回落 `weekday` 并把原因写进读数，不得当成「无缺口」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from ..common.atomic import atomic_write_bytes, atomic_write_json
from ..common.errors import AdapterError
from ..common.timeutil import (
    CALENDAR_BASE_CN_SSE,
    CALENDAR_BASE_WEEKDAY,
    CALENDAR_BASES,
)

# 唯一可用的官方日历接口；source_url 取自 akshare 函数 docstring（不猜测）。
CN_SSE_SOURCE = "akshare.tool_trade_date_hist_sina"
CN_SSE_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"

# 口径名以 common.timeutil 为唯一权威（与配置校验共用），本文件只做起草与读取。
BASIS_CN_SSE = CALENDAR_BASE_CN_SSE
BASIS_WEEKDAY = CALENDAR_BASE_WEEKDAY
VALID_BASES = CALENDAR_BASES

# 每种日历口径的落盘文件名：CSV 是正文，meta 记录来源/抓取时间/行数/校验和。
_CALENDAR_FILES = {
    BASIS_CN_SSE: ("trade_calendar_cn_sse.csv", "trade_calendar_cn_sse.meta.json"),
}

_INTEGRITY_OK = "OK"
_INTEGRITY_MISSING = "MISSING_FILE"
_INTEGRITY_UNREADABLE = "UNREADABLE"
_INTEGRITY_SHA_MISMATCH = "SHA_MISMATCH"
_INTEGRITY_EMPTY = "EMPTY"


@dataclass(frozen=True)
class TradeCalendar:
    """一种口径下的「应交易日」集合。

    Attributes:
        basis: 口径名，`cn_sse`（上交所官方交易日）或 `weekday`（自然工作日近似）。
        days: 应交易日集合；`integrity` 非 `OK` 时为空集。
        integrity: `OK`/`MISSING_FILE`/`UNREADABLE`/`SHA_MISMATCH`/`EMPTY`；
            非 `OK` 表示这份日历不可用于判缺口，调用方必须披露回落口径。
            `SHA_MISMATCH` 同时覆盖「meta 缺 sha256」与「校验和不符」两种情形。
        note: 人类可读的补充说明（来源、行数、失败原因）。
        rows: 日历行数；`integrity` 非 `OK` 时为 0。
        as_of_min / as_of_max: 日历覆盖的首末交易日；无数据时为 None。
        fetched_at: 本份日历的抓取时间（ISO-8601 字符串）；未知为 None。
        source: 接口名，便于复核来源；`weekday` 口径固定为 `approx:mon-fri`。

    Notes:
        本类不判定「数据好不好」，只提供分母；`covers()` 用来提醒日历已过期
        （日历源通常只给到当年 12-31，跨年时必须重抓）。
    """

    basis: str
    days: frozenset[date] = field(default_factory=frozenset)
    integrity: str = _INTEGRITY_MISSING
    note: str = ""
    rows: int = 0
    as_of_min: date | None = None
    as_of_max: date | None = None
    fetched_at: str | None = None
    source: str = "approx:mon-fri"

    @property
    def available(self) -> bool:
        """这份日历能否直接用于判缺口：校验通过且非空才算可用。"""
        return self.integrity == _INTEGRITY_OK and bool(self.days)

    def covers(self, reference: date) -> bool:
        """判断日历是否覆盖到参考日（含）；未覆盖说明需要重抓。

        Args:
            reference: 参考日（通常是验收基准日）。

        Returns:
            bool: 日历末端 >= 参考日 → True；日历不可用或已过期 → False。
        """
        return self.available and self.as_of_max is not None and self.as_of_max >= reference

    def count_between(self, start: date, end: date) -> int:
        """统计闭区间 [start, end] 内的应交易日数，单位：天。

        Args:
            start: 起始日（含）。
            end: 结束日（含）。

        Returns:
            int: 应交易日数；`start > end` 或日历不可用时为 0。
        """
        if not self.available or start > end:
            return 0
        return sum(1 for day in self.days if start <= day <= end)


def weekday_days(start: date, end: date) -> list[date]:
    """按自然工作日（周一至周五）列出闭区间内的日期。

    输入输出都是日期，无单位换算；不含节假日概念，因此相对官方交易日历偏多，
    用它做分母会低估覆盖率（保守）。

    Args:
        start: 起始日（含）。
        end: 结束日（含）。

    Returns:
        list: 升序的周一至周五日期；`start > end` 时为空列表。
    """
    if start > end:
        return []
    out: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _calendar_paths(directory: Path | str, basis: str) -> tuple[Path, Path]:
    """取某一口径的日历正文与元数据路径。

    Args:
        directory: 参考数据目录（`paths.reference`）。
        basis: 口径名，必须在 `VALID_BASES` 内。

    Returns:
        tuple: `(csv 路径, meta 路径)`；`weekday` 是算法近似，没有落盘文件，
            返回的路径仅用于报错信息。

    Raises:
        ValueError: `basis` 不在 `VALID_BASES` 内（拼错口径不得静默回落到近似）。
    """
    if basis not in VALID_BASES:
        raise ValueError(f"未知日历口径：{basis}（可用 {list(VALID_BASES)}）")
    root = Path(directory)
    files = _CALENDAR_FILES.get(basis)
    if files is None:
        raise ValueError(f"口径 {basis} 是算法近似，不落盘日历文件")
    name, meta = files
    return root / name, root / meta


def fetch_cn_trade_days(timeout_note: str = "") -> list[date]:
    """抓取上交所官方交易日历史（联网）。

    Args:
        timeout_note: 附加说明，写进异常文本便于定位是哪条日链触发的；可为空串。

    Returns:
        list: 升序、去重后的交易日列表；单位：日期（无时区，交易所日历本身就是日期）。

    Raises:
        AdapterError: akshare 未安装、接口异常或返回空。宁可抛错让调用方降级，
            也不返回空列表冒充「没有任何交易日」。
    """
    try:
        import akshare as ak  # 延迟导入：离线测试不应为此付出加载成本
    except ImportError as exc:  # akshare 缺失属于环境未装好，不能静默
        raise AdapterError(f"缺少 akshare，无法抓取交易日历：{exc}") from exc
    try:
        frame = ak.tool_trade_date_hist_sina()
    except Exception as exc:  # 网络/解析异常一律转成统一适配器错误
        suffix = f"（{timeout_note}）" if timeout_note else ""
        raise AdapterError(f"抓取交易日历失败：{type(exc).__name__}: {exc}{suffix}") from exc
    if frame is None or frame.empty:
        raise AdapterError("交易日历接口返回空表，拒绝当作有效日历")
    column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
    parsed = pd.to_datetime(frame[column], errors="coerce")
    days = sorted({value.date() for value in parsed.dropna().tolist()})
    if not days:
        raise AdapterError("交易日历全部日期不可解析")
    return days


def save_cn_trade_calendar(
    directory: Path | str,
    days: Iterable[date],
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """把交易日列表原子写入参考层，并记录来源、行数与校验和。

    Args:
        directory: 参考数据目录（`paths.reference`）；不存在时创建。
        days: 交易日；会去重排序后写入。
        fetched_at: 抓取时间；None 时取当前上海时间（由调用方决定是否注入固定时间）。

    Returns:
        dict: 写入的元数据（同 meta JSON 内容），含 `sha256`、`rows`、
            `as_of_min/as_of_max`、`source`、`source_url`。

    Raises:
        ValueError: `days` 为空——空日历会伪装成「无缺口」，禁止落盘。
    """
    ordered = sorted({day for day in days})
    if not ordered:
        raise ValueError("交易日历为空，拒绝落盘（空日历会把缺口冒充成无缺口）")
    stamp = fetched_at or datetime.now()
    iso = stamp.isoformat()
    csv_text = "trade_date\n" + "".join(f"{day.isoformat()}\n" for day in ordered)
    payload = csv_text.encode("utf-8")
    digest = sha256(payload).hexdigest()
    csv_path, meta_path = _calendar_paths(Path(directory), BASIS_CN_SSE)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "basis": BASIS_CN_SSE,
        "source": CN_SSE_SOURCE,
        "source_url": CN_SSE_URL,
        "fetched_at": iso,
        "as_of_min": ordered[0].isoformat(),
        "as_of_max": ordered[-1].isoformat(),
        "rows": len(ordered),
        "sha256": digest,
    }
    atomic_write_bytes(csv_path, payload)
    atomic_write_json(meta_path, meta)
    return meta


def load_trade_calendar(directory: Path | str, basis: str) -> TradeCalendar:
    """读取某一口径的日历；不可用时返回带原因的占位对象，不抛异常。

    Args:
        directory: 参考数据目录（`paths.reference`）。
        basis: `cn_sse` 或 `weekday`。

    Returns:
        TradeCalendar: `weekday` 口径永远 `available=False`（它是算法近似，
            由 `expected_days()` 现场生成日期，不落盘）；`cn_sse` 口径在读到
            完好文件时 `available=True`，否则 `integrity` 说明缺什么。
    """
    if basis == BASIS_WEEKDAY:
        return TradeCalendar(
            basis=BASIS_WEEKDAY,
            integrity="APPROX",
            note="自然工作日近似：未接入官方日历，节假日会被算成缺口（覆盖率偏保守）",
        )
    csv_path, meta_path = _calendar_paths(Path(directory), basis)
    if not csv_path.exists() or not meta_path.exists():
        return TradeCalendar(
            basis=basis,
            integrity=_INTEGRITY_MISSING,
            note=f"日历未落盘（缺 {csv_path.name} 或 {meta_path.name}）",
        )
    try:
        meta: dict[str, Any] = _read_json(meta_path)
        payload = csv_path.read_bytes()
    except Exception as exc:  # 半截 JSON/权限问题都算不可用，但必须留原因
        return TradeCalendar(
            basis=basis, integrity=_INTEGRITY_UNREADABLE, note=f"读取失败：{type(exc).__name__}: {exc}"
        )
    digest = sha256(payload).hexdigest()
    expected_digest = str(meta.get("sha256") or "")
    if not expected_digest:
        # 缺校验和与校验和不符同档：没有 sha256 就无法证明正文没被改过，不得判 OK 放行
        # （AGENTS §6.4 参考层「正文与校验和不符时视为不可用」，缺校验和同样不可用）。
        return TradeCalendar(
            basis=basis,
            integrity=_INTEGRITY_SHA_MISMATCH,
            note="meta 缺 sha256：无校验和的日历视为不可用，回落工作日近似",
        )
    if expected_digest != digest:
        return TradeCalendar(
            basis=basis,
            integrity=_INTEGRITY_SHA_MISMATCH,
            note=f"校验和不符（meta={expected_digest[:12]}… file={digest[:12]}…）",
        )
    frame = pd.read_csv(csv_path)
    column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
    parsed = pd.to_datetime(frame[column], errors="coerce")
    days = frozenset(value.date() for value in parsed.dropna().tolist())
    if not days:
        return TradeCalendar(basis=basis, integrity=_INTEGRITY_EMPTY, note="日历文件无有效日期")
    fetched = meta.get("fetched_at")
    return TradeCalendar(
        basis=basis,
        days=days,
        integrity=_INTEGRITY_OK,
        note=f"{meta.get('source', CN_SSE_SOURCE)} 共 {len(days)} 个交易日",
        rows=len(days),
        as_of_min=min(days),
        as_of_max=max(days),
        fetched_at=str(fetched) if fetched else None,
        source=str(meta.get("source") or CN_SSE_SOURCE),
    )


def _read_json(path: Path) -> dict[str, Any]:
    """读一个小 JSON 元数据文件。

    Args:
        path: meta 文件路径。

    Returns:
        dict: 解析结果。

    Raises:
        Exception: 文件缺失或 JSON 非法时原样抛出，由 `load_trade_calendar` 收敛成
            `integrity=UNREADABLE`，不静默当作空配置。
    """
    import json

    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    return loaded if isinstance(loaded, dict) else {}


def expected_days(
    basis: str,
    start: date,
    end: date,
    calendars: dict[str, TradeCalendar] | TradeCalendar | None = None,
) -> tuple[list[date], str]:
    """按口径给出闭区间内的应交易日，并返回可供读数使用的口径标签。

    Args:
        basis: 数据集声明的口径（`cn_sse`/`weekday`）。
        start: 起始日（含）。
        end: 结束日（含）。
        calendars: 已加载日历；可传 `{basis: TradeCalendar}`、单个
            `TradeCalendar`，或 None。`cn_sse` 声明但日历不可用时回落到
            `weekday`，标签会写明回落原因，绝不冒充官方日历。

    Returns:
        tuple: `(日期列表, 口径标签)`；口径标签形如
            `cn_sse(8797 日, 1990-12-19..2026-12-31)` 或
            `weekday_approx`、`weekday_approx(cn_sse 日历未落盘)`。
    """
    if isinstance(calendars, TradeCalendar):
        calendars = {calendars.basis: calendars}
    if basis == BASIS_CN_SSE:
        calendar = (calendars or {}).get(BASIS_CN_SSE)
        if calendar is not None and calendar.available:
            days = sorted(day for day in calendar.days if start <= day <= end)
            label = (
                f"cn_sse({calendar.rows} 日, {calendar.as_of_min}..{calendar.as_of_max}"
                f"{', fetched ' + calendar.fetched_at if calendar.fetched_at else ''})"
            )
            return days, label
        reason = calendar.note if calendar is not None else "日历未加载"
        return weekday_days(start, end), f"weekday_approx({reason})"
    return weekday_days(start, end), "weekday_approx"


def describe(calendars: dict[str, TradeCalendar] | TradeCalendar | None) -> dict[str, Any]:
    """把已加载日历整理成报告字段，供验收卡与覆盖首屏引用。

    Args:
        calendars: `load_all_calendars()` 的结果，或 None。

    Returns:
        dict: 每种口径的 `integrity/rows/as_of_min/as_of_max/fetched_at/source/note`；
            没有加载到时该口径不出现在字典里。
    """
    if isinstance(calendars, TradeCalendar):
        calendars = {calendars.basis: calendars}
    out: dict[str, Any] = {}
    for basis, calendar in (calendars or {}).items():
        out[basis] = {
            "integrity": calendar.integrity,
            "available": calendar.available,
            "rows": calendar.rows,
            "as_of_min": calendar.as_of_min.isoformat() if calendar.as_of_min else None,
            "as_of_max": calendar.as_of_max.isoformat() if calendar.as_of_max else None,
            "fetched_at": calendar.fetched_at,
            "source": calendar.source,
            "note": calendar.note,
        }
    return out


def load_all_calendars(directory: Path | str) -> dict[str, TradeCalendar]:
    """一次读齐所有会落盘的日历口径。

    Args:
        directory: 参考数据目录（`paths.reference`）。

    Returns:
        dict: `{basis: TradeCalendar}`，包含 `cn_sse`（读到什么算什么，
            不可用时也返回带原因的占位对象）与 `weekday`（近似口径标签）。
    """
    loaded = {BASIS_CN_SSE: load_trade_calendar(directory, BASIS_CN_SSE)}
    loaded[BASIS_WEEKDAY] = load_trade_calendar(directory, BASIS_WEEKDAY)
    return loaded
