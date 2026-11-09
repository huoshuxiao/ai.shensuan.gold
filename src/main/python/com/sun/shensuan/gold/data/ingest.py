"""抓取编排：重试、原始快照、规范化、质量闸、运行链路状态。

职责：按配置逐个数据集执行「取数 → 写原始快照 → 规范化 → 落规范化层 → 质量评估」，
把每个节点的进入/退出写入 ``data/decisions/<date>/pipeline_status.json``，最后
汇总 ``data/normalized/_quality.json``。

关键不变量：
- 单个数据集失败不中断整条链：取数失败标 FAILED/TIMEOUT/DEGRADED，规范化或落盘
  失败标 FAILED + ``PIPELINE_*`` 错误码，两种都继续下一个数据集，并由
  ``quality_gate`` 汇总核心数据集是否达标；每个节点离开循环时必为终态，
  不得把 ``RUNNING`` 残态留给链路页（AGENTS §4.5）。
- 质量档取「本次规范化」与「合并后历史」的较差者：整批被剔空或空源判 RED，
  不能因为历史序列新鲜就洗成 GREEN/YELLOW。
- 质量报告按数据集集合分层：全量运行写 ``scope=full``；``--datasets`` 子集运行与
  上一份 ``_quality.json`` 合并，保留未刷新数据集并标 ``inherited``，不得把全量
  报告缩成子集；没有旧报告时也显式标 ``scope=subset``。
- 重试只针对可重试错误（网络/解析/超时）；``DegradedSourceError`` 表示
  “可用但降级”，直接标 DEGRADED，不再重试。
- 原始快照 append-only 且先于规范化落盘：规范化失败时本次抓取的原始证据仍在，
  可复盘“源给了什么”；规范化层按 ``key_columns`` 幂等合并；失败数据集不写
  规范化文件，避免半截数据进入事实包。
- 链路状态由 ``PipelineStatusWriter`` 原子写，进程中断时保留上一次完整快照。
"""
from __future__ import annotations

import signal
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from ..common.errors import AdapterError, DegradedSourceError
from ..common.hashing import sha256_file
from ..common.timeutil import iso, now_shanghai
from ..config.loader import DatasetConfig, LoadedConfig
from . import adapters  # noqa: F401  触发注册
from .adapters.base import AdapterContext, get_adapter
from .models import IngestOutcome, NodeStatus, Quality
from .normalize import assess_dataset, normalize_frame
from .remediation import build_remediation_report, summarize as summarize_remediation
from .trade_calendar import TradeCalendar, describe as describe_calendars, load_all_calendars
from .pipeline import PipelineStatusWriter
from .store import NormalizedStore, RawStore


