"""集成层：data.ingest.run_ingest 的离线日链（抓取→规范化→质量闸）行为。

覆盖场景：各源状态汇总与状态文件、幂等重跑、全失败跳过质量闸、
超时记 TIMEOUT、降级成功记录零成交、手工表裸日期可见时点、
规范化环节失败后的终态与续跑、整批剔空不被历史洗白。
"""
from __future__ import annotations

import json
import signal
import threading
import time
from datetime import date
from pathlib import Path

import pandas as pd

from com.sun.shensuan.gold.common.errors import DegradedSourceError
from com.sun.shensuan.gold.data.ingest import _call_with_timeout, run_ingest
from com.sun.shensuan.gold.data.models import FetchResult, NodeStatus, Quality


def _fetch(spec, frame):
    """测试辅助：把给定 DataFrame 包成带固定抓取时刻的 FetchResult。"""
    return FetchResult(
        frame=frame,
        source=spec.source,
        fetched_at=pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai").to_pydatetime(),
    )


def _sge_frame():
    """测试辅助：返回 3 行合成上海金现货日线，用于正常成功路径。"""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "open": [480.0, 482.0, 481.0],
            "high": [486.0, 485.0, 484.0],
            "low": [478.0, 479.0, 478.0],
            "close": [484.0, 481.0, 483.0],
        }
    )


def _gc_frame():
    """测试辅助：返回 3 行合成 COMEX 日线，volume/open_interest 全 0，模拟零持仓降级场景。"""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "open": [2060.0, 2062.0, 2061.0],
            "high": [2070.0, 2068.0, 2065.0],
            "low": [2055.0, 2056.0, 2057.0],
            "close": [2065.0, 2060.0, 2063.0],
            "volume": [0, 0, 0],
            "open_interest": [0, 0, 0],
        }
    )


def _lookup(etf_frame, fail_sge=True, degrade_gc=True):
    """测试辅助：构造按名字返回假适配器的查找函数；可切换 SGE 失败与 GC 降级，模拟离线故障组合。"""
    def factory(name):
        """内层工厂：按数据集名返回对应假适配器；未知名抛 KeyError 以暴露测试配置错误。"""
        if name == "etf":
            return lambda spec, start, end, ctx: _fetch(spec, etf_frame)
        if name == "sge":
            if fail_sge:
                def raiser(spec, start, end, ctx):
                    """内层假适配器：直接抛 ConnectionError，模拟 SGE 源不可达。"""
                    raise ConnectionError("sge unreachable")
                return raiser
            return lambda spec, start, end, ctx: _fetch(spec, _sge_frame())
        if name == "foreign_futures":
            if degrade_gc:
                def degrader(spec, start, end, ctx):
                    """内层假适配器：抛 DegradedSourceError，模拟 GC 被限流后的降级而非硬失败。"""
                    raise DegradedSourceError("GC 429")
                return degrader
            return lambda spec, start, end, ctx: _fetch(spec, _gc_frame())
        raise KeyError(name)

    return factory


def test_ingest_offline_statuses_and_report(tmp_config, etf_frame):
    """验证三源混合场景：ETF 成功、SGE 失败、GC 降级，汇总 DEGRADED，状态文件逐节点记录，缺口在报告可见。"""
    selected = ["etf_518880", "sge_au9999", "comex_gc"]
    outcomes, report = run_ingest(
        tmp_config,
        datasets=selected,
        end=date(2024, 1, 8),
        run_id="run-a",
        adapter_lookup=_lookup(etf_frame),
        retries=0,
    )
    by_name = {outcome.dataset: outcome for outcome in outcomes}
    assert by_name["etf_518880"].status == NodeStatus.OK
    assert by_name["etf_518880"].quality == Quality.GREEN
    assert by_name["sge_au9999"].status == NodeStatus.FAILED
    assert by_name["comex_gc"].status == NodeStatus.DEGRADED
    assert report["missing"] == ["sge_au9999"]
    assert "comex_gc" in report["degraded"]
    assert report["global_status"] == "DEGRADED"
    assert report["missing"] and report["degraded"]

    status_path = tmp_config.path("decisions") / "2024-01-08" / "pipeline_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    nodes = {node["node_id"]: node["status"] for node in status["nodes"]}
    assert nodes["ingest.etf_518880"] == "OK"
    assert nodes["ingest.sge_au9999"] == "FAILED"
    assert nodes["ingest.comex_gc"] == "DEGRADED"
    assert nodes["normalize"] == "OK"
    assert nodes["quality_gate"] == "DEGRADED"


