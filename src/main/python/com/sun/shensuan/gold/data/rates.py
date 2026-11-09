"""汇率选择与同日差异闸：折算价优先、中间价兜底、同日绝不混算。

P1 数据层提供确定性选择函数 :func:`select_usdcny`；P2 事实包只从这里取
USD/CNY，不得自行拼接两条汇率序列（AGENTS §5.2 同日兜底条款）。本模块
不抓取、不写盘，输入是规范化长表（含 ``date/usdcny/quality/flags`` 列）。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 同日两条汇率（BOC 折算价 vs SAFE 中间价）的相对差异告警阈值：超过即标
# diverged，上层必须按 DEGRADED 披露，不得静默使用（AGENTS §5.2 同日兜底
# 条款）。P1 先作模块常量，P2 接入策略配置后改为配置注入。
USDCNY_DIVERGENCE_WARN = 0.0005

SOURCE_BOC = "akshare.currency_boc_sina"
SOURCE_SAFE = "safe.rmb_mid"


@dataclass(frozen=True)
class RateSelection:
    """某一天的 USD/CNY 选择结果；``value=None`` 表示 MISSING。

    Attributes:
        as_of: 目标日期 ``YYYY-MM-DD``。
        value: 选中的汇率；MISSING 时为 None。
        source: 实际使用的那条序列来源；MISSING 时为 None。
        quality: ``GREEN``（正常）、``DEGRADED``（同日差异超阈值）、
            ``MISSING``（两条都不可用）。
        fallback_reason: 走兜底或 MISSING 时的原因；正常为 None。
        divergence: 同日两序列都可用时的相对差异 |boc−mid|/mid；至少一条
            不可用时为 None（盘中临时值不参与比较，避免误报）。
        diverged: 差异是否超过告警阈值。
    """

    as_of: str
    value: float | None
    source: str | None
    quality: str
    fallback_reason: str | None
    divergence: float | None
    diverged: bool

    def as_dict(self) -> dict:
        """序列化为 JSON 友好字典，键名固定，供 CLI 与事实包复用。"""
        return {
            "as_of": self.as_of,
            "value": self.value,
            "source": self.source,
            "quality": self.quality,
            "fallback_reason": self.fallback_reason,
            "divergence": self.divergence,
            "diverged": self.diverged,
        }


def _row_for_date(frame: pd.DataFrame, as_of: str) -> pd.Series | None:
    """从长表里取出 ``as_of`` 当天的最后一行；无该日行返回 None。

    日期列先统一成 ``YYYY-MM-DD`` 字符串再比较：主表读回是全字符串，
    测试夹具可能给 Timestamp，两种都要能认。
    """
    if frame is None or frame.empty or "date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    mask = dates == as_of
    if not mask.any():
        return None
    return frame.loc[mask].iloc[-1]


def _usable(row: pd.Series | None) -> tuple[bool, float | None]:
    """判断一行是否可作为当天的正式值。

    判据：``quality == GREEN``、flags 无 ``PROVISIONAL``、数值可解析且非空。
    任何一条不满足即不可用——不填 0、不前向填充、不用旧值冒充。
    """
    if row is None:
        return False, None
    if str(row.get("quality", "")) != "GREEN":
        return False, None
    flags = row.get("flags", "")
    if pd.isna(flags):
        flags = ""
    if "PROVISIONAL" in str(flags):
        return False, None
    raw = row.get("usdcny", None)
    if raw is None:
        return False, None
    value = pd.to_numeric(raw, errors="coerce")
    if pd.isna(value):
        return False, None
    return True, float(value)


def select_usdcny(
    as_of: str,
    usdcny: pd.DataFrame,
    usdcny_mid: pd.DataFrame,
    divergence_warn: float = USDCNY_DIVERGENCE_WARN,
) -> RateSelection:
    """按“折算价优先、中间价兜底”选择某一天的 USD/CNY（确定性）。

    判据（全部确定性，不填 0、不插值、不平均）：
      1. 折算价当天行可用（GREEN、无 PROVISIONAL、数值非空）→ 用折算价。
      2. 否则中间价当天行可用 → 用中间价兜底，fallback_reason 写明原因。
      3. 两条都不可用 → MISSING，value/source 为 None。
    同日两条都可用（GREEN）时计算相对差异 |boc−mid|/mid；超过
    ``divergence_warn`` 时 ``diverged=True`` 且 quality 压成 DEGRADED，此时仍
    按判据取折算价，绝不把两条平均或拼接。盘中临时值（PROVISIONAL）不参与
    差异比较，避免用未发布值误报分化。

    Args:
        as_of: 目标日期 ``YYYY-MM-DD``。
        usdcny: BOC 折算价长表，至少含 date/usdcny/quality/flags 列。
        usdcny_mid: SAFE 中间价长表，同上。
        divergence_warn: 同日差异告警阈值（相对比例，默认 0.0005）。

    Returns:
        RateSelection；MISSING 时 value/source 为 None、quality 为
        ``MISSING``，fallback_reason 写明原因。
    """
    boc_row = _row_for_date(usdcny, as_of)
    safe_row = _row_for_date(usdcny_mid, as_of)
    boc_ok, boc_value = _usable(boc_row)
    safe_ok, safe_value = _usable(safe_row)

    divergence: float | None = None
    diverged = False
    if boc_value is not None and safe_value is not None and safe_value > 0:
        divergence = abs(boc_value - safe_value) / safe_value
        diverged = divergence > divergence_warn

    if boc_ok:
        return RateSelection(
            as_of=as_of,
            value=boc_value,
            source=SOURCE_BOC,
            quality="DEGRADED" if diverged else "GREEN",
            fallback_reason=None,
            divergence=divergence,
            diverged=diverged,
        )
    if safe_ok:
        return RateSelection(
            as_of=as_of,
            value=safe_value,
            source=SOURCE_SAFE,
            quality="DEGRADED" if diverged else "GREEN",
            fallback_reason="折算价不可用（缺失或 PROVISIONAL），按 §5.2 兜底条款用 SAFE 中间价",
            divergence=divergence,
            diverged=diverged,
        )
    return RateSelection(
        as_of=as_of,
        value=None,
        source=None,
        quality="MISSING",
        fallback_reason="折算价与中间价当天均不可用",
        divergence=divergence,
        diverged=diverged,
    )
