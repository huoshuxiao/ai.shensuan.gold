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
from com.sun.shensuan.gold.common.timeutil import SHANGHAI, now_shanghai
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


# ---------------------------------------------------------------------------
# T-045：daily 的监控与互斥（每步耗时 / 汇总落盘 / stage 日志 / 锁 / 观察有效性）
# ---------------------------------------------------------------------------

DAILY_STEPS = [
    ("command_preflight", 0),
    ("command_ingest", 0),
    ("command_accept", 0),
    ("command_doctor", 1),
    ("command_rate", 0),
]


def _stub_daily_steps(monkeypatch, calls, codes=None):
    """把 daily 调用的五个子命令全部换成替身；calls 记录顺序，codes 覆盖各自退出码。"""
    import com.sun.shensuan.gold.cli as cli_mod

    overrides = codes or {}
    for name, default in DAILY_STEPS:
        monkeypatch.setattr(cli_mod, name, _stub_step(calls, name, overrides.get(name, default)))
    return cli_mod


def _json_documents(text):
    """从 stdout 里按顺序切出多个 JSON 文档；`_print_json` 用 indent=2 多行打印，不能按行取。"""
    decoder = json.JSONDecoder()
    docs: list[dict] = []
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        document, cursor = decoder.raw_decode(text, cursor)
        docs.append(document)
    return docs


def test_daily_summary_reports_step_durations_and_total(tmp_config, monkeypatch, capsys):
    """监控读数：汇总每步必须带 `duration_ms`，并有 `total_ms`；§15.7 要求阶段耗时可见，不能只有退出码。"""
    cli_mod = _stub_daily_steps(monkeypatch, [])
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 0
    payload = _json_documents(capsys.readouterr().out)[-1]
    assert {s["step"] for s in payload["daily"]} == {"preflight", "ingest", "accept", "doctor", "rate"}
    for step in payload["daily"]:
        assert isinstance(step["duration_ms"], int) and step["duration_ms"] >= 0, f"缺耗时：{step}"
    assert isinstance(payload["total_ms"], int) and payload["total_ms"] >= sum(
        s["duration_ms"] for s in payload["daily"]
    ), "total_ms 必须覆盖各步耗时之和"


def test_daily_writes_summary_artifact(tmp_config, monkeypatch):
    """汇总必须落盘（无人值守时 stdout 会丢）：`data/decisions/<date>/daily_summary.json` 带 run_id/config_hash/产物指针。"""
    cli_mod = _stub_daily_steps(monkeypatch, [])
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 0
    summary = cli_mod._daily_summary_path(tmp_config)
    assert summary.is_file(), "daily 汇总未落盘，cron/复核无凭证可读"
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1"
    assert payload["run_id"] and payload["config_hash"]
    assert payload["artifacts"]["pipeline_status"].endswith("pipeline_status.json")
    assert payload["artifacts"]["quality"].endswith("_quality.json")


def test_daily_refuses_when_another_daily_holds_the_lock(tmp_config, monkeypatch):
    """负对照：锁被活进程持有时 daily 必须拒绝，且不执行任何子命令、不覆写质量报告（2026-11-11 覆写事故的形状）。"""
    import os

    calls: list[str] = []
    cli_mod = _stub_daily_steps(monkeypatch, calls)
    lock = tmp_config.path("data") / cli_mod.DAILY_LOCK_NAME
    lock.write_text(f"{os.getpid()} startup\n", encoding="utf-8")
    code = cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    assert code == cli_mod.EXIT_BUSY, f"并发日更应返回 {cli_mod.EXIT_BUSY}，实得 {code}"
    assert calls == [], f"锁被占用时不得执行任何子命令：{calls}"
    assert not cli_mod._daily_summary_path(tmp_config).exists(), "拒绝执行时不得留下汇总产物"