def test_non_core_red_marks_quality_gate_degraded(tmp_config):
    """验证非核心数据集近期坏行判 RED 时，链路质量闸同步降级而不是显示 OK。

    负对照对象：``run_ingest`` 的 ``quality_gate`` 终态。若只看核心数据集，
    COMEX/WTI 这类非核心源判 RED 时节点仍是 OK，与 ``_quality.json`` 的
    global_status=DEGRADED 不一致（AGENTS §4.5、§6.6）。
    """
    bad = _gc_frame()
    bad.loc[bad.index[-1], "low"] = 2100.0  # low > high，触发 OHLC_INVALID

    def factory(name):
        """假适配器：COMEX 返回含 OHLC 不自洽行的合成表。"""
        if name == "foreign_futures":
            return lambda spec, start, end, ctx: _fetch(spec, bad)
        raise KeyError(name)

    outcomes, report = run_ingest(
        tmp_config,
        datasets=["comex_gc"],
        end=date(2024, 1, 8),
        run_id="run-red-noncore",
        adapter_lookup=factory,
        retries=0,
    )
    assert outcomes[0].quality == Quality.RED
    assert report["red"] == ["comex_gc"]
    assert report["global_status"] == "DEGRADED"

    status_path = tmp_config.path("decisions") / "2024-01-08" / "pipeline_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    nodes = {node["node_id"]: node for node in status["nodes"]}
    assert nodes["quality_gate"]["status"] == "DEGRADED"
    assert nodes["quality_gate"]["error_code"] == "DATASET_RED"


def test_subset_ingest_merges_previous_quality_report(tmp_config, etf_frame):
    """验证子集 ingest 保留未刷新数据集并标 inherited，不把全量报告缩成子集。

    负对照对象：``_merge_quality_report`` 的子集分支。若不合并旧报告，第二次
    只抓 ETF 时 ``_quality.json`` 会只剩 ETF，之前已判 RED 的 COMEX 从
    ``red``/``global_status`` 中消失，全量质量视图被静默洗白。
    """
    first_outcomes, first = run_ingest(
        tmp_config,
        datasets=["etf_518880", "comex_gc"],
        end=date(2024, 1, 8),
        run_id="run-first",
        adapter_lookup=_lookup(etf_frame),
        retries=0,
    )
    assert first["scope"] == "subset"
    assert first["inherited"] == []
    assert set(first["datasets"]) == {"etf_518880", "comex_gc"}
    assert "comex_gc" in first["red"]

    outcomes, report = run_ingest(
        tmp_config,
        datasets=["etf_518880"],
        end=date(2024, 1, 8),
        run_id="run-subset",
        adapter_lookup=_lookup(etf_frame),
        retries=0,
    )
    assert report["scope"] == "subset"
    assert report["refreshed"] == ["etf_518880"]
    assert report["inherited"] == ["comex_gc"]
    assert report["datasets"]["comex_gc"]["inherited"] is True
    assert report["datasets"]["comex_gc"]["inherited_run_id"] == "run-first"
    assert report["datasets"]["etf_518880"]["inherited"] is False
    assert "comex_gc" in report["red"]
    assert report["global_status"] == "DEGRADED"

    written = json.loads(
        (tmp_config.path("normalized") / "_quality.json").read_text(encoding="utf-8")
    )
    assert written["datasets"]["comex_gc"]["inherited"] is True
    assert written["inherited"] == ["comex_gc"]


def test_ingest_is_idempotent(tmp_config, etf_frame):
    """验证同一输入二次 ingest 新增 0 行且规范化文件字节不变（幂等重写）。"""
    selected = ["etf_518880"]
    run_ingest(
        tmp_config,
        datasets=selected,
        end=date(2024, 1, 8),
        run_id="run-a",
        adapter_lookup=_lookup(etf_frame),
        retries=0,
    )
    path = tmp_config.path("normalized") / "etf_518880.csv"
    before = path.read_bytes()
    outcomes, _ = run_ingest(
        tmp_config,
        datasets=selected,
        end=date(2024, 1, 8),
        run_id="run-b",
        adapter_lookup=_lookup(etf_frame),
        retries=0,
    )
    assert outcomes[0].new_rows == 0
    assert path.read_bytes() == before


def test_all_sources_failed_skips_quality_gate(tmp_config, etf_frame):
    """验证全部源失败时 overall=FAILED，且 quality_gate 标 SKIPPED；上游失败不得继续质量闸。"""
    outcomes, report = run_ingest(
        tmp_config,
        datasets=["sge_au9999"],
        end=date(2024, 1, 8),
        run_id="run-fail",
        adapter_lookup=_lookup(etf_frame, fail_sge=True),
        retries=0,
    )
    assert outcomes[0].status == NodeStatus.FAILED
    assert report["global_status"] == "FAILED"
    status_path = tmp_config.path("decisions") / "2024-01-08" / "pipeline_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    nodes = {node["node_id"]: node["status"] for node in status["nodes"]}
    assert nodes["normalize"] == "FAILED"
    assert nodes["quality_gate"] == "SKIPPED"
    assert status["overall"] == "FAILED"


