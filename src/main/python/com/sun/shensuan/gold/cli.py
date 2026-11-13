"""P1 CLI：preflight / ingest / normalize / status / coverage / rate / calendar / list-datasets / validate-manual / extract-events / accept / doctor / daily。

职责：为 P1 数据层提供可脚本化入口；每个子命令只做参数解析与调用，业务逻辑
留在 ``data`` 包。所有输出统一为 JSON（``ensure_ascii=False``），供 cron、
人工复核与后续阶段消费。

退出码约定：
- 0：命令成功（质量可为 OK/DEGRADED；``ingest`` 只要全局状态不是 FAILED 即 0）；
- 1：``status`` 读到全局 FAILED；``accept`` 判据存在 FAIL（阶段验收不通过）；
  ``calendar`` 表示日历不可用或未覆盖基准日（需要 ``--refresh``）；
  ``doctor`` 表示存在必须人工处理的数据问题（``NEEDS_HUMAN``/``ERROR``）；
- 2：预检存在 RED、配置/参数错误，或状态文件/质量报告缺失。
- 3：仅 ``daily``：已有日更持锁（``data/.daily.lock`` 属于存活进程），本次一个
  子命令都不执行、不写任何产物，等对方跑完再重跑；``--force`` 才越过。

关键不变量：
- 只读命令不写业务数据；``ingest``/``normalize`` 的写盘全部走 data 层。
- ``accept`` 只读：判据不写盘、不改配置，只输出 verdict 与逐条读数。
- 不打印密钥；错误信息只回显异常文本。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

if __package__ in (None, ""):
    # 直接 `python cli.py` 运行时没有包上下文：把 src/main/python 挂到 sys.path，
    # 再用绝对包名导入，避免相对导入在脚本模式失败（parents[4] 即 src/main/python）
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from com.sun.shensuan.gold.acceptance.p1 import run_p1_acceptance
    from com.sun.shensuan.gold.common.atomic import atomic_write_json, atomic_write_text
    from com.sun.shensuan.gold.common.errors import GoldError
    from com.sun.shensuan.gold.common.hashing import hash_obj
    from com.sun.shensuan.gold.common.logging_setup import setup_logging
    from com.sun.shensuan.gold.common.timeutil import iso, now_shanghai, parse_date
    from com.sun.shensuan.gold.config.loader import LoadedConfig, load_config
    from com.sun.shensuan.gold.data.adapters import available_adapters
    from com.sun.shensuan.gold.data.ingest import run_ingest
    from com.sun.shensuan.gold.data.manual import validate_manual_dir
    from com.sun.shensuan.gold.data.news_events import (
        EVENT_COLUMNS,
        excluded_rows,
        extract_events,
        load_rules,
        merge_with_manual,
    )
    from com.sun.shensuan.gold.data.normalize import assess_dataset, summarize_continuity
    from com.sun.shensuan.gold.data.pipeline import load_status
    from com.sun.shensuan.gold.data.rates import select_usdcny
    from com.sun.shensuan.gold.data.remediation import build_remediation_report, summarize as summarize_remediation
    from com.sun.shensuan.gold.data.store import NormalizedStore
    from com.sun.shensuan.gold.data.trade_calendar import (
        fetch_cn_trade_days,
        load_all_calendars,
        save_cn_trade_calendar,
    )
else:
    from .acceptance.p1 import run_p1_acceptance
    from .common.atomic import atomic_write_json, atomic_write_text
    from .common.errors import GoldError
    from .common.hashing import hash_obj
    from .common.logging_setup import setup_logging
    from .common.timeutil import iso, now_shanghai, parse_date
    from .config.loader import LoadedConfig, load_config
    from .data.adapters import available_adapters
    from .data.ingest import run_ingest
    from .data.manual import validate_manual_dir
    from .data.news_events import (
        EVENT_COLUMNS,
        excluded_rows,
        extract_events,
        load_rules,
        merge_with_manual,
    )
    from .data.normalize import assess_dataset, summarize_continuity
    from .data.pipeline import load_status
    from .data.rates import select_usdcny
    from .data.remediation import build_remediation_report, summarize as summarize_remediation
    from .data.store import NormalizedStore
    from .data.trade_calendar import (
        fetch_cn_trade_days,
        load_all_calendars,
        save_cn_trade_calendar,
    )


def _print_json(payload) -> None:
    """以 UTF-8 友好的缩进 JSON 打印到 stdout。

    ``default=str`` 兜住 datetime/Path 等不可直接序列化的对象；
    ``sort_keys=True`` 保证同一输入输出稳定，便于 diff 与留证。
    """
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _check(name: str, status: str, detail: str) -> dict:
    """构造一条预检结果（``check/status/detail`` 三字段），供统一汇总。"""
    return {"check": name, "status": status, "detail": detail}


def _mem_available_bytes() -> int | None:
    """读取 ``/proc/meminfo`` 的 MemAvailable。

    Returns:
        可用内存字节数；文件不存在或找不到该字段时返回 None（预检按 YELLOW 处理）。
    """
    path = Path("/proc/meminfo")
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return None


def _writable(directory: Path) -> tuple[bool, str]:
    """验证目录可创建且可写：建目录 + 写一个临时文件后立即删除。

    Args:
        directory: 待检查目录（不存在时会创建）。

    Returns:
        ``(True, 路径)`` 或 ``(False, 错误详情)``；不抛异常，由预检汇总成 RED。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".preflight-", delete=True):
            pass
        return True, str(directory)
    except OSError as exc:
        return False, f"{directory}: {exc}"


