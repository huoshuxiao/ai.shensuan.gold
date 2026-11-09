"""结构化 JSONL 日志，按日一个文件，保留 N 天。

职责：为 CLI 与日链提供单一日志出口：按自然日切分 ``app-YYYY-MM-DD.jsonl``，
每行一条 JSON（``ts/level/logger/message`` + 可选业务字段），并清理过期文件。

关键不变量：
- 只写 ``log_dir`` 下的 ``app-<date>.jsonl``；不碰其他文件、不递归子目录。
- ``retention_days >= 1``；删除线是 ``today - retention_days``，恰好等于该
  日期的文件保留（见 ``purge_old_logs`` 的严格小于比较）。
- 日志不含密钥、账号、完整个人路径（AGENTS §15.5/§20.12）；异常只记堆栈文本。
- 重复调用 ``setup_logging`` 会先关闭并清空旧 handler，同一进程只保留一个
  文件句柄；只 ``clear()`` 不 ``close()`` 会持续泄漏文件描述符。

不负责：控制台输出、按大小轮转、日志聚合/上报、日志内容的脱敏检查
（脱敏靠调用方不把敏感字段放进 ``extra``）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .timeutil import now_shanghai

# 只认 app-YYYY-MM-DD.jsonl；手工放进目录的其他文件不会被清理。
LOG_PATTERN = re.compile(r"app-(\d{4}-\d{2}-\d{2})\.jsonl$")


class JsonlFormatter(logging.Formatter):
    """把 ``LogRecord`` 渲染为单行 JSON。

    固定字段 ``ts/level/logger/message``；业务字段
    ``run_id/stage/status/duration_ms/error/dataset`` 仅在调用方通过
    ``extra=`` 提供时出现；异常额外写 ``exc`` 完整堆栈。

    Notes:
        不输出文件名、行号、进程号：避免把本机绝对路径带进日志。
    """

    def format(self, record: logging.LogRecord) -> str:
        """把一条日志记录序列化为单行 JSON。

        Args:
            record: 标准库 ``logging.LogRecord``；可能带 ``extra`` 业务字段。

        Returns:
            str: 单行 JSON，键排序、中文不转义、无换行。

        Raises:
            无：``default=str`` 兜底未知类型，格式化本身不应抛错。

        Notes:
            ``ts`` 取 ``record.created``（epoch 秒）再换算到 Asia/Shanghai，
            与进程本地时区无关；同一记录在任何机器上都应得到同一时刻。
        """
        payload = {
            # 用记录创建时刻（epoch）换算，避免依赖格式化时的本地时区。
            "ts": datetime.fromtimestamp(record.created, tz=now_shanghai().tzinfo).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 只透出约定的业务字段，避免 extra= 把任意对象（含敏感值）带进日志。
        for key in ("run_id", "stage", "status", "duration_ms", "error", "dataset"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def setup_logging(log_dir: Path, retention_days: int = 7, level: str = "INFO") -> logging.Logger:
    """初始化 ``gold`` logger：按日文件、JSONL、保留 N 天。

    步骤：创建 ``log_dir`` → 清理过期日志 → 关闭并清空旧 handler → 挂一个写
    ``app-<今天>.jsonl`` 的 ``FileHandler`` → 关闭向 root 传播。

    Args:
        log_dir: 日志目录，通常为 ``<repo>/logs``。
        retention_days: 保留天数，必须 ``>=1``；默认 7（AGENTS §15.7）。
        level: 日志级别名，大小写不敏感；无法识别时静默回退 ``INFO``
            （与 ``logging.getLevelName`` 的容错口径一致）。

    Returns:
        logging.Logger: 名为 ``gold`` 的 logger；后续模块统一从它取子 logger。

    Raises:
        ValueError: ``retention_days < 1``（由 ``purge_old_logs`` 抛出）。
        OSError: 目录无法创建或日志文件无法打开。

    Notes:
        当日文件名用 ``date.today()``，即系统本地日期；生产机应把系统时区设为
        Asia/Shanghai，否则跨零点时文件名与 ``ts`` 字段的日期可能不一致。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    # 先清理再创建当天文件：当天文件生成于清理之后，天然不会被 retention 误删。
    purge_old_logs(log_dir, retention_days)
    logger = logging.getLogger("gold")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # clear() 只摘除 handler，不会关闭其文件描述符；必须先逐个 close()，
    # 否则重复初始化（日链重启/测试）会持续泄漏 fd。
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    handler = logging.FileHandler(log_dir / f"app-{date.today().isoformat()}.jsonl", encoding="utf-8")
    handler.setFormatter(JsonlFormatter())
    logger.addHandler(handler)
    # 不向 root logger 传播，防止被外部 basicConfig 再写一份、造成重复行。
    logger.propagate = False
    return logger


def purge_old_logs(log_dir: Path, retention_days: int, today: date | None = None) -> list[Path]:
    """删除超过保留期的 app-*.jsonl；返回被删除的路径。

    只处理文件名匹配 ``app-YYYY-MM-DD.jsonl`` 的普通文件；其他文件与子目录
    原样保留。删除条件是 ``文件日期 < today - retention_days``，因此恰好落在
    保留边界当天的文件会保留（保留窗口含今天共 ``retention_days + 1`` 天）。

    Args:
        log_dir: 日志目录；不存在时直接返回空列表（不创建目录）。
        retention_days: 保留天数，必须 ``>=1``。
        today: 可注入的“今天”，用于测试固定时钟；默认 ``date.today()``。

    Returns:
        list[Path]: 实际删除的文件路径，按文件名升序。

    Raises:
        ValueError: ``retention_days < 1``，防止误删当天日志。
        OSError: 目录不可读或删除失败（权限、占用等）。

    Notes:
        文件名日期用 ``date.fromisoformat`` 解析；匹配正则但日期非法的文件名
        （如 ``app-2026-13-40.jsonl``）会抛 ``ValueError``，不会被静默跳过。
    """
    if retention_days < 1:
        raise ValueError("retention_days 必须 >= 1")
    current = today or date.today()
    cutoff = current - timedelta(days=retention_days)
    removed: list[Path] = []
    if not log_dir.exists():
        return removed
    for path in sorted(log_dir.iterdir()):
        match = LOG_PATTERN.match(path.name)
        if not match:
            continue
        file_date = date.fromisoformat(match.group(1))
        # 严格小于：恰好等于 cutoff 的文件属于保留窗口最后一天。
        if file_date < cutoff:
            path.unlink()
            removed.append(path)
    return removed