def test_timeout_is_reported_as_timeout(tmp_config, etf_frame):
    """验证适配器超过 request_timeout_s 时记 TIMEOUT/SOURCE_TIMEOUT 并写入状态文件，慢源不得阻塞日链。"""
    tmp_config.application.ingest.request_timeout_s = 1

    def slow(spec, start, end, ctx):
        """内层假适配器：睡 2 秒超过 1 秒超时阈值，人为制造 TIMEOUT 场景。"""
        time.sleep(2)
        return _fetch(spec, etf_frame)

    outcomes, _ = run_ingest(
        tmp_config,
        datasets=["etf_518880"],
        end=date(2024, 1, 8),
        run_id="run-timeout",
        adapter_lookup=lambda name: slow,
        retries=0,
    )
    assert outcomes[0].status == NodeStatus.TIMEOUT
    assert outcomes[0].error_code == "SOURCE_TIMEOUT"
    status_path = tmp_config.path("decisions") / "2024-01-08" / "pipeline_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    node = next(node for node in status["nodes"] if node["node_id"] == "ingest.etf_518880")
    assert node["status"] == "TIMEOUT"
    assert node["error_code"] == "SOURCE_TIMEOUT"


def test_manual_explicit_available_as_bare_date(tmp_config):
    """手工事件表的 available_at 只写日期时必须能入库，并落成上海零点的 PIT 时点。

    负对照对象：`geo_events` 用 `available_at_rule: explicit`，用户在 CSV 里常只填
    `2024-01-04` 这样的裸日期。口径必须与手工层校验器（`parse_datetime`：naive 补
    Asia/Shanghai）一致，不得一处放行、一处把整条日链打死。
    """
    (tmp_config.path("manual") / "geo_events.csv").write_text(
        "event_id,date,type,severity,region,source_url,available_at\n"
        "E1,2024-01-04,制裁,2,RU,https://example.com/a,2024-01-04\n",
        encoding="utf-8",
    )
    outcomes, report = run_ingest(
        tmp_config,
        datasets=["geo_events"],
        end=date(2024, 1, 8),
        run_id="run-manual",
        retries=0,
    )
    assert outcomes[0].status == NodeStatus.OK
    assert report["global_status"] == "OK"

    normalized = pd.read_csv(tmp_config.path("normalized") / "geo_events.csv")
    assert normalized["available_at"].tolist() == ["2024-01-04T00:00:00+08:00"]

    status_path = tmp_config.path("decisions") / "2024-01-08" / "pipeline_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    nodes = {node["node_id"]: node["status"] for node in status["nodes"]}
    assert nodes["ingest.geo_events"] == "OK"
    assert nodes["normalize"] == "OK"
    assert nodes["quality_gate"] == "OK"


def test_normalize_failure_is_terminal_and_chain_continues(tmp_config, etf_frame):
    """规范化阶段抛错时本数据集必须落终态 FAILED，后续数据集与质量报告照常完成。

    负对照对象：列契约破坏（源改名导致缺 `volume/amount`）发生在适配器之后。
    它若逃出 ``run_ingest``，状态文件会停在 `RUNNING`、`_quality.json` 不写、
    其余数据集全部不抓（AGENTS §4.5「终态必写」「下游 SKIPPED」）。
    """
    broken = etf_frame.drop(columns=["volume", "amount"])

    def factory(name):
        """ETF 返回缺列的 OHLC 表（触发 normalize 列契约错），SGE 返回正常表。"""
        if name == "etf":
            return lambda spec, start, end, ctx: _fetch(spec, broken)
        return lambda spec, start, end, ctx: _fetch(spec, _sge_frame())

    outcomes, report = run_ingest(
        tmp_config,
        datasets=["etf_518880", "sge_au9999"],
        end=date(2024, 1, 8),
        run_id="run-normalize-fail",
        adapter_lookup=factory,
        retries=0,
    )
    by_name = {outcome.dataset: outcome for outcome in outcomes}
    assert by_name["etf_518880"].status == NodeStatus.FAILED
    assert by_name["etf_518880"].quality == Quality.RED
    assert by_name["etf_518880"].error_code == "PIPELINE_ADAPTERERROR"
    # 取数已成功，原始快照必须先于规范化落盘：否则规范化失败时没有任何原始证据可复盘。
    assert by_name["etf_518880"].raw_path is not None
    assert Path(by_name["etf_518880"].raw_path).exists()
    assert by_name["sge_au9999"].status == NodeStatus.OK
    assert report["missing"] == ["etf_518880"]

    status_path = tmp_config.path("decisions") / "2024-01-08" / "pipeline_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    nodes = {node["node_id"]: node["status"] for node in status["nodes"]}
    assert nodes["ingest.etf_518880"] == "FAILED"
    assert nodes["normalize"] == "OK"
    assert nodes["quality_gate"] == "DEGRADED"
    assert (tmp_config.path("normalized") / "_quality.json").exists()