def command_preflight(config: LoadedConfig, _args) -> int:
    """环境与配置预检：Python 版本、配置哈希、目录可写、磁盘、内存、手工 CSV。

    预检只读不写业务数据；``_writable`` 会创建缺失目录并写临时文件（自清理）。
    内存低于 LLM 阈值只标 YELLOW 而不是 RED：P1 不启 LLM，不应因此阻断抓取。

    Args:
        config: 已加载配置。
        _args: argparse 命名空间（本命令无参数）。

    Returns:
        进程退出码：0 表示无 RED；2 表示存在 RED 项（YELLOW 不阻断）。
    """
    checks = []
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(
        _check("python", "GREEN" if sys.version_info[:2] == (3, 10) else "RED", f"{version} ({sys.executable})")
    )
    checks.append(_check("config_hash", "GREEN", config.config_hash))
    for key in ("data", "raw", "normalized", "manual", "decisions", "logs"):
        ok, detail = _writable(config.path(key))
        checks.append(_check(f"path.{key}", "GREEN" if ok else "RED", detail))

    usage = shutil.disk_usage(config.repo_root)
    free_gb = usage.free / (1024**3)
    minimum = config.application.preflight.min_disk_free_gb
    checks.append(
        _check(
            "disk_free",
            "GREEN" if free_gb >= minimum else "RED",
            f"{free_gb:.2f} GiB free，阈值 {minimum} GiB",
        )
    )
    mem = _mem_available_bytes()
    if mem is None:
        checks.append(_check("mem_available", "YELLOW", "无法读取 /proc/meminfo"))
    else:
        mem_gb = mem / (1024**3)
        checks.append(
            _check(
                "mem_available",
                "GREEN" if mem_gb >= config.application.preflight.llm_min_free_gb else "YELLOW",
                f"{mem_gb:.2f} GiB；LLM 阈值 {config.application.preflight.llm_min_free_gb} GiB（P1 不启 LLM）",
            )
        )
    try:
        manual = validate_manual_dir(config.path("manual"))
        bad = [item for item in manual if item["status"] == "MISSING"]
        checks.append(
            _check(
                "manual_csv",
                "YELLOW" if bad else "GREEN",
                f"缺失 {[item['file'] for item in bad]}" if bad else f"{len(manual)} 个手工文件已校验",
            )
        )
    except GoldError as exc:
        checks.append(_check("manual_csv", "RED", str(exc)))

    overall = "RED" if any(item["status"] == "RED" for item in checks) else (
        "YELLOW" if any(item["status"] == "YELLOW" for item in checks) else "GREEN"
    )
    _print_json({"generated_at": iso(now_shanghai()), "overall": overall, "checks": checks})
    return 0 if overall != "RED" else 2


def command_ingest(config: LoadedConfig, args) -> int:
    """执行抓取并打印逐数据集摘要。

    输入：``--datasets``（逗号分隔）、``--start``/``--end``（YYYY-MM-DD）、
    ``--run-id``。
    输出：stdout JSON（run_id、全局状态、缺失/降级/RED/核心未达标/修订序列清单、
    每数据集行数与质量明细）；同时逐数据集写结构化日志。
    退出码：0 表示全局状态不是 FAILED；2 表示全部数据源失败。

    Args:
        config: 已加载配置。
        args: argparse 命名空间，含 datasets/start/end/run_id。

    Returns:
        进程退出码（0 或 2）。
    """
    logger = setup_logging(
        config.path("logs"),
        config.application.logging.retention_days,
        config.application.logging.level,
    )
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    datasets = [name.strip() for name in args.datasets.split(",")] if args.datasets else None
    outcomes, report = run_ingest(
        config,
        datasets=datasets,
        start=start,
        end=end,
        run_id=args.run_id,
    )
    for outcome in outcomes:
        logger.info(
            "ingest outcome",
            extra={
                "run_id": report["run_id"],
                "stage": outcome.dataset,
                "status": outcome.status.value,
                "dataset": outcome.dataset,
            },
        )
    _print_json(
        {
            "run_id": report["run_id"],
            "global_status": report["global_status"],
            "missing": report["missing"],
            "degraded": report["degraded"],
            "red": report["red"],
            "core_not_green": report["core_not_green"],
            "revised_only": report["revised_only"],
            "datasets": {
                name: {
                    "status": payload["status"],
                    "quality": payload["quality"],
                    "rows": payload["rows"],
                    "total_rows": payload["total_rows"],
                    "new_rows": payload["new_rows"],
                    "notes": payload["notes"],
                    "quality_detail": payload["quality_detail"],
                }
                for name, payload in report["datasets"].items()
            },
        }
    )
    return 0 if report["global_status"] != "FAILED" else 2


