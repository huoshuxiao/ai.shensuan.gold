"""单元层：common.logging_setup 的日志轮转与 JSONL 输出。

覆盖场景：保留窗口清理、结构化字段落盘、非法 retention_days 拒绝、
重复初始化时旧 handler 的文件句柄必须关闭。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from com.sun.shensuan.gold.common.logging_setup import purge_old_logs, setup_logging


def _shutdown_logger() -> None:
    """测试收尾：关闭并摘除 ``gold`` logger 的全部 handler，避免句柄跨用例泄漏。"""
    logger = logging.getLogger("gold")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def test_purge_keeps_retention_window(tmp_path):
    """验证 purge_old_logs 只删除超过 7 天保留期的 app-*.jsonl：第 7 天保留、第 8 天删除，无关文件不动。"""
    today = date(2026, 10, 19)
    for offset in range(0, 12):
        day = today - timedelta(days=offset)
        (tmp_path / f"app-{day.isoformat()}.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("keep", encoding="utf-8")
    removed = purge_old_logs(tmp_path, retention_days=7, today=today)
    assert len(removed) == 4
    remaining = sorted(path.name for path in tmp_path.iterdir())
    assert "unrelated.txt" in remaining
    assert f"app-{(today - timedelta(days=7)).isoformat()}.jsonl" in remaining
    assert f"app-{(today - timedelta(days=8)).isoformat()}.jsonl" not in remaining


def test_setup_logging_writes_jsonl(tmp_path):
    """验证 setup_logging 生成单个 JSONL 文件，message/run_id/level 等结构化字段按行落盘。"""
    logger = setup_logging(tmp_path, retention_days=7, level="INFO")
    logger.info("hello", extra={"run_id": "r1", "stage": "test", "status": "OK"})
    for handler in logger.handlers:
        handler.flush()
    files = sorted(tmp_path.glob("app-*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["run_id"] == "r1"
    assert payload["level"] == "INFO"
    _shutdown_logger()


def test_setup_logging_closes_previous_handlers(tmp_path):
    """验证重复 setup_logging 会关闭旧 FileHandler。

    仅 ``logger.handlers.clear()`` 只摘除引用、不关闭文件描述符：日链反复初始化
    会持续泄漏句柄，长跑后可能耗尽 fd。旧 handler 的 ``stream is None`` 是
    ``FileHandler.close()`` 的确定结果，用它区分“真关闭”与“只是摘掉”。
    """
    logger = setup_logging(tmp_path, retention_days=7, level="INFO")
    first = logger.handlers[0]
    assert isinstance(first, logging.FileHandler)
    assert first.stream is not None

    setup_logging(tmp_path, retention_days=7, level="INFO")
    assert first.stream is None
    assert len(logger.handlers) == 1
    assert logger.handlers[0] is not first
    _shutdown_logger()


def test_retention_days_must_be_positive(tmp_path):
    """负向验证 retention_days=0 抛 ValueError：保留期为 0 会立刻删光当日日志，必须拒绝。"""
    import pytest

    with pytest.raises(ValueError):
        purge_old_logs(tmp_path, retention_days=0)
