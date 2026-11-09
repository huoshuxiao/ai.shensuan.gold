"""地缘政治与快讯适配器：GPR、GDELT、CLS/Sina/SHMET 快讯、手工事件日历。

职责：把量化指数（GPR）、新闻元数据（快讯/GDELT）与手工事件日历抓成统一
DataFrame，PIT 字段、质量判定与 severity 校验交给规范化层。

关键不变量：
- v1 不把新闻原文喂给 LLM；新闻类结果只保留
  ``published_at/title/content_hash``，正文只参与哈希，不落规范化主表。
- GPR 官方文件是修订后序列且无 vintage，notes 固定标 ``REVISED_ONLY``，
  不得用于 H-GEO 晋升（见 AGENTS.md §12.8）。
- GDELT 是可选增强源：限流（HTTP 429）/超时/非 200 一律抛
  ``DegradedSourceError``，由编排层标 DEGRADED 且不重试，不阻塞主链。
- 空数据源返回空表而非伪造数据；由规范化层判 ``EMPTY_SOURCE``（事件日历除外，
  空日历表示「未登记事件」，不是「无风险」）。
- 快讯单行时间戳非法只丢该行并在 notes 计数；一行脏数据不得中断整源抓取。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from ...common.errors import AdapterError, DegradedSourceError
from ...common.timeutil import SHANGHAI, now_shanghai, parse_datetime
from ...config.loader import DatasetConfig
from ..models import FetchResult
from ..vintage import parse_gpr_payload
from .base import AdapterContext, register
from .market import _filter_dates, _require


def content_hash(text: str) -> str:
    """计算文本的 SHA-256 摘要，统一带 ``sha256:`` 前缀。

    用于快讯去重与审计：下游只保留哈希而不保留正文，避免新闻原文进入事实包
    或 LLM 提示词。

    Args:
        text: 原始文本，按 UTF-8 编码后哈希。

    Returns:
        形如 ``sha256:<64 位十六进制>`` 的字符串。
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _combine_datetime(date_value: object, time_value: object) -> datetime:
    """把「发布日期 + 发布时间」两列拼成带时区的 datetime。

    CLS 快讯把日期与时间拆成两列，且时间列可能已自带日期；按三种情况处理：
    时间列为空→只解析日期；时间列不含日期且长度 ≤8（``HH:MM:SS``）→拼接；
    其余情况→时间列本身已完整，直接解析。

    Args:
        date_value: 日期列原始值（可能为空串）。
        time_value: 时间列原始值（可能为 None/空串）。

    Returns:
        ``Asia/Shanghai`` 时区的 datetime（缺失时区由 ``parse_datetime`` 补齐）。

    Raises:
        ValueError: 两列都无法解析成时间时由 ``parse_datetime`` 抛出。
    """
    date_text = str(date_value).strip()
    time_text = str(time_value).strip() if time_value is not None else ""
    if not time_text:
        return parse_datetime(date_text)
    if date_text and date_text not in time_text and len(time_text) <= 8:
        return parse_datetime(f"{date_text} {time_text}")
    return parse_datetime(time_text)


def _news_time(value: object, date_value: object | None = None) -> datetime | None:
    """解析一条快讯的发布时间；坏值返回 None，由调用方丢弃并计数。

    快讯源偶发脏时间（空串、``not-a-time``、拼接失败的半截日期）。单行解析
    失败不应中断整源抓取，更不能伪造时间；调用方丢弃该行并在 notes 写明条数。

    Args:
        value: 时间列原始值；``date_value`` 为 None 时直接解析它。
        date_value: CLS 的发布日期；给出时与 ``value`` 拼接解析。

    Returns:
        带时区的 datetime；无法解析时为 None。

    Raises:
        无；解析异常在内部转成 None。
    """
    try:
        if date_value is None:
            return parse_datetime(value)
        return _combine_datetime(date_value, value)
    except (TypeError, ValueError):
        return None


def _news_notes(count: int, dropped: int) -> list[str]:
    """生成快讯抓取 notes：去重后条数，以及被丢弃的坏时间行数（仅非零时出现）。"""
    message = f"快讯 {count} 条（去重后）"
    if dropped:
        message += f"；丢弃发布时间非法 {dropped} 行"
    return [message]