def command_normalize(config: LoadedConfig, args) -> int:
    """对已有规范化文件重跑质量闸（不重新抓取）。

    结果写入 ``data/normalized/_quality_recheck.json`` 并打印，用于在不改数据
    的前提下复核阈值/配置变化的影响。单数据集 MISSING 只体现在报告里。
    质量判定读取 ``rejected/<dataset>.csv`` 并与 ``run_ingest`` 同口径：近期
    被剔除的坏行会把状态压到 YELLOW/RED，不会被历史好数据洗成 GREEN。

    Args:
        config: 已加载配置。
        args: argparse 命名空间；``datasets`` 支持单个名或逗号分隔列表。

    Returns:
        固定返回 0；配置里不存在的数据集名会由 ``main`` 统一转成退出码 2。
    """
    store = NormalizedStore(config.path("normalized"))
    names = [args.datasets] if args.datasets else [
        name for name, spec in config.data_sources.datasets.items() if spec.enabled
    ]
    if args.datasets and "," in args.datasets:
        names = [name.strip() for name in args.datasets.split(",")]
    reports = {}
    # 日历只读一次：16 个数据集共用同一份分母，避免每个源重复解析 8,000 行文件。
    calendars = load_all_calendars(config.path("reference"))
    for name in names:
        spec = config.data_sources.datasets[name]
        frame = store.load(name)
        if frame.empty:
            reports[name] = {"status": "MISSING", "rows": 0}
            continue
        # 与 run_ingest 同口径：剔除留档必须参与质量判定，否则近期坏行会被历史
        # 好数据洗成 GREEN，复核报告比真实日链更乐观。
        rejected = store.read_rejected(name)
        quality = assess_dataset(
            frame,
            spec,
            invalid_rows=len(rejected),
            rejected=rejected,
            calendars=calendars,
        )
        reports[name] = quality.as_dict()
    payload = {
        "generated_at": iso(now_shanghai()),
        "config_hash": config.config_hash,
        "mode": "recheck",
        "datasets": reports,
    }
    target = config.path("normalized") / "_quality_recheck.json"
    # 质量复核报告也要原子写：链路页与后续命令随时可能读到它，半截 JSON 比缺文件更难查。
    atomic_write_json(target, payload)
    _print_json(payload)
    return 0


def command_status(config: LoadedConfig, args) -> int:
    """读取指定日期的链路状态文件并打印。

    超过 ``request_timeout_s + 60s`` 仍为 RUNNING 的节点会被 ``load_status``
    改判 TIMEOUT，这是进程崩溃后残留状态的兜底；链路页/CLI 只读状态文件，
    不依赖日志发现断路。

    Args:
        config: 已加载配置。
        args: argparse 命名空间；``date`` 为 YYYY-MM-DD。

    Returns:
        退出码：0 表示全局不是 FAILED；1 表示 FAILED；2 表示状态文件不存在。
    """
    path = config.path("decisions") / args.date / "pipeline_status.json"
    if not path.exists():
        _print_json({"status": "MISSING", "detail": f"状态文件不存在：{path}"})
        return 2
    # 超时阈值取“单次请求超时 + 60s 余量”：低于它仍可能是正常进行中的节点
    stale_after_ms = config.application.ingest.request_timeout_s * 1000 + 60_000
    payload = load_status(path, stale_after_ms=stale_after_ms)
    payload["status_file_hash"] = hash_obj(payload)
    _print_json(payload)
    return 0 if payload.get("overall") != "FAILED" else 1


def command_coverage(config: LoadedConfig, _args) -> int:
    """打印最近一次 ingest 写下的质量/覆盖报告。

    输出额外附 ``continuity``：按工作日覆盖率排序的连续性强弱摘要，让
    SPDR 这类缺口不只在明细里、而是在第一屏就能看到。该字段只作展示，
    不改变 ``datasets.*.status``，避免把节假日误判成 RED。

    Args:
        config: 已加载配置。
        _args: argparse 命名空间（本命令无参数）。

    Returns:
        退出码：0 成功；2 表示 ``_quality.json`` 不存在（通常意味着还没跑过 ingest）。
    """
    store = NormalizedStore(config.path("normalized"))
    report = store.read_quality_report()
    if not report:
        _print_json({"status": "MISSING", "detail": "data/normalized/_quality.json 不存在，先跑 ingest"})
        return 2
    report["continuity"] = summarize_continuity(
        report.get("datasets") or {}, load_all_calendars(config.path("reference"))
    )
    # 自动诊断摘要放第一屏：旧质量报告可能还没有 remediation 字段，单独读文件补上；
    # 让“哪些源被自动降能力/还需要人处理”不用再翻另一个命令。
    remediation = store.read_remediation_report()
    if remediation:
        report["remediation"] = summarize_remediation(remediation)
    _print_json(report)
    return 0


def command_rate(config: LoadedConfig, args) -> int:
    """打印某一天的 USD/CNY 汇率选择结果（折算价优先、中间价兜底）。

    只读 ``data/normalized/usdcny.csv`` 与 ``usdcny_mid.csv``，不抓取、不写盘；
    是 ``data/rates.select_usdcny`` 的消费入口，供开发期直接查看“今天用哪条
    汇率、为什么”。

    Args:
        config: 已加载配置。
        args: argparse 命名空间；``date`` 为 YYYY-MM-DD。

    Returns:
        固定 0；数据缺失时输出 quality=MISSING、value=None，不是命令失败。
    """
    store = NormalizedStore(config.path("normalized"))
    selection = select_usdcny(
        args.date,
        store.load("usdcny"),
        store.load("usdcny_mid"),
    )
    _print_json(selection.as_dict())
    return 0


