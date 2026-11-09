"""运行链路节点状态：每个节点进入/退出都原子写 pipeline_status.json。

职责：`PipelineStatusWriter` 维护一次日链的节点状态机，任何状态变化立即落盘；
`load_status()` 供链路页/CLI 只读展示，并把进程崩溃后残留的 `RUNNING` 改判 `TIMEOUT`。

关键不变量：
- 文件必须是合法 JSON：所有写盘走 `common.atomic`（临时文件 + fsync + os.replace），
  读侧永远看不到半截文件。
- 终态必写：`finish()` 是把节点送入终态的唯一入口；`mark_skipped()` 只动仍为
  `PENDING` 的节点，绝不覆盖已 `RUNNING`/已终态的真实结果。
- 节点集合在构造时固定；`start/finish/mark_skipped` 遇到未登记节点抛 `KeyError`，
  防止“节点名拼错”被静默吞掉、链路页缺一块却没人发现。
- 字段名（`node_id/status/started_at/ended_at/duration_ms/error_code/retry_count/
  artifact_hash/message`）是链路页与状态文件的外部契约，禁止改名。
- `overall` 是派生态而非写入态：失败/超时 > 降级/跳过 > 全 OK > 仍在运行，
  永远取最坏值，不能因为多数节点成功就报 OK。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..common.atomic import atomic_write_json
from ..common.timeutil import iso, now_shanghai, parse_datetime
from .models import NodeStatus


class PipelineStatusWriter:
    """一次日链的链路状态写入器；构造即落盘一份全 `PENDING` 的快照。

    每个节点记录 `node_id/status/started_at/ended_at/duration_ms/error_code/
    retry_count/artifact_hash/message`。时间一律用 Asia/Shanghai 的 ISO-8601，
    耗时单位毫秒。

    Attributes:
        path: 状态文件路径（`data/decisions/<date>/pipeline_status.json`）。
        run_id: 本次运行批次号，用于与日志/产物哈希对齐。
        date: 状态归属日期（`YYYY-MM-DD`）。
        started_at: 写入器创建时间，作为整次运行的起点。
        nodes: 有序节点字典（按构造时传入顺序），`snapshot()` 直接输出其 values。
    """

    def __init__(self, path: Path, run_id: str, date: str, node_ids: Iterable[str]) -> None:
        """登记节点并立即落盘初始快照。

        先落盘再执行：即使第一个节点就崩溃，链路页也能看到“有哪些节点、现在谁在跑”。

        Args:
            path: 状态文件路径；父目录由原子写自动创建。
            run_id: 运行批次号，写进快照供链路页对齐日志。
            date: 状态归属日期字符串。
            node_ids: 节点 id 列表（如 `ingest.etf_518880`、`normalize`、`quality_gate`），
                顺序即链路展示顺序；重复 id 后者覆盖前者（仍保持首次出现的位置）。
        """
        self.path = path
        self.run_id = run_id
        self.date = date
        self.started_at = now_shanghai()
        self.nodes: dict[str, dict[str, Any]] = {}
        for node_id in node_ids:
            self.nodes[node_id] = {
                "node_id": node_id,
                "status": NodeStatus.PENDING.value,
                "started_at": None,
                "ended_at": None,
                "duration_ms": None,
                "error_code": None,
                "retry_count": 0,
                "artifact_hash": None,
                "message": None,
            }
        self.flush()

    def _now(self) -> datetime:
        """统一取当前上海时间：时区口径只在这里定义，避免各调用点各写一套。"""
        return now_shanghai()

    def start(self, node_id: str) -> None:
        """把节点置 `RUNNING` 并记录开始时间，立即落盘。

        重复调用会刷新 `started_at`（不累计多次尝试的耗时）；重试次数由 `finish()`
        回填，本方法不清零，便于调用方按自己的重试模型维护。

        Args:
            node_id: 已登记节点 id。

        Raises:
            KeyError: 节点未在构造时登记。
        """
        node = self._node(node_id)
        node["status"] = NodeStatus.RUNNING.value
        node["started_at"] = iso(self._now())
        self.flush()

    def finish(
        self,
        node_id: str,
        status: NodeStatus,
        error_code: str | None = None,
        artifact_hash: str | None = None,
        retry_count: int = 0,
        message: str | None = None,
    ) -> None:
        """把节点送入终态并落盘：状态、结束时间、耗时、错误码、产物哈希、重试次数。

        未 `start()` 就直接 `finish()` 时把 `started_at` 补成当前时间、耗时记 0——
        保证状态文件里不存在“有终态无开始时间”的节点，链路页不必处理空值分支。
        `status` 由调用方按质量档位选择（`OK`/`DEGRADED`/`FAILED`/`TIMEOUT`），
        本方法不做合法性校验：七态机之外的值会被原样写入（`status.value`），
        因此调用方只能传 `NodeStatus`。

        Args:
            node_id: 已登记节点 id。
            status: 终态或 `DEGRADED`。
            error_code: 机器可读错误码（`SOURCE_TIMEOUT`、`QUALITY_YELLOW`、
                `STALE_RUNNING`、`ALL_SOURCES_FAILED` 等）；成功时为 None。
            artifact_hash: 产物哈希（如规范化 CSV 的 sha256）；无产物为 None。
            retry_count: 实际发生的重试次数（0 表示一次成功；降级源不计重试）。
            message: 中文摘要；本方法不再裁剪，调用方应自行截断（ingest 用 300 字）。

        Raises:
            KeyError: 节点未在构造时登记。
        """
        node = self._node(node_id)
        now = self._now()
        # 未 start 直接 finish：以当前时间兜底，duration 记 0，而不是让 started_at 为空。
        started = parse_datetime(node["started_at"]) if node.get("started_at") else now
        node["status"] = status.value
        node["started_at"] = node.get("started_at") or iso(now)
        node["ended_at"] = iso(now)
        node["duration_ms"] = int((now - started).total_seconds() * 1000)
        node["error_code"] = error_code
        node["artifact_hash"] = artifact_hash
        node["retry_count"] = retry_count
        node["message"] = message
        self.flush()

    def mark_skipped(self, node_ids: Iterable[str], reason: str) -> None:
        """把因上游失败/预算不足未执行的节点标 `SKIPPED`，一次性落盘。

        只处理仍为 `PENDING` 的节点：已 `RUNNING`/已有终态的节点不动，避免把真实结果
        覆盖成“跳过”（`ingest` 里 `quality_gate` 可能被重复标记，靠这条保证幂等）。
        `error_code` 固定写 `UPSTREAM_FAILED`，便于链路页按断点聚合。

        Args:
            node_ids: 待跳过的节点 id 集合。
            reason: 跳过原因（中文），写入 `message`。

        Raises:
            KeyError: 任一节点未登记。
        """
        for node_id in node_ids:
            node = self._node(node_id)
            if node["status"] == NodeStatus.PENDING.value:
                node["status"] = NodeStatus.SKIPPED.value
                node["error_code"] = "UPSTREAM_FAILED"
                node["message"] = reason
        self.flush()

    def _node(self, node_id: str) -> dict[str, Any]:
        """取节点记录；未登记即抛错，防止拼错节点名时状态静默丢失。

        Args:
            node_id: 节点 id。

        Returns:
            可原地修改的节点字典（`self.nodes` 中的同一对象）。

        Raises:
            KeyError: 节点未在构造时登记。
        """
        if node_id not in self.nodes:
            raise KeyError(f"未登记节点：{node_id}")
        return self.nodes[node_id]

    def snapshot(self) -> dict[str, Any]:
        """生成当前快照（不写盘），`flush()` 与测试共用。

        `overall` 为派生态，优先级从高到低：任一 `FAILED/TIMEOUT` → `FAILED`；
        任一 `DEGRADED/SKIPPED` → `DEGRADED`；全部 `OK` → `OK`；否则 `RUNNING`。
        空节点列表会落到 `RUNNING`（`values and all(...)` 为假），表示“还没有任何
        节点完成”，不会被误报成 `OK`。

        Returns:
            `{run_id, date, started_at, updated_at, overall, nodes}`；
            `nodes` 顺序与构造时一致，直接对应链路页的展示顺序。
        """
        values = list(self.nodes.values())
        # 整体状态取最坏值：一个节点失败就足以说明这条日链不完整，不能按多数票报 OK。
        if any(node["status"] in {NodeStatus.FAILED.value, NodeStatus.TIMEOUT.value} for node in values):
            overall = NodeStatus.FAILED.value
        elif any(node["status"] == NodeStatus.DEGRADED.value for node in values) or any(
            node["status"] == NodeStatus.SKIPPED.value for node in values
        ):
            # 降级与跳过都不是失败，但都不能对外宣称“完整成功”。
            overall = NodeStatus.DEGRADED.value
        elif values and all(node["status"] == NodeStatus.OK.value for node in values):
            overall = NodeStatus.OK.value
        else:
            overall = NodeStatus.RUNNING.value
        return {
            "run_id": self.run_id,
            "date": self.date,
            "started_at": iso(self.started_at),
            "updated_at": iso(now_shanghai()),
            "overall": overall,
            "nodes": values,
        }

    def flush(self) -> None:
        """原子落盘当前快照；写失败向上抛，不吞异常。

        每个状态变化都调用一次：状态文件是链路页的唯一数据源，宁可多写几次，
        也不要让页面看到过期状态。
        """
        atomic_write_json(self.path, self.snapshot())


def load_status(path: Path, stale_after_ms: int | None = None) -> dict[str, Any]:
    """读取链路状态文件；可选把超期的 `RUNNING` 改判 `TIMEOUT`。

    进程崩溃会留下永远 `RUNNING` 的节点，下一次启动/链路页读取时据此清理：
    超过 `stale_after_ms` 的 `RUNNING` 改为 `TIMEOUT`，补 `ended_at/duration_ms`，
    `error_code=STALE_RUNNING`；任一改判即把 `overall` 置 `FAILED` 并原子回写。
    改判是“事实修正”而非“重试”：原节点已经没有进程在跑，必须显式失败而不是永远转圈。

    Args:
        path: 状态文件路径。
        stale_after_ms: 判定阈值（毫秒）；为 None 时只读、不改判。

    Returns:
        状态字典（含 `run_id/date/started_at/updated_at/overall/nodes`）。

    Raises:
        FileNotFoundError: 状态文件不存在（不返回空快照，避免链路页误显示“全部正常”）。
    """
    # 局部导入：读侧需要真正的 json 解析（写侧由 atomic_write_json 统一做 sort_keys）。
    import json

    if not path.exists():
        raise FileNotFoundError(f"状态文件不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if stale_after_ms is not None:
        changed = False
        now = now_shanghai()
        for node in payload.get("nodes", []):
            # 只处理“RUNNING 且有开始时间”的节点：PENDING 是还没跑，终态是已有结论，
            # 都不能被超时逻辑改写。
            if node.get("status") != NodeStatus.RUNNING.value or not node.get("started_at"):
                continue
            started = parse_datetime(node["started_at"])
            if (now - started).total_seconds() * 1000 > stale_after_ms:
                node["status"] = NodeStatus.TIMEOUT.value
                node["ended_at"] = iso(now)
                node["duration_ms"] = int((now - started).total_seconds() * 1000)
                node["error_code"] = "STALE_RUNNING"
                changed = True
        # 有任一节点被改判，说明这次运行已不可能完整结束（进程中途死掉），
        # `overall` 直接按最坏值判 FAILED，并原子回写，让后续读者看到修正后的状态，
        # 而不是每次都靠本函数重复兜底。
        if changed:
            payload["overall"] = NodeStatus.FAILED.value
            payload["updated_at"] = iso(now)
            atomic_write_json(path, payload)
    return payload
