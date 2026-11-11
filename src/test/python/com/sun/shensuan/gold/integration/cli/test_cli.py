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
    command_validate_manual,
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


def test_validate_manual(tmp_config, capsys):
    """验证 validate-manual 契约：模板齐全→OK；缺文件→YELLOW 披露（隔离沙箱，不读真实仓库数据）。

    旧写法直读仓库 data/manual（ gitignored），新鲜克隆里必 YELLOW——把生产数据当夹具，
    2026-11-09 克隆闸抓出。负对照抽掉一个模板，证明 YELLOW 臂真能翻动。
    """
    code = command_validate_manual(tmp_config, None)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "OK"
    (tmp_config.path("manual") / "tips10y.csv").unlink()
    code = command_validate_manual(tmp_config, None)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "YELLOW"


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


def _stub_step(calls, name, code):
    """造一个记录调用顺序并返回指定退出码的命令替身（离线编排测试用）。"""
    def stub(config, args):
        """替身命令：把自身名字追加进 calls 并返回预定退出码 code。"""
        calls.append(name)
        return code
    return stub


def test_daily_runs_all_steps_in_order(tmp_config, monkeypatch, capsys):
    """daily 顺序跑五步；doctor=1（NEEDS_HUMAN）是预期信号，不拖垮 daily（accept=0 → exit 0）。"""
    import com.sun.shensuan.gold.cli as cli_mod

    calls = []
    for name, code in [
        ("command_preflight", 0),
        ("command_ingest", 0),
        ("command_accept", 0),
        ("command_doctor", 1),
        ("command_rate", 0),
    ]:
        monkeypatch.setattr(cli_mod, name, _stub_step(calls, name, code))
    exit_code = cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    assert calls == [
        "command_preflight", "command_ingest", "command_accept", "command_doctor", "command_rate",
    ]
    assert exit_code == 0
    assert "daily" in capsys.readouterr().out


def test_daily_stops_at_preflight(tmp_config, monkeypatch):
    """preflight 非 0 → 硬停，ingest 不得执行（失败关闭）。"""
    import com.sun.shensuan.gold.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod, "command_preflight", _stub_step(calls, "preflight", 2))
    monkeypatch.setattr(cli_mod, "command_ingest", _stub_step(calls, "ingest", 0))
    exit_code = cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    assert calls == ["preflight"]
    assert exit_code == 2


def test_daily_stops_at_ingest(tmp_config, monkeypatch):
    """ingest 非 0 → 硬停，accept/doctor/rate 不得执行（没有新数据不验收）。"""
    import com.sun.shensuan.gold.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod, "command_preflight", _stub_step(calls, "preflight", 0))
    monkeypatch.setattr(cli_mod, "command_ingest", _stub_step(calls, "ingest", 2))
    monkeypatch.setattr(cli_mod, "command_accept", _stub_step(calls, "accept", 0))
    exit_code = cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    assert calls == ["preflight", "ingest"]
    assert exit_code == 2


def test_daily_accept_fail_continues_and_signals(tmp_config, monkeypatch):
    """accept=1（判红）仍跑完 doctor/rate，但 daily 退出码反映 accept 红（exit 1）。"""
    import com.sun.shensuan.gold.cli as cli_mod

    calls = []
    for name, code in [
        ("command_preflight", 0),
        ("command_ingest", 0),
        ("command_accept", 1),
        ("command_doctor", 1),
        ("command_rate", 0),
    ]:
        monkeypatch.setattr(cli_mod, name, _stub_step(calls, name, code))
    exit_code = cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    assert calls == ["command_preflight", "command_ingest", "command_accept", "command_doctor", "command_rate"]
    assert exit_code == 1


def test_daily_default_run_id_has_time_component(tmp_config, monkeypatch, capsys):
    """默认 run-id 必须含时分秒：否则同日重跑撞原始快照 append-only，全源误判 FAILED（2026-11-11 真跑抓出）。"""
    import re

    import com.sun.shensuan.gold.cli as cli_mod

    for name in ["command_preflight", "command_ingest", "command_accept", "command_doctor", "command_rate"]:
        monkeypatch.setattr(cli_mod, name, _stub_step([], name, 0))
    cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    out = capsys.readouterr().out
    assert re.search(r'"run_id":\s*"daily-\d{8}-\d{6}"', out), "daily 默认 run-id 需为 daily-YYYYMMDD-HHMMSS"