def command_calendar(config: LoadedConfig, args) -> int:
    """读取（或刷新）上交所交易日历，打印它能否用作缺口分母。

    默认只读盘不联网：打印日历的行数、首末交易日、抓取时间与校验状态，
    并给出「是否覆盖到基准日」。`--refresh` 才联网抓取并原子写入
    ``data/reference/trade_calendar_cn_sse.csv``（含 meta：来源、URL、行数、sha256）。

    Args:
        config: 已加载配置，用于取 ``paths.reference``。
        args: argparse 命名空间；``--refresh`` 触发抓取，``--date`` 指定基准日
            （默认今天，Asia/Shanghai）。

    Returns:
        退出码：0 = 日历可用且覆盖基准日；1 = 日历不可用或未覆盖（需要重抓）；
        ``--refresh`` 抓取失败时由 ``AdapterError`` 走 ``main`` 的退出码 2。
    """
    reference = parse_date(args.date) if args.date else now_shanghai().date()
    directory = config.path("reference")
    if getattr(args, "refresh", False):
        save_cn_trade_calendar(directory, fetch_cn_trade_days(), now_shanghai())
    calendars = load_all_calendars(directory)
    cn = calendars["cn_sse"]
    payload = {
        "reference_date": reference.isoformat(),
        "directory": str(directory),
        "cn_sse": {
            "integrity": cn.integrity,
            "available": cn.available,
            "rows": cn.rows,
            "as_of_min": cn.as_of_min.isoformat() if cn.as_of_min else None,
            "as_of_max": cn.as_of_max.isoformat() if cn.as_of_max else None,
            "fetched_at": cn.fetched_at,
            "source": cn.source,
            "covers_reference": cn.covers(reference),
            "note": cn.note,
        },
        "weekday_basis": calendars["weekday"].note,
    }
    _print_json(payload)
    return 0 if (cn.available and cn.covers(reference)) else 1


def command_extract_events(config: LoadedConfig, args) -> int:
    """从已入库的快讯标题抽取地缘事件，写入事件日历输入文件（默认只试跑不写盘）。

    只读 `data/normalized/news_*.csv`，不联网、不读新闻正文、不调用 LLM；
    规则表来自 `config/strategy.yaml` 的 `geo.news_events`（含 `rules_version`）。
    默认 `--dry-run` 语义：不加 `--write` 时只在内存里算并打印统计，绝不改盘；
    加 `--write` 才原子写入 `paths.manual/geo_events.csv`，且只替换本模块生成的
    行（`event_id` 以 `XGE-` 开头），人工登记的行原样保留。

    Args:
        config: 已加载配置。
        args: argparse 命名空间；``--write`` 落盘，``--news`` 限定参与抽取的新闻数据集。

    Returns:
        退出码：0 = 抽取完成（事件数可以为 0，0 表示「未识别到事件」，不表示无风险）；
        2 = 规则表缺失/非法（由 ``main`` 统一转成退出码 2）。

    Notes:
        抽出来的 severity 未经人工复核，不得作为加仓依据（§6.2、§7.3、§8.5）。
    """
    news_names = [
        name for name, spec in config.data_sources.datasets.items()
        if spec.enabled and spec.kind == "news" and (not args.news or name in [
            item.strip() for item in str(args.news).split(",") if item.strip()
        ])
    ]
    store = NormalizedStore(config.path("normalized"))
    frames = []
    per_source: dict[str, int] = {}
    for name in news_names:
        frame = store.load(name)
        per_source[name] = int(len(frame))
        if not frame.empty:
            frames.append(frame)
    news = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["published_at", "title", "source", "content_hash"]
    )
    section = ((config.strategy or {}).get("geo") or {}).get("news_events")
    rules = load_rules(section)
    events = extract_events(news, rules)
    manual_dir = config.path("manual")
    target = manual_dir / "geo_events.csv"
    existing = (
        pd.read_csv(target, dtype=str, keep_default_na=False)
        if target.exists() and target.stat().st_size > 0
        else pd.DataFrame(columns=list(EVENT_COLUMNS))
    )
    merged, stats = merge_with_manual(existing, events)
    payload = {
        "rules_version": rules.rules_version,
        "dry_run": not getattr(args, "write", False),
        "news_rows_read": per_source,
        "news_rows_total": int(len(news)),
        # 复核排除表生效与否要单独念：0 既可能是「没命中」，也可能是「哈希写错了」。
        "review_excluded_rows": excluded_rows(news, rules),
        "events_extracted": int(len(events)),
        "by_severity": {str(key): int(value) for key, value in events["severity"].value_counts().items()} if not events.empty else {},
        "by_type": {str(key): int(value) for key, value in events["type"].value_counts().items()} if not events.empty else {},
        "output": str(target),
        "manual_rows_kept": stats["manual_kept"],
        "replaced_extracted_rows": stats["replaced_extracted_rows"],
        "rows_after_merge": int(len(merged)),
        "csv_sha256": hashlib.sha256(merged.to_csv(index=False).encode("utf-8")).hexdigest(),
        "note": "severity 由关键词规则自动赋值，未经人工复核不得作为加仓依据（§6.2、§8.5）",
    }
    if not payload["dry_run"]:
        atomic_write_text(target, merged.to_csv(index=False))
    _print_json(payload)
    return 0


