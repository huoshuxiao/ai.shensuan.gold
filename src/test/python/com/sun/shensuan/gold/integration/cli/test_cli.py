"""集成层：cli.main 的命令入口、JSON 输出与退出码契约。

覆盖场景：list-datasets、preflight、validate-manual、status 缺文件、
未知数据集 ingest 返回 RED、normalize 复核读取剔除留档、rate 汇率探针 JSON 契约、
coverage 附带自动诊断摘要。
"""
from __future__ import annotations

import json
from argparse import Namespace

import pandas as pd

from com.sun.shensuan.gold.cli import (
    command_coverage,
    command_ingest,
    command_normalize,
    main,
)
from com.sun.shensuan.gold.common.timeutil import now_shanghai
from com.sun.shensuan.gold.data.models import IngestOutcome, NodeStatus, Quality
from com.sun.shensuan.gold.data.normalize import normalize_frame
from com.sun.shensuan.gold.data.store import NormalizedStore


def test_list_datasets(capsys):
    """验证 list-datasets 退出码 0，输出含数据集与适配器清单（etf_518880、gpr）。"""
    code = main(["list-datasets"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "etf_518880" in payload["datasets"]
    assert "gpr" in payload["datasets"]
    assert "gpr" in payload["adapters"]


def test_preflight(capsys):
    """验证 preflight 退出码 0、overall 为 GREEN/YELLOW，且 python 与 config_hash 检查为 GREEN。"""
    code = main(["preflight"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["overall"] in {"GREEN", "YELLOW"}
    checks = {item["check"]: item["status"] for item in payload["checks"]}
    assert checks["python"] == "GREEN"
    assert checks["config_hash"] == "GREEN"


def test_validate_manual(capsys):
    """验证 validate-manual 退出码 0 且 status=OK（仓库手工模板合法）。"""
    code = main(["validate-manual"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "OK"


def test_rate_command(capsys):
    """验证 rate 探针的 JSON 契约：as_of 回读一致，source 只可能是两条序列之一。"""
    code = main(["rate", "--date", "2026-10-22"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["as_of"] == "2026-10-22"
    assert payload["source"] in {
        "akshare.currency_boc_sina",
        "safe.rmb_mid",
        None,
    }
    assert payload["quality"] in {"GREEN", "DEGRADED", "MISSING"}
    assert isinstance(payload["diverged"], bool)


def test_status_missing_file(capsys):
    """验证 status 查询不存在日期时退出码 2 且 status=MISSING，缺失状态必须显式。"""
    code = main(["status", "--date", "1999-01-01"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "MISSING"


def test_unknown_dataset_returns_red(capsys):
    """负向验证 ingest 未知数据集返回退出码 2 与 RED，未知输入不得被当作成功。"""
    code = main(["ingest", "--datasets", "not_a_dataset", "--run-id", "x"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "RED"


def test_ingest_prints_dataset_summary(tmp_config, capsys, monkeypatch):
    """验证 ingest 子命令输出 run_id、全局状态与逐数据集摘要，成功链退出码为 0。"""
    outcome = IngestOutcome(
        dataset="etf_518880",
        status=NodeStatus.OK,
        quality=Quality.GREEN,
        rows=5,
        total_rows=5,
        new_rows=5,
        notes=[],
        normalized_path="/tmp/etf_518880.csv",
        raw_path="/tmp/raw/etf_518880.csv",
    )
    report = {
        "run_id": "run-cli",
        "global_status": "OK",
        "missing": [],
        "degraded": [],
        "red": [],
        "core_not_green": [],
        "revised_only": [],
        "datasets": {
            "etf_518880": {
                "status": "OK",
                "quality": "GREEN",
                "rows": 5,
                "total_rows": 5,
                "new_rows": 5,
                "notes": [],
                "quality_detail": {"dataset": "akshare.fund_etf_hist_sina"},
            }
        },
    }
    monkeypatch.setattr("com.sun.shensuan.gold.cli.run_ingest", lambda *args, **kwargs: ([outcome], report))

    code = command_ingest(
        tmp_config,
        Namespace(datasets="etf_518880", start=None, end=None, run_id="run-cli"),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["run_id"] == "run-cli"
    assert payload["global_status"] == "OK"
    assert payload["datasets"]["etf_518880"]["rows"] == 5
    assert payload["datasets"]["etf_518880"]["quality_detail"]["dataset"] == "akshare.fund_etf_hist_sina"


def test_normalize_reports_rejected_rows(tmp_config, capsys):
    """验证 normalize 复核读取剔除留档：近期缺失坏行必须把绿档压成 YELLOW 并标 RECENT_MISSING_ROWS。"""
    store = NormalizedStore(tmp_config.path("normalized"))
    spec = tmp_config.data_sources.datasets["us10y"]
    dates = pd.bdate_range(end=now_shanghai().date(), periods=5)
    raw = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d").tolist(),
            "us10y": [4.0, 4.1, 4.2, 4.3, None],
        }
    )
    result = normalize_frame(raw, spec, now_shanghai(), "run-recheck")
    assert len(result.rejected) == 1
    store.write("us10y", result.frame)
    store.write_rejected("us10y", result.rejected)

    code = command_normalize(tmp_config, Namespace(datasets="us10y"))
    payload = json.loads(capsys.readouterr().out)
    report = payload["datasets"]["us10y"]
    assert code == 0
    assert report["status"] == "YELLOW"
    assert report["invalid_rows"] == 1
    assert report["rejected_rows"] == 1
    assert report["recent_missing_rows"] == 1
    assert "RECENT_MISSING_ROWS" in report["flags"]


def test_coverage_includes_remediation_summary(tmp_config, capsys):
    """验证 coverage 第一屏附带自动诊断摘要，且没有诊断文件时不伪造该字段。"""
    normalized = tmp_config.path("normalized")
    quality = {
        "schema_version": "1",
        "run_id": "run-coverage",
        "generated_at": "2026-10-25T00:00:00+08:00",
        "reference_date": "2026-10-23",
        "config_hash": tmp_config.config_hash,
        "global_status": "DEGRADED",
        "missing": [],
        "degraded": [],
        "red": [],
        "datasets": {},
    }
    (normalized / "_quality.json").write_text(json.dumps(quality), encoding="utf-8")

    code = command_coverage(tmp_config, Namespace())
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "remediation" not in payload

    remediation = {
        "schema_version": "1",
        "overall": "CONTAINED",
        "counts": {"AUTO_RESOLVED": 0, "CONTAINED": 1, "NEEDS_HUMAN": 0},
        "issues": [{"issue_id": "wti_cl:ZERO_FLOW", "status": "CONTAINED"}],
        "next_actions": ["PRICE_ONLY_BLOCK_VOLUME_FIELDS"],
        "global_block": False,
        "report_hash": "sha256:abc",
    }
    (normalized / "_remediation.json").write_text(
        json.dumps(remediation), encoding="utf-8"
    )
    code = command_coverage(tmp_config, Namespace())
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["remediation"]["overall"] == "CONTAINED"
    assert payload["remediation"]["report_hash"] == "sha256:abc"
    assert payload["remediation"]["next_actions"] == ["PRICE_ONLY_BLOCK_VOLUME_FIELDS"]