def test_daily_clears_stale_lock_and_runs(tmp_config, monkeypatch):
    """放行对照：持锁进程已死（pid 不存在）时，daily 清掉陈旧锁并照常跑完，跑完释放锁。"""
    import subprocess

    calls: list[str] = []
    cli_mod = _stub_daily_steps(monkeypatch, calls)
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = tmp_config.path("data") / cli_mod.DAILY_LOCK_NAME
    lock.write_text(f"{dead.pid} startup\n", encoding="utf-8")
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 0
    assert [c for c in calls] == [n for n, _ in DAILY_STEPS], f"陈旧锁未清理：{calls}"
    assert not lock.exists(), "跑完必须释放锁"


def test_daily_logs_stage_lines_with_duration(tmp_config, monkeypatch):
    """每步要写一条结构化日志（含 duration_ms），否则日志只有 ingest outcome、看不到阶段与耗时。"""
    cli_mod = _stub_daily_steps(monkeypatch, [])
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 0
    lines = []
    for path in sorted(tmp_config.path("logs").glob("app-*.jsonl")):
        lines += [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines() if text.strip()]
    steps = [row for row in lines if row.get("message") == "daily step"]
    assert {row["stage"] for row in steps} == {"preflight", "ingest", "accept", "doctor", "rate"}
    for row in steps:
        assert isinstance(row.get("duration_ms"), int), f"stage 日志缺耗时：{row}"
        assert row.get("run_id"), f"stage 日志缺 run_id：{row}"


def test_daily_passes_datasets_and_marks_subset(tmp_config, monkeypatch, capsys):
    """子集运行要透传给 ingest 并在汇总标 `scope=subset`，禁止把子集冒充全量（§6.6 质量报告范围口径）。"""
    captured = {}
    calls: list[str] = []
    cli_mod = _stub_daily_steps(monkeypatch, calls)

    def spy_ingest(config, args):
        """ingest 替身：记录收到的 datasets 后返回 0。"""
        captured["datasets"] = args.datasets
        calls.append("ingest")
        return 0

    monkeypatch.setattr(cli_mod, "command_ingest", spy_ingest)
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None, datasets="etf_518880,usdcny")) == 0
    assert captured["datasets"] == "etf_518880,usdcny"
    payload = _json_documents(capsys.readouterr().out)[-1]
    assert payload["scope"] == "subset" and payload["datasets"] == ["etf_518880", "usdcny"]


def test_daily_marks_rate_observation_by_wall_clock(tmp_config, monkeypatch, capsys):
    """T-025 观察只在 09:30–18:00 之间有效（此前折算价未转正才会落到中间价）；晚间跑必须在汇总里写 `observation_valid=false`。"""
    from datetime import datetime

    cli_mod = _stub_daily_steps(monkeypatch, [])
    evening = datetime(2026, 11, 12, 22, 30, tzinfo=SHANGHAI)
    monkeypatch.setattr(cli_mod, "now_shanghai", lambda: evening)
    cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    payload = _json_documents(capsys.readouterr().out)[-1]
    assert payload["rate_observation"]["observation_valid"] is False
    assert payload["rate_observation"]["phase"] == "post_1800"

    intraday = datetime(2026, 11, 12, 9, 45, tzinfo=SHANGHAI)
    monkeypatch.setattr(cli_mod, "now_shanghai", lambda: intraday)
    cli_mod.command_daily(tmp_config, Namespace(run_id=None))
    payload = _json_documents(capsys.readouterr().out)[-1]
    assert payload["rate_observation"]["observation_valid"] is True
    assert payload["rate_observation"]["phase"] == "intraday"


def test_daily_hard_stop_still_writes_summary(tmp_config, monkeypatch):
    """硬停也必须有凭证：preflight=2 时汇总落盘 `overall=STOPPED`、`stopped_at=preflight`，只含已跑的那一步。"""
    calls: list[str] = []
    cli_mod = _stub_daily_steps(monkeypatch, calls, codes={"command_preflight": 2})
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 2
    payload = json.loads(cli_mod._daily_summary_path(tmp_config).read_text(encoding="utf-8"))
    assert payload["overall"] == "STOPPED" and payload["stopped_at"] == "preflight"
    assert [step["step"] for step in payload["daily"]] == ["preflight"], "硬停后不得继续执行后续步骤"
    assert payload["exit_code"] == 2