def command_list_datasets(config: LoadedConfig, _args) -> int:
    """列出已注册适配器与每个数据集的配置摘要（只读，不含密钥）。

    Returns:
        退出码 0；输出含 ``adapters``（注册表名称）与逐数据集
        enabled/adapter/source/kind/core/available_at_rule/stale_days。
    """
    payload = {
        name: {
            "enabled": spec.enabled,
            "adapter": spec.adapter,
            "source": spec.source,
            "kind": spec.kind,
            "core": spec.core,
            "available_at_rule": spec.available_at_rule,
            "stale_days": spec.stale_days,
        }
        for name, spec in config.data_sources.datasets.items()
    }
    _print_json({"adapters": available_adapters(), "datasets": payload})
    return 0


def command_validate_manual(config: LoadedConfig, _args) -> int:
    """校验手工 CSV 的字段与取值。

    Returns:
        退出码：0 表示校验完成（状态可能为 YELLOW）；2 表示校验器本身失败
        （目录/清单不可读）。有文件缺失时状态为 YELLOW，不阻断。
    """
    try:
        reports = validate_manual_dir(config.path("manual"))
    except GoldError as exc:
        _print_json({"status": "RED", "error": str(exc)})
        return 2
    status = "OK" if all(item["status"] != "MISSING" for item in reports) else "YELLOW"
    _print_json({"status": status, "files": reports})
    return 0


def command_accept(config: LoadedConfig, args) -> int:
    """执行阶段验收闸（程序化判据），当前只支持 P1。

    Args:
        config: 已加载配置（含 `acceptance` 阈值）。
        args: argparse 命名空间；`stage` 必须为 ``P1``，`date` 可选验收基准日。

    Returns:
        退出码：0 表示 verdict=PASS；1 表示存在 FAIL（阶段验收不通过）；
        2 表示参数错误、质量报告缺失或判据自身异常。

    Notes:
        只读命令：不写数据、不改配置。判据读数全部打进 JSON，验收卡只引用本轮输出，
        不允许人工目视替代（AGENTS §18.6）。
    """
    stage = (args.stage or "").upper()
    if stage != "P1":
        _print_json({"stage": stage, "verdict": "ERROR", "error": f"未实现的阶段验收：{args.stage}"})
        return 2
    try:
        payload = run_p1_acceptance(config, reference=args.date)
    except GoldError as exc:
        _print_json({"stage": stage, "verdict": "ERROR", "error": str(exc)})
        return 2
    _print_json(payload)
    return 0 if payload["verdict"] == "PASS" else 1


def command_doctor(config: LoadedConfig, args) -> int:
    """扫描数据问题，输出自动处置与每数据集能力清单。

    读取最近一次 ``_quality.json``、规范化表、``rejected/`` 留档、原始快照与
    手工事件表，按 AGENTS 的数据口径给出：
    - ``AUTO_RESOLVED``/``CONTAINED``：已自动隔离或已通过能力清单限制下游；
    - ``NEEDS_HUMAN``：没有安全自动动作（如事件 severity 未复核、RED 无留档）。

    默认只打印、不写盘；``--write`` 才原子写 ``data/normalized/_remediation.json``。
    日链 ``ingest`` 结束时也会自动生成同一份文件，本命令主要用于复核与补跑。

    Args:
        config: 已加载配置。
        args: argparse 命名空间；``--write`` 控制是否落盘。

    Returns:
        退出码：0 = ``CLEAN``/``AUTO_CONTAINED``（无需人工）；1 = ``NEEDS_HUMAN``
        或 ``ERROR``；2 = 质量报告缺失。
    """
    store = NormalizedStore(config.path("normalized"))
    quality = store.read_quality_report()
    if not quality:
        _print_json(
            {
                "status": "MISSING",
                "detail": "data/normalized/_quality.json 不存在，先跑 ingest",
            }
        )
        return 2
    report = build_remediation_report(config, quality)
    if getattr(args, "write", False):
        report["written_path"] = str(store.write_remediation_report(report))
    _print_json(report)
    return 0 if report.get("overall") in {"CLEAN", "AUTO_CONTAINED"} else 1


