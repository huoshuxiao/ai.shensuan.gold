"""时间与 PIT 工具。所有时间戳带时区，存储用 ISO-8601。

职责：统一时区口径（存储/展示都用 Asia/Shanghai）、日期与时间解析、
``available_at``（数据在现实中首次可见的时点）换算，以及按 cutoff 做 PIT 截断。

关键不变量：
- 对外的时间戳一律带时区；naive datetime 在 ``to_shanghai`` 处直接拒绝，
  不允许“默认当成本地时间”的隐式假设。
- 存储格式为 ISO-8601（``YYYY-MM-DDTHH:MM:SS+08:00``）；纯日期为 ``YYYY-MM-DD``。
- ``available_at`` 规则按**自然日**计算，不查交易日历、不跳周末与节假日；
  交易日/半日市判断属于上游日历模块，不在本文件内。
- PIT 截断只做 ``available_at <= cutoff``；cutoff 的取值（07:00）由调用方/
  配置给出，本文件不写死。
- 抓取早于 ``available_at`` 的行是临时快照（``PROVISIONAL``）：即使满足 cutoff，
  也不得进入事实包；``filter_fact_pack`` 同时执行时间截断与临时行剔除。

不负责：交易日历维护、节假日判断、夏令时规则（Asia/Shanghai 无夏令时）、
把修订数据标记为 ``REVISED_ONLY``（由 PIT 字段生成层负责）。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")

ISO_DATE = "%Y-%m-%d"

# PIT 可见时点规则的唯一权威清单：配置加载与 available_at_for 共用，
# 避免“配置放行、跑到 PIT 才炸”以及两处清单各自漂移。
AVAILABLE_AT_RULES = (
    "same_day_0915",
    "same_day_1530",
    "same_day_1800",
    "next_day_0600",
    "as_of_plus_3d",
    "fetched_at",
    "explicit",
)

# 连续性判据的分母口径清单（只命名口径，不在本文件维护日历数据；
# 真正的日历抓取/落盘/读取见 `data/trade_calendar.py`）：
# - `cn_sse`：上交所官方交易日（新浪 `klc_td_sh.txt`），境内股票/期货/现货源用它；
# - `weekday`：周一至周五近似，没有官方日历可直连的源（外汇报价日、美国交易日）
#   必须用它，并在读数里写明「近似」，不得冒充官方口径。
CALENDAR_BASE_CN_SSE = "cn_sse"
CALENDAR_BASE_WEEKDAY = "weekday"
CALENDAR_BASES = (CALENDAR_BASE_CN_SSE, CALENDAR_BASE_WEEKDAY)


def now_shanghai() -> datetime:
    """返回当前时刻，时区为 Asia/Shanghai。

    Returns:
        datetime: 带 ``+08:00`` 偏移的 aware datetime；用于 ``fetched_at``、
        日志与账本时间戳。

    Raises:
        无。

    Notes:
        依赖系统时钟；测试应注入固定时间，不要直接断言本函数的返回值。
    """
    return datetime.now(tz=SHANGHAI)


def to_shanghai(value: datetime) -> datetime:
    """把带时区的 datetime 转换为 Asia/Shanghai 时区。

    Args:
        value: 任意时区的 aware datetime；同一时刻换时区，epoch 不变。

    Returns:
        datetime: 同一时刻的 Asia/Shanghai 表示（不做日期截断、不改微秒）。

    Raises:
        ValueError: ``value`` 为 naive（无 tzinfo），拒绝猜测时区。

    Notes:
        这是“日期归属哪个交易日”的前置步骤：先统一到上海时区，再取 date，
        避免 UTC 晚间数据被算到前一天。
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime 不允许；必须带时区")
    return value.astimezone(SHANGHAI)


def iso(value: datetime) -> str:
    """把 datetime 转成 Asia/Shanghai 时区的 ISO-8601 字符串。

    Args:
        value: 带时区的 datetime。

    Returns:
        str: 形如 ``2026-10-16T15:30:00+08:00``；有微秒时保留微秒。

    Raises:
        ValueError: ``value`` 为 naive。

    Notes:
        这是写入 CSV/JSON/账本的统一时间格式；反向解析用 ``parse_datetime``。
    """
    return to_shanghai(value).isoformat()


