"""GPR vintage 读取：只用抓取时点已存在的原始快照，防止把修订后历史当当时可见值。

职责：解析 GPR 原始文件，并从 ``data/raw`` 的 append-only 快照中挑出
``available_at`` 之前可见、且与 ``as_of`` 足够接近的那一份，返回单日值及
vintage 元数据。

不负责：生成特征、决定是否加仓、补齐历史修订；这些属于 P2/P3 或人工数据治理。

关键不变量：
- 只读原始快照（``data/raw/.../*.meta.json``），不读规范化层 ``gpr.csv``；
  后者是修订后合并表，不能当 PIT 证据（AGENTS §12.8）。
- ``fetched_at > available_at`` 的快照一律不可见；晚到的修订不能被提前使用。
- ``as_of`` 与快照抓取日相差超过 ``GPR_MAX_LAG_DAYS`` 时返回 ``None``：
  2026 年抓到的快照不能反推 1985 年的“当时值”，历史段仍标 ``REVISED_ONLY``。
- 快照校验和与 meta 不一致时抛错，不静默使用被改过的原始文件。
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from ..common.hashing import sha256_bytes
from ..common.timeutil import parse_datetime

GPR_SOURCE = "matteoiacoviello.gpr_daily_recent"
GPR_MAX_LAG_DAYS = 7
"""GPR 快照允许的最大“抓取日 - as_of”间隔（自然日）。

