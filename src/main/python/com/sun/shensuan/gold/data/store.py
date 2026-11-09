"""原始快照（append-only）与规范化层（幂等合并）存储。

职责：`RawStore` 把每次抓取原样落盘（不可覆盖）；`NormalizedStore` 把规范化结果按主键
合并进单个 CSV，并单独留档被剔除行与质量报告。两层都只负责“存”和“并”，不做清洗、
不做质量判定（那在 normalize 层）。

关键不变量：
- 原始层 append-only：同一 `(source, run_date, dataset, run_id)` 重复保存抛
  `FileExistsError`，绝不覆盖；原始文件旁必须有一份 `.meta.json`（含 sha256/bytes），
  恢复演练靠它校验证据未被改动。
- 规范化层幂等：同一份输入重复合并、写出的字节完全一致。单元格先经 `format_cell()`
  字符串化——浮点保留 12 位有效数字，NaN/Inf 一律空串——避免二进制尾差与 pandas
  版本差异造成文件抖动。
- 合并身份 = `key_columns`（如 `["as_of"]`、`["as_of","content_hash"]`、`["event_id"]`）。
  同一主键下只有业务列变化才覆盖；`fetched_at`/`ingest_run_id` 差异不算变化，
  否则每次重跑都会改写全表、哈希天天变。唯一例外：旧行是盘中临时快照
  （`fetched_at < available_at`）而新行已过可见时点时，必须替换旧行。
- 规范化 CSV 一律全字符串、无 NaN；缺失用空串表示，读回时 `keep_default_na=False`，
  防止 `"NA"`、`"null"` 被 pandas 当缺失值二次解释。
- 所有写盘走 `common.atomic`（临时文件 + fsync + os.replace），读侧不会看到半截文件。
- 被剔除行写 `normalized/rejected/<dataset>.csv`，按 `(as_of, reject_reason)` 去重；
  主表不保留这些行，但审计可回查。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..common.atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from ..common.hashing import sha256_bytes
from ..common.timeutil import iso, now_shanghai, parse_datetime
from .models import FetchResult

# 合并时视为“运行元数据”的列：重跑会变、不代表数据修订。比较新旧行时排除它们，
# 否则同一批数据重抓一次就会被判为“全部变化”，文件哈希随抓取时间抖动。
META_EXCLUDE = {"fetched_at", "ingest_run_id"}


def _is_provisional_record(record: dict) -> bool:
    """判断一条规范化记录是否仍是“抓取早于 available_at”的临时快照。

    Args:
        record: 字符串化后的规范化行。

    Returns:
        True 表示 ``fetched_at < available_at``；缺列或时间无法解析时返回 False，
        避免把旧数据格式误判为需要升级。
    """
    fetched = record.get("fetched_at", "")
    available = record.get("available_at", "")
    if not fetched or not available:
        return False
    try:
        return parse_datetime(fetched) < parse_datetime(available)
    except ValueError:
        return False
# 规范化 CSV 的列序白名单：行情 → 持仓/宏观 → 新闻/事件 → 手工值 → PIT 尾部。
# 不在表内的列按字母序追加，保证同一数据集多次写出的列序稳定（列序也是字节的一部分）。
PREFERRED_ORDER = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "amount",
    "holdings_tonnes",
    "change_tonnes",
    "total_value_usd",
    "us10y",
    "usdcny",
    "long",
    "short",
    "net",
    "gprd",
    "gprd_act",
    "gprd_threat",
    "gprd_ma7",
    "gprd_ma30",
    "published_at",
    "title",
    "content_hash",
    "event_id",
    "type",
    "severity",
    "region",
    "source_url",
    "value",
    "source",
    "as_of",
    "available_at",
    "fetched_at",
    "ingest_run_id",
    "vintage",
    "quality",
    "flags",
]
# 数据集名/源名会直接拼进文件路径，只允许字母数字与 `_ . -`，其余统一替换为 `_`，
# 防止 `../` 或空白字符造成路径穿越与不可见文件名。
UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize(value: str) -> str:
    """把数据集名/源名/批次号转成安全文件名片段。

    Args:
        value: 原始名称（如 `sh518880`、`akshare.fund_etf_hist_sina`）。

    Returns:
        仅含 `[A-Za-z0-9_.-]` 的字符串；非法字符每段替换成一个下划线。
    """
    return UNSAFE.sub("_", value)


def format_cell(value: Any) -> str:
    """把单个单元格确定性字符串化，是规范化层“逐字节可复现”的地基。

    规则：
    - `None` 与 NaN/Inf → 空串（缺失写空串，不能写 `"nan"`，否则下游无法区分缺失）；
    - float / numpy 浮点 → 12 位有效数字（`0.1+0.2` 写作 `0.3`，消除二进制尾差）；
    - numpy 整数 → Python int 字符串（避免 `3.0` 这种伪浮点）；
    - `pd.Timestamp` → ISO-8601；
    - 其余标量：`pd.isna` 为真（如 `pd.NA`）返回空串，否则 `str()`；不可 `isna`
      的对象（列表等）直接 `str()`。

    Args:
        value: 任意标量（来自 DataFrame 单元格）。

    Returns:
        用于 CSV 的字符串；缺失一律空串，非字符串永远不会出现 `"None"`/`"nan"`。
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return format(value, ".12g")
    if isinstance(value, (np.floating,)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return ""
        return format(number, ".12g")
    if isinstance(value, (np.integer,)):
        return str(int(value))
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def canonicalize(frame: pd.DataFrame) -> pd.DataFrame:
    """逐列经 `format_cell()` 字符串化，得到“全字符串、无 NaN”的表。

    这是幂等合并与逐字节对比的前提：同一天两次抓取得到的 float/object 混排表，
    在不同 pandas 版本或不同读取路径下可能给出不同 repr；字符串化后比较与写盘都稳定。

    Args:
        frame: 任意列类型的表（通常来自 `NormalizeResult.frame`）。

    Returns:
        与输入同形状、所有单元格为字符串的新表；不修改入参。
    """
    return pd.DataFrame({column: frame[column].map(format_cell) for column in frame.columns})


def ordered_columns(columns: list[str]) -> list[str]:
    """按 `PREFERRED_ORDER` 排序列名，未登记的列按字母序追加。

    稳定列序让同一数据集在多次运行之间写出完全相同的 CSV 字节，便于哈希对比与
    “重跑不产生 diff”的验收；不要改成按插入顺序。

    Args:
        columns: 去重后的列名集合（调用方通常传 `sorted(set(...))`）。

    Returns:
        重排后的列名列表；白名单列在前，其余按字母序。
    """
    preferred = [column for column in PREFERRED_ORDER if column in columns]
    extras = sorted(column for column in columns if column not in PREFERRED_ORDER)
    return preferred + extras


class RawStore:
    """原始层存储：`<root>/<source>/<run_date>/<dataset>.<run_id><suffix>`，append-only。

    每次抓取同时写原始文件与 `<文件名>.meta.json`（dataset/source/fetched_at/status/
    notes/rows/raw_file/sha256/bytes）。命中同名文件即抛错，绝不覆盖——原始层是全链路
    可重放的证据，不是可随意刷新的缓存。

    Attributes:
        root: 原始层根目录（通常是 `config.path("raw")`）；构造时不创建。
    """

    def __init__(self, root: Path) -> None:
        """只记录根目录，不做 I/O；目录由原子写的 `mkdir(parents=True)` 兜底创建。

        Args:
            root: 原始层根目录。
        """
        self.root = root

    def save(self, dataset: str, run_id: str, result: FetchResult, run_date: str | None = None) -> Path:
        """保存一次抓取的原始快照与元数据，返回原始文件路径。

        有 `result.raw_bytes`（如 GPR 的 `.xls`）时原样落盘；否则把 `result.frame`
        转成 CSV（统一 `\\n` 换行、UTF-8）。dataset/source/run_id 都过 `_sanitize()`。

        Args:
            dataset: 配置中的 dataset key；只用于命名，不写进文件内容。
            run_id: 本次抓取批次号；与 dataset 共同决定文件名，保证同源多次抓取不互相覆盖。
            result: 适配器结果，提供 source/fetched_at/notes/raw_bytes/raw_suffix。
            run_date: 快照归属日期（`YYYY-MM-DD`）；缺省取当前上海日期，便于按日归档。

        Returns:
            原始文件路径（基准与传入的 root 相同）。

        Raises:
            FileExistsError: 同 (source, run_date, dataset, run_id) 的快照已存在。
        """
        day = run_date or now_shanghai().date().isoformat()
        directory = self.root / _sanitize(result.source) / day
        suffix = result.raw_suffix if result.raw_suffix.startswith(".") else f".{result.raw_suffix}"
        path = directory / f"{_sanitize(dataset)}.{_sanitize(run_id)}{suffix}"
        if path.exists():
            # 原始层是证据链：宁可任务失败让人工确认，也不能静默覆盖上一次抓取。
            raise FileExistsError(f"原始快照已存在，禁止覆盖：{path}")
        if result.raw_bytes is not None:
            payload = result.raw_bytes
        else:
            # 统一 \n 与 UTF-8：换行符随平台变化会改变字节与 sha256，破坏可复现对比。
            payload = result.frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        atomic_write_bytes(path, payload)
        meta = {
            "dataset": dataset,
            "source": result.source,
            "fetched_at": iso(result.fetched_at),
            "status": result.status.value,
            "notes": result.notes,
            "rows": int(len(result.frame)),
            "raw_file": path.name,
            "sha256": sha256_bytes(payload),
            "bytes": len(payload),
        }
        atomic_write_json(directory / f"{path.name}.meta.json", meta)
        return path


class NormalizedStore:
    """规范化层存储：`<root>/<dataset>.csv`，按主键幂等合并，另存剔除行与质量报告。

    目录约定：
    - `<root>/<dataset>.csv`：主表，全字符串、无 NaN、稳定列序。
    - `<root>/rejected/<dataset>.csv`：被剔除行留档，按 `(as_of, reject_reason)` 去重。
    - `<root>/_quality.json`：全量质量报告，由 `ingest._quality_report()` 组装。
    - `<root>/_remediation.json`：自动诊断与能力清单，由 `remediation.build_remediation_report()` 生成。

    Attributes:
        root: 规范化层根目录（通常是 `config.path("normalized")`）；构造时不创建。
    """

    def __init__(self, root: Path) -> None:
        """只记录根目录，不做 I/O。

        Args:
            root: 规范化层根目录。
        """
        self.root = root

    def path_for(self, dataset: str) -> Path:
        """主表路径：`<root>/<dataset>.csv`；dataset 会过 `_sanitize()`。"""
        return self.root / f"{_sanitize(dataset)}.csv"

    def load(self, dataset: str) -> pd.DataFrame:
        """读主表；文件不存在返回空表而不是抛异常。

        统一 `dtype=str, keep_default_na=False`：主表本身就是全字符串，读回时不能
        让 pandas 把 `"NA"`、`"null"`、空串再解释成 NaN，否则合并时会误判为“值变了”。

        Args:
            dataset: dataset key。

        Returns:
            主表；不存在时为空 DataFrame（无列），调用方用 `.empty` 判断。
        """
        path = self.path_for(dataset)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def merge(
        self,
        dataset: str,
        frame: pd.DataFrame,
        key_columns: list[str],
        removed: pd.DataFrame | None = None,
        authoritative: bool = False,
    ) -> tuple[pd.DataFrame, int]:
        """把本次规范化结果并入已有 CSV，返回合并表与新增行数。

        身份由 `key_columns` 决定（`canonicalize()` 后按字符串精确比较）：
        - 新主键 → 计入 `new_rows` 并插入；
        - 已有主键且任一非元数据列（排除 `fetched_at`/`ingest_run_id`）变化 → 覆盖该行；
        - 只有元数据差异 → 保留旧行，避免每次重跑都改写文件、刷新时间戳。

        列集合取新旧并集并按 `ordered_columns()` 排序；缺失列补空串，旧文件升级列契约
        时不需要迁移脚本。结果按主键做稳定排序（mergesort）。

        删除有两条显式通道（默认都不开，只并集不删——历史行不能因为「这次没抓到」而消失）：
        - `removed`：本次被规范化剔除的行（`NormalizeResult.rejected`）。被剔除的主键
          必须从主表消失，否则留档与主表会同时存在同一时点，C6/C7 永远判红。
        - `authoritative`：输入本身就是这张表的全部真值（如手工/抽取生成的事件日历，
          每次整份重写）。此时不做历史并集，旧行不在新输入里即删除——否则删掉的误报
          会从历史并集里复活。历史完整性由源文件负责，不由规范化层兜底。

        Args:
            dataset: dataset key，决定目标文件。
            frame: 规范化后的新表（`NormalizeResult.frame`），无需先字符串化。
            key_columns: 主键列名，通常来自 `DatasetConfig.key_columns`。
            removed: 本次剔除行留档；需含 `key_columns`，缺列时不做删除（宁可不删，
                也不按空主键误删整表）。
            authoritative: 是否按完整快照替换整表（不再与旧行做并集）。

        Returns:
            `(合并后的全字符串 DataFrame, 新增行数)`；新增行数不含被覆盖的行。
        """
        existing = self.load(dataset)
        new = canonicalize(frame)
        columns = ordered_columns(sorted(set(existing.columns) | set(new.columns)))
        existing = existing.reindex(columns=columns, fill_value="")
        new = new.reindex(columns=columns, fill_value="")
        if removed is not None and not removed.empty and set(key_columns) <= set(removed.columns):
            # 剔除即从主表移除：留档仍保留原值与原因，但主表不得再有这个主键，
            # 否则「已剔除」的行会通过 merge 的历史并集原样复活。
            dropped = canonicalize(removed)
            dropped_keys = {
                tuple(str(record.get(column, "")) for column in key_columns)
                for record in dropped.to_dict("records")
            }
            keep_mask = pd.Series(
                [
                    tuple(str(record.get(column, "")) for column in key_columns) not in dropped_keys
                    for record in existing.to_dict("records")
                ],
                index=existing.index,
                dtype=bool,
            )
            existing = existing.loc[keep_mask].reset_index(drop=True)

        old_keys = {
            tuple(str(record.get(column, "")) for column in key_columns)
            for record in existing.to_dict("records")
        }
        records: dict[tuple[str, ...], dict[str, str]] = {}
        if not authoritative:
            for record in existing.to_dict("records"):
                records[tuple(record.get(key, "") for key in key_columns)] = record

        new_rows = 0
        for record in new.to_dict("records"):
            # 主键列必须真实存在于表中：缺列时 `record.get` 一律返回空串，所有行会塌缩
            # 成同一个主键、只剩最后一行（静默丢数据）。因此不做默认补齐，配置侧保证
            # `key_columns ⊆ required_columns`。
            key = tuple(record.get(column, "") for column in key_columns)
            if key not in records:
                # 完整快照模式下 records 从空开始，"新增"仍按旧表是否出现过该主键算，
                # 否则每次整表重写都会把全部行报成新增。
                if key not in old_keys:
                    new_rows += 1
                records[key] = record
                continue
            previous = records[key]
            # 只比较业务列：重跑带来的 fetched_at/ingest_run_id 变化不算数据修订。
            identity_columns = [column for column in columns if column not in META_EXCLUDE]
            changed = any(
                previous.get(column, "") != record.get(column, "") for column in identity_columns
            )
            # 盘中快照升级为收盘后正式快照：即使业务值完全没变，也必须替换旧行，
            # 否则旧 fetched_at 会永远停留在 available_at 之前，事实包永远不可用。
            if not changed and _is_provisional_record(previous) and not _is_provisional_record(record):
                changed = True
            if changed:
                records[key] = record

        merged = pd.DataFrame(list(records.values()), columns=columns).fillna("")
        # 主键排序必须稳定：同日重复行已在规范化层去重，这里只负责让字节顺序不随机。
        sort_columns = [column for column in key_columns if column in merged.columns]
        if sort_columns:
            merged = merged.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)
        return merged, new_rows

    def write(self, dataset: str, frame: pd.DataFrame) -> Path:
        """原子写回规范化 CSV，返回路径；调用方应传入 `merge()` 的结果。

        列序与单元格格式已由 `merge()`/`canonicalize()` 固定，这里只负责落盘；
        不再做二次排序或格式化，否则会破坏幂等（同一输入两次写出字节不同）。

        Args:
            dataset: dataset key，决定目标文件。
            frame: `merge()` 返回的合并表。

        Returns:
            写入路径。
        """
        path = self.path_for(dataset)
        text = frame.to_csv(index=False, lineterminator="\n")
        atomic_write_text(path, text)
        return path

    def rejected_path_for(self, dataset: str) -> Path:
        """被剔除行的留档路径：`<root>/rejected/<dataset>.csv`。"""
        return self.root / "rejected" / f"{_sanitize(dataset)}.csv"

    def read_rejected(self, dataset: str) -> pd.DataFrame:
        """读剔除留档（全字符串）；文件不存在返回空表，不抛异常。

        返回空表而非 None，是为了让 `assess_dataset(rejected=...)` 的调用方不必额外判空。

        Args:
            dataset: dataset key。

        Returns:
            留档表；不存在时为空 DataFrame，列未知。
        """
        path = self.rejected_path_for(dataset)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def write_rejected(self, dataset: str, frame: pd.DataFrame) -> Path | None:
        """被剔除行单独留档，供审计；主表不保留这些行。

        幂等：按 `(as_of, reject_reason)` 去重后整表重写，同一坏点重复入库不会增长文件。
        `reject_reason` 参与主键，是为了同一天因不同原因被剔除时两类记录都能留下
        （例如同日既有 OHLC 结构错又有量级越界）。

        Args:
            dataset: dataset key。
            frame: `NormalizeResult.rejected`；为 None 或空表时不建文件。

        Returns:
            写入路径；`frame` 为 None/空表时返回 None。
        """
        if frame is None or frame.empty:
            return None
        path = self.rejected_path_for(dataset)
        new = canonicalize(frame)
        if path.exists():
            existing = pd.read_csv(path, dtype=str, keep_default_na=False)
            columns = ordered_columns(sorted(set(existing.columns) | set(new.columns)))
            existing = existing.reindex(columns=columns, fill_value="")
            new = new.reindex(columns=columns, fill_value="")
            key_columns = [column for column in ("as_of", "reject_reason") if column in columns]
            records: dict[tuple[str, ...], dict[str, str]] = {}
            for record in existing.to_dict("records"):
                records[tuple(record.get(key, "") for key in key_columns)] = record
            for record in new.to_dict("records"):
                records[tuple(record.get(key, "") for key in key_columns)] = record
            merged = pd.DataFrame(list(records.values()), columns=columns).fillna("")
        else:
            columns = ordered_columns(list(new.columns))
            merged = new.reindex(columns=columns, fill_value="")
        sort_columns = [column for column in ("as_of", "reject_reason") if column in merged.columns]
        if sort_columns:
            merged = merged.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)
        atomic_write_text(path, merged.to_csv(index=False, lineterminator="\n"))
        return path

    def write_quality_report(self, payload: dict[str, Any]) -> Path:
        """原子写 `_quality.json`（全量质量报告），返回路径。

        报告由 `ingest._quality_report()` 组装，含 `schema_version/run_id/generated_at/
        reference_date/config_hash/global_status/missing/degraded/red/core_not_green/
        revised_only/datasets/pipeline`；本方法只落盘，不校验字段完整性。

        Args:
            payload: JSON 可序列化字典。

        Returns:
            `_quality.json` 路径。
        """
        path = self.root / "_quality.json"
        atomic_write_json(path, payload)
        return path

    def read_quality_report(self) -> dict[str, Any]:
        """读 `_quality.json`；文件不存在返回 `{}`，由调用方决定如何提示缺失。

        Returns:
            报告字典；不存在时为空字典（不抛异常，避免首跑即失败）。
        """
        path = self.root / "_quality.json"
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def write_remediation_report(self, payload: dict[str, Any]) -> Path:
        """原子写 `_remediation.json`（问题清单 + 消费能力清单）。

        Args:
            payload: `remediation.build_remediation_report()` 产出的 JSON 友好字典。

        Returns:
            `_remediation.json` 路径。

        Notes:
            本文件是 P2 特征层的消费前提：没有它，下游只能从 `_quality.json` 猜
            某个源能不能用。写到规范化层根目录，与质量报告同批原子落盘。
        """
        path = self.root / "_remediation.json"
        atomic_write_json(path, payload)
        return path

    def read_remediation_report(self) -> dict[str, Any]:
        """读 `_remediation.json`；文件不存在返回 `{}`（兼容尚未跑过自动诊断的旧数据）。

        Returns:
            报告字典；不存在时为空字典，不抛异常。
        """
        path = self.root / "_remediation.json"
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
