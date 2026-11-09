"""单元层：data.store 的原始层与规范化层落盘行为。

覆盖场景：单元格格式化稳定、规范化合并幂等、值变更更新、原始层 append-only、
原始字节保真、拒绝行保留且幂等。
"""
from __future__ import annotations

import pandas as pd
import pytest

from com.sun.shensuan.gold.data.models import FetchResult
from com.sun.shensuan.gold.data.normalize import normalize_frame
from com.sun.shensuan.gold.data.store import NormalizedStore, RawStore, format_cell


def test_format_cell_is_stable():
    """验证 format_cell 的稳定文本格式：浮点去尾零、NaN/NA 输出空串、整数不带小数点，保证哈希可复现。"""
    assert format_cell(4.8) == "4.8"
    assert format_cell(0.1 + 0.2) == "0.3"
    assert format_cell(float("nan")) == ""
    assert format_cell(pd.NA) == ""
    assert format_cell(3) == "3"


def test_normalized_merge_is_idempotent(repo_config, etf_frame, tmp_path):
    """验证规范化层合并幂等：相同输入二次合并新增 0 行且文件字节不变。"""
    spec = repo_config.data_sources.datasets["etf_518880"]
    store = NormalizedStore(tmp_path)
    first = normalize_frame(
        etf_frame, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-1"
    )
    merged, new_rows = store.merge("etf_518880", first.frame, spec.key_columns)
    path = store.write("etf_518880", merged)
    before = path.read_bytes()
    assert new_rows == 5

    second = normalize_frame(
        etf_frame, spec, pd.Timestamp("2024-01-09 16:00", tz="Asia/Shanghai"), "run-2"
    )
    merged2, new_rows2 = store.merge("etf_518880", second.frame, spec.key_columns)
    store.write("etf_518880", merged2)
    assert new_rows2 == 0
    assert path.read_bytes() == before


def test_normalized_merge_updates_changed_value(repo_config, etf_frame, tmp_path):
    """验证同键值变更时合并更新既有行（close 4.90→4.91），不新增行。"""
    spec = repo_config.data_sources.datasets["etf_518880"]
    store = NormalizedStore(tmp_path)
    first = normalize_frame(
        etf_frame, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-1"
    )
    merged, _ = store.merge("etf_518880", first.frame, spec.key_columns)
    store.write("etf_518880", merged)

    changed = etf_frame.copy()
    changed.loc[4, "close"] = 4.91
    second = normalize_frame(
        changed, spec, pd.Timestamp("2024-01-09 16:00", tz="Asia/Shanghai"), "run-2"
    )
    merged2, new_rows = store.merge("etf_518880", second.frame, spec.key_columns)
    store.write("etf_518880", merged2)
    assert new_rows == 0
    loaded = store.load("etf_518880")
    row = loaded.loc[loaded["as_of"] == "2024-01-08"].iloc[0]
    assert float(row["close"]) == pytest.approx(4.91)


def test_merge_upgrades_legacy_provisional_row_after_late_fetch(repo_config, tmp_path):
    """验证同值重抓也能把“旧行 fetched_at < available_at”升级为正式行。

    负对照对象：``NormalizedStore.merge`` 的临时→正式升级分支；若只比较业务列，
    旧行 fetched_at 仍是盘中时点，但价格没变，会被误留为临时快照。
    """
    spec = repo_config.data_sources.datasets["us10y"]
    raw = pd.DataFrame({"date": ["2026-10-20"], "us10y": [5.1]})
    final = normalize_frame(
        raw, spec, pd.Timestamp("2026-10-21 06:30", tz="Asia/Shanghai"), "run-late"
    )
    legacy = final.frame.copy()
    legacy.loc[0, "fetched_at"] = "2026-10-21T00:40:00+08:00"
    legacy.loc[0, "ingest_run_id"] = "run-early"
    store = NormalizedStore(tmp_path)
    store.write("us10y", legacy)

    merged, new_rows = store.merge("us10y", final.frame, spec.key_columns)
    assert new_rows == 0
    store.write("us10y", merged)
    row = store.load("us10y").iloc[0]
    assert row["fetched_at"].startswith("2026-10-21T06:30")
    assert row["ingest_run_id"] == "run-late"
    assert row["flags"] == ""
    assert row["quality"] == "GREEN"


def test_raw_store_is_append_only(repo_config, etf_frame, tmp_path):
    """验证原始层落盘生成数据文件与 .meta.json，重复写同一批次抛 FileExistsError（append-only）。"""
    store = RawStore(tmp_path)
    result = FetchResult(
        frame=etf_frame,
        source="test.source",
        fetched_at=pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai").to_pydatetime(),
    )
    path = store.save("etf_518880", "run-1", result, "2024-01-08")
    assert path.exists()
    meta = path.parent / f"{path.name}.meta.json"
    assert meta.exists()
    with pytest.raises(FileExistsError):
        store.save("etf_518880", "run-1", result, "2024-01-08")


