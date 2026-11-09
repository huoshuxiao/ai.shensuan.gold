"""单元层：数据问题自动识别与能力清单的放行/判红对照。

覆盖：
- WTI 近期坏行：已留档、自动封闭分数，并给出还需多少有效行离开最近窗口；
- 零成交量/零持仓：系统性缺陷降为价格-only，个别历史零行不封锁字段；
- SPDR 低覆盖：只展示，流量特征禁入分数；
- GPR 无 vintage：禁止晋升，原始快照作为未来 vintage 证据；
- 事件 severity 未复核：自动隔离并进复核队列，复核后才解锁；
- 日历口径：weekday 近似标注为保守下界，声明 cn_sse 却回落时判红；
- 配置/报告不同版本：全部能力关闭；干净报告不误报；
- 报告哈希不随生成时间漂移；规范化表读坏时判人工处理而不是崩溃。
"""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import pandas as pd

from com.sun.shensuan.gold.cli import command_doctor
from com.sun.shensuan.gold.common.timeutil import SHANGHAI
from com.sun.shensuan.gold.data.remediation import build_remediation_report


def _entry(
    name: str,
    *,
    quality: str = "GREEN",
    status: str = "OK",
    flags: list[str] | None = None,
    notes: list[str] | None = None,
    rows: int = 10,
    **detail,
) -> dict:
    """构造一条最小质量报告数据集条目；未给的 detail 用中性默认值。"""
    payload = {
        "flags": flags or [],
        "rows": rows,
        "zero_rows": 0,
        "rejected_rows": 0,
        "recent_invalid_rows": 0,
        "recent_missing_rows": 0,
        "stale_days": 0,
        "as_of_min": "2026-10-20",
        "as_of_max": "2026-10-23",
        "weekday_gaps": 0,
        "weekday_gap_sample": [],
        "continuity_basis": "cn_sse(8797 日)",
    }
    payload.update(detail)
    return {
        "dataset": name,
        "status": status,
        "quality": quality,
        "rows": rows,
        "notes": notes or [],
        "quality_detail": payload,
    }


