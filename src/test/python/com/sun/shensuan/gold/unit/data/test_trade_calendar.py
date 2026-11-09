"""单元层：`data/trade_calendar` 的日历落盘、读取、校验与分母口径。

覆盖场景：工作日近似不含周末、保存后能原样读回、缺文件/被改写/空文件/缺校验和四种
「日历不可用」都必须带原因、`cn_sse` 声明但日历不可用时要回落近似并在标签里
写清原因、未知口径直接拒绝、空日历拒绝落盘。
负对照一律动被试数据（改写 CSV、清空文件），不改判据本身。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

import pandas as pd
import pytest

from com.sun.shensuan.gold.common.errors import AdapterError
from com.sun.shensuan.gold.data.trade_calendar import (
    BASIS_CN_SSE,
    BASIS_WEEKDAY,
    expected_days,
    fetch_cn_trade_days,
    load_all_calendars,
    load_trade_calendar,
    save_cn_trade_calendar,
    weekday_days,
)

DAYS = [date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]


def test_weekday_days_skips_weekends_and_empty_range():
    """工作日近似只排周一至周五；区间反向时返回空，不报错也不冒充「全天都是交易日」。"""
    days = weekday_days(date(2026, 10, 9), date(2026, 10, 18))
    assert days == [
        date(2026, 10, 9),
        date(2026, 10, 12),
        date(2026, 10, 13),
        date(2026, 10, 14),
        date(2026, 10, 15),
        date(2026, 10, 16),
    ]
    assert weekday_days(date(2026, 10, 18), date(2026, 10, 9)) == []


def test_save_then_load_roundtrip(tmp_path):
    """正对照：保存后读回应可用，行数/首末日/来源都从 meta 回到内存。"""
    meta = save_cn_trade_calendar(tmp_path, sorted(DAYS, reverse=True), None)
    calendar = load_trade_calendar(tmp_path, BASIS_CN_SSE)
    assert meta["rows"] == 4
    assert calendar.available is True and calendar.integrity == "OK"
    assert calendar.rows == 4
    assert calendar.as_of_min == date(2026, 10, 9) and calendar.as_of_max == date(2026, 10, 14)
    assert calendar.count_between(date(2026, 10, 10), date(2026, 10, 13)) == 2
    assert calendar.covers(date(2026, 10, 14)) is True
    assert calendar.covers(date(2026, 10, 15)) is False


def test_empty_calendar_refuses_to_write(tmp_path):
    """空日历会把「缺口」冒充成「无缺口」，必须拒绝落盘。"""
    with pytest.raises(ValueError):
        save_cn_trade_calendar(tmp_path, [], None)


@pytest.mark.parametrize(
    "breakage",
    ["missing_file", "missing_meta", "tampered_csv", "empty_csv"],
)
def test_unusable_calendar_reports_its_reason(tmp_path, breakage):
    """四种「日历不可用」各有一条可定位原因，且都不算 available。"""
    save_cn_trade_calendar(tmp_path, DAYS, None)
    csv_path = tmp_path / "trade_calendar_cn_sse.csv"
    meta_path = tmp_path / "trade_calendar_cn_sse.meta.json"
    if breakage == "missing_file":
        csv_path.unlink()
    elif breakage == "missing_meta":
        meta_path.unlink()
    elif breakage == "tampered_csv":
        # 负对照只动被试数据：追加一行交易日，校验和必须发现正文被改过。
        csv_path.write_text(csv_path.read_text(encoding="utf-8") + "2026-10-15\n", encoding="utf-8")
    else:
        # 造一份「校验和自洽但内容为空」的日历：只留表头，meta 的 sha 同步改掉，
        # 这样测到的是 EMPTY 分支，而不是被 SHA_MISMATCH 抢先。
        csv_path.write_text("trade_date\n", encoding="utf-8")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    calendar = load_trade_calendar(tmp_path, BASIS_CN_SSE)
    assert calendar.available is False
    assert calendar.days == frozenset()
    assert calendar.note
    expected_integrity = {
        "missing_file": "MISSING_FILE",
        "missing_meta": "MISSING_FILE",
        "tampered_csv": "SHA_MISMATCH",
        "empty_csv": "EMPTY",
    }
    assert calendar.integrity == expected_integrity[breakage]


@pytest.mark.parametrize("sha_value", [None, ""])
def test_meta_missing_or_empty_sha256_is_unusable(tmp_path, sha_value):
    """F3 回归：meta 缺 sha256（或为空）时日历不可用——没有校验和就无法证明正文没被改过。

    负对照动被试侧：先把正文追加一行（篡改），再把 meta 的 sha256 删掉/清空。
    旧实现 `if expected_digest and ...` 在缺校验和时跳过比对判 OK，等于放行被改过的日历；
    AGENTS §6.4 要求校验和不符视为不可用，缺校验和同样不可用。
    """
    save_cn_trade_calendar(tmp_path, DAYS, None)
    csv_path = tmp_path / "trade_calendar_cn_sse.csv"
    meta_path = tmp_path / "trade_calendar_cn_sse.meta.json"
    csv_path.write_text(csv_path.read_text(encoding="utf-8") + "2026-10-15\n", encoding="utf-8")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if sha_value is None:
        meta.pop("sha256", None)
    else:
        meta["sha256"] = sha_value
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    calendar = load_trade_calendar(tmp_path, BASIS_CN_SSE)
    assert calendar.available is False
    assert calendar.integrity != "OK"
    assert "sha256" in calendar.note


def test_meta_with_correct_sha256_stays_ok(tmp_path):
    """放行对照：meta 校验和正确时日历仍判 OK，F3 收紧不得误伤完好日历。"""
    save_cn_trade_calendar(tmp_path, DAYS, None)
    calendar = load_trade_calendar(tmp_path, BASIS_CN_SSE)
    assert calendar.integrity == "OK"
    assert calendar.available is True


def test_unknown_basis_rejected_and_weekday_has_no_file(tmp_path):
    """拼错口径要直接拒绝；weekday 是算法近似，不落盘也不冒充官方。"""
    with pytest.raises(ValueError):
        load_trade_calendar(tmp_path, "nasdaq")
    approx = load_trade_calendar(tmp_path, BASIS_WEEKDAY)
    assert approx.available is False
    assert approx.integrity == "APPROX"
    assert "近似" in approx.note


def test_expected_days_switches_denominator_and_labels_basis():
    """同一区间在两种口径下分母不同，标签必须说清用了哪一个。"""
    holidays = {date(2026, 10, 12), date(2026, 10, 13)}
    all_weekdays = weekday_days(date(2026, 10, 9), date(2026, 10, 16))
    sse_days = [day for day in all_weekdays if day not in holidays]
    calendar = load_from_days(sse_days)
    weekday_list, weekday_label = expected_days(BASIS_WEEKDAY, date(2026, 10, 9), date(2026, 10, 16), None)
    assert len(weekday_list) == 6 and weekday_label == "weekday_approx"
    sse_list, sse_label = expected_days(BASIS_CN_SSE, date(2026, 10, 9), date(2026, 10, 16), {BASIS_CN_SSE: calendar})
    assert sse_list == sse_days and sse_label.startswith("cn_sse(4 日")


def test_expected_days_falls_back_and_discloses_reason(tmp_path):
    """声明官方口径但日历没落盘：回落近似并把原因写进标签，不得冒充官方交易日。"""
    days, label = expected_days(BASIS_CN_SSE, date(2026, 10, 9), date(2026, 10, 16), None)
    assert days == weekday_days(date(2026, 10, 9), date(2026, 10, 16))
    assert label == "weekday_approx(日历未加载)"
    missing = load_all_calendars(tmp_path)
    _, label2 = expected_days(BASIS_CN_SSE, date(2026, 10, 9), date(2026, 10, 16), missing)
    assert "MISSING" in label2 or "未落盘" in label2


def test_fetch_reports_failure_instead_of_empty_calendar(monkeypatch):
    """抓取失败必须抛错：返回空表会被下游当成「没有交易日」，比报错更危险。"""
    monkeypatch.setitem(
        __import__("sys").modules,
        "akshare",
        type("Fake", (), {"tool_trade_date_hist_sina": staticmethod(lambda: pd.DataFrame())})(),
    )
    with pytest.raises(AdapterError):
        fetch_cn_trade_days()


def load_from_days(days):
    """把一组日期直接包成可用日历，避免为了单测去读磁盘。

    Args:
        days: 交易日列表。

    Returns:
        TradeCalendar: `integrity=OK` 的内存日历。
    """
    from com.sun.shensuan.gold.data.trade_calendar import TradeCalendar

    return TradeCalendar(
        basis=BASIS_CN_SSE,
        days=frozenset(days),
        integrity="OK",
        note="fixture",
        rows=len(days),
        as_of_min=min(days),
        as_of_max=max(days),
    )