def test_raw_store_persists_original_bytes(repo_config, tmp_path):
    """验证原始字节与扩展名（.xls）原样保存，供日后核查与重放。"""
    payload = b"date,GPRD\n2026-10-13,102.0\n"
    result = FetchResult(
        frame=pd.DataFrame({"date": ["2026-10-13"], "gprd": [102.0]}),
        source="matteoiacoviello.gpr_daily_recent",
        fetched_at=pd.Timestamp("2026-10-19 23:00", tz="Asia/Shanghai").to_pydatetime(),
        raw_bytes=payload,
        raw_suffix=".xls",
    )
    path = RawStore(tmp_path).save("gpr", "run-g", result, "2026-10-19")
    assert path.read_bytes() == payload
    assert path.suffix == ".xls"


def test_rejected_rows_are_kept_and_idempotent(tmp_path):
    """验证拒绝行单独落盘且幂等：重复写不改变字节，新日期追加后可按序读回。"""
    store = NormalizedStore(tmp_path)
    first = pd.DataFrame(
        {"as_of": ["2024-01-02"], "close": [0.0], "reject_reason": ["PRICE_OUT_OF_RANGE"]}
    )
    path = store.write_rejected("etf_518880", first)
    assert path is not None
    before = path.read_bytes()
    store.write_rejected("etf_518880", first)
    assert path.read_bytes() == before

    second = pd.DataFrame(
        {"as_of": ["2024-01-03"], "close": [0.0], "reject_reason": ["PRICE_OUT_OF_RANGE"]}
    )
    store.write_rejected("etf_518880", second)
    loaded = store.read_rejected("etf_518880")
    assert loaded["as_of"].tolist() == ["2024-01-02", "2024-01-03"]


def test_merge_removes_rejected_keys_from_main_table(repo_config, etf_frame, tmp_path):
    """被剔除的主键要从主表消失，否则历史并集会让它原样复活（C6/C7 永久判红）。

    三格：①先灌 5 行；②不传 `removed` 时旧行仍在（默认只并集，证明删除不是无条件发生的）；
    ③传 `removed` 后该时点消失，其他 4 行不动。
    """
    spec = repo_config.data_sources.datasets["etf_518880"]
    store = NormalizedStore(tmp_path)
    first = normalize_frame(etf_frame, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-1")
    store.write("etf_518880", store.merge("etf_518880", first.frame, spec.key_columns)[0])

    broken = etf_frame.copy()
    broken.loc[broken["date"] == pd.Timestamp("2024-01-04"), "close"] = 0.0
    second = normalize_frame(broken, spec, pd.Timestamp("2024-01-09 16:00", tz="Asia/Shanghai"), "run-2")
    assert second.rejected["as_of"].tolist() == ["2024-01-04"]

    kept, _ = store.merge("etf_518880", second.frame, spec.key_columns)
    assert len(kept) == 5 and "2024-01-04" in set(kept["as_of"])

    replaced, _ = store.merge("etf_518880", second.frame, spec.key_columns, removed=second.rejected)
    assert len(replaced) == 4
    assert "2024-01-04" not in set(replaced["as_of"])


def test_merge_authoritative_replaces_whole_table(repo_config, etf_frame, tmp_path):
    """完整快照语义：新输入就是这张表的全部真值，旧行不在其中即删除。

    事件日历每次整份重写（删掉的误报不能复活），历史完整性由源文件负责；
    默认模式（不传 authoritative）仍是并集，旧行保留——两格对照证明替换不是无条件发生。
    """
    spec = repo_config.data_sources.datasets["etf_518880"]
    store = NormalizedStore(tmp_path)
    first = normalize_frame(etf_frame, spec, pd.Timestamp("2024-01-08 16:00", tz="Asia/Shanghai"), "run-1")
    store.write("etf_518880", store.merge("etf_518880", first.frame, spec.key_columns)[0])

    recent_only = normalize_frame(
        etf_frame.iloc[3:], spec, pd.Timestamp("2024-01-09 16:00", tz="Asia/Shanghai"), "run-2"
    )
    kept, _ = store.merge("etf_518880", recent_only.frame, spec.key_columns)
    assert len(kept) == 5, "默认只并集：旧行不得因为这次没给而消失"

    replaced, new_rows = store.merge("etf_518880", recent_only.frame, spec.key_columns, authoritative=True)
    assert len(replaced) == 2
    assert new_rows == 0, "旧表里已出现过的主键不算新增"
    assert set(replaced["as_of"]) == {"2024-01-05", "2024-01-08"}