def _news_frame(records: list[dict]) -> pd.DataFrame:
    """把原始新闻记录整理成统一三列并去重排序。

    去重键为正文哈希（同一条快讯可能被多个源或多次抓取重复推送）；标题为空时
    截取正文前 40 字兜底，保证下游展示不为空。正文本身不进入结果。

    Args:
        records: 每项含 ``published_at``、``title``、``content`` 的字典列表。

    Returns:
        列为 ``published_at/title/content_hash`` 的 DataFrame，按发布时间稳定
        排序；全部记录无效时返回同列空表（由规范化层判 EMPTY_SOURCE）。
    """
    rows = []
    seen = set()
    for record in records:
        content = str(record.get("content", "")).strip()
        if not content:
            continue
        digest = content_hash(content)
        if digest in seen:
            continue
        seen.add(digest)
        title = str(record.get("title", "")).strip() or content[:40]
        rows.append(
            {
                "published_at": record["published_at"],
                "title": title,
                "content_hash": digest,
            }
        )
    frame = pd.DataFrame(rows, columns=["published_at", "title", "content_hash"])
    if frame.empty:
        return frame
    frame = frame.sort_values("published_at", kind="mergesort").reset_index(drop=True)
    return frame


@register("gpr")
def fetch_gpr(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取 GPR 日度地缘政治风险指数（本地 Excel/CSV 或 HTTP 下载）。

    数据口径：
    - 官方文件列名大小写不一，这里统一转小写匹配；日期必须用 ``date`` 列，
      ``GPRD`` 是指数值不是日期。
    - 仅保留 ``date/gprd/gprd_act/gprd_threat/gprd_ma7/gprd_ma30/n10d``；
      缺失的可选列不补造。
    - 无 vintage：notes 固定标注 ``REVISED_ONLY``，不得用于 H-GEO 晋升。

    Args:
        spec: 数据集配置；``options.local_path`` 与 ``options.url`` 二选一
            （本地优先），``options.download_timeout_s`` 可覆盖上下文超时。
        start / end: 按 ``date`` 的闭区间过滤，None 表示不限制。
        context: 适配器上下文，仅提供缺省下载超时。

    Returns:
        ``FetchResult``；``raw_bytes`` 保存原始文件，便于事后复现。

    Raises:
        AdapterError: 未配置 url/local_path、本地文件不存在、下载失败、
            HTTP 非 200，或解析后缺少 ``date``/``GPRD`` 列。
    """
    import requests

    fetched_at = now_shanghai()
    url = str(spec.options.get("url", ""))
    timeout = int(spec.options.get("download_timeout_s", context.request_timeout_s))
    local_path = spec.options.get("local_path")
    if local_path:
        from pathlib import Path

        path = Path(str(local_path))
        if not path.exists():
            raise AdapterError(f"GPR 本地文件不存在：{path}")
        payload = path.read_bytes()
        suffix = path.suffix.lower()
    else:
        if not url:
            raise AdapterError("GPR 未配置 url 或 local_path")
        try:
            response = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise AdapterError(f"GPR 下载失败：{exc}") from exc
        if response.status_code != 200:
            raise AdapterError(f"GPR 下载 HTTP {response.status_code}")
        payload = response.content
        # 官网当前只提供 .xls；后缀硬编码成 .xls 是为了让下方走 Excel 分支
        suffix = ".xls"

    try:
        frame = parse_gpr_payload(payload, suffix)
    except ValueError as exc:
        # 解析失败属于源文件契约问题，保留 AdapterError 类型供编排层重试/降级。
        raise AdapterError(str(exc)) from exc
    frame = _filter_dates(frame, start, end)
    return FetchResult(
        frame=frame.reset_index(drop=True),
        source=spec.source,
        fetched_at=fetched_at,
        raw_bytes=payload,
        raw_suffix=suffix if suffix in {".xls", ".xlsx"} else ".csv",
        notes=["GPR 为修订后序列，无 vintage：全部标 REVISED_ONLY，不得用于 H-GEO 晋升"],
    )


@register("news_cls")
def fetch_news_cls(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取财联社电报快讯（AkShare ``stock_info_global_cls``）。

    接口字段：``标题/内容/发布日期/发布时间``；日期与时间分列，需要拼接。
    只保留标题、正文哈希与发布时间，正文不落库。

    Args:
        spec: 数据集配置；``symbol`` 缺省为“全部”。
        start / end: 未使用（接口只返回最近快讯，时间窗口交给下游筛选）。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``；notes 记录去重后的条数，空表由规范化层判 EMPTY_SOURCE。

    失败行为：网络/接口异常不在此层吞掉，原样上抛给编排层重试；接口返回空表
    时返回空结果，不伪造数据。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.stock_info_global_cls(symbol=spec.symbol or "全部")
    if raw is None or raw.empty:
        return FetchResult(pd.DataFrame(columns=["published_at", "title", "content_hash"]), spec.source, fetched_at)
    records = []
    dropped = 0
    for _, row in raw.iterrows():
        published = _news_time(row.get("发布时间", ""), row.get("发布日期", ""))
        if published is None:
            dropped += 1
            continue
        records.append(
            {
                "published_at": published,
                "title": row.get("标题", ""),
                "content": row.get("内容", ""),
            }
        )
    frame = _news_frame(records)
    return FetchResult(frame=frame, source=spec.source, fetched_at=fetched_at, notes=_news_notes(len(frame), dropped))


@register("news_sina")
def fetch_news_sina(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取新浪财经全球快讯（AkShare ``stock_info_global_sina``）。

    接口字段：``时间/内容``；没有独立标题，标题留空由 ``_news_frame`` 用正文
    前 40 字兜底。只保留正文哈希，原文不落库。

    Args:
        spec: 数据集配置（未使用 symbol）。
        start / end: 未使用（时间窗口交给下游）。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``；notes 记录去重后的条数。

    失败行为：网络/接口异常原样上抛，由编排层重试；空表返回空结果。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.stock_info_global_sina()
    if raw is None or raw.empty:
        return FetchResult(pd.DataFrame(columns=["published_at", "title", "content_hash"]), spec.source, fetched_at)
    records = []
    dropped = 0
    for _, row in raw.iterrows():
        published = _news_time(row.get("时间", ""))
        if published is None:
            dropped += 1
            continue
        records.append(
            {
                "published_at": published,
                "title": "",
                "content": row.get("内容", ""),
            }
        )
    frame = _news_frame(records)
    return FetchResult(frame=frame, source=spec.source, fetched_at=fetched_at, notes=_news_notes(len(frame), dropped))


@register("news_shmet")
def fetch_news_shmet(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取上海金属网快讯（AkShare ``futures_news_shmet``）。

    接口字段：``发布时间/内容``；``symbol`` 只接受接口支持的分类
    （全部/要闻/VIP/财经/铜/铝/铅/锌/镍/锡/贵金属/小金属），默认“贵金属”，
    不得传“黄金”（接口无此分类，会返回空）。

    Args:
        spec: 数据集配置；``symbol`` 为分类名。
        start / end: 未使用（时间窗口交给下游）。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``；notes 记录去重后的条数。

    失败行为：网络/接口异常原样上抛，由编排层重试；空表返回空结果。
    """
    import akshare as ak

    fetched_at = now_shanghai()
    raw = ak.futures_news_shmet(symbol=spec.symbol or "贵金属")
    if raw is None or raw.empty:
        return FetchResult(pd.DataFrame(columns=["published_at", "title", "content_hash"]), spec.source, fetched_at)
    records = []
    dropped = 0
    for _, row in raw.iterrows():
        published = _news_time(row.get("发布时间", ""))
        if published is None:
            dropped += 1
            continue
        records.append(
            {
                "published_at": published,
                "title": "",
                "content": row.get("内容", ""),
            }
        )
    frame = _news_frame(records)
    return FetchResult(frame=frame, source=spec.source, fetched_at=fetched_at, notes=_news_notes(len(frame), dropped))


@register("gdelt")
def fetch_gdelt(spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext) -> FetchResult:
    """抓取 GDELT 2.0 DOC API 新闻列表（可选增强源）。

    限流策略：探针中该接口返回 HTTP 429；这里把请求异常/429/非 200 统一转成
    ``DegradedSourceError``，由编排层标 DEGRADED 且不重试，避免拖垮主链。
    时间字段 ``seendate`` 为 UTC ``YYYYMMDDTHHMMSSZ``，转成 Asia/Shanghai；
    无法解析的记录直接跳过。

    Args:
        spec: 数据集配置；``options`` 支持
            ``url/query/maxrecords/download_timeout_s``。
        start / end: 未使用（查询窗口由 ``query`` 决定）。
        context: 适配器上下文（未使用）。

    Returns:
        ``FetchResult``；标题列存新闻标题，正文列拼“标题 + URL”，仅用于哈希。

    Raises:
        DegradedSourceError: 请求异常、HTTP 429 或非 200。
    """
    import requests

    fetched_at = now_shanghai()
    url = str(spec.options.get("url", "https://api.gdeltproject.org/api/v2/doc/doc"))
    params = {
        "query": spec.options.get("query", "gold OR bullion"),
        "mode": "artlist",
        "maxrecords": int(spec.options.get("maxrecords", 250)),
        "format": "json",
    }
    try:
        response = requests.get(url, params=params, timeout=min(int(spec.options.get("download_timeout_s", 60)), 120))
    except requests.RequestException as exc:
        raise DegradedSourceError(f"GDELT 请求失败：{exc}") from exc
    if response.status_code == 429:
        raise DegradedSourceError("GDELT HTTP 429 限流：可选源降级，不影响主链")
    if response.status_code != 200:
        raise DegradedSourceError(f"GDELT HTTP {response.status_code}")
    payload = response.json()
    records = []
    for article in payload.get("articles", []):
        seen = str(article.get("seendate", ""))
        try:
            published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(SHANGHAI)
        except ValueError:
            continue
        title = str(article.get("title", ""))
        url_text = str(article.get("url", ""))
        records.append({"published_at": published, "title": title, "content": title + "\n" + url_text})
    frame = _news_frame(records)
    return FetchResult(frame=frame, source=spec.source, fetched_at=fetched_at, notes=[f"GDELT {len(frame)} 条"])


@register("manual_geo_events")
def fetch_manual_geo_events(
    spec: DatasetConfig, start: date | None, end: date | None, context: AdapterContext
) -> FetchResult:
    """读取手工维护的地缘事件日历 ``data/manual/geo_events.csv``。

    CSV 以字符串读入并保留空值（``keep_default_na=False``），避免 ``severity``
    等字段被 pandas 自动转成 NaN；severity 只允许 1/2/3，合法性校验在规范化层。
    空日历表示「未登记事件」，不等于「无地缘风险」。

    Args:
        spec: 数据集配置；``manual_path`` 相对 ``context.manual_dir``，
            ``required_columns`` 为必需列清单。
        start / end: 按 ``date`` 列做闭区间过滤。
        context: 适配器上下文，提供手工目录。

    Returns:
        ``FetchResult``；文件为空时返回带必需列的空表并在 notes 说明。

    Raises:
        AdapterError: 未配置 manual_path、文件不存在，或缺必需列。
    """
    if not spec.manual_path:
        raise AdapterError(f"{spec.source} 未配置 manual_path")
    path = context.manual_dir / spec.manual_path
    if not path.exists():
        raise AdapterError(f"手工事件日历不存在：{path}")
    fetched_at = now_shanghai()
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    if raw.empty:
        frame = pd.DataFrame(columns=spec.required_columns)
        return FetchResult(frame=frame, source=spec.source, fetched_at=fetched_at, notes=["事件日历为空"])
    _require(raw, spec.required_columns, spec.source)
    frame = raw.copy()
    frame = _filter_dates(frame, start, end)
    return FetchResult(frame=frame.reset_index(drop=True), source=spec.source, fetched_at=fetched_at)
