"""规范化 JSON 与 SHA-256。哈希必须可跨运行复现。

职责：把事实包、配置、账本等对象序列化为稳定 JSON 并计算摘要；
``config_hash`` / ``fact_pack_hash`` / ``decision_hash`` 都依赖这里。

关键不变量：
- ``canonical_json`` 跨运行、跨机器稳定：键递归排序、无多余空白、UTF-8；
  不依赖 dict 插入顺序，因此同一语义对象永远得到同一字符串。
- 非有限浮点（NaN/±Inf）统一归一为 ``null``：宁可显式缺值，也不写出非法
  JSON 或让 NaN 参与比较（AGENTS §0.1：缺失不得填数）。
- 所有摘要都带 ``sha256:`` 前缀，与账本 ``prev_hash/hash`` 的口径一致。
- 本模块只做类型转换与摘要，不读文件、不写文件、不修改入参。

不负责：日期时区换算（datetime 的 ``isoformat`` 保留其自身偏移）、
哈希链校验（见纸面账本模块）、签名/加密。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def to_jsonable(value: Any) -> Any:
    """把混合类型递归转换为可 JSON 序列化的基础类型。

    转换规则（自上而下匹配，命中即返回）：
    - ``dict``：键统一转 ``str``，值递归；
    - ``list``/``tuple``：逐项递归，``tuple`` 变为 ``list``；
    - ``datetime``/``date``/``pd.Timestamp``：``isoformat()`` 字符串
      （保留值自身的时区偏移，本函数不做时区转换）；
    - ``np.floating``：先转 ``float``，再走浮点分支；
    - ``np.integer``：转 ``int``；
    - ``float``：NaN/±Inf 返回 ``None``，其余原样返回；
    - ``Path``：转 ``str``；
    - 其他对象：原样返回，由 ``json.dumps`` 决定能否编码。

    Args:
        value: 任意嵌套结构（dict/list/tuple/标量/numpy/pandas 时间类型）。

    Returns:
        仅由 dict/list/str/int/float/bool/None 组成的新结构；不返回 numpy 标量。

    Raises:
        无：本函数不主动抛错；不可编码对象原样返回，最终由调用方处理。

    Notes:
        纯函数，不修改入参；容器与键都是新建的，调用方可安全缓存结果。
    """
    if isinstance(value, dict):
        # JSON 只允许字符串键；统一 str 可避免 int 键被 json 静默改写。
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.floating):
        # 先落成 float 再判 NaN：np.float64("nan") 也必须走到 None 分支。
        value = float(value)
    elif isinstance(value, np.integer):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    """生成规范化 JSON 文本（键排序、无空白分隔符、中文不转义）。

    该文本就是哈希输入，格式必须稳定：``sort_keys=True`` 保证键序无关，
    ``separators=(",", ":")`` 去掉多余空格，``ensure_ascii=False`` 让中文
    按 UTF-8 原样输出（配合 ``sha256_text`` 的 UTF-8 编码）。

    Args:
        value: 任意可被 ``to_jsonable`` 处理的对象。

    Returns:
        str: 单行 JSON，如 ``{"a":2,"b":1}``；空 dict 为 ``{}``。

    Raises:
        TypeError: 含 json 无法编码且未被 ``to_jsonable`` 覆盖的类型。
        ValueError: 含 NaN/Inf（``allow_nan=False``）；正常路径已被
            ``to_jsonable`` 归一为 ``None``，此处是防御性第二道闸。

    Notes:
        浮点按 Python ``repr`` 最短表示输出；跨 Python 大版本升级前应重跑
        哈希回归，确认 ``config_hash`` 等未漂移。
    """
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    """对文本按 UTF-8 编码后取 SHA-256。

    Args:
        text: 任意文本；按 UTF-8 编码（与 ``canonical_json`` 的输出口径一致）。

    Returns:
        str: ``sha256:<64 位小写十六进制>``。

    Raises:
        无。
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """对字节串直接取 SHA-256。

    Args:
        data: 原始字节（如快照文件内容）。

    Returns:
        str: ``sha256:<64 位小写十六进制>``。

    Raises:
        无。
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    """对文件内容取 SHA-256（分块流式读取）。

    每次读 1 MiB，避免把大文件（原始快照、GPR xls）整块载入内存，
    符合 16GB 单机约束（AGENTS §15.10）。

    Args:
        path: 文件路径。

    Returns:
        str: ``sha256:<64 位小写十六进制>``。

    Raises:
        OSError: 文件不存在、无读权限或读取中断。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def hash_obj(value: Any) -> str:
    """对任意对象取规范化哈希：``sha256_text(canonical_json(value))``。

    Args:
        value: 任意可被 ``to_jsonable`` 处理的对象。

    Returns:
        str: ``sha256:<64 位小写十六进制>``。

    Raises:
        同 ``canonical_json``（不可编码类型、NaN 防御闸）。

    Notes:
        哈希与 dict 键顺序无关（键被排序），因此同语义配置换行序/键序
        不会改变 ``config_hash``；但任何取值变化都会改变哈希。
    """
    return sha256_text(canonical_json(value))