def test_all_rows_dropped_is_red_even_with_fresh_history(tmp_config, etf_frame):
    """整批行被剔空时不得用历史数据洗成 GREEN：必需列全空必须判 RED 并可见。

    负对照对象：源改名后适配器补 `pd.NA`，`NAN_IN_REQUIRED` 会剔除整批。
    此时规范化层已判 RED，但质量档由「合并后的历史表」算出，历史仍新鲜就会
    回到 GREEN/OK，等于把今天已经坏掉的源说成正常（AGENTS §2.1-4 失败关闭）。
    """
    run_ingest(
        tmp_config,
        datasets=["etf_518880"],
        end=date(2024, 1, 8),
        run_id="run-history",
        adapter_lookup=_lookup(etf_frame, fail_sge=False),
        retries=0,
    )
    no_amount = etf_frame.copy()
    no_amount["amount"] = pd.NA
    outcomes, report = run_ingest(
        tmp_config,
        datasets=["etf_518880"],
        end=date(2024, 1, 8),
        run_id="run-all-dropped",
        adapter_lookup=lambda name: (lambda spec, start, end, ctx: _fetch(spec, no_amount)),
        retries=0,
    )
    assert outcomes[0].rows == 0
    assert outcomes[0].quality == Quality.RED
    assert outcomes[0].status == NodeStatus.DEGRADED
    assert "ALL_ROWS_DROPPED" in outcomes[0].notes
    assert "etf_518880" in report["red"]
    assert report["global_status"] == "DEGRADED"


def test_timeout_guard_reports_when_not_installed():
    """非主线程装不了 SIGALRM 时必须留说明，而不是让调用方以为有超时保护。

    负对照：主线程（Linux 有 SIGALRM）调用同一函数不得写入说明；两条断言
    一起证明这条说明只在“保护真的没装上”时出现。
    """
    notes: list[str] = []
    observed: list[int] = []

    def worker():
        """工作线程：线程内无法安装 SIGALRM，应走“未安装保护”分支。"""
        observed.append(_call_with_timeout(lambda: 7, 5, notes))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert observed == [7]
    assert len(notes) == 1
    assert "超时保护未安装" in notes[0]

    if hasattr(signal, "SIGALRM"):
        main_notes: list[str] = []
        assert _call_with_timeout(lambda: 1, 5, main_notes) == 1
        assert main_notes == []


def test_degraded_success_records_zero_volume(tmp_config, etf_frame):
    """验证零成交的 GC 数据标 DEGRADED/YELLOW 并记录 ZERO_VOLUME，价格可用但流动性不可信。"""
    outcomes, report = run_ingest(
        tmp_config,
        datasets=["comex_gc"],
        end=date(2024, 1, 8),
        run_id="run-gc",
        adapter_lookup=lambda name: (lambda spec, start, end, ctx: _fetch(spec, _gc_frame())),
        retries=0,
    )
    assert outcomes[0].status == NodeStatus.DEGRADED
    assert outcomes[0].quality == Quality.YELLOW
    assert "ZERO_VOLUME" in outcomes[0].notes
    assert "comex_gc" in report["degraded"]


def test_ingest_writes_remediation_report(tmp_config):
    """验证日链自动写 _remediation.json，且质量报告摘要与独立报告哈希一致。"""
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "open": [2060.0, 2062.0, 2061.0],
            "high": [2070.0, 2068.0, 2065.0],
            "low": [2055.0, 2056.0, 2057.0],
            "close": [2065.0, 2060.0, 2063.0],
            "volume": [100, 120, 90],
            "open_interest": [10, 11, 12],
        }
    )
    outcomes, report = run_ingest(
        tmp_config,
        datasets=["comex_gc"],
        end=date(2024, 1, 8),
        run_id="run-remediation",
        adapter_lookup=lambda name: (lambda spec, start, end, ctx: _fetch(spec, frame)),
        retries=0,
    )
    path = tmp_config.path("normalized") / "_remediation.json"
    assert path.exists()
    remediation = json.loads(path.read_text(encoding="utf-8"))
    assert remediation["run_id"] == "run-remediation"
    assert remediation["overall"] == "AUTO_CONTAINED"
    assert remediation["capabilities"]["comex_gc"]["score_eligible"] is True
    assert remediation["capabilities"]["comex_gc"]["coverage_is_lower_bound"] is True
    assert report["remediation"]["report_hash"] == remediation["report_hash"]
    assert outcomes[0].quality == Quality.GREEN