GPR 是日频且有发布/修订滞后；超过一周仍拿不到的快照不能证明是当时可见值。
该值只约束 vintage 读取，不改变数据源的 ``stale_days`` 质量闸。
"""


def parse_gpr_payload(payload: bytes, suffix: str) -> pd.DataFrame:
    """把 GPR 原始字节解析成统一列名、数值化的 DataFrame。

    官方文件列名大小写不一，这里统一转小写匹配；日期必须使用 ``date`` 列，
    ``GPRD`` 是指数值不是日期。解析失败抛 ``ValueError``，由调用方决定转成
    ``AdapterError`` 还是直接上抛。

    Args:
        payload: 原始文件字节（``.xls``/``.xlsx`` 或 CSV）。
        suffix: 文件后缀，大小写不敏感；``.xls``/``.xlsx`` 走 Excel 分支，
            其余走 CSV（先 UTF-8，解码失败再回退 GBK）。

    Returns:
        列为 ``date/gprd`` 及可用可选列的 DataFrame；``date`` 为 datetime，
        其余数值列已 ``to_numeric``。

    Raises:
        ValueError: 文件无法解析、缺少 ``date`` 或 ``GPRD`` 列。
    """
    normalized_suffix = suffix.lower()
    if normalized_suffix in {".xls", ".xlsx"}:
        raw = pd.read_excel(io.BytesIO(payload), engine="xlrd")
    else:
        try:
            raw = pd.read_csv(io.BytesIO(payload))
        except UnicodeDecodeError:
            # 官网备用 CSV 可能是 GBK；只在解码失败时回退，避免掩盖其他解析错误。
            raw = pd.read_csv(io.BytesIO(payload), encoding="gbk")

    columns = {str(column).strip().lower(): column for column in raw.columns}
    if "date" not in columns:
        raise ValueError(f"GPR 缺少 date 列；实际列：{list(raw.columns)}")
    if "gprd" not in columns:
        raise ValueError("GPR 缺少 GPRD 列")
    rename = {columns["date"]: "date", columns["gprd"]: "gprd"}
    optional = ("gprd_act", "gprd_threat", "gprd_ma7", "gprd_ma30", "n10d")
    for name in optional:
        if name in columns:
            rename[columns[name]] = name
    frame = raw.rename(columns=rename)
    keep = ["date", "gprd"] + [name for name in optional if name in frame.columns]
    frame = frame[keep].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.loc[frame["date"].notna()].copy()
    for column in keep[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.reset_index(drop=True)


def _load_meta(meta_path: Path) -> dict:
    """读取并校验一份 GPR 快照的 ``.meta.json``。

    Args:
        meta_path: ``*.meta.json`` 路径。

    Returns:
        含 ``dataset/source/fetched_at/raw_file/sha256`` 的字典。

    Raises:
        ValueError: JSON 损坏或缺少必需字段。
    """
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"GPR 快照 meta 无法读取：{meta_path}：{exc}") from exc
    required = ("dataset", "source", "fetched_at", "raw_file", "sha256")
    missing = [key for key in required if not meta.get(key)]
    if missing:
        raise ValueError(f"GPR 快照 meta 缺少字段 {missing}：{meta_path}")
    if meta["dataset"] != "gpr":
        raise ValueError(f"GPR 快照 meta 的 dataset 不是 gpr：{meta_path}")
    return meta


def load_gpr_vintage(
    raw_root: Path,
    as_of: date,
    available_at: datetime,
    *,
    max_lag_days: int = GPR_MAX_LAG_DAYS,
    verify_integrity: bool = True,
) -> dict | None:
    """从原始快照中读取 ``as_of`` 当日 GPR 值及其 vintage 元数据。

    选择规则：只看 ``fetched_at <= available_at`` 的快照；再要求
    ``0 <= fetched_at.date() - as_of <= max_lag_days``；取 ``fetched_at``
    最新的一份。若最新可见快照没有该日行，返回 ``None``，不回退到更晚的修订。

    Args:
        raw_root: 原始层根目录（通常是 ``config.path("raw")``）。
        as_of: 要查询的 GPR 日期。
        available_at: 决策时点；必须带时区，早于该时刻的快照才可见。
        max_lag_days: 允许的“抓取日 - as_of”最大自然日数；默认
            ``GPR_MAX_LAG_DAYS``。过大等于允许用修订后历史冒充当时值。
        verify_integrity: 是否校验原始文件 sha256 与 meta 一致；默认 True。

    Returns:
        含 ``as_of/gprd/.../vintage/raw_file/sha256/source`` 的字典；
        无可见快照、无该日行或 ``gprd`` 为空时返回 ``None``。

    Raises:
        ValueError: ``available_at`` 无时区、``max_lag_days < 0``、meta 损坏、
            原始文件校验和不符，或原始文件无法解析。
        FileNotFoundError: meta 指向的原始文件不存在。
    """
    if available_at.tzinfo is None:
        raise ValueError("available_at 必须带时区")
    if max_lag_days < 0:
        raise ValueError("max_lag_days 不能为负")
    source_dir = raw_root / GPR_SOURCE
    if not source_dir.exists():
        return None

    candidates: list[tuple[datetime, Path, dict]] = []
    for meta_path in source_dir.glob("**/*.meta.json"):
        meta = _load_meta(meta_path)
        fetched_at = parse_datetime(meta["fetched_at"])
        lag_days = (fetched_at.date() - as_of).days
        if fetched_at > available_at:
            continue
        if lag_days < 0 or lag_days > max_lag_days:
            continue
        candidates.append((fetched_at, meta_path.parent / meta["raw_file"], meta))
    if not candidates:
        return None

    # 取 available_at 之前最新的一份快照：它代表当时最新的可见知识。
    fetched_at, raw_path, meta = max(candidates, key=lambda item: (item[0], item[1].name))
    payload = raw_path.read_bytes()
    if verify_integrity and sha256_bytes(payload) != meta["sha256"]:
        raise ValueError(f"GPR 原始快照校验和不符：{raw_path}")
    frame = parse_gpr_payload(payload, raw_path.suffix)
    matched = frame.loc[frame["date"].dt.date == as_of]
    if matched.empty:
        return None
    row = matched.iloc[-1]
    if pd.isna(row["gprd"]):
        return None

    result = {
        "as_of": as_of.isoformat(),
        "gprd": float(row["gprd"]),
        "vintage": fetched_at.isoformat(),
        "raw_file": str(raw_path),
        "sha256": meta["sha256"],
        "source": meta["source"],
    }
    for column in ("gprd_act", "gprd_threat", "gprd_ma7", "gprd_ma30", "n10d"):
        if column in row.index and not pd.isna(row[column]):
            result[column] = float(row[column])
    return result
