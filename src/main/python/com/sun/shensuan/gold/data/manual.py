"""手工 CSV 校验与模板。

职责：`data/manual/*.csv` 是 TIPS 实际利率、官方 AU 指数、GLD 持仓、地缘事件日历等
无稳定直连数据的入口。本模块只回答“这张手工表能不能进主链”，并负责补建缺失模板；
不做抓取、不做规范化、不写 PIT 列、不做质量分档（这些由 adapters/normalize 承担）。

关键不变量：
- 只接受 `MANUAL_SPECS` 登记的四个文件名；未登记文件一律拒绝，避免来历不明的 CSV
  混进事实包。
- 每行必须有 `source_url`；`available_at` 不得早于 `date`，否则等价于未来函数。
- 校验失败抛 `ManualDataError`（不做“修一修继续跑”）；空文件允许，但必须显式返回
  `EMPTY`——地缘事件为空只表示“未登记事件”，不表示“无地缘风险”。
- `ensure_templates()` 是本模块唯一写动作，且只创建缺失文件，绝不覆盖人工维护内容。
- 全部按字符串读取（`dtype=str, keep_default_na=False`），空单元格保持空串，
  不让 pandas 先转成 NaN 再由下游静默填 0。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..common.atomic import atomic_write_text
from ..common.errors import ManualDataError
from ..common.timeutil import parse_date, parse_datetime

MANUAL_SPECS: dict[str, dict[str, Any]] = {
    # 手工层文件契约：`required` 是“缺一即拒绝”的列集合，`kind` 决定后续按事件表还是
    # 序列表校验。新增手工数据源必须同时补 MANUAL_SPECS 与 HEADERS，缺一不可。
    "geo_events.csv": {
        "required": ["event_id", "date", "type", "severity", "region", "source_url", "available_at"],
        "kind": "events",
    },
    "tips10y.csv": {
        "required": ["date", "value", "available_at", "source_url"],
        "kind": "series",
    },
    "au_index.csv": {
        "required": ["date", "value", "available_at", "source_url"],
        "kind": "series",
    },
    "gld_holdings.csv": {
        "required": ["date", "value", "available_at", "source_url"],
        "kind": "series",
    },
}

HEADERS: dict[str, str] = {
    # 模板表头必须与 MANUAL_SPECS.required 完全一致且同序：用户照模板填就能过校验，
    # 避免“模板能创建、填完却缺列”的自相矛盾。
    "geo_events.csv": "event_id,date,type,severity,region,source_url,available_at\n",
    "tips10y.csv": "date,value,available_at,source_url\n",
    "au_index.csv": "date,value,available_at,source_url\n",
    "gld_holdings.csv": "date,value,available_at,source_url\n",
}


def validate_manual_file(path: Path, kind: str | None = None) -> dict[str, Any]:
    """校验单个手工 CSV，返回可直接展示的报告字典。

    列契约（全部按字符串读取，不复用配置文件；文件类型由文件名决定）：
    - `geo_events.csv`：`event_id,date,type,severity,region,source_url,available_at`。
      `date` 为 ISO 日期；`severity` 只能是 1/2/3（1=局部、2=区域冲突/制裁、
      3=全球战争/大国直接冲突），越界或非数字整表拒绝；`available_at` 为带时区
      ISO-8601，其日期不得早于 `date`。
    - `tips10y.csv` / `au_index.csv` / `gld_holdings.csv`：
      `date,value,available_at,source_url`。单位由数据源决定（TIPS 为 %、AU 指数为点、
      GLD 为吨），本模块不换算、不校验量级——换算属特征层职责。

    本函数只判“格式与主键”，不判新鲜度、不判数值合理性；质量分档由后续
    `normalize/assess_dataset` 完成。

    Args:
        path: 手工 CSV 路径；文件名必须命中 `MANUAL_SPECS`。
        kind: 预留参数，当前不参与判断（文件类型以文件名为准），保留是为了兼容
            未来“同名不同 kind”的场景。

    Returns:
        `{"file": 文件名, "status": "OK"|"EMPTY", "rows": 行数, "errors": []}`。
        空文件返回 `EMPTY` 而不是 `OK`：让“没有事件”与“有事件但都没问题”可区分。

    Raises:
        ManualDataError: 文件未登记、不存在、缺必需列、date/available_at 非法、
            source_url 为空、严重度越界、时间倒挂或主键重复。
    """
    name = path.name
    spec = MANUAL_SPECS.get(name)
    if spec is None:
        raise ManualDataError(f"未登记的手工文件：{name}")
    if not path.exists():
        raise ManualDataError(f"手工文件不存在：{path}")
    # dtype=str + keep_default_na=False：保留空串语义，避免 pandas 把空值变成 NaN 后
    # 被下游当作“0/缺失”静默处理。
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [column for column in spec["required"] if column not in frame.columns]
    if missing:
        raise ManualDataError(f"{name} 缺少字段：{missing}")
    errors: list[str] = []
    if frame.empty:
        return {"file": name, "status": "EMPTY", "rows": 0, "errors": []}

    dates = []
    available = []
    for index, row in frame.iterrows():
        # 报错行号 = 表头 1 行 + 1-based 行号，与用户在 Excel/编辑器中看到的行号一致，
        # 否则用户按报错找不到问题行。
        line = int(index) + 2
        try:
            parsed_date = parse_date(row["date"])
            dates.append(parsed_date)
        except ValueError as exc:
            errors.append(f"第 {line} 行 date 非法：{exc}")
            dates.append(None)
        try:
            parsed_available = parse_datetime(row["available_at"])
            available.append(parsed_available)
        except ValueError as exc:
            errors.append(f"第 {line} 行 available_at 非法：{exc}")
            available.append(None)
        if not str(row.get("source_url", "")).strip():
            errors.append(f"第 {line} 行 source_url 为空")
        if spec["kind"] == "events":
            if str(row.get("severity", "")).strip() not in {"1", "2", "3"}:
                errors.append(f"第 {line} 行 severity 必须是 1/2/3")
            if not str(row.get("event_id", "")).strip():
                errors.append(f"第 {line} 行 event_id 为空")

    for index, (day, stamp) in enumerate(zip(dates, available)):
        # 第二遍比较时间：先收齐所有日期再判倒挂，行序不会影响结论；任一侧解析失败
        # 已在上一轮记错，这里跳过，避免同一个错误报两遍。
        if day is None or stamp is None:
            continue
        if stamp.date() < day:
            errors.append(f"第 {index + 2} 行 available_at 早于 date")

    if spec["kind"] == "series":
        # 序列以 date 为主键：同日两值无法判断谁对，直接拒绝而不是“取最后一笔”。
        duplicated = sorted({value for value in dates if value is not None and dates.count(value) > 1})
        for value in duplicated:
            errors.append(f"date 重复：{value.isoformat()}")
    else:
        # 事件以 event_id 为主键：重复 id 说明人工复制粘贴出错，必须人工修正。
        ids = frame["event_id"].astype(str)
        for value in sorted(set(ids[ids.duplicated()])):
            errors.append(f"event_id 重复：{value}")

    if errors:
        # 只回传前 10 条：手工表可能整列都错，全量报错会把终端和报告刷屏；
        # 截断后仍足以定位首个问题。
        raise ManualDataError(f"{name} 校验失败：{errors[:10]}")
    return {"file": name, "status": "OK", "rows": int(len(frame)), "errors": []}


def validate_manual_dir(directory: Path) -> list[dict[str, Any]]:
    """逐个校验登记过的手工文件；缺失文件记 `MISSING`，不抛异常。

    缺失与非法要分开对待：文件还没建属于“尚未提供”，链路应继续跑并显示缺口；
    文件存在但内容非法才说明人工录入有问题，必须停手。返回顺序固定按
    `MANUAL_SPECS` 登记顺序，便于报告逐行对比。

    Args:
        directory: 手工数据目录，通常为 `config.path("manual")`。

    Returns:
        每个登记文件一项 `{"file", "status": "OK"|"EMPTY"|"MISSING", "rows", "errors"}`。

    Raises:
        ManualDataError: 某个已存在文件内容非法（由 `validate_manual_file()` 抛出）。
    """
    reports = []
    for name in MANUAL_SPECS:
        path = directory / name
        if not path.exists():
            reports.append({"file": name, "status": "MISSING", "rows": 0, "errors": []})
            continue
        reports.append(validate_manual_file(path))
    return reports


def ensure_templates(directory: Path) -> list[Path]:
    """只创建缺失的模板；已有文件一字不改。

    这是本模块唯一的写动作：人工表是用户自己维护的数据，任何“覆盖成标准表头”的行为
    都会造成不可恢复的数据丢失，因此只做存在性判断。目录不存在时先递归创建。

    Args:
        directory: 手工数据目录。

    Returns:
        本次实际创建的模板路径列表（按 `HEADERS` 顺序）；全部已存在时为空列表，
        调用方可据此在报告里只提示“新建了哪些”。
    """
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    for name, header in HEADERS.items():
        path = directory / name
        if not path.exists():
            # 模板也要原子写：中途失败时宁可没有文件，也不留一张半截表头，
            # 否则下次启动会把它当成“用户已有的手工表”跳过创建。
            atomic_write_text(path, header)
            created.append(path)
    return created
