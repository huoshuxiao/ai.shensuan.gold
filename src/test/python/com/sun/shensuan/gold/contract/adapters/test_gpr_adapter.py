"""契约层：data.adapters.geo.fetch_gpr 对 GPR 文件的口径契约。

覆盖场景：本地 CSV 解析与 start 过滤、REVISED_ONLY 标注、列名大小写兼容、
15,261 行日频规模夹具、缺 GPRD 列报错。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from com.sun.shensuan.gold.common.errors import AdapterError
from com.sun.shensuan.gold.data.adapters.base import AdapterContext
from com.sun.shensuan.gold.data.adapters.geo import fetch_gpr
from com.sun.shensuan.gold.data.models import Quality
from com.sun.shensuan.gold.data.normalize import normalize_frame


def _spec(repo_config, path):
    """测试辅助：复制 GPR 数据集规格并把 local_path 指向临时文件，避免联网。"""
    spec = repo_config.data_sources.datasets["gpr"].model_copy(deep=True)
    spec.options = {"local_path": str(path)}
    return spec


def test_gpr_local_csv_parse(repo_config, tmp_path):
    """验证本地 GPR CSV 解析：按 start 过滤、保留原始字节，并标注 REVISED_ONLY。"""
    path = tmp_path / "gpr.csv"
    path.write_text(
        "date,GPRD,GPRD_ACT,GPRD_THREAT,GPRD_MA7\n"
        "2026-10-11,100.0,90.0,110.0,99.0\n"
        "2026-10-12,101.5,91.0,111.0,100.0\n"
        "2026-10-13,102.0,92.0,112.0,101.0\n",
        encoding="utf-8",
    )
    spec = _spec(repo_config, path)
    result = fetch_gpr(spec, date(2026, 10, 12), None, AdapterContext(manual_dir=tmp_path))
    assert len(result.frame) == 2
    assert list(result.frame["date"].dt.strftime("%Y-%m-%d")) == ["2026-10-12", "2026-10-13"]
    assert result.raw_bytes == path.read_bytes()
    assert any("REVISED_ONLY" in note for note in result.notes)


def test_gpr_normalize_marks_revised_only(repo_config, tmp_path):
    """验证 GPR 规范化后 vintage=REVISED_ONLY、quality=DEGRADED、available_at 取抓取时刻。"""
    path = tmp_path / "gpr.csv"
    path.write_text("date,GPRD\n2026-10-13,102.0\n", encoding="utf-8")
    spec = _spec(repo_config, path)
    result = fetch_gpr(spec, None, None, AdapterContext(manual_dir=tmp_path))
    normalized = normalize_frame(result.frame, spec, result.fetched_at, "run-gpr")
    assert normalized.status == Quality.YELLOW
    assert normalized.frame.iloc[0]["vintage"] == "REVISED_ONLY"
    assert normalized.frame.iloc[0]["available_at"] == normalized.frame.iloc[0]["fetched_at"]


def test_gpr_uppercase_columns(repo_config, tmp_path):
    """验证大写列名（DATE/GPRD）统一为小写，兼容官网文件格式差异。"""
    path = tmp_path / "gpr.csv"
    path.write_text("DATE,GPRD\n2026-10-13,102.0\n", encoding="utf-8")
    spec = _spec(repo_config, path)
    result = fetch_gpr(spec, None, None, AdapterContext(manual_dir=tmp_path))
    assert list(result.frame.columns)[:2] == ["date", "gprd"]


def test_gpr_daily_scale_fixture(repo_config, tmp_path):
    """验证 AGENTS §17.3 的 GPR 规模夹具：1985-01-01..2026-10-13 日频 15,261 行完整解析。"""
    dates = pd.date_range("1985-01-01", "2026-10-13", freq="D")
    assert len(dates) == 15_261
    path = tmp_path / "gpr_scale.csv"
    pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "GPRD": [100.0] * len(dates)}).to_csv(
        path, index=False
    )
    spec = _spec(repo_config, path)
    result = fetch_gpr(spec, None, None, AdapterContext(manual_dir=tmp_path))
    assert len(result.frame) == 15_261
    assert result.frame["date"].iloc[0] == pd.Timestamp("1985-01-01")
    assert result.frame["date"].iloc[-1] == pd.Timestamp("2026-10-13")


def test_gpr_missing_gprd_raises(repo_config, tmp_path):
    """负向验证缺 GPRD 列抛 AdapterError：核心指标缺失时不得用其他列顶替。"""
    path = tmp_path / "gpr.csv"
    path.write_text("date,OTHER\n2026-10-13,1\n", encoding="utf-8")
    spec = _spec(repo_config, path)
    with pytest.raises(AdapterError, match="GPRD"):
        fetch_gpr(spec, None, None, AdapterContext(manual_dir=tmp_path))
