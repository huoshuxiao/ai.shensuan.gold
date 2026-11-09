"""原子写：临时文件 + fsync + os.replace，避免半截 JSON/CSV。

职责：为 JSON、CSV、``pipeline_status.json`` 等状态文件提供“要么旧内容、
要么完整新内容”的写入语义；进程被杀或断电时不留下半截文件。

关键不变量：
- 临时文件与目标同目录（同一文件系统），``os.replace`` 才是原子替换；
  跨目录/跨盘的替换会退化为复制，可能留下半文件。
- 写入后先 ``flush`` 再 ``fsync``，确认数据落盘才替换，避免“文件名已更新、
  内容仍在页缓存”的断电半截状态。
- 任一步失败（含 ``KeyboardInterrupt``）都删除临时文件后原样抛出：
  不吞异常，也不留下 ``.tmp`` 垃圾。
- 替换后沿用 mkstemp 创建的 0600 权限，不继承旧文件权限；决策/日志内容
  不会因为旧文件权限过宽而泄露。

不负责：文件内容格式（JSON 规范化见 ``hashing``）、目录权限修复、跨主机同步；
网络文件系统上的 replace 原子性不由本模块保证。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .hashing import to_jsonable


def _atomic_write(path: Path, data: bytes) -> None:
    """把完整字节串原子地写到 ``path``（内部实现，不对外导出）。

    Args:
        path: 目标文件；父目录不存在时递归创建。
        data: 待写入的完整字节内容，编码由调用方完成。

    Returns:
        None

    Raises:
        OSError: 创建临时文件、写入、fsync 或 replace 失败时抛出；
            失败路径会先尝试删除临时文件（已不存在则忽略）再重新抛出。

    单位/时区：与内容单位、时区无关，本函数只搬运字节。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件必须与目标同目录：os.replace 只在同一文件系统内保证原子。
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            # 先 fsync 到磁盘再 replace，断电时不会出现“名字换了、内容没落盘”。
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # 捕获 BaseException 是为了 Ctrl-C 时也清理临时文件；清理后继续向上抛。
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path | str, text: str) -> None:
    """以 UTF-8 原子写入文本。

    Args:
        path: 目标路径，``str`` 或 ``Path`` 均可。
        text: 文本内容；按 UTF-8 编码后交给 ``_atomic_write``。

    Returns:
        None

    Raises:
        OSError: 同 ``_atomic_write``（目录不可建、磁盘满、replace 失败等）。
    """
    _atomic_write(Path(path), text.encode("utf-8"))


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """原子写入原始字节，供非文本产物使用。

    Args:
        path: 目标路径。
        data: 已编码的字节内容（如 parquet、原始快照）。

    Returns:
        None

    Raises:
        OSError: 同 ``_atomic_write``。
    """
    _atomic_write(Path(path), data)


def atomic_write_json(path: Path | str, payload: Any) -> None:
    """把可 JSON 化对象按固定格式原子写入。

    输出格式固定：``ensure_ascii=False``（中文原样）、``indent=2``、
    ``sort_keys=True``（键序稳定，便于 diff）、结尾补一个换行；
    ``allow_nan=False`` 作为第二道闸，禁止写出 NaN/Infinity 这类非法 JSON。
    numpy/pandas/日期类型的归一化由 ``to_jsonable`` 负责。

    Args:
        path: 目标路径。
        payload: 任意可被 ``to_jsonable`` 处理的对象。

    Returns:
        None

    Raises:
        ValueError: 序列化结果仍含 NaN/Inf（``allow_nan=False`` 触发）。
        TypeError: 含 json 无法编码且 ``to_jsonable`` 未覆盖的类型。
        OSError: 同 ``_atomic_write``。

    Notes:
        先整体序列化成字符串再一次性写入，因此文件要么不存在、要么是完整 JSON，
        不会出现“写了一半的对象”。
    """
    text = json.dumps(
        to_jsonable(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    atomic_write_text(path, text + "\n")