DAILY_LOCK_NAME = ".daily.lock"
DAILY_SCHEMA_VERSION = "1"
EXIT_BUSY = 3
"""`daily` 的并发拒绝码：已有日更持锁时返回它，不当参数错误（2）也不当验收失败（1）。"""


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否还活着：发信号 0 只探测不杀伤。

    Args:
        pid: 进程号（>0）。

    Returns:
        True 表示进程存在（包括属于其他用户、无权限探测的 `PermissionError`
        ——那也是存在，不能当作陈旧锁删掉）；pid<=0 或 `ProcessLookupError`
        返回 False。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_daily_lock(lock_path: Path, force: bool = False) -> tuple[bool, bool, str]:
    """原子取得日更互斥锁。

    2026-11-11 真跑事故：同日两批抓取撞 run-id，把 `_quality.json` 覆写成
    FAILED。锁只防「两个 daily 同时写同一批目录」，不做排队。

    Args:
        lock_path: 锁文件路径（`data/.daily.lock`），内容形如 `"<pid> <启动时刻>"`。
        force: True 时即使被活进程占用也放行（调用方自担覆写风险，且不取得所有权）。

    Returns:
        三元组 `(可以运行, 本次是否持有所有权, 原因)`。陈旧锁（pid 不存在）会
        删除后重取一次；`PermissionError` 之类的真实冲突返回
        `(False, False, 原因)`，调用方必须硬停而不是继续写。
    """
    def _try_create() -> tuple[bool, bool, str]:
        """用 `O_CREAT|O_EXCL` 抢锁；成功即持锁，失败说明锁已存在。"""
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False, False, "already_held"
        try:
            os.write(fd, f"{os.getpid()} {iso(now_shanghai())}\n".encode("utf-8"))
        finally:
            os.close(fd)
        return True, True, "acquired"

    ok, owned, reason = _try_create()
    if ok:
        return True, True, reason
    content = lock_path.read_text(encoding="utf-8").strip()
    holder = int(content.split()[0]) if content.split() and content.split()[0].isdigit() else -1
    if not _pid_alive(holder):
        # 陈旧锁：持锁进程早就不在了，删掉重取一次；再失败就如实报冲突。
        lock_path.unlink(missing_ok=True)
        ok, owned, reason = _try_create()
        if ok:
            return True, True, "stale_replaced"
        return not force, False, f"lock_raced:{reason}"
    if force:
        return True, False, f"forced_over_pid_{holder}"
    return False, False, f"another_daily_running:pid_{holder}"


def _release_daily_lock(lock_path: Path, owned: bool) -> None:
    """释放本次持有的锁；不是本次创建的（`force` 越权或抢锁失败）绝不删别人的锁。"""
    if owned:
        lock_path.unlink(missing_ok=True)


def _daily_summary_path(config: LoadedConfig) -> Path:
    """返回本次日更汇总的落盘路径 `data/decisions/<date>/daily_summary.json`。"""
    return config.path("decisions") / now_shanghai().date().isoformat() / "daily_summary.json"


def _rate_observation(now) -> dict:
    """按墙钟时点判断这条 `rate` 读数能不能用来推进 T-025 汇率观察。

    §5.2 同日兜底：18:00 前折算价（`same_day_1800`）还没转正，只有 09:30–18:00
    之间取到当日行才证明 SAFE 中间价（`same_day_0915`）当日可见；晚间跑必然取
    折算价，测不到 SAFE 腿。观察窗口起点取 09:30（看板 T-025 的「09:30 后抽查」）。

    Args:
        now: 带时区的当前时刻（Asia/Shanghai）。

    Returns:
        `{"phase", "observation_valid", "window", "note"}`；`phase` 为
        `pre_0930`/`intraday`/`post_1800` 三档之一。
    """
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        phase = "pre_0930"
    elif minutes < 18 * 60:
        phase = "intraday"
    else:
        phase = "post_1800"
    return {
        "phase": phase,
        "observation_valid": phase == "intraday",
        "window": "09:30-18:00 Asia/Shanghai",
        "note": "T-025 只在 intraday 档推进；pre_0930 当日行尚未生成、post_1800 必然取折算价",
    }


def command_daily(config: LoadedConfig, args) -> int:
    """一键日更：preflight→ingest→accept→doctor→rate，硬失败即停，末尾汇总并落盘。

    Args:
        config: 已加载配置。
        args: 含可选 ``run_id``（默认 ``daily-YYYYMMDD-HHMMSS``）、``datasets``
            （逗号分隔的子集运行，透传给 ingest 并标 ``scope=subset``）、``force``
            （跳过互斥检查，自担覆写风险）。

    Returns:
        退出码：0=accept PASS；1=accept 判红；2=preflight/ingest 硬停或 accept 异常；
        3（``EXIT_BUSY``）=已有日更持锁，本次一个子命令都没执行。

    Notes:
        仍手动触发（不装 cron）；本命令是将来 P7 cron 的载荷，所以监控读数必须
        落盘而不是只在 stdout：每步 ``duration_ms``、``total_ms``、产物指针、
        汇率观察时点档全部写进 ``data/decisions/<date>/daily_summary.json``，
        并按 §15.7 给每步补一条含 ``duration_ms`` 的结构化日志。
    """
    today = now_shanghai().date().isoformat()
    stamp = now_shanghai().strftime("%H%M%S")
    run_id = getattr(args, "run_id", None) or f"daily-{today.replace('-', '')}-{stamp}"
    datasets_arg = getattr(args, "datasets", None)
    started_at = iso(now_shanghai())
    t0 = time.perf_counter()
    lock_path = config.path("data") / DAILY_LOCK_NAME
    may_run, owned, reason = _acquire_daily_lock(lock_path, force=getattr(args, "force", False))
    if not may_run:
        _print_json(
            {"daily": [], "overall": "BUSY", "run_id": run_id, "date": today, "detail": reason, "lock": str(lock_path)}
        )
        return EXIT_BUSY

    logger = setup_logging(
        config.path("logs"),
        config.application.logging.retention_days,
        config.application.logging.level,
    )
    steps: list[dict] = []

    def run(name, func, ns):
        """跑一个子命令：记退出码与耗时，并写一条阶段日志；返回退出码供调用方判停。"""
        step_t0 = time.perf_counter()
        code = func(config, ns)
        duration_ms = int((time.perf_counter() - step_t0) * 1000)
        steps.append({"step": name, "exit": code, "duration_ms": duration_ms})
        logger.info(
            "daily step",
            extra={
                "run_id": run_id,
                "stage": name,
                "status": f"exit_{code}",
                "duration_ms": duration_ms,
            },
        )
        return code

    try:
        summary: dict = {
            "schema_version": DAILY_SCHEMA_VERSION,
            "run_id": run_id,
            "date": today,
            "started_at": started_at,
            "scope": "subset" if datasets_arg else "full",
            "datasets": [item.strip() for item in datasets_arg.split(",")] if datasets_arg else None,
            "config_hash": config.config_hash,
            "lock": {"acquired": True, "owned": owned, "reason": reason},
        }
        code = run("preflight", command_preflight, None)
        if code != 0:
            _finish_daily(config, summary, steps, t0, "STOPPED", "preflight", code)
            return code
        code = run(
            "ingest",
            command_ingest,
            argparse.Namespace(datasets=datasets_arg, start=None, end=None, run_id=run_id),
        )
        if code != 0:
            _finish_daily(config, summary, steps, t0, "STOPPED", "ingest", code)
            return code
        accept_code = run("accept", command_accept, argparse.Namespace(stage="P1", date=None))
        run("doctor", command_doctor, argparse.Namespace(write=True))
        run("rate", command_rate, argparse.Namespace(date=today))
        overall = "PASS" if accept_code == 0 else ("FAIL" if accept_code == 1 else "ERROR")
        _finish_daily(config, summary, steps, t0, overall, None, accept_code)
        return accept_code
    finally:
        _release_daily_lock(lock_path, owned)