def _call_with_timeout(function: Callable, timeout_s: int, notes: list[str] | None = None):
    """在 Unix 主线程上用 SIGALRM 给适配器调用加硬超时。

    边界：Windows 无 SIGALRM、工作线程无法安装信号处理器、``timeout_s <= 0``
    时退化为直接调用（不设超时），由上层重试兜底。超时抛 ``TimeoutError``，
    由调用方转成节点 TIMEOUT。

    Args:
        function: 无参可调用对象（通常用 ``lambda`` 包住适配器调用）。
        timeout_s: 超时秒数。
        notes: 可选的结果说明列表；保护装不上时追加一条固定告警，让
            “卡死”与“跑得慢”在链路页/outcome 里可区分。同一列表重复调用
            只记一次。

    Returns:
        ``function()`` 的返回值。

    Raises:
        TimeoutError: 超过 ``timeout_s`` 仍未返回。
    """
    if timeout_s <= 0 or threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        # 退化调用没有任何超时兜底：必须留痕，否则无限等待会被当成正常慢查询。
        warning = "超时保护未安装（timeout<=0/非主线程/平台无 SIGALRM），本次调用可能无限等待"
        if notes is not None and warning not in notes:
            notes.append(warning)
        return function()

    def handler(signum, frame):
        """SIGALRM 处理器：把信号转成可被上层捕获的 ``TimeoutError``。"""
        raise TimeoutError(f"适配器超过 {timeout_s}s")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return function()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _new_run_id() -> str:
    """生成抓取批次 ID：UTC 时间戳 + 6 位随机后缀。

    时间戳用 UTC 便于跨时区对表；随机后缀避免同一秒内重跑/并发时撞号。
    """
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def run_ingest(
    config: LoadedConfig,
    datasets: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    run_id: str | None = None,
    run_date: date | None = None,
    adapter_lookup: Callable[[str], Callable] = get_adapter,
    retries: int | None = None,
    calendars: dict[str, TradeCalendar] | None = None,
) -> tuple[list[IngestOutcome], dict]:
    """执行一次抓取编排，返回逐数据集结果与质量报告。

    流程：选择数据集 → 逐个抓取（带重试/超时）→ 写原始快照 → 规范化 →
    写规范化层 → 质量评估 → 写链路状态 → 汇总 ``_quality.json`` 报告。

    Args:
        config: 已加载配置（数据集清单、重试次数、超时、路径）。
        datasets: 指定数据集名列表；None 表示全部 ``enabled`` 的数据集。
        start / end: 传给适配器的日期闭区间；None 表示由适配器决定默认窗口。
        run_id: 指定批次号；None 时自动生成。
        run_date: 链路状态文件所属日期；None 时取 ``end`` 或今天。
        adapter_lookup: 适配器查找函数，默认查全局注册表；测试可注入假适配器。
        retries: 覆盖配置中的重试次数；None 表示用
            ``application.ingest.retries``。
        calendars: 连续性缺口的分母日历 ``{basis: TradeCalendar}``；None 表示从
            ``paths.reference`` 读盘。读不到官方日历时按 weekday 近似，口径会
            写进每个数据集的 ``continuity_basis``（§6.6）。

    Returns:
        ``(outcomes, report)``：逐数据集 ``IngestOutcome`` 列表，以及写入
        ``data/normalized/_quality.json`` 的质量报告（含 config_hash 与链路
        快照，便于事后复现）。

    Raises:
        KeyError: 指定了配置中不存在的数据集名。

    Notes:
        取数之后环节的异常（``normalize_frame`` 的列契约/PIT 错误、落盘的
        ``OSError``）不外抛，转成该数据集的 ``FAILED`` 节点与 ``PIPELINE_*``
        错误码，其余数据集继续抓取；只有配置层面的错误才让整条链起不来。
        超时保护无法安装时（非主线程/平台无 SIGALRM/``timeout<=0``），
        ``_call_with_timeout`` 的告警会并入节点 ``message`` 与
        ``IngestOutcome.notes``，不会静默退化成“无超时”。
    """
    specs = config.data_sources.datasets
    if datasets:
        unknown = [name for name in datasets if name not in specs]
        if unknown:
            raise KeyError(f"未知数据集：{unknown}")
        selected = list(datasets)
    else:
        selected = [name for name, spec in specs.items() if spec.enabled]

    run_id = run_id or _new_run_id()
    # 质量闸的“陈旧度”基准：补历史时以 end 为准，避免把旧数据误判为 STALE
    reference = end or run_date or now_shanghai().date()
    run_day = (run_date or reference).isoformat()
    retry_count = config.application.ingest.retries if retries is None else retries
    timeout_s = config.application.ingest.request_timeout_s
    raw_store = RawStore(config.path("raw"))
    normalized_store = NormalizedStore(config.path("normalized"))
    # 连续性缺口分母用的日历：默认从参考层读盘（不联网抓取，缺文件就回落 weekday
    # 近似并在 continuity_basis 里写明原因）。测试可注入假日历验证两种口径。
    if calendars is None:
        calendars = load_all_calendars(config.path("reference"))
    status_path = config.path("decisions") / run_day / "pipeline_status.json"
    node_ids = [f"ingest.{name}" for name in selected] + ["normalize", "quality_gate"]
    writer = PipelineStatusWriter(status_path, run_id, run_day, node_ids)
    adapter_context = AdapterContext(
        manual_dir=config.path("manual"),
        request_timeout_s=timeout_s,
    )

    outcomes: list[IngestOutcome] = []
    for name in selected:
        spec = specs[name]
        node_id = f"ingest.{name}"
        writer.start(node_id)
        last_error: Exception | None = None
        result = None
        attempts = 0
        degraded = False
        timed_out = False
        # 每次数据集独立收集：超时保护装不上时由 _call_with_timeout 追加告警，
        # 最终并入节点 message 与 outcome.notes，不静默退化成“无超时”。
        timeout_notes: list[str] = []
        for attempt in range(retry_count + 1):
            # attempts 记录“已重试次数”：首次尝试记 0，与节点字段 retry_count 同义
            attempts = attempt
            try:
                function = adapter_lookup(spec.adapter)
                result = _call_with_timeout(
                    lambda: function(spec, start, end, adapter_context),
                    timeout_s,
                    timeout_notes,
                )
                last_error = None
                break
            except DegradedSourceError as exc:
                # 限流/降级类错误重试通常无效，直接落 DEGRADED，不再浪费退避时间
                last_error = exc
                degraded = True
                break
            except TimeoutError as exc:
                last_error = exc
                timed_out = True
                if attempt < retry_count:
                    time.sleep(config.application.ingest.backoff_s * (2**attempt))
            except Exception as exc:  # 网络/解析错误统一进入重试
                last_error = exc
                if attempt < retry_count:
                    time.sleep(config.application.ingest.backoff_s * (2**attempt))

        if result is None:
            if timed_out:
                status = NodeStatus.TIMEOUT
                error_code = "SOURCE_TIMEOUT"
            elif degraded:
                status = NodeStatus.DEGRADED
                error_code = "DEGRADED_SOURCE"
            else:
                status = NodeStatus.FAILED
                error_code = f"SOURCE_{type(last_error).__name__.upper()}"
            writer.finish(
                node_id,
                status,
                error_code=error_code,
                retry_count=attempts,
                message=str(last_error)[:300] if last_error else None,
            )
            outcomes.append(
                IngestOutcome(
                    dataset=name,
                    status=status,
                    quality=Quality.RED,
                    rows=0,
                    total_rows=0,
                    new_rows=0,
                    notes=[*([str(last_error)] if last_error else []), *timeout_notes],
                    normalized_path=None,
                    raw_path=None,
                    error_code=error_code,
                )
            )
            continue

        # 原始快照先落盘：规范化抛异常时仍保留本次抓取的原始证据，便于复盘；
        # RawStore 是 append-only，重复运行不会覆盖历史快照。
        raw_path = None
        try:
            raw_path = raw_store.save(name, run_id, result, run_day)
            normalize_result = normalize_frame(result.frame, spec, result.fetched_at, run_id)
            rejected_path = normalized_store.write_rejected(name, normalize_result.rejected)
            # 剔除的行必须从主表移除（否则历史并集会让它复活）；声明为完整快照的
            # 数据集（事件日历）还会删掉新输入区间内已不存在的旧行。
            merged, new_rows = normalized_store.merge(
                name,
                normalize_result.frame,
                spec.key_columns,
                removed=normalize_result.rejected,
                authoritative=spec.authoritative_snapshot,
            )
            normalized_path = normalized_store.write(name, merged)
            quality = assess_dataset(
                merged,
                spec,
                reference_date=reference,
                invalid_rows=normalize_result.dropped_rows,
                rejected=normalized_store.read_rejected(name),
                calendars=calendars,
                coverage_floor=config.application.acceptance.min_weekday_coverage,
            )
        except Exception as exc:  # 规范化/落盘失败：本数据集落终态，不得把 RUNNING 残态留给链路页
            # 取数已经成功，坏的是后续环节，所以错误码与源错误（SOURCE_*）分开，
            # 便于链路页区分“源不可达”和“我们把它写坏了”。
            error_code = f"PIPELINE_{type(exc).__name__.upper()}"
            writer.finish(
                node_id,
                NodeStatus.FAILED,
                error_code=error_code,
                retry_count=attempts,
                message=str(exc)[:300],
            )
            outcomes.append(
                IngestOutcome(
                    dataset=name,
                    status=NodeStatus.FAILED,
                    quality=Quality.RED,
                    rows=0,
                    total_rows=0,
                    new_rows=0,
                    notes=[str(exc)[:300], *timeout_notes],
                    normalized_path=None,
                    raw_path=str(raw_path) if raw_path is not None else None,
                    error_code=error_code,
                )
            )
            continue

        if normalize_result.status == Quality.RED:
            # 规范化整表判 RED（源给空表、或整批行被剔空）时，合并后的历史序列可能
            # 依然新鲜；不能拿旧数据把今天已经坏掉的源洗成 GREEN/YELLOW（AGENTS §2.1-4）。
            # 剔除行已从主表移除后，合并表可能也是空表——此时两边的 flags 都要留：
            # `EMPTY` 说「现在没有行」，`ALL_ROWS_DROPPED` 说「为什么没有行」。
            quality.status = Quality.RED
            quality.flags = sorted({*quality.flags, *normalize_result.flags})

        quality_detail = quality.as_dict()
        if rejected_path is not None:
            quality_detail["rejected_path"] = str(rejected_path)

        if quality.status == Quality.GREEN:
            node_status = NodeStatus.OK
        else:
            node_status = NodeStatus.DEGRADED
        writer.finish(
            node_id,
            node_status,
            error_code=None if node_status == NodeStatus.OK else f"QUALITY_{quality.status.value}",
            artifact_hash=sha256_file(normalized_path),
            retry_count=attempts,
            message=";".join(result.notes + normalize_result.notes + timeout_notes)[:300] or None,
        )
        outcomes.append(
            IngestOutcome(
                dataset=name,
                status=node_status,
                quality=quality.status,
                rows=len(normalize_result.frame),
                total_rows=len(merged),
                new_rows=new_rows,
                notes=result.notes + normalize_result.notes + timeout_notes + quality.flags,
                normalized_path=str(normalized_path),
                raw_path=str(raw_path),
                quality_detail=quality_detail,
            )
        )

    # DEGRADED/TIMEOUT 仍算“有产物”（数据可能已落盘），只有 FAILED 才算整源丢失
    successful = [outcome for outcome in outcomes if outcome.status != NodeStatus.FAILED]
    writer.start("normalize")
    if successful:
        writer.finish("normalize", NodeStatus.OK, message=f"规范化成功 {len(successful)}/{len(outcomes)}")
    else:
        writer.finish("normalize", NodeStatus.FAILED, error_code="ALL_SOURCES_FAILED")
        writer.mark_skipped(["quality_gate"], "所有数据源失败")

    # 核心数据集必须达到 OK；DEGRADED/TIMEOUT/FAILED 都视为未达标
    core_failed = [
        outcome.dataset
        for outcome in outcomes
        if specs[outcome.dataset].core and outcome.status != NodeStatus.OK
    ]
    # 非核心数据集也可能因近期坏行判 RED，并已把 _quality.json 的 global_status
    # 压成 DEGRADED；链路节点必须同口径，否则链路页会显示“质量闸 OK、报告降级”。
    red_datasets = [
        outcome.dataset for outcome in outcomes if outcome.quality == Quality.RED
    ]
    if not successful:
        # 与上面的 mark_skipped 重复调用是安全的：它只改仍为 PENDING 的节点
        writer.mark_skipped(["quality_gate"], "所有数据源失败")
    elif core_failed:
        writer.finish(
            "quality_gate",
            NodeStatus.DEGRADED,
            error_code="CORE_DATASET_NOT_GREEN",
            message=f"核心数据集未达 GREEN：{core_failed}",
        )
    elif red_datasets:
        writer.finish(
            "quality_gate",
            NodeStatus.DEGRADED,
            error_code="DATASET_RED",
            message=f"数据集质量为 RED：{red_datasets}",
        )
    else:
        writer.finish("quality_gate", NodeStatus.OK)

    report = _quality_report(config, run_id, reference, outcomes, writer.snapshot(), calendars)
    # 子集运行不能把全量 _quality.json 缩成子集：与上一份报告合并，未刷新的数据集
    # 标 inherited；是否为全量按实际选中集合判断，而非只看 datasets 参数是否为 None。
    enabled = [name for name, spec in specs.items() if spec.enabled]
    full_run = datasets is None or set(selected) == set(enabled)
    previous = {} if full_run else normalized_store.read_quality_report()
    report = _merge_quality_report(config, report, previous, selected, full_run)
    # 自动诊断必须跟在质量报告之后：它读的是刚算出的 RED/黄灯与留档，而不是旧结论。
    # 诊断失败不能把已完成的抓取/规范化整条链打死；把错误写进质量报告的
    # remediation 摘要，让 CLI/看板看见“自动诊断自己坏了”，不得静默当 CLEAN。
    try:
        remediation = build_remediation_report(config, report)
        normalized_store.write_remediation_report(remediation)
        report["remediation"] = summarize_remediation(remediation)
    except Exception as exc:  # noqa: BLE001 - 诊断失败要留痕，不能中断 ingest
        report["remediation"] = {
            "overall": "ERROR",
            "errors": [f"{type(exc).__name__}: {str(exc)[:300]}"],
        }
    normalized_store.write_quality_report(report)
    return outcomes, report