def parse_date(value: object) -> date:
    """从多种输入中提取“日期”部分（不涉及时区换算）。

    支持：``datetime``/``date`` 对象；``pd.Timestamp``（datetime 子类）；
    字符串 ``YYYY-MM-DD``、``YYYY/MM/DD``、带 ``HH:MM[:SS]`` 或以 ``T``
    分隔的完整时间戳。

    Args:
        value: 日期对象或日期/时间字符串。

    Returns:
        datetime.date: 仅日期，无时间、无时区。

    Raises:
        ValueError: 字符串无法按 ``%Y-%m-%d`` 解析（如缺少分隔符、月日越界）；
            ``None`` 会先被转成 ``"None"``，同样落在这个分支。

    Notes:
        - 字符串先做 ``/``→``-``，再在空格或 ``T`` 处截断，最后只取前 10 个字符；
        - 对带时区的时间戳，日期按字面取值，不转换到上海时区——调用方若需要
          “上海日期”，应先用 ``to_shanghai`` 再取 ``.date()``；
        - 不做交易日判断，周末/节假日原样返回。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    if " " in text:
        text = text.split(" ", 1)[0]
    if "T" in text:
        text = text.split("T", 1)[0]
    return datetime.strptime(text[:10], ISO_DATE).date()


def parse_datetime(value: object, tz: ZoneInfo = SHANGHAI) -> datetime:
    """把输入解析为带时区的 datetime；naive 值按 ``tz`` 补齐。

    解析顺序：
    1. ``datetime``：带 tzinfo 原样返回（不转换时区）；naive 则挂上 ``tz``；
    2. ``date``：当日 00:00:00 附加 ``tz``；
    3. 字符串：``/`` 换 ``-`` 后先试 ``datetime.fromisoformat``（支持
       ``2026-10-16T15:30:00+08:00`` 等带偏移格式），naive 结果挂 ``tz``；
    4. 回退格式：``%Y-%m-%d %H:%M:%S``、``%Y-%m-%d %H:%M``、``%Y-%m-%d``。

    Args:
        value: datetime/date/字符串。
        tz: naive 值采用的时区；默认 ``SHANGHAI``（Asia/Shanghai）。

    Returns:
        datetime: 一定带 tzinfo 的时间；字符串自带偏移时保留原偏移，
        不会被强制改成 ``tz``。

    Raises:
        ValueError: 所有格式都无法解析，错误消息包含原始值。

    Notes:
        不做夏令时/交易日判断；``tz`` 只影响“缺省时区”，不影响已带偏移的值。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=tz)
    text = str(value).strip().replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间：{value!r}")


def available_at_for(
    as_of: date,
    rule: str,
    fetched_at: datetime,
    explicit: object | None = None,
) -> datetime:
    """把 as_of 按发布规则换算成 available_at。

    单位/时区：输入 ``as_of`` 是数据自身的日期（date，不含时区）；输出是
    Asia/Shanghai 的 aware datetime。所有窗口均按**自然日**计算，不查交易日历，
    周末与节假日不跳过——例如周五数据的 ``next_day_0600`` 会落在周六 06:00，
    这是已知口径；截断由调用方用 ``filter_available`` 与给定 cutoff（文档口径
    为交易日 07:00）完成，本函数不判断“今天能不能看”。

    rule 取值：
      same_day_0915：当日 09:15 可见（人民币中间价发布后，当天固定）
      same_day_1530：当日 15:30 可见（A 股/ETF 收盘后）
      same_day_1800：当日 18:00 可见（SGE/夜盘前后数据）
      next_day_0600：``as_of + 1 自然日`` 06:00（美市收盘类数据）
      as_of_plus_3d：``as_of + 3 自然日`` 06:00（CFTC 等按发布滞后）
      fetched_at：直接用抓取时间（无 vintage 的修订类数据，如今日下载的 GPR）
      explicit：用 ``explicit`` 参数给出的 available_at（手工 CSV 的显式列）

    合法值由模块常量 ``AVAILABLE_AT_RULES`` 单一维护；配置加载阶段按同一
    清单校验，非法值不会拖到日链中途才炸。

    Args:
        as_of: 数据对应日期（不含时区）。
        rule: 上述规则名之一。
        fetched_at: 本次抓取时间，必须带时区。
        explicit: 仅 ``explicit`` 规则使用；可为 datetime、ISO 字符串或 date；
            ``None`` 或 NaN 视为缺失。

    Returns:
        datetime: 带 Asia/Shanghai 时区的可见时点，可直接交给 ``iso``。

    Raises:
        ValueError: ``rule`` 未知、``explicit`` 规则缺少有效值，或
            ``fetched_at`` 为 naive（由 ``to_shanghai`` 抛出）。

    Notes:
        ``fetched_at`` 规则返回抓取时刻本身，即使它晚于 ``as_of`` 很多；
        这是“只有修订后序列、没有真正 vintage”时的保守落点，调用方必须
        同时把该数据集标为 ``REVISED_ONLY``。
    """
    fetched = to_shanghai(fetched_at)
    if rule not in AVAILABLE_AT_RULES:
        raise ValueError(f"未知 available_at_rule：{rule}")
    if rule == "fetched_at":
        # 无 vintage 数据只能以“抓取那一刻”作为首次可见时间，历史段标 REVISED_ONLY。
        return fetched
    if rule == "explicit":
        # NaN 是 pandas 读 CSV 空列的常见产物，必须与 None 一样视为缺失。
        if explicit is None or (isinstance(explicit, float) and explicit != explicit):
            raise ValueError("explicit 规则缺少 available_at")
        return to_shanghai(parse_datetime(explicit))
    if rule == "same_day_0915":
        # 人民币中间价当日 09:15 左右发布且当天固定，09:15 后抓取即最终值。
        return datetime.combine(as_of, time(9, 15), tzinfo=SHANGHAI)
    if rule == "same_day_1530":
        return datetime.combine(as_of, time(15, 30), tzinfo=SHANGHAI)
    if rule == "same_day_1800":
        return datetime.combine(as_of, time(18, 0), tzinfo=SHANGHAI)
    if rule == "next_day_0600":
        # 自然日 +1，不跳周末；周五数据会落在周六 06:00，属已知口径。
        return datetime.combine(as_of + timedelta(days=1), time(6, 0), tzinfo=SHANGHAI)
    if rule == "as_of_plus_3d":
        # 自然日 +3，用于 CFTC 等“发布滞后”数据；同样不查交易日历。
        return datetime.combine(as_of + timedelta(days=3), time(6, 0), tzinfo=SHANGHAI)
    # 常量已收录但分支未实现：新增规则时由这里兜底，防止静默返回 None。
    raise ValueError(f"available_at_rule 尚未实现：{rule}")