def _report(config, datasets: dict, *, config_hash: str | None = None) -> dict:
    """构造质量报告外壳；默认使用当前配置哈希，保证版本一致。"""
    return {
        "schema_version": "1",
        "run_id": "test-run",
        "reference_date": "2026-10-23",
        "generated_at": "2026-10-24T00:55:20+08:00",
        "config_hash": config_hash or config.config_hash,
        "datasets": datasets,
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    """写一个最小 CSV；父目录不存在时先建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _issues(report: dict) -> dict[str, dict]:
    """按 issue_id 建索引，避免测试里按位置取。"""
    return {item["issue_id"]: item for item in report["issues"]}


def test_wti_red_quarantines_rows_and_blocks_scoring(tmp_config):
    """WTI 近期坏行：状态 CONTAINED、分数禁用、价格-only、还需 4 个有效行离开窗口。"""
    normalized = tmp_config.path("normalized")
    _write_csv(
        normalized / "wti_cl.csv",
        [
            {"as_of": "2026-10-20", "flags": ""},
            {"as_of": "2026-10-21", "flags": ""},
            {"as_of": "2026-10-22", "flags": ""},
            {"as_of": "2026-10-23", "flags": ""},
        ],
    )
    _write_csv(
        normalized / "rejected" / "wti_cl.csv",
        [{"as_of": "2026-10-22", "reject_reason": "OHLC_INVALID"}],
    )
    entry = _entry(
        "wti_cl",
        quality="RED",
        status="DEGRADED",
        flags=["INVALID_ROWS_DROPPED", "RECENT_INVALID_ROWS", "ZERO_VOLUME", "ZERO_OPEN_INTEREST"],
        notes=["volume 全为 0：标 DEGRADED，只当收盘价", "open_interest 全为 0：标 DEGRADED，只当收盘价"],
        rows=4,
        rejected_rows=1,
        recent_invalid_rows=1,
        zero_rows=20,
        continuity_basis="weekday_approx",
    )
    report = build_remediation_report(tmp_config, _report(tmp_config, {"wti_cl": entry}))
    issues = _issues(report)
    assert issues["wti_cl:RECENT_BAD_ROWS"]["status"] == "CONTAINED"
    assert issues["wti_cl:RECENT_BAD_ROWS"]["evidence"]["clear_after_valid_rows"] == 4
    assert issues["wti_cl:ZERO_FLOW"]["status"] == "CONTAINED"
    capability = report["capabilities"]["wti_cl"]
    assert capability["score_eligible"] is False
    assert capability["feature_eligible"] is False
    assert capability["price_only"] is True
    assert capability["blocked_fields"] == ["open_interest", "volume"]
    assert report["overall"] == "AUTO_CONTAINED"


def test_isolated_zero_volume_does_not_block_the_field(tmp_config):
    """个别历史零成交量只标 AUTO_RESOLVED；不得把整个成交量字段封掉。"""
    entry = _entry(
        "etf_159934",
        flags=["ZERO_VOLUME"],
        notes=["丢弃价格/OHLC 非法行 0 行"],
        rows=1000,
        zero_rows=4,
    )
    report = build_remediation_report(tmp_config, _report(tmp_config, {"etf_159934": entry}))
    issues = _issues(report)
    assert issues["etf_159934:HISTORICAL_ZERO_FLOW"]["status"] == "AUTO_RESOLVED"
    capability = report["capabilities"]["etf_159934"]
    assert capability["price_only"] is False
    assert capability["blocked_fields"] == []


def test_spdr_low_coverage_blocks_flow_features(tmp_config):
    """SPDR 覆盖率低于下限：只展示，1W/4W 流量特征禁入分数。"""
    entry = _entry(
        "spdr_gold",
        quality="YELLOW",
        status="DEGRADED",
        flags=["LOW_COVERAGE"],
        rows=2894,
        weekday_gaps=2829,
    )
    report = build_remediation_report(tmp_config, _report(tmp_config, {"spdr_gold": entry}))
    issues = _issues(report)
    assert issues["spdr_gold:LOW_COVERAGE"]["status"] == "CONTAINED"
    capability = report["capabilities"]["spdr_gold"]
    assert capability["feature_eligible"] is False
    assert capability["score_eligible"] is False
    assert capability["blocked_features"] == ["FLOW_SPDR_CHG_1W", "FLOW_SPDR_CHG_4W"]


def test_gpr_revised_only_blocks_promotion_and_counts_vintage(tmp_config):
    """GPR 无 vintage：禁止晋升；有原始快照时报告 vintage 起点，不再要求人工重建。"""
    meta_dir = (
        tmp_config.path("raw")
        / "matteoiacoviello.gpr_daily_recent"
        / "2026-10-19"
    )
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "gpr.csv.meta.json").write_text(
        json.dumps(
            {
                "dataset": "gpr",
                "source": "matteoiacoviello.gpr_daily_recent",
                "fetched_at": "2026-10-19T23:43:55+08:00",
                "raw_file": "gpr.csv",
                "sha256": "sha256:abc",
            }
        ),
        encoding="utf-8",
    )
    entry = _entry(
        "gpr",
        quality="YELLOW",
        status="DEGRADED",
        flags=["REVISED_ONLY"],
        rows=15267,
    )
    report = build_remediation_report(tmp_config, _report(tmp_config, {"gpr": entry}))
    issues = _issues(report)
    assert issues["gpr:REVISED_ONLY"]["status"] == "CONTAINED"
    assert issues["gpr:REVISED_ONLY"]["evidence"]["snapshots"] == 1
    capability = report["capabilities"]["gpr"]
    assert capability["promotion_eligible"] is False
    assert capability["score_eligible"] is False
    assert capability["blocked_promotions"] == ["H-GEO-1", "H-GEO-2"]


def test_gpr_without_snapshot_needs_human(tmp_config):
    """没有任何原始快照时不能假装有 vintage，必须判 NEEDS_HUMAN。"""
    entry = _entry("gpr", quality="YELLOW", status="DEGRADED", flags=["REVISED_ONLY"])
    report = build_remediation_report(tmp_config, _report(tmp_config, {"gpr": entry}))
    assert _issues(report)["gpr:REVISED_ONLY"]["status"] == "NEEDS_HUMAN"
    assert report["overall"] == "NEEDS_HUMAN"


def test_unreviewed_events_are_queued_and_blocked(tmp_config):
    """抽取事件 severity 未复核：进复核队列、禁入分数；补 reviewed=1 后自动解锁。"""
    manual = tmp_config.path("manual") / "geo_events.csv"
    row = {
        "event_id": "XGE-ge-2-2026-10-19-regionalconflictmideast-432af6d5",
        "date": "2026-10-19",
        "type": "regional_conflict",
        "severity": "2",
        "region": "mideast",
        "source_url": "akshare.stock_info_global_sina#sha256:abc",
        "available_at": "2026-10-19T23:25:02+08:00",
    }
    pd.DataFrame([row]).to_csv(manual, index=False)
    entry = _entry("geo_events", flags=["MANUAL"])
    report = build_remediation_report(tmp_config, _report(tmp_config, {"geo_events": entry}))
    issues = _issues(report)
    assert issues["geo_events:UNREVIEWED_EVENT_SEVERITY"]["status"] == "NEEDS_HUMAN"
    assert len(report["review_queue"]) == 1
    assert report["capabilities"]["geo_events"]["feature_eligible"] is False

    row["severity_reviewed"] = "1"
    pd.DataFrame([row]).to_csv(manual, index=False)
    report2 = build_remediation_report(tmp_config, _report(tmp_config, {"geo_events": entry}))
    assert "geo_events:UNREVIEWED_EVENT_SEVERITY" not in _issues(report2)
    assert report2["capabilities"]["geo_events"]["feature_eligible"] is True


def test_calendar_approx_is_labeled_and_missing_official_calendar_is_human(tmp_config):
    """weekday 近似只标注为保守下界；声明 cn_sse 却回落时必须判 NEEDS_HUMAN。"""
    comex = _entry(
        "comex_gc",
        quality="YELLOW",
        status="DEGRADED",
        flags=["ZERO_VOLUME", "ZERO_OPEN_INTEREST"],
        notes=["volume 全为 0：标 DEGRADED，只当收盘价"],
        rows=1000,
        zero_rows=2000,
        continuity_basis="weekday_approx",
    )
    etf = _entry(
        "etf_518880",
        flags=[],
        continuity_basis="weekday_approx(cn_sse 日历未落盘)",
    )
    report = build_remediation_report(
        tmp_config, _report(tmp_config, {"comex_gc": comex, "etf_518880": etf})
    )
    issues = _issues(report)
    assert issues["*:CALENDAR_APPROX"]["status"] == "CONTAINED"
    assert issues["*:CALENDAR_MISSING"]["status"] == "NEEDS_HUMAN"
    assert report["capabilities"]["comex_gc"]["coverage_is_lower_bound"] is True
    assert report["capabilities"]["etf_518880"]["score_eligible"] is False


def test_config_hash_mismatch_blocks_every_capability(tmp_config):
    """质量报告与当前配置不同版本：全部能力关闭，防止旧数据配新判据。"""
    entry = _entry("etf_518880")
    report = build_remediation_report(
        tmp_config,
        _report(tmp_config, {"etf_518880": entry}, config_hash="sha256:old"),
    )
    assert report["global_block"] is True
    assert report["overall"] == "NEEDS_HUMAN"
    capability = report["capabilities"]["etf_518880"]
    assert capability["display_eligible"] is False
    assert capability["feature_eligible"] is False
    assert capability["score_eligible"] is False
    assert "STALE_QUALITY_REPORT" in capability["reasons"]


def test_clean_report_has_no_issues(tmp_config):
    """干净报告不制造问题：无 issues、总体 CLEAN、能力全开。"""
    entry = _entry("etf_518880")
    report = build_remediation_report(tmp_config, _report(tmp_config, {"etf_518880": entry}))
    assert report["issues"] == []
    assert report["overall"] == "CLEAN"
    assert report["capabilities"]["etf_518880"]["score_eligible"] is True


def test_doctor_writes_report_and_exit_codes(tmp_config, capsys):
    """doctor 干净报告退出 0 并落盘；有未复核事件时退出 1。"""
    normalized = tmp_config.path("normalized")
    quality_path = normalized / "_quality.json"
    clean = _report(tmp_config, {"etf_518880": _entry("etf_518880")})
    quality_path.write_text(json.dumps(clean), encoding="utf-8")

    code = command_doctor(tmp_config, Namespace(write=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["overall"] == "CLEAN"
    assert (normalized / "_remediation.json").exists()

    manual = tmp_config.path("manual") / "geo_events.csv"
    pd.DataFrame(
        [
            {
                "event_id": "XGE-ge-2-2026-10-19-regionalconflictmideast-432af6d5",
                "date": "2026-10-19",
                "type": "regional_conflict",
                "severity": "2",
                "region": "mideast",
                "source_url": "akshare.stock_info_global_sina#sha256:abc",
                "available_at": "2026-10-19T23:25:02+08:00",
            }
        ]
    ).to_csv(manual, index=False)
    needs = _report(
        tmp_config,
        {
            "etf_518880": _entry("etf_518880"),
            "geo_events": _entry("geo_events", flags=["MANUAL"]),
        },
    )
    quality_path.write_text(json.dumps(needs), encoding="utf-8")
    code = command_doctor(tmp_config, Namespace(write=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["overall"] == "NEEDS_HUMAN"
    assert payload["review_queue"]

    quality_path.unlink()
    code = command_doctor(tmp_config, Namespace(write=False))
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "MISSING"


def test_report_hash_ignores_generation_time(tmp_config):
    """同一质量报告在不同生成时刻应得到同一内容哈希，便于跨次对表。"""
    entry = _entry("etf_518880")
    quality = _report(tmp_config, {"etf_518880": entry})
    first = build_remediation_report(
        tmp_config,
        quality,
        now=datetime(2026, 10, 25, 9, 0, tzinfo=SHANGHAI),
    )
    second = build_remediation_report(
        tmp_config,
        quality,
        now=datetime(2026, 10, 25, 15, 0, tzinfo=SHANGHAI),
    )
    assert first["generated_at"] != second["generated_at"]
    assert first["report_hash"] == second["report_hash"]


def test_unreadable_normalized_table_needs_human(tmp_config):
    """规范化表读不出来时判 READ_ERROR 并关闭全部能力，不得当作干净数据。"""
    normalized = tmp_config.path("normalized")
    # 用目录占住 CSV 路径，read_csv 会抛 IsADirectoryError；这是真实的读失败形状。
    (normalized / "etf_518880.csv").mkdir(parents=True, exist_ok=True)
    entry = _entry("etf_518880")
    report = build_remediation_report(tmp_config, _report(tmp_config, {"etf_518880": entry}))
    issue = _issues(report)["etf_518880:READ_ERROR"]
    assert issue["status"] == "NEEDS_HUMAN"
    capability = report["capabilities"]["etf_518880"]
    assert capability["display_eligible"] is False
    assert capability["feature_eligible"] is False
    assert capability["score_eligible"] is False
    assert capability["promotion_eligible"] is False
    assert "READ_ERROR" in capability["reasons"]
    assert report["overall"] == "NEEDS_HUMAN"
