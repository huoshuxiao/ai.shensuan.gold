"""集成层：data.pipeline.PipelineStatusWriter/load_status 的节点状态机。

覆盖场景：RUNNING/OK/SKIPPED 状态流转与原子写、失败汇总 FAILED、
残留 RUNNING 超时改 TIMEOUT。
"""
from __future__ import annotations

import json

from com.sun.shensuan.gold.data.models import NodeStatus
from com.sun.shensuan.gold.data.pipeline import PipelineStatusWriter, load_status


def test_writer_states_and_atomic_json(tmp_path):
    """验证状态文件随 start/finish/skip 更新：RUNNING→OK 带耗时、下游 SKIPPED、overall=DEGRADED，且每次落盘可解析。"""
    path = tmp_path / "pipeline_status.json"
    writer = PipelineStatusWriter(path, "run-1", "2026-10-19", ["a", "b"])
    writer.start("a")
    running = json.loads(path.read_text(encoding="utf-8"))
    assert running["nodes"][0]["status"] == "RUNNING"
    writer.finish("a", NodeStatus.OK, artifact_hash="sha256:x")
    writer.mark_skipped(["b"], "上游失败")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["nodes"][0]["status"] == "OK"
    assert payload["nodes"][0]["duration_ms"] is not None
    assert payload["nodes"][1]["status"] == "SKIPPED"
    assert payload["overall"] == "DEGRADED"


def test_failed_node_makes_overall_failed(tmp_path):
    """验证任一节点 FAILED 时 overall=FAILED，错误码保留。"""
    writer = PipelineStatusWriter(tmp_path / "s.json", "run-1", "2026-10-19", ["a"])
    writer.start("a")
    writer.finish("a", NodeStatus.FAILED, error_code="SOURCE_TIMEOUT")
    payload = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert payload["overall"] == "FAILED"


def test_stale_running_becomes_timeout(tmp_path):
    """验证读取时把超期的残留 RUNNING 改为 TIMEOUT/STALE_RUNNING，防止崩溃后节点永远显示运行中。"""
    path = tmp_path / "s.json"
    writer = PipelineStatusWriter(path, "run-1", "2026-10-19", ["a"])
    writer.start("a")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["nodes"][0]["started_at"] = "2020-01-01T00:00:00+08:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_status(path, stale_after_ms=1)
    assert loaded["nodes"][0]["status"] == "TIMEOUT"
    assert loaded["nodes"][0]["error_code"] == "STALE_RUNNING"
    assert loaded["overall"] == "FAILED"