def _summarize_quality(config: LoadedConfig, datasets: dict) -> dict:
    """按数据集条目重算质量报告的总览字段。

    质量报告有两种产生方式：全量运行直接用本次 outcomes；子集运行要把本次
    outcomes 与上一份报告合并后再重算。两种路径共用本函数，避免“列表字段”
    与 ``global_status`` 的口径分叉。

    Args:
        config: 已加载配置，提供 core 标记。
        datasets: 数据集名到报告条目的映射；条目至少含 ``status``/``quality``/``notes``。

    Returns:
        ``failed/degraded/red/core_not_green/revised_only/global_status`` 六个字段。
    """
    failed = [name for name, payload in datasets.items() if payload["status"] == NodeStatus.FAILED.value]
    degraded = [
        name
        for name, payload in datasets.items()
        if payload["status"] == NodeStatus.DEGRADED.value
    ]
    red = [name for name, payload in datasets.items() if payload["quality"] == Quality.RED.value]
    revised_only = [
        name
        for name, payload in datasets.items()
        if "REVISED_ONLY" in payload.get("notes", [])
        or "REVISED_ONLY" in (payload.get("quality_detail") or {}).get("flags", [])
    ]
    core_not_green = []
    for name, payload in datasets.items():
        spec = config.data_sources.datasets.get(name)
        if spec is not None and spec.core and payload["status"] != NodeStatus.OK.value:
            core_not_green.append(name)
    if failed and len(failed) == len(datasets):
        global_status = NodeStatus.FAILED.value
    elif failed or red or core_not_green:
        global_status = NodeStatus.DEGRADED.value
    else:
        global_status = NodeStatus.OK.value
    return {
        "global_status": global_status,
        "missing": failed,
        "degraded": degraded,
        "red": red,
        "core_not_green": core_not_green,
        "revised_only": revised_only,
    }