def provisional_mask(
    frame,
    fetched_column: str = "fetched_at",
    available_column: str = "available_at",
):
    """标出“抓取早于可见时点”的临时行。

    这类行来自盘中抓取：抓取时刻早于 ``available_at``，说明数据源当时尚未给出
    最终值，不能当作收盘/发布后的正式读数。返回布尔 Series，True 表示临时行。

    Args:
        frame: 含 PIT 时间列的 DataFrame。
        fetched_column: 抓取时间列名，默认 ``fetched_at``。
        available_column: 可见时点列名，默认 ``available_at``。

    Returns:
        与 frame 同索引的布尔 Series；空表返回空 Series。

    Raises:
        KeyError: 缺少任一 PIT 时间列。
        ValueError: 时间列无法解析，或时间戳缺少时区。
    """
    missing = [column for column in (fetched_column, available_column) if column not in frame.columns]
    if missing:
        raise KeyError(f"缺少 PIT 时间列：{missing}")
    if frame.empty:
        return frame[fetched_column].map(lambda _value: False)
    fetched = frame[fetched_column].map(parse_datetime)
    available = frame[available_column].map(parse_datetime)
    return fetched < available


def filter_fact_pack(
    frame,
    cutoff: datetime,
    available_column: str = "available_at",
    fetched_column: str = "fetched_at",
):
    """按 cutoff 截断并剔除临时行，供事实包使用。

    与 ``filter_available`` 的区别：除了 ``available_at <= cutoff``，还要求
    ``fetched_at >= available_at``。盘中抓取的行即使可见时点已过，也必须等
    收盘后重抓的正式快照才能进入事实包。

    Args:
        frame: 含 PIT 时间列的 DataFrame。
        cutoff: 决策 cutoff，必须带时区。
        available_column: 可见时点列名，默认 ``available_at``。
        fetched_column: 抓取时间列名，默认 ``fetched_at``。

    Returns:
        截断后的副本；列与索引保持原样。

    Raises:
        ValueError: cutoff 缺时区、PIT 时间无法解析。
        KeyError: 缺少时间列。
    """
    eligible = filter_available(frame, cutoff, available_column)
    if eligible.empty:
        return eligible.copy()
    temporary = provisional_mask(eligible, fetched_column, available_column)
    return eligible.loc[~temporary].copy()


def filter_available(frame, cutoff: datetime, column: str = "available_at"):
    """只保留 cutoff 之前可见的记录（PIT 截断）。

    对 ``frame[column]`` 的每一行调用 ``parse_datetime`` 解析成带时区 datetime，
    再统一转换到 Asia/Shanghai 与 ``cutoff`` 比较，保留 ``available_at <= cutoff``
    的行并返回副本。

    Args:
        frame: 含可见时间列的 DataFrame；逐行解析，不做向量化猜测。
        cutoff: 决策 cutoff（如交易日 07:00 Asia/Shanghai），必须带时区。
        column: 可见时间列名，默认 ``available_at``。

    Returns:
        DataFrame: 截断后的副本，列与索引保持原样（不重置索引）；原 frame 不变。

    Raises:
        ValueError: ``cutoff`` 为 naive，或某行时间无法解析。
        KeyError: ``column`` 不存在。

    Notes:
        使用 ``map(parse_datetime)`` 而非 ``pd.to_datetime``：前者会为无偏移的
        字符串补上上海时区，并与 ``available_at_for`` 的输出口径一致；无法解析
        的行会显式报错，而不是静默变成 ``NaT`` 后被比较吞掉。
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff 必须带时区")
    parsed = frame[column].map(lambda v: parse_datetime(v))
    return frame.loc[parsed <= to_shanghai(cutoff)].copy()