def test_daily_summary_surfaces_provisional_tail(tmp_config, monkeypatch):
    """待转正的 PROVISIONAL 行必须显式进汇总：境外每晚必留尾，明早不补抓事实包就永远缺最新一天。"""
    calls: list[str] = []
    cli_mod = _stub_daily_steps(monkeypatch, calls)
    (tmp_config.path("normalized") / "_remediation.json").write_text(
        json.dumps(
            {
                "overall": "NEEDS_HUMAN",
                "issues": [
                    {
                        "code": "PROVISIONAL",
                        "dataset": "comex_gc",
                        "action": "WAIT_UNTIL_AVAILABLE_AT",
                        "evidence": {
                            "rows": 1,
                            "dates": ["2026-11-12"],
                            "max_available_at": "2026-11-13T06:00:00+08:00",
                            "refresh_due": False,
                        },
                    },
                    {
                        "code": "ZERO_FLOW",
                        "dataset": "wti_cl",
                        "action": "PRICE_ONLY_BLOCK_VOLUME_FIELDS",
                        "evidence": {"rows": 7652, "zero_rows": 15304},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 0
    payload = json.loads(cli_mod._daily_summary_path(tmp_config).read_text(encoding="utf-8"))
    tail = payload["pending_finalization"]
    assert tail["report_overall"] == "NEEDS_HUMAN"
    assert [item["dataset"] for item in tail["items"]] == ["comex_gc"], "只应列出 PROVISIONAL 类问题"
    assert tail["items"][0]["refresh_due"] is False
    assert "06:00" in tail["next_action"]


def test_daily_summary_next_action_uses_item_due_times(tmp_config, monkeypatch):
    """盘中跑的待转正提示必须按 items 到期时间生成：usdcny 当日 18:00 不得被「境外 06:00」盖掉。"""
    calls: list[str] = []
    cli_mod = _stub_daily_steps(monkeypatch, calls)
    (tmp_config.path("normalized") / "_remediation.json").write_text(
        json.dumps(
            {
                "overall": "NEEDS_HUMAN",
                "issues": [
                    {
                        "code": "PROVISIONAL",
                        "dataset": "usdcny",
                        "action": "WAIT_UNTIL_AVAILABLE_AT",
                        "evidence": {
                            "rows": 1,
                            "dates": ["2026-11-13"],
                            "max_available_at": "2026-11-13T18:00:00+08:00",
                            "refresh_due": False,
                        },
                    },
                    {
                        "code": "PROVISIONAL",
                        "dataset": "comex_gc",
                        "action": "WAIT_UNTIL_AVAILABLE_AT",
                        "evidence": {
                            "rows": 1,
                            "dates": ["2026-11-13"],
                            "max_available_at": "2026-11-14T06:00:00+08:00",
                            "refresh_due": False,
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 0
    tail = json.loads(cli_mod._daily_summary_path(tmp_config).read_text(encoding="utf-8"))["pending_finalization"]
    assert "2026-11-13T18:00:00+08:00" in tail["next_action"], "最早到期时间必须来自 usdcny 行"
    assert "2026-11-14T06:00:00+08:00" in tail["next_action"], "最晚到期时间必须来自境外行"
    assert "境外 06:00 之后" not in tail["next_action"], "固定文案不得再出现"


def test_daily_summary_without_remediation_report_does_not_invent_tail(tmp_config, monkeypatch):
    """放行对照：没有 `_remediation.json` 时不得凭空造待转正行，必须写清「没有可诊断的报告」。"""
    calls: list[str] = []
    cli_mod = _stub_daily_steps(monkeypatch, calls)
    assert cli_mod.command_daily(tmp_config, Namespace(run_id=None)) == 0
    payload = json.loads(cli_mod._daily_summary_path(tmp_config).read_text(encoding="utf-8"))
    tail = payload["pending_finalization"]
    assert tail["items"] == [] and tail["report_overall"] is None
    assert "不猜" in tail["next_action"]
