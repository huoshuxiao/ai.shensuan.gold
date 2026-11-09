"""单元层：data.vintage 的 GPR 原始快照读取与 PIT 选择。

覆盖场景：只选 available_at 之前可见的快照、修订后值按 vintage 生效、
历史段因快照太晚而拒绝、sha256 校验、无时区参数拒绝。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from com.sun.shensuan.gold.common.hashing import sha256_bytes
from com.sun.shensuan.gold.common.timeutil import parse_datetime
from com.sun.shensuan.gold.data.vintage import GPR_SOURCE, load_gpr_vintage


def _write_snapshot(root: Path, day: str, run: str, fetched_at: str, rows: str) -> Path:
    """在原始层写一份 GPR CSV 快照及其 meta，返回原始文件路径。

    Args:
        root: 原始层根目录（测试用 tmp_path）。
        day: 快照归档日期目录（YYYY-MM-DD）。
        run: 快照文件名中的 run_id。
        fetched_at: 抓取时间，带 Asia/Shanghai 偏移。
        rows: CSV 正文（含表头）。

    Returns:
        写入的原始 CSV 路径。
    """
    directory = root / GPR_SOURCE / day
    directory.mkdir(parents=True, exist_ok=True)
    raw_path = directory / f"gpr.{run}.csv"
    payload = rows.encode("utf-8")
    raw_path.write_bytes(payload)
    meta = {
        "dataset": "gpr",
        "source": GPR_SOURCE,
        "fetched_at": fetched_at,
        "status": "OK",
        "notes": [],
        "rows": max(rows.count("\n") - 1, 0),
        "raw_file": raw_path.name,
        "sha256": sha256_bytes(payload),
        "bytes": len(payload),
    }
    (directory / f"{raw_path.name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return raw_path


def test_vintage_uses_latest_visible_snapshot(tmp_path):
    """同一 as_of 在修订前后应返回各自 vintage 的值，晚到的快照不能提前用。"""
    root = tmp_path / "raw"
    _write_snapshot(
        root,
        "2026-01-02",
        "snap1",
        "2026-01-02T23:00:00+08:00",
        "date,gprd,gprd_act,gprd_threat\n2026-01-01,100,90,110\n2026-01-02,101,91,111\n",
    )
    _write_snapshot(
        root,
        "2026-01-03",
        "snap2",
        "2026-01-03T23:00:00+08:00",
        "date,gprd,gprd_act,gprd_threat\n2026-01-01,110,95,120\n2026-01-02,101,91,111\n",
    )

    before = load_gpr_vintage(root, date(2026, 1, 1), parse_datetime("2026-01-02T23:30:00+08:00"))
    after = load_gpr_vintage(root, date(2026, 1, 1), parse_datetime("2026-01-03T23:30:00+08:00"))
    assert before is not None and before["gprd"] == pytest.approx(100.0)
    assert after is not None and after["gprd"] == pytest.approx(110.0)
    assert before["vintage"] != after["vintage"]


def test_vintage_before_first_snapshot_returns_none(tmp_path):
    """决策时点早于所有快照时返回 None，不能拿未来抓取的数据回填。"""
    root = tmp_path / "raw"
    _write_snapshot(
        root,
        "2026-01-02",
        "snap1",
        "2026-01-02T23:00:00+08:00",
        "date,gprd\n2026-01-01,100\n",
    )
    result = load_gpr_vintage(root, date(2026, 1, 1), parse_datetime("2026-01-02T22:00:00+08:00"))
    assert result is None


def test_vintage_rejects_revised_history_far_from_as_of(tmp_path):
    """as_of 与快照抓取日相差超过 7 天时返回 None：历史段仍是 REVISED_ONLY。"""
    root = tmp_path / "raw"
    _write_snapshot(
        root,
        "2026-01-03",
        "snap1",
        "2026-01-03T23:00:00+08:00",
        "date,gprd\n2025-12-01,100\n",
    )
    result = load_gpr_vintage(root, date(2025, 12, 1), parse_datetime("2026-01-03T23:30:00+08:00"))
    assert result is None


def test_vintage_detects_tampered_raw_file(tmp_path):
    """原始文件被改但 meta 校验和未变时必须报错，不能静默使用。"""
    root = tmp_path / "raw"
    raw_path = _write_snapshot(
        root,
        "2026-01-02",
        "snap1",
        "2026-01-02T23:00:00+08:00",
        "date,gprd\n2026-01-01,100\n",
    )
    raw_path.write_text("date,gprd\n2026-01-01,999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="校验和不符"):
        load_gpr_vintage(root, date(2026, 1, 1), parse_datetime("2026-01-02T23:30:00+08:00"))


def test_vintage_missing_raw_file_raises(tmp_path):
    """meta 指向的原始文件不存在时抛 FileNotFoundError，不返回伪造值。"""
    root = tmp_path / "raw"
    raw_path = _write_snapshot(
        root,
        "2026-01-02",
        "snap1",
        "2026-01-02T23:00:00+08:00",
        "date,gprd\n2026-01-01,100\n",
    )
    raw_path.unlink()
    with pytest.raises(FileNotFoundError):
        load_gpr_vintage(root, date(2026, 1, 1), parse_datetime("2026-01-02T23:30:00+08:00"))


def test_vintage_requires_aware_available_at(tmp_path):
    """available_at 必须带时区；naive datetime 直接拒绝。"""
    root = tmp_path / "raw"
    _write_snapshot(
        root,
        "2026-01-02",
        "snap1",
        "2026-01-02T23:00:00+08:00",
        "date,gprd\n2026-01-01,100\n",
    )
    with pytest.raises(ValueError, match="必须带时区"):
        load_gpr_vintage(root, date(2026, 1, 1), datetime(2026, 1, 2, 23, 30))