def _merge_quality_report(
    config: LoadedConfig,
    report: dict,
    previous: dict,
    selected: list[str],
    full_run: bool,
) -> dict:
    """把本次质量报告与上一份合并，避免子集运行覆盖全量视图。

    全量运行：本次选中全部 enabled 数据集，直接覆盖旧报告，条目标
    ``inherited=False``、``scope=full``。

    子集运行：保留旧报告中本次未选中的数据集，条目标 ``inherited=True`` 与
    ``inherited_run_id``；本次选中条目标 ``inherited=False``。随后按合并后的
    数据集重算总览字段，保证 ``global_status``/``red``/``degraded`` 不会只反映子集。
    没有旧报告时仍写子集报告，但显式标 ``scope=subset``，不冒充全量。

    Args:
        config: 已加载配置，用于重算 core 口径。
        report: 本次 ``_quality_report`` 的结果。
        previous: 上一份 ``_quality.json``；没有时传空字典。
        selected: 本次实际选中的数据集名。
        full_run: 本次是否覆盖全部 enabled 数据集。

    Returns:
        合并后的报告字典；原 ``report`` 字典会被原地更新并返回。
    """
    current = report["datasets"]
    if full_run:
        for payload in current.values():
            payload["inherited"] = False
        report["scope"] = "full"
        report["refreshed"] = list(selected)
        report["inherited"] = []
        return report

    old_datasets = previous.get("datasets", {}) if isinstance(previous, dict) else {}
    if not isinstance(old_datasets, dict):
        old_datasets = {}
    inherited_names = sorted(name for name in old_datasets if name not in current)
    merged: dict[str, dict] = {}
    for name in inherited_names:
        payload = dict(old_datasets[name])
        payload["inherited"] = True
        payload["inherited_run_id"] = previous.get("run_id")
        payload["inherited_generated_at"] = previous.get("generated_at")
        merged[name] = payload
    for name, payload in current.items():
        entry = dict(payload)
        entry["inherited"] = False
        merged[name] = entry
    report["datasets"] = {name: merged[name] for name in sorted(merged)}
    report["scope"] = "subset"
    report["refreshed"] = list(selected)
    report["inherited"] = inherited_names
    report.update(_summarize_quality(config, report["datasets"]))
    return report