def _pending_finalization(config: LoadedConfig) -> dict:
    """从 `_remediation.json` 提取「等 `available_at` 到期后重抓转正」的临时行读数。

    境外 COMEX/WTI 每晚抓取必然留下 `fetched_at < available_at` 的 `PROVISIONAL` 行
    （§6.5），次日 06:00 后再跑一次才转正、才进得了事实包。这里把它显式写进汇总，
    避免「今天 `global_status=OK` 看着正常，事实包却永久缺最新一天」。

    Returns:
        `{"report_overall", "items": [...], "next_action"}`；诊断文件不存在时
        `report_overall=None`、`items=[]`，并写明「没有可诊断的报告」，不猜值。
        `next_action` 的到期时间按 items 自身的 `max_available_at` 生成最早/最晚，
        不写死「境外 06:00」——外汇（`usdcny`）当日 18:00 与境外次日 06:00 不同。
    """
    report = NormalizedStore(config.path("normalized")).read_remediation_report()
    items = []
    for issue in report.get("issues", []) or []:
        if issue.get("code") != "PROVISIONAL":
            continue
        evidence = issue.get("evidence") or {}
        items.append(
            {
                "dataset": issue.get("dataset"),
                "rows": evidence.get("rows"),
                "dates": evidence.get("dates"),
                "max_available_at": evidence.get("max_available_at"),
                "refresh_due": evidence.get("refresh_due"),
                "action": issue.get("action"),
            }
        )
    if not report:
        next_action = "无 `_remediation.json`：先跑 ingest 或 doctor，不猜待转正行"
    elif items:
        # 到期时间来自 items：14:03 这类盘中跑会遇到 usdcny 当日 18:00 与境外次日
        # 06:00 两种点，固定写「境外 06:00」会把外汇那行漏成「明早再说」。
        dues = sorted({str(item["max_available_at"]) for item in items if item.get("max_available_at")})
        if not dues:
            next_action = "available_at 到期后重跑 daily 或只跑对应数据集子集（待转正行未给出到期时间）"
        else:
            window = f"最早 {dues[0]}，最晚 {dues[-1]}" if len(dues) > 1 else dues[0]
            next_action = f"available_at 到期后重跑 daily 或只跑对应数据集子集（{window}）"
    else:
        next_action = "无待转正的 PROVISIONAL 行"
    return {"report_overall": report.get("overall"), "items": items, "next_action": next_action}


