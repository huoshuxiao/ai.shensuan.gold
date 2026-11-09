"""数据问题自动识别与安全处置：把已知缺陷转成可执行的能力限制。

职责：
- 读取 ``_quality.json``、规范化表、``rejected/`` 留档、原始快照与手工事件表；
- 对每类已知问题给出确定性判定、处置动作与证据；
- 产出 ``_remediation.json``（问题清单 + 每个数据集的消费能力清单），供 P2 特征层
  在生成事实包前直接读取，避免“知道有缺陷但下游忘了排除”。

不负责：
- 不修源、不补值、不猜 severity、不把 RED 洗成 GREEN；
- 不替用户做人工复核；需要人判断的事项标 ``NEEDS_HUMAN``；
- 不发网络请求；需要重抓时只写 ``RERUN_INGEST`` 动作，由日链/人工执行。

判定原则（与 AGENTS §2.1-5、§6.6、§7.4、§12.8 一致）：
- 能自动做的只有“隔离、降能力、标注、阻止晋升”；
- 不能自动做的（事件 severity、无留档的 RED、旧配置配新报告）必须显式标
  ``NEEDS_HUMAN``，不得用默认值或猜测填平；
- ``AUTO_RESOLVED`` 只用于“坏行已留档且下游已封锁”的确定性动作；
- ``CONTAINED`` 表示源缺陷无法消除，但已通过能力清单阻止其进入分数/晋升；
- ``NEEDS_HUMAN`` 表示没有任何安全自动动作，必须人工处理。

能力清单字段（``capabilities.<dataset>``）：
- ``display_eligible``：能否只读展示；
- ``feature_eligible``：能否进入特征计算；
- ``score_eligible``：能否进入综合分数；
- ``promotion_eligible``：能否作为预注册/晋升证据（加仓规则）；
- ``price_only``：是否只能当收盘价使用（零成交量/零持仓源）；
- ``blocked_fields``：禁止使用的字段名；
- ``blocked_features``：禁止进入特征注册表的 ``fact_id``；
- ``calendar_basis``：覆盖率分母口径；
- ``coverage_is_lower_bound``：覆盖率是否只是保守下界（周一至周五近似）；
- ``reasons``：触发限制的原因码，便于审计与去重。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..common.hashing import hash_obj
from ..common.timeutil import iso, now_shanghai, parse_datetime
from ..config.loader import DatasetConfig, LoadedConfig
from .vintage import GPR_SOURCE

SCHEMA_VERSION = "1"
RECENT_WINDOW_ROWS = 5
"""“最近坏行”窗口：与 normalize.assess_dataset 的尾部 5 个有效行口径一致。"""

ZERO_FLOW_FIELDS = ("open_interest", "volume")
"""零成交量/零持仓源只能当收盘价；字段顺序固定，保证报告可复现。"""

SPDR_BLOCKED_FEATURES = ("FLOW_SPDR_CHG_1W", "FLOW_SPDR_CHG_4W")
"""SPDR 覆盖率 50.5%，4 周流量窗口会落在空洞上；P2 只展示、不进分数。"""

GPR_BLOCKED_PROMOTIONS = ("H-GEO-1", "H-GEO-2")
"""GPR 无 vintage，只能研究展示；地缘加仓假设不得用它做晋升证据。"""

EVENT_BLOCKED_FEATURES = ("GEO_EVENT_SEVERITY",)
"""抽取事件的 severity 未经人工复核，不得进入地缘分数或加仓依据。"""


@dataclass
class Issue:
    """一条自动识别出的数据问题及其处置。

    Attributes:
        issue_id: 稳定 ID，``<dataset>:<code>``；同一问题重复运行保持同号。
        dataset: 数据集名；全局问题用 ``*``。
        code: 机器可读原因码（如 ``RECENT_BAD_ROWS``、``ZERO_FLOW``）。
        status: ``AUTO_RESOLVED``/``CONTAINED``/``NEEDS_HUMAN``。
        action: 已执行或建议执行的动作码；必须能被人类读懂。
        detail: 中文一句话说明，写清“为什么”和影响。
        evidence: 可复核的读数（日期、行数、哈希、vintage 起止等），只放 JSON 友好值。
    """

    issue_id: str
    dataset: str
    code: str
    status: str
    action: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """序列化为 JSON 字典；键名固定，供 ``_remediation.json`` 与测试共用。"""
        return {
            "issue_id": self.issue_id,
            "dataset": self.dataset,
            "code": self.code,
            "status": self.status,
            "action": self.action,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class Capability:
    """单个数据集在 P2 及之后允许的消费方式。

    默认全开；任何降级规则命中后只收紧、不放宽。下游必须读本对象，禁止
    再从 ``_quality.json`` 自己猜“这个源能不能用”。
    """

    dataset: str
    display_eligible: bool = True
    feature_eligible: bool = True
    score_eligible: bool = True
    promotion_eligible: bool = True
    price_only: bool = False
    blocked_fields: list[str] = field(default_factory=list)
    blocked_features: list[str] = field(default_factory=list)
    blocked_promotions: list[str] = field(default_factory=list)
    calendar_basis: str = "weekday_approx"
    coverage_is_lower_bound: bool = False
    reasons: list[str] = field(default_factory=list)

    def add_reason(self, reason: str) -> None:
        """追加原因码并按字典序去重，保证同一输入的报告哈希稳定。"""
        if reason not in self.reasons:
            self.reasons.append(reason)
            self.reasons.sort()

    def as_dict(self) -> dict[str, Any]:
        """序列化为 JSON 字典；列表统一排序，便于逐字比对与哈希。"""
        return {
            "dataset": self.dataset,
            "display_eligible": self.display_eligible,
            "feature_eligible": self.feature_eligible,
            "score_eligible": self.score_eligible,
            "promotion_eligible": self.promotion_eligible,
            "price_only": self.price_only,
            "blocked_fields": sorted(set(self.blocked_fields)),
            "blocked_features": sorted(set(self.blocked_features)),
            "blocked_promotions": sorted(set(self.blocked_promotions)),
            "calendar_basis": self.calendar_basis,
            "coverage_is_lower_bound": self.coverage_is_lower_bound,
            "reasons": sorted(set(self.reasons)),
        }


def _read_table(path: Path) -> pd.DataFrame:
    """读 CSV 为全字符串表；文件不存在返回空表。

    统一 ``dtype=str, keep_default_na=False``：不能让 pandas 把 ``"NA"`` 或空串
    自动转成 NaN，否则“源给了空值”和“列不存在”会被混为一谈。

    Args:
        path: CSV 路径。

    Returns:
        空表或读入的 DataFrame；读取失败向上抛，不静默当成空表。
    """
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _as_dates(values: pd.Series) -> list[date]:
    """把 ``as_of`` 字符串列转成去重升序日期列表；非法值丢弃。"""
    parsed = pd.to_datetime(values, errors="coerce").dropna()
    return sorted({item.date() for item in parsed})


def _recent_bad_evidence(
    frame: pd.DataFrame,
    rejected: pd.DataFrame,
) -> dict[str, Any]:
    """计算最近坏行的证据与自动离开窗口所需的有效行数。

    Args:
        frame: 规范化主表，需含 ``as_of``。
        rejected: 剔除留档，需含 ``as_of`` 与可选 ``reject_reason``。

    Returns:
        ``{recent_rows, dates, reasons, latest_bad_date, clear_after_valid_rows}``；
        没有坏行时各项为空/0。``clear_after_valid_rows`` 是按尾部 5 个有效行
        口径估算的“还需多少个有效交易日才离开 RED 窗口”，不是承诺日期。
    """
    valid_dates = _as_dates(frame["as_of"]) if "as_of" in frame.columns else []
    if rejected.empty or "as_of" not in rejected.columns:
        return {
            "recent_rows": 0,
            "dates": [],
            "reasons": [],
            "latest_bad_date": None,
            "clear_after_valid_rows": 0,
        }
    bad_dates = _as_dates(rejected["as_of"])
    recent_valid = valid_dates[-RECENT_WINDOW_ROWS:]
    recent_floor = min(recent_valid) if recent_valid else None
    as_of_max = max(valid_dates) if valid_dates else None
    rows: list[dict[str, Any]] = []
    for _, row in rejected.iterrows():
        raw_date = str(row.get("as_of") or "").strip()
        try:
            bad_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            continue
        is_recent = recent_floor is None or bad_date >= recent_floor
        if as_of_max is not None and bad_date > as_of_max:
            is_recent = True
        if is_recent:
            rows.append(
                {
                    "as_of": bad_date.isoformat(),
                    "reject_reason": str(row.get("reject_reason") or ""),
                }
            )
    latest_bad = max(bad_dates) if bad_dates else None
    clear_after = 0
    if latest_bad is not None:
        after = sum(1 for item in valid_dates if item > latest_bad)
        clear_after = max(0, RECENT_WINDOW_ROWS - after)
    rows.sort(key=lambda item: (item["as_of"], item["reject_reason"]))
    return {
        "recent_rows": len(rows),
        "dates": sorted({item["as_of"] for item in rows}),
        "reasons": sorted({item["reject_reason"] for item in rows if item["reject_reason"]}),
        "latest_bad_date": latest_bad.isoformat() if latest_bad else None,
        "clear_after_valid_rows": clear_after,
    }


def _provisional_evidence(frame: pd.DataFrame, now: datetime) -> dict[str, Any]:
    """识别仍带 ``PROVISIONAL`` 的行，并判断是否已到可重抓时点。

    Args:
        frame: 规范化主表，需含 ``flags`` 与 ``available_at``。
        now: 当前带时区时间。

    Returns:
        ``{rows, dates, max_available_at, refresh_due}``；没有临时行时为空/False。
    """
    if frame.empty or "flags" not in frame.columns:
        return {"rows": 0, "dates": [], "max_available_at": None, "refresh_due": False}
    mask = frame["flags"].fillna("").astype(str).str.contains("PROVISIONAL", regex=False)
    selected = frame.loc[mask]
    if selected.empty:
        return {"rows": 0, "dates": [], "max_available_at": None, "refresh_due": False}
    dates = sorted({str(value)[:10] for value in selected.get("as_of", []) if str(value).strip()})
    available: list[datetime] = []
    if "available_at" in selected.columns:
        for value in selected["available_at"]:
            try:
                available.append(parse_datetime(value))
            except (TypeError, ValueError):
                continue
    max_available = max(available) if available else None
    return {
        "rows": int(len(selected)),
        "dates": dates,
        "max_available_at": iso(max_available) if max_available else None,
        "refresh_due": bool(max_available is not None and max_available <= now),
    }


def _vintage_evidence(raw_root: Path) -> dict[str, Any]:
    """统计 GPR 原始快照，作为“从哪天起才有 vintage”的证据。

    只读 ``data/raw`` 下的 ``.meta.json``；不解析 GPR 大文件，也不推断历史。
    没有快照时返回 0，调用方必须判 ``NEEDS_HUMAN``，不得自行编造 vintage。

    Args:
        raw_root: 原始层根目录。

    Returns:
        ``{snapshots, first_fetched_at, last_fetched_at}``。
    """
    source_dir = raw_root / GPR_SOURCE
    fetched: list[datetime] = []
    for meta_path in source_dir.glob("**/*.meta.json") if source_dir.exists() else []:
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched.append(parse_datetime(payload["fetched_at"]))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    fetched.sort()
    return {
        "snapshots": len(fetched),
        "first_fetched_at": iso(fetched[0]) if fetched else None,
        "last_fetched_at": iso(fetched[-1]) if fetched else None,
    }


def _event_review_queue(manual_dir: Path, spec: DatasetConfig) -> list[dict[str, Any]]:
    """列出由快讯抽取、但 severity 尚未人工复核的事件。

    识别口径：``event_id`` 以 ``XGE-`` 开头（news_events.py 的抽取前缀）。
    若手工表未来新增 ``severity_reviewed`` 列，取值为 1/true/yes/是 时视为已复核；
    没有该列时，抽取行一律视为未复核——不得把自动赋的 severity 当人工结论。

    Args:
        manual_dir: 手工层根目录。
        spec: 事件数据集配置，``manual_path`` 指向 CSV。

    Returns:
        未复核事件列表（最多 50 条，按日期/event_id 排序），每条含
        ``event_id/date/type/severity/region/source_url``。
    """
    if not spec.manual_path:
        return []
    frame = _read_table(manual_dir / spec.manual_path)
    if frame.empty or "event_id" not in frame.columns:
        return []
    reviewed_column = "severity_reviewed" if "severity_reviewed" in frame.columns else None
    queue: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        event_id = str(row.get("event_id") or "").strip()
        if not event_id.startswith("XGE-"):
            continue
        if reviewed_column and str(row.get(reviewed_column) or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "是",
        }:
            continue
        queue.append(
            {
                "event_id": event_id,
                "date": str(row.get("date") or ""),
                "type": str(row.get("type") or ""),
                "severity": str(row.get("severity") or ""),
                "region": str(row.get("region") or ""),
                "source_url": str(row.get("source_url") or ""),
            }
        )
    queue.sort(key=lambda item: (item["date"], item["event_id"]))
    return queue[:50]


def _systematic_zero(detail: dict[str, Any], notes: list[str]) -> bool:
    """判断零成交/零持仓是“源长期如此”还是“个别历史行”。

    只有系统性缺陷才降为价格-only；ETF/AU0 偶尔几行零成交量是历史脏点，
    已经按行标记并留档，不能把整个数据集的成交量字段封掉。判据：
    - 适配器/质量说明明确写“全为 0”；或
    - ``zero_rows``（按字段累计）达到主表行数的一半以上。

    Args:
        detail: 质量报告的 ``quality_detail``。
        notes: 数据集的说明列表（含适配器 notes 与质量 flags）。

    Returns:
        bool: True 表示系统性零值，应限制为价格-only。
    """
    if any("全为 0" in str(note) or "全为0" in str(note) for note in notes):
        return True
    rows = int(detail.get("rows") or 0)
    zero_rows = int(detail.get("zero_rows") or 0)
    return rows > 0 and zero_rows >= rows * 0.5


def _issue(
    dataset: str,
    code: str,
    status: str,
    action: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
) -> Issue:
    """构造一条 ``Issue``；issue_id 固定为 ``<dataset>:<code>``，便于去重与对表。"""
    return Issue(
        issue_id=f"{dataset}:{code}",
        dataset=dataset,
        code=code,
        status=status,
        action=action,
        detail=detail,
        evidence=evidence or {},
    )


def build_remediation_report(
    config: LoadedConfig,
    quality: dict[str, Any],
    *,
    normalized_root: Path | None = None,
    raw_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """扫描质量报告与留档，生成问题清单和每数据集能力清单。

    这是纯读取 + 确定性计算：不修改数据、不抓取、不写盘。调用方决定是否把
    结果原子写到 ``data/normalized/_remediation.json``。

    Args:
        config: 已加载配置，提供数据集 spec、路径与 ``config_hash``。
        quality: ``_quality.json`` 解析结果；至少含 ``datasets`` 与 ``config_hash``。
        normalized_root: 规范化层根目录；None 时取 ``config.path("normalized")``。
        raw_root: 原始层根目录；None 时取 ``config.path("raw")``。
        now: 当前带时区时间；None 时取 ``now_shanghai()``。

    Returns:
        可直接 JSON 序列化的报告，含 ``schema_version/generated_at/run_id/
        quality_run_id/config_hash/quality_config_hash/config_hash_match/
        global_block/overall/counts/issues/capabilities/review_queue/next_actions/
        report_hash``。

    Notes:
        只要 ``quality`` 缺少数据集节点，就返回 ``overall=ERROR`` 的报告而不是抛异常：
        诊断工具不能在质量报告本身坏掉时把整条日链打死；错误原因写进 ``errors``。
    """
    normalized_root = normalized_root or config.path("normalized")
    raw_root = raw_root or config.path("raw")
    now = now or now_shanghai()
    datasets = quality.get("datasets") or {}
    issues: list[Issue] = []
    capabilities: dict[str, Capability] = {}
    review_queue: list[dict[str, Any]] = []
    approx_calendar: list[str] = []
    missing_calendar: list[str] = []
    errors: list[str] = []

    quality_hash = quality.get("config_hash")
    config_hash_match = bool(quality_hash) and quality_hash == config.config_hash
    global_block = False
    if not quality_hash:
        issues.append(
            _issue(
                "*",
                "MISSING_CONFIG_HASH",
                "NEEDS_HUMAN",
                "RERUN_INGEST_AND_ACCEPT",
                "质量报告没有 config_hash，无法证明数据与判据同版本；必须先重跑 ingest。",
            )
        )
        global_block = True
    elif not config_hash_match:
        issues.append(
            _issue(
                "*",
                "STALE_QUALITY_REPORT",
                "NEEDS_HUMAN",
                "RERUN_INGEST_AND_ACCEPT",
                "质量报告 config_hash 与当前配置不一致；旧数据配新判据，全部能力暂停。",
                {
                    "report_config_hash": quality_hash,
                    "current_config_hash": config.config_hash,
                },
            )
        )
        global_block = True

    if not isinstance(datasets, dict) or not datasets:
        errors.append("质量报告缺少 datasets 节点")
        return _assemble_report(
            quality,
            config,
            now,
            issues,
            capabilities,
            review_queue,
            global_block,
            errors,
        )

    for name in sorted(datasets):
        spec = config.data_sources.datasets.get(name)
        if spec is None:
            issues.append(
                _issue(
                    name,
                    "UNKNOWN_DATASET",
                    "NEEDS_HUMAN",
                    "FIX_CONFIG_OR_REPORT",
                    "质量报告里的数据集不在当前配置中；无法判断口径与能力。",
                )
            )
            continue
        entry = datasets[name] or {}
        detail = entry.get("quality_detail") or {}
        flags = set(detail.get("flags") or []) | set(entry.get("notes") or [])
        quality_status = str(entry.get("quality") or "")
        node_status = str(entry.get("status") or "")
        capability = Capability(
            dataset=name,
            calendar_basis=str(detail.get("continuity_basis") or spec.calendar),
        )
        try:
            frame = _read_table(normalized_root / f"{name}.csv")
            rejected = _read_table(normalized_root / "rejected" / f"{name}.csv")
        except Exception as exc:  # noqa: BLE001
            # 规范化层本身读不出来时不能继续猜“也许没问题”：全部能力关掉，
            # 把异常类型与截断消息留作证据，由人工修文件后重跑。
            capability.display_eligible = False
            capability.feature_eligible = False
            capability.score_eligible = False
            capability.promotion_eligible = False
            capability.add_reason("READ_ERROR")
            issues.append(
                _issue(
                    name,
                    "READ_ERROR",
                    "NEEDS_HUMAN",
                    "FIX_NORMALIZED_FILE",
                    "规范化表或剔除留档无法读取；无法判断数据是否可用，全部能力暂停。",
                    {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                )
            )
            capabilities[name] = capability
            continue

        if node_status == "FAILED":
            capability.display_eligible = False
            capability.feature_eligible = False
            capability.score_eligible = False
            capability.promotion_eligible = False
            capability.add_reason("SOURCE_FAILED")
            issues.append(
                _issue(
                    name,
                    "SOURCE_FAILED",
                    "NEEDS_HUMAN",
                    "RESTORE_SOURCE",
                    "数据源本次抓取失败；没有数据可展示或使用，必须恢复源后重跑。",
                    {"error_code": entry.get("error_code")},
                )
            )
            capabilities[name] = capability
            continue

        if quality_status == "RED":
            evidence = _recent_bad_evidence(frame, rejected)
            capability.display_eligible = True
            capability.feature_eligible = False
            capability.score_eligible = False
            capability.promotion_eligible = False
            capability.add_reason("QUALITY_RED")
            if evidence["recent_rows"] > 0:
                capability.add_reason("RECENT_BAD_ROWS")
                issues.append(
                    _issue(
                        name,
                        "RECENT_BAD_ROWS",
                        "CONTAINED",
                        "QUARANTINE_ROWS_AND_BLOCK_SCORING",
                        "近期坏行已从主表剔除并留档；在离开最近窗口前禁止进入特征/分数。",
                        evidence,
                    )
                )
            else:
                capability.add_reason("RED_WITHOUT_ROW_EVIDENCE")
                issues.append(
                    _issue(
                        name,
                        "RED_WITHOUT_ROW_EVIDENCE",
                        "NEEDS_HUMAN",
                        "REVIEW_RECENT_BAD_ROWS",
                        "数据集判 RED，但找不到对应的近期剔除留档；无法证明只是坏行，必须人工复核。",
                        {"rejected_rows": int(detail.get("rejected_rows") or 0)},
                    )
                )

        zero_flags = {"ZERO_VOLUME", "ZERO_OPEN_INTEREST"} & flags
        if zero_flags:
            evidence = {
                "fields": sorted(ZERO_FLOW_FIELDS),
                "flags": sorted(zero_flags),
                "zero_rows": int(detail.get("zero_rows") or 0),
                "rows": int(detail.get("rows") or 0),
            }
            if _systematic_zero(detail, list(entry.get("notes") or [])):
                capability.price_only = True
                capability.blocked_fields.extend(ZERO_FLOW_FIELDS)
                capability.add_reason("ZERO_FLOW")
                capability.promotion_eligible = False
                issues.append(
                    _issue(
                        name,
                        "ZERO_FLOW",
                        "CONTAINED",
                        "PRICE_ONLY_BLOCK_VOLUME_FIELDS",
                        "源长期给出零成交量/零持仓；只允许使用收盘价，成交量与持仓量禁止进入特征。",
                        evidence,
                    )
                )
            else:
                # 个别历史零值已按行标记并留档，不封整个字段；自动动作到此为止。
                issues.append(
                    _issue(
                        name,
                        "HISTORICAL_ZERO_FLOW",
                        "AUTO_RESOLVED",
                        "KEEP_ROWS_AND_MARK_DEGRADED",
                        "只有少量历史零成交量/零持仓行，已逐行标记并留档；不封锁整个字段。",
                        evidence,
                    )
                )

        if "LOW_COVERAGE" in flags:
            capability.feature_eligible = False
            capability.score_eligible = False
            capability.promotion_eligible = False
            capability.add_reason("LOW_COVERAGE")
            if name == "spdr_gold":
                capability.blocked_features.extend(SPDR_BLOCKED_FEATURES)
            issues.append(
                _issue(
                    name,
                    "LOW_COVERAGE",
                    "CONTAINED",
                    "DISPLAY_ONLY_BLOCK_FEATURES",
                    "覆盖率低于下限，历史空洞会让窗口类特征失真；只展示、不进分数。",
                    {
                        "weekday_gaps": int(detail.get("weekday_gaps") or 0),
                        "weekday_gap_sample": list(detail.get("weekday_gap_sample") or [])[:10],
                    },
                )
            )

        if spec.revised_only or "REVISED_ONLY" in flags:
            capability.promotion_eligible = False
            capability.score_eligible = False
            capability.blocked_promotions.extend(GPR_BLOCKED_PROMOTIONS)
            capability.add_reason("REVISED_ONLY")
            vintage = _vintage_evidence(raw_root)
            if vintage["snapshots"] > 0:
                issues.append(
                    _issue(
                        name,
                        "REVISED_ONLY",
                        "CONTAINED",
                        "RESEARCH_ONLY_CAPTURE_DAILY_VINTAGE",
                        "只有修订后序列，禁止用于晋升；原始快照可作为未来 vintage，历史段永久只读。",
                        vintage,
                    )
                )
            else:
                issues.append(
                    _issue(
                        name,
                        "REVISED_ONLY",
                        "NEEDS_HUMAN",
                        "ESTABLISH_VINTAGE_ARCHIVE",
                        "只有修订后序列且没有原始快照，无法建立 vintage；必须建立逐日快照后再研究。",
                        vintage,
                    )
                )

        if "RECENT_MISSING_ROWS" in flags or "STALE" in flags:
            capability.score_eligible = False
            capability.promotion_eligible = False
            capability.add_reason("RECENT_MISSING_ROWS" if "RECENT_MISSING_ROWS" in flags else "STALE")
            issues.append(
                _issue(
                    name,
                    "RECENT_MISSING_ROWS" if "RECENT_MISSING_ROWS" in flags else "STALE",
                    "CONTAINED",
                    "BLOCK_SCORE_UNTIL_FRESH",
                    "最近存在缺失行或已过期；不得填前值，恢复新鲜前禁止进入分数。",
                    {
                        "recent_missing_rows": int(detail.get("recent_missing_rows") or 0),
                        "stale_days": detail.get("stale_days"),
                        "as_of_max": detail.get("as_of_max"),
                    },
                )
            )

        provisional = _provisional_evidence(frame, now)
        if provisional["rows"]:
            capability.score_eligible = False
            capability.promotion_eligible = False
            capability.add_reason("PROVISIONAL")
            action = (
                "RERUN_INGEST_TO_FINALIZE"
                if provisional["refresh_due"]
                else "WAIT_UNTIL_AVAILABLE_AT"
            )
            issues.append(
                _issue(
                    name,
                    "PROVISIONAL",
                    "CONTAINED",
                    action,
                    "存在抓取早于可见时点的临时行；已剔除出事实包，到可见时点后需重抓转正。",
                    provisional,
                )
            )

        if spec.kind in {"price", "series"} and capability.calendar_basis.startswith("weekday_approx"):
            capability.coverage_is_lower_bound = True
            if spec.calendar == "weekday":
                approx_calendar.append(name)
            else:
                missing_calendar.append(name)
                capability.score_eligible = False
                capability.add_reason("CALENDAR_MISSING")

        if spec.kind == "events":
            try:
                queue = _event_review_queue(config.path("manual"), spec)
            except Exception as exc:  # noqa: BLE001
                capability.display_eligible = False
                capability.feature_eligible = False
                capability.score_eligible = False
                capability.promotion_eligible = False
                capability.add_reason("READ_ERROR")
                issues.append(
                    _issue(
                        name,
                        "READ_ERROR",
                        "NEEDS_HUMAN",
                        "FIX_NORMALIZED_FILE",
                        "手工事件表无法读取；无法判断 severity 是否已复核，全部能力暂停。",
                        {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                    )
                )
                capabilities[name] = capability
                continue
            if queue:
                review_queue.extend(queue)
                capability.feature_eligible = False
                capability.score_eligible = False
                capability.promotion_eligible = False
                capability.blocked_features.extend(EVENT_BLOCKED_FEATURES)
                capability.add_reason("UNREVIEWED_EVENT_SEVERITY")
                issues.append(
                    _issue(
                        name,
                        "UNREVIEWED_EVENT_SEVERITY",
                        "NEEDS_HUMAN",
                        "REVIEW_EVENT_SEVERITY",
                        "抽取事件的 severity 未经人工复核；复核前禁止进入地缘分数或加仓依据。",
                        {"pending": len(queue), "sample": queue[:10]},
                    )
                )

        capabilities[name] = capability

    if approx_calendar:
        issues.append(
            _issue(
                "*",
                "CALENDAR_APPROX",
                "CONTAINED",
                "LABEL_APPROX_AND_TREAT_COVERAGE_AS_LOWER_BOUND",
                "美国交易日与外汇报价日没有官方直连日历；覆盖率按周一至周五近似，读数只是保守下界。",
                {"datasets": sorted(approx_calendar)},
            )
        )
    if missing_calendar:
        issues.append(
            _issue(
                "*",
                "CALENDAR_MISSING",
                "NEEDS_HUMAN",
                "RESTORE_OFFICIAL_CALENDAR",
                "声明了 cn_sse 官方日历但实际回落为工作日近似；境内缺口不可信，必须恢复日历。",
                {"datasets": sorted(missing_calendar)},
            )
        )

    return _assemble_report(
        quality,
        config,
        now,
        issues,
        capabilities,
        review_queue,
        global_block,
        errors,
    )


def _assemble_report(
    quality: dict[str, Any],
    config: LoadedConfig,
    now: datetime,
    issues: list[Issue],
    capabilities: dict[str, Capability],
    review_queue: list[dict[str, Any]],
    global_block: bool,
    errors: list[str],
) -> dict[str, Any]:
    """把扫描结果装配成稳定、可哈希的 JSON 报告。

    Args:
        quality: 原始质量报告，用于取 run_id/reference_date/config_hash。
        config: 当前配置，用于写当前 config_hash。
        now: 生成时间。
        issues: 已收集的问题。
        capabilities: 每数据集能力。
        review_queue: 未复核事件队列。
        global_block: 是否因配置/报告不同版本而全局封锁。
        errors: 扫描过程中的结构性错误。

    Returns:
        报告字典；``overall`` 取 ``NEEDS_HUMAN`` > ``AUTO_CONTAINED`` >
        ``CLEAN``；有任何结构性错误时为 ``ERROR``。
    """
    issues = sorted(issues, key=lambda item: (item.dataset, item.code))
    counts = {
        "AUTO_RESOLVED": sum(1 for item in issues if item.status == "AUTO_RESOLVED"),
        "CONTAINED": sum(1 for item in issues if item.status == "CONTAINED"),
        "NEEDS_HUMAN": sum(1 for item in issues if item.status == "NEEDS_HUMAN"),
    }
    if errors:
        overall = "ERROR"
    elif global_block or counts["NEEDS_HUMAN"]:
        overall = "NEEDS_HUMAN"
    elif issues:
        overall = "AUTO_CONTAINED"
    else:
        overall = "CLEAN"
    if global_block:
        # 配置/报告不同版本时，能力清单本身也不能信：全部关掉，避免下游只读
        # capabilities 而漏看 global_block，把旧数据当新口径使用。
        for capability in capabilities.values():
            capability.display_eligible = False
            capability.feature_eligible = False
            capability.score_eligible = False
            capability.promotion_eligible = False
            capability.add_reason("STALE_QUALITY_REPORT")
    capability_map = {name: capabilities[name].as_dict() for name in sorted(capabilities)}
    blocked = [
        name
        for name, item in capability_map.items()
        if not item["score_eligible"] or not item["feature_eligible"]
    ]
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso(now),
        "run_id": quality.get("run_id"),
        "quality_run_id": quality.get("run_id"),
        "reference_date": quality.get("reference_date"),
        "config_hash": config.config_hash,
        "quality_config_hash": quality.get("config_hash"),
        "config_hash_match": bool(quality.get("config_hash")) and quality.get("config_hash") == config.config_hash,
        "global_block": global_block,
        "overall": overall,
        "counts": counts,
        "issues": [item.as_dict() for item in issues],
        "capabilities": capability_map,
        "blocked_datasets": blocked,
        "review_queue": review_queue,
        "next_actions": sorted({item.action for item in issues}),
        "errors": errors,
    }
    report["report_hash"] = hash_obj(
        {key: value for key, value in report.items() if key != "generated_at"}
    )
    return report


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """提取写进 ``_quality.json`` 的轻量摘要，避免把整份问题清单塞进质量报告。

    Args:
        report: ``build_remediation_report()`` 的返回值。

    Returns:
        ``schema_version/overall/counts/needs_human/next_actions/global_block/
        report_hash`` 七字段；``needs_human`` 是 issue_id 列表。
    """
    return {
        "schema_version": report.get("schema_version"),
        "overall": report.get("overall"),
        "counts": report.get("counts"),
        "needs_human": [
            item["issue_id"] for item in report.get("issues", []) if item.get("status") == "NEEDS_HUMAN"
        ],
        "next_actions": report.get("next_actions", []),
        "global_block": report.get("global_block", False),
        "report_hash": report.get("report_hash"),
    }