def _quality_report(
    config: LoadedConfig,
    run_id: str,
    reference: date,
    outcomes: list[IngestOutcome],
    pipeline: dict,
    calendars: dict[str, TradeCalendar] | None = None,
) -> dict:
    """汇总逐数据集结果为全局质量报告。

    全局状态规则：全部失败→FAILED；存在失败/RED/核心未达 OK→DEGRADED；
    否则 OK。``missing`` 收集 FAILED 数据集，``revised_only`` 依据 notes 中是否
    含 ``REVISED_ONLY`` 判定（GPR 等修订序列）。

    Args:
        config: 已加载配置，用于读取核心标记与 ``config_hash``。
        run_id: 本次抓取批次号。
        reference: 质量判定的参考日期（陈旧度以它为基准）。
        outcomes: 逐数据集结果。
        pipeline: 链路状态快照；写入报告，UI 可直接展示断点而无需翻日志。
        calendars: 本次缺口分母用的日历；写进报告 ``calendar`` 节点，让验收卡
            能指出“这个覆盖率是按官方交易日还是按工作日近似算的”。

    Returns:
        可直接 JSON 序列化的报告字典。
    """
    datasets = {outcome.dataset: outcome.as_dict() for outcome in outcomes}
    summary = _summarize_quality(config, datasets)
    return {
        "schema_version": "1",
        "run_id": run_id,
        "generated_at": iso(now_shanghai()),
        "reference_date": reference.isoformat(),
        "config_hash": config.config_hash,
        **summary,
        "calendar": describe_calendars(calendars),
        "datasets": datasets,
        "pipeline": pipeline,
    }


def load_normalized(config: LoadedConfig, dataset: str) -> pd.DataFrame:
    """读取某数据集的规范化层 CSV（不存在时返回空表）。

    Args:
        config: 已加载配置，用于定位 ``data/normalized``。
        dataset: 数据集名称（文件名主干）。

    Returns:
        规范化后的 DataFrame；文件缺失时由 ``NormalizedStore.load`` 返回空表。
    """
    return NormalizedStore(config.path("normalized")).load(dataset)