def _finish_daily(config: LoadedConfig, summary: dict, steps: list[dict], t0: float, overall: str, stopped_at, exit_code: int) -> None:
    """补全 daily 汇总（耗时、产物指针、观察档、终态），原子落盘并打印。

    Args:
        config: 已加载配置，用于派生四个产物路径。
        summary: ``command_daily`` 已填好头部字段的字典，本函数就地补剩余字段。
        steps: 各步 ``{step, exit, duration_ms}`` 列表。
        t0: ``time.perf_counter()`` 起点，用于算 ``total_ms``。
        overall: ``PASS``/``FAIL``/``ERROR``/``STOPPED``。
        stopped_at: 硬停在哪一步；正常跑完传 None。
        exit_code: 本次 daily 将返回的退出码，写进汇总便于事后对表。

    Notes:
        落盘失败不改退出码：汇总缺失由监控侧发现，不能反过来打死日链——但失败
        原因会写进 ``summary["summary_error"]`` 并随 stdout 一起出来。
    """
    today = summary["date"]
    path = _daily_summary_path(config)
    summary.update(
        {
            "daily": steps,
            "overall": overall,
            "stopped_at": stopped_at,
            "exit_code": exit_code,
            "total_ms": int((time.perf_counter() - t0) * 1000),
            "step_ms": {step["step"]: step["duration_ms"] for step in steps},
            "ended_at": iso(now_shanghai()),
            "rate_observation": _rate_observation(now_shanghai()),
            "pending_finalization": _pending_finalization(config),
            "artifacts": {
                "quality": str(config.path("normalized") / "_quality.json"),
                "remediation": str(config.path("normalized") / "_remediation.json"),
                "pipeline_status": str(config.path("decisions") / today / "pipeline_status.json"),
                "daily_summary": str(path),
                "log_dir": str(config.path("logs")),
            },
        }
    )
    try:
        atomic_write_json(path, summary)
        summary["summary_path"] = str(path)
    except OSError as exc:
        summary["summary_error"] = f"{type(exc).__name__}: {exc}"
    _print_json(summary)



def build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器与全部子命令参数。

    Returns:
        配置好子命令的 ``ArgumentParser``；``required=True`` 保证必须给出命令。
    """
    parser = argparse.ArgumentParser(prog="gold", description="黄金量化系统 P1 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="环境与配置预检")
    ingest = sub.add_parser("ingest", help="抓取、规范化、质量闸")
    ingest.add_argument("--datasets", help="逗号分隔的数据集名；默认全部 enabled")
    ingest.add_argument("--start", help="起始日期 YYYY-MM-DD")
    ingest.add_argument("--end", help="结束日期 YYYY-MM-DD")
    ingest.add_argument("--run-id", help="指定 run_id（默认自动生成）")
    normalize = sub.add_parser("normalize", help="对已有规范化文件重跑质量闸")
    normalize.add_argument("--datasets", help="逗号分隔的数据集名")
    status = sub.add_parser("status", help="读取运行链路状态")
    status.add_argument("--date", required=True, help="YYYY-MM-DD")
    sub.add_parser("coverage", help="打印最近一次质量/覆盖报告")
    rate = sub.add_parser("rate", help="某一天的 USD/CNY 汇率选择（折算价优先、中间价兜底）")
    rate.add_argument("--date", required=True, help="YYYY-MM-DD")
    sub.add_parser("list-datasets", help="列出数据集与适配器")
    calendar = sub.add_parser("calendar", help="读取/刷新上交所交易日历（连续性缺口的分母）")
    calendar.add_argument("--date", help="基准日 YYYY-MM-DD；默认今天")
    calendar.add_argument("--refresh", action="store_true", help="联网重抓并落盘日历")
    sub.add_parser("validate-manual", help="校验手工 CSV")
    extract = sub.add_parser("extract-events", help="从快讯标题抽取地缘事件（默认不写盘）")
    extract.add_argument("--write", action="store_true", help="把抽取结果写入 data/manual/geo_events.csv")
    extract.add_argument("--news", help="限定参与抽取的新闻数据集，逗号分隔")
    accept = sub.add_parser("accept", help="阶段验收闸：数据质量/连续性/一致性的程序化判据")
    accept.add_argument("--stage", required=True, help="验收阶段，当前只支持 P1")
    accept.add_argument("--date", help="验收基准日 YYYY-MM-DD；默认取质量报告的 reference_date")
    doctor = sub.add_parser("doctor", help="自动识别数据问题并生成处置/能力清单（默认不写盘）")
    doctor.add_argument("--write", action="store_true", help="原子写 data/normalized/_remediation.json")
    daily = sub.add_parser("daily", help="一键日更：preflight→ingest→accept→doctor→rate（手动触发）")
    daily.add_argument("--run-id", help="指定 run_id（默认 daily-YYYYMMDD-HHMMSS）")
    daily.add_argument("--datasets", help="逗号分隔的数据集名；子集运行，透传给 ingest 并在汇总标 scope=subset")
    daily.add_argument(
        "--force",
        action="store_true",
        help="已有日更持锁时仍强行执行（自担覆写风险；默认拒绝并返回退出码 3）",
    )
    return parser


COMMANDS = {
    "preflight": command_preflight,
    "ingest": command_ingest,
    "normalize": command_normalize,
    "status": command_status,
    "coverage": command_coverage,
    "rate": command_rate,
    "calendar": command_calendar,
    "extract-events": command_extract_events,
    "list-datasets": command_list_datasets,
    "validate-manual": command_validate_manual,
    "accept": command_accept,
    "doctor": command_doctor,
    "daily": command_daily,
}


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数 → 加载配置 → 分发命令。

    Args:
        argv: 参数列表；None 表示取 ``sys.argv[1:]``（便于测试注入）。

    Returns:
        进程退出码；配置加载失败或业务异常统一返回 2 并输出 JSON 错误。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except Exception as exc:
        print(json.dumps({"status": "RED", "error": f"配置加载失败：{exc}"}, ensure_ascii=False))
        return 2
    try:
        return COMMANDS[args.command](config, args)
    except (GoldError, FileNotFoundError, KeyError, ValueError) as exc:
        print(json.dumps({"status": "RED", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
