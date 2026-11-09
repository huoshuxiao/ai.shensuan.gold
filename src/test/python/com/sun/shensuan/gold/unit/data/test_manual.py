"""单元层：data.manual 的手工 CSV 校验与模板生成。

覆盖场景：合法/空事件表、缺列、severity 越界、available_at 早于 date、
event_id 重复、序列缺 source_url、目录缺文件报告、模板不覆盖已有文件。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from com.sun.shensuan.gold.common.errors import ManualDataError
from com.sun.shensuan.gold.data.manual import ensure_templates, validate_manual_dir, validate_manual_file

EVENT_HEADER = "event_id,date,type,severity,region,source_url,available_at\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """测试辅助：在 tmp_path 下写入指定文本文件并返回其路径。"""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_geo_events(tmp_path):
    """验证合法 geo_events.csv（severity=3）通过校验，状态 OK 且行数为 1。"""
    path = _write(
        tmp_path,
        "geo_events.csv",
        EVENT_HEADER
        + "e1,2026-10-01,war,3,MiddleEast,https://example.com/a,2026-10-01T12:00:00+08:00\n",
    )
    report = validate_manual_file(path)
    assert report["status"] == "OK"
    assert report["rows"] == 1


def test_empty_geo_events_allowed(tmp_path):
    """验证只有表头的空事件表标 EMPTY 而非报错：空表是合法且必须可见的状态。"""
    path = _write(tmp_path, "geo_events.csv", EVENT_HEADER)
    report = validate_manual_file(path)
    assert report["status"] == "EMPTY"
    assert report["rows"] == 0


def test_missing_column_rejected(tmp_path):
    """负向验证缺必需列报“缺少字段”：缺列会让 PIT 与严重度字段静默丢失。"""
    path = _write(tmp_path, "geo_events.csv", "event_id,date\n" + "e1,2026-10-01\n")
    with pytest.raises(ManualDataError, match="缺少字段"):
        validate_manual_file(path)


def test_bad_severity_rejected(tmp_path):
    """负向验证 severity=4 越界被拒：严重度只允许 1/2/3，越界值会污染地缘评分。"""
    path = _write(
        tmp_path,
        "geo_events.csv",
        EVENT_HEADER + "e1,2026-10-01,war,4,X,https://example.com/a,2026-10-01T12:00:00+08:00\n",
    )
    with pytest.raises(ManualDataError, match="severity"):
        validate_manual_file(path)


def test_available_at_before_date_rejected(tmp_path):
    """负向验证 available_at 早于事件日期被拒：这等于事件发生前就知道结果，属于未来函数。"""
    path = _write(
        tmp_path,
        "geo_events.csv",
        EVENT_HEADER + "e1,2026-10-02,war,2,X,https://example.com/a,2026-10-01T12:00:00+08:00\n",
    )
    with pytest.raises(ManualDataError, match="available_at 早于 date"):
        validate_manual_file(path)


def test_duplicate_event_id_rejected(tmp_path):
    """负向验证 event_id 重复被拒：重复事件会被重复计数，放大地缘信号。"""
    path = _write(
        tmp_path,
        "geo_events.csv",
        EVENT_HEADER
        + "e1,2026-10-01,war,3,X,https://example.com/a,2026-10-01T12:00:00+08:00\n"
        + "e1,2026-10-02,sanction,2,X,https://example.com/b,2026-10-02T12:00:00+08:00\n",
    )
    with pytest.raises(ManualDataError, match="event_id 重复"):
        validate_manual_file(path)


def test_series_requires_source_url(tmp_path):
    """负向验证时间序列行缺 source_url 被拒：手工数据无来源就无法复核，不得进入事实包。"""
    path = _write(
        tmp_path,
        "tips10y.csv",
        "date,value,available_at,source_url\n2026-10-01,2.0,2026-10-01T18:00:00+08:00,\n",
    )
    with pytest.raises(ManualDataError):
        validate_manual_file(path)


def test_validate_dir_reports_missing(tmp_path):
    """验证 validate_manual_dir 对缺失文件逐个报 MISSING，四个模板文件齐全可见。"""
    reports = validate_manual_dir(tmp_path)
    assert {report["file"] for report in reports} == {
        "geo_events.csv",
        "tips10y.csv",
        "au_index.csv",
        "gld_holdings.csv",
    }
    assert all(report["status"] == "MISSING" for report in reports)


def test_ensure_templates_never_overwrites(tmp_path):
    """验证 ensure_templates 只创建缺失模板、不覆盖已有文件；覆盖会抹掉人工录入数据。"""
    existing = _write(tmp_path, "tips10y.csv", "date,value,available_at,source_url\n2026-10-01,1,x,y\n")
    before = existing.read_text(encoding="utf-8")
    created = ensure_templates(tmp_path)
    assert existing.read_text(encoding="utf-8") == before
    assert {path.name for path in created} == {"geo_events.csv", "au_index.csv", "gld_holdings.csv"}
