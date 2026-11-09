"""单元层：新闻→地缘事件抽取（`data/news_events.py`）的规则解析、命中、去重与合并。

每条判据都给「放行格 + 判红格」：规则表非法必须报错（不许按空规则冒充无事件）、
冷却期内同类型重复报道只算一条、人工行不被覆盖、坏 `published_at` 直接拒绝
而不是跳过整表。夹具全部用合成标题，不读生产数据。
"""
from __future__ import annotations

import io

import pandas as pd
import pytest

from com.sun.shensuan.gold.data.news_events import (
    EVENT_COLUMNS,
    EXTRACT_PREFIX,
    NewsEventRules,
    detect_region,
    excluded_rows,
    extract_events,
    load_rules,
    match_rule,
    merge_with_manual,
)

BASE_RULES: dict = {
    "rules_version": "ge-1",
    "min_title_chars": 6,
    "dedupe_window_days": 7,
    "regions": {"mideast": ["加沙", "以色列", "伊朗"], "russia_ukraine": ["俄罗斯", "乌克兰"]},
    "rules": [
        {"type": "armed_conflict", "severity": 3, "keywords": ["宣战", "全面战争", "空袭"]},
        {"type": "sanctions", "severity": 2, "keywords": ["制裁", "出口管制"]},
        {"type": "local_incident", "severity": 1, "keywords": ["爆炸", "抗议"], "excludes": ["爆炸性"]},
    ],
}


def _rules(**over) -> NewsEventRules:
    """构造规则对象，允许覆盖单个字段以造负对照。

    Args:
        over: 覆盖 `BASE_RULES` 的键。

    Returns:
        NewsEventRules: 解析后的规则。
    """
    section = {**BASE_RULES, **over}
    return load_rules(section)


def _news(rows: list[dict]) -> pd.DataFrame:
    """构造最小新闻表（带时区发布时间 + 标题 + 来源 + 哈希）。

    Args:
        rows: 每行需含 `published_at` 与 `title`；缺 `source/content_hash` 时补齐。

    Returns:
        DataFrame: 抽取器要求的输入形状。
    """
    filled = [
        {"published_at": row["published_at"], "title": row["title"],
         "source": row.get("source", "akshare.unit"), "content_hash": row.get("content_hash", f"sha256:{i}")}
        for i, row in enumerate(rows)
    ]
    return pd.DataFrame(filled)


def test_load_rules_accepts_valid_section_and_rejects_four_breakages():
    """正对照：合法规则表解析成功；四种坏配置各报一条可定位错误。"""
    rules = _rules()
    assert rules.rules_version == "ge-1" and len(rules.rules) == 3
    with pytest.raises(ValueError, match="rules_version"):
        load_rules({**BASE_RULES, "rules_version": ""})
    with pytest.raises(ValueError, match="rules 为空"):
        load_rules({**BASE_RULES, "rules": []})
    with pytest.raises(ValueError, match="severity"):
        load_rules({**BASE_RULES, "rules": [{"type": "war", "severity": 5, "keywords": ["开战"]}]})
    with pytest.raises(ValueError, match="keywords 为空"):
        load_rules({**BASE_RULES, "rules": [{"type": "war", "severity": 3, "keywords": []}]})
    with pytest.raises(ValueError, match="type 重复"):
        load_rules({**BASE_RULES, "rules": [
            {"type": "war", "severity": 3, "keywords": ["开战"]},
            {"type": "war", "severity": 2, "keywords": ["停火"]},
        ]})
    with pytest.raises(ValueError, match="缺失"):
        load_rules(None)


def test_match_rule_hits_keywords_and_respects_min_length():
    """命中/不命中都要有格：短标题被跳过，长标题命中并定出地区。"""
    rules = _rules()
    hits = match_rule("以色列对加沙地带发动空袭", rules)
    assert [(rule.type, region) for rule, region in hits] == [("armed_conflict", "mideast")]
    assert match_rule("黄金ETF今日净流入", rules) == []
    assert match_rule("空袭", rules) == []  # 短于 min_title_chars=6 视为噪声
    assert detect_region("俄罗斯央行宣布新举措", rules.regions) == "russia_ukraine"
    assert detect_region("无关标题文字", rules.regions) == "global"


def test_excludes_cancel_only_the_matched_occurrence():
    """排除短语按命中位置抵消：行情修辞不算爆炸事件，真爆炸仍要算。

    这是实跑真实快讯时抓到的误命中形状：「黄金爆炸性上涨未结束」里
    「爆炸」确实出现，但它属于价格修辞；同一条规则必须仍能抓住「发生爆炸」。
    """
    rules = _rules()
    assert match_rule("德银：黄金爆炸性上涨未结束 年末目标4700美元", rules) == []
    hits = match_rule("市中心地铁站发生爆炸，警方封锁现场", rules)
    assert [rule.type for rule, _ in hits] == ["local_incident"]
    mixed = match_rule("矿区发生爆炸事故后公司宣布爆炸性扩产计划", rules)
    assert [rule.type for rule, _ in mixed] == ["local_incident"], "一处真命中 + 一处修辞命中，仍应算事件"


def test_load_rules_rejects_orphan_excludes():
    """排除短语必须包含本规则的关键词，否则它会静默失效——加载期直接拒绝。"""
    with pytest.raises(ValueError, match="excludes"):
        load_rules({**BASE_RULES, "rules": [
            {"type": "war", "severity": 3, "keywords": ["宣战"], "excludes": ["无关词"]},
        ]})


def test_extract_events_maps_titles_to_structured_rows():
    """正对照：命中标题产出结构化事件，字段齐、available_at 带时区、severity 来自规则。"""
    news = _news([
        {"published_at": "2026-10-19T09:15:00+08:00", "title": "某国宣布对伊朗实施新制裁", "content_hash": "sha256:a"},
        {"published_at": "2026-10-20T10:00:00+08:00", "title": "市中心发生爆炸事件引发关注", "content_hash": "sha256:b"},
        {"published_at": "2026-10-21T11:00:00+08:00", "title": "世界黄金协会发布季度报告", "content_hash": "sha256:c"},
    ])
    events = extract_events(news, _rules())
    assert list(events.columns) == EVENT_COLUMNS
    assert len(events) == 2
    first = events.iloc[0]
    assert first["type"] == "sanctions" and int(first["severity"]) == 2 and first["region"] == "mideast"
    assert first["available_at"] == "2026-10-19T09:15:00+08:00"
    assert first["source_url"].startswith("akshare.unit#sha256:a")
    assert first["event_id"].startswith(f"{EXTRACT_PREFIX}-ge-1-2026-10-19-")
    assert set(events["date"]) == {"2026-10-19", "2026-10-20"}


def test_extract_events_is_deterministic_across_runs():
    """同一输入两次抽取必须逐字相同：event_id 由来源哈希决定，不含运行时间。"""
    news = _news([
        {"published_at": "2026-10-19T09:15:00+08:00", "title": "以色列宣布对加沙展开军事行动并空袭", "content_hash": "sha256:a"},
        {"published_at": "2026-10-19T09:20:00+08:00", "title": "联合国呼吁各方停火并停止空袭", "content_hash": "sha256:d"},
    ])
    once = extract_events(news, _rules())
    twice = extract_events(news.iloc[::-1].reset_index(drop=True), _rules())
    assert sorted(once["event_id"]) == sorted(twice["event_id"])
    # 同一标题重跑（顺序不同）id 不变；换 rules_version 后整批 id 变，便于历史对表。
    newer = extract_events(news, _rules(rules_version="ge-2"))
    assert all(event_id.startswith(f"{EXTRACT_PREFIX}-ge-2-") for event_id in newer["event_id"])


def test_dedupe_window_keeps_one_event_per_window():
    """冷却期：同 (type, region) 窗口内多条报道只算一条，窗口过后另起新事件。"""
    news = _news([
        {"published_at": "2026-10-19T09:00:00+08:00", "title": "边境冲突升级并发生空袭行动", "content_hash": "sha256:a"},
        {"published_at": "2026-10-21T09:00:00+08:00", "title": "该国正式宣战进入全面战争状态", "content_hash": "sha256:b"},
        {"published_at": "2026-11-05T09:00:00+08:00", "title": "又一轮空袭造成基础设施损毁", "content_hash": "sha256:c"},
    ])
    events = extract_events(news, _rules())
    conflicts = events[events["type"] == "armed_conflict"]
    assert len(conflicts) == 2, f"冷却期应把 10-19/10-21 合并、11-05 另起一条：{conflicts.to_dict('records')}"
    assert set(conflicts["date"]) == {"2026-10-19", "2026-11-05"}
    assert conflicts.iloc[0]["source_url"].endswith("sha256:a"), "窗口内应保留最早发布的那条"


def test_one_title_hitting_two_types_counts_once_per_type():
    """一条标题命中两个类型：各留一条事件，但同类型连续报道不再重复计数。"""
    news = _news([
        {"published_at": "2026-10-19T09:00:00+08:00", "title": "以色列对加沙空袭并宣布新一轮制裁", "content_hash": "sha256:a"},
        {"published_at": "2026-10-20T09:00:00+08:00", "title": "以军继续空袭，制裁波及多家银行", "content_hash": "sha256:b"},
    ])
    events = extract_events(news, _rules())
    assert sorted(events["type"]) == ["armed_conflict", "sanctions"]
    assert set(events["region"]) == {"mideast"}
    assert all(event_date == "2026-10-19" for event_date in events["date"])


def test_extract_rejects_bad_inputs_instead_of_returning_empty():
    """负对照：缺列、naive 时间戳、坏时间格式都要报错，不许用空表冒充「没有事件」。"""
    rules = _rules()
    with pytest.raises(ValueError, match="缺列"):
        extract_events(pd.DataFrame([{"title": "空袭"}]), rules)
    with pytest.raises(ValueError, match="缺少时区"):
        extract_events(_news([{"published_at": "2026-10-19T09:00:00", "title": "某地发生爆炸"}]), rules)
    with pytest.raises(ValueError, match="不可解析"):
        extract_events(_news([{"published_at": "昨天早上", "title": "某地发生爆炸"}]), rules)
    empty = extract_events(_news([{"published_at": "2026-10-19T09:00:00+08:00", "title": "与规则无关的标题"}]), rules)
    assert empty.empty and list(empty.columns) == EVENT_COLUMNS


def test_merge_preserves_manual_rows_and_replaces_only_extracted():
    """人工行不被覆盖；重跑只替换抽取行，并如实报出替换了多少。"""
    existing = pd.DataFrame([
        {"event_id": "M-2026-001", "date": "2026-09-01", "type": "sanctions", "severity": 2,
         "region": "global", "source_url": "https://example.gov/a", "available_at": "2026-09-01T08:00:00+08:00"},
        {"event_id": f"{EXTRACT_PREFIX}-ge-1-2026-10-01-sanctions-aaaa1111", "date": "2026-10-01",
         "type": "sanctions", "severity": 2, "region": "global",
         "source_url": "akshare.unit#sha256:old", "available_at": "2026-10-01T08:00:00+08:00"},
    ])
    extracted = pd.DataFrame([
        {"event_id": f"{EXTRACT_PREFIX}-ge-1-2026-10-19-sanctions-bbbb2222", "date": "2026-10-19",
         "type": "sanctions", "severity": 2, "region": "mideast",
         "source_url": "akshare.unit#sha256:new", "available_at": "2026-10-19T09:15:00+08:00"},
    ])
    merged, stats = merge_with_manual(existing, extracted)
    assert stats == {"manual_kept": 1, "extracted_rows": 1, "replaced_extracted_rows": 1}
    assert list(merged["event_id"]) == ["M-2026-001", f"{EXTRACT_PREFIX}-ge-1-2026-10-19-sanctions-bbbb2222"]
    empty_merge, empty_stats = merge_with_manual(pd.DataFrame(columns=EVENT_COLUMNS), pd.DataFrame(columns=EVENT_COLUMNS))
    assert empty_merge.empty and empty_stats == {"manual_kept": 0, "extracted_rows": 0, "replaced_extracted_rows": 0}


def test_merge_preserves_manual_review_column():
    """F2 回归：人工复核列 severity_reviewed 必须随合并原样保留，重跑抽取不得截掉。

    真实路径（cli.extract-events）用 dtype=str 读手工表、to_csv 写回；用户复核后在
    该列写 1，remediation 靠字面 "1" 解锁 GEO_EVENT_SEVERITY。若 merge 把列截掉，
    复核结论静默丢失、封锁永远解不开。负对照动被试侧：给现有表加一列复核标记。
    """
    manual_id = "M-2026-001"
    old_extracted_id = f"{EXTRACT_PREFIX}-ge-1-2026-10-01-sanctions-aaaa1111"
    new_extracted_id = f"{EXTRACT_PREFIX}-ge-1-2026-10-19-sanctions-bbbb2222"
    existing = pd.DataFrame([
        {"event_id": manual_id, "date": "2026-09-01", "type": "sanctions", "severity": 2,
         "region": "global", "source_url": "https://example.gov/a",
         "available_at": "2026-09-01T08:00:00+08:00", "severity_reviewed": "1"},
        {"event_id": old_extracted_id, "date": "2026-10-01", "type": "sanctions", "severity": 2,
         "region": "global", "source_url": "akshare.unit#sha256:old",
         "available_at": "2026-10-01T08:00:00+08:00", "severity_reviewed": ""},
    ])
    extracted = pd.DataFrame([
        {"event_id": new_extracted_id, "date": "2026-10-19", "type": "sanctions", "severity": 2,
         "region": "mideast", "source_url": "akshare.unit#sha256:new",
         "available_at": "2026-10-19T09:15:00+08:00"},
    ])
    merged, stats = merge_with_manual(existing, extracted)
    assert stats["manual_kept"] == 1
    assert list(merged.columns) == EVENT_COLUMNS + ["severity_reviewed"], "额外列必须保留且排在 EVENT_COLUMNS 之后"
    manual_row = merged.loc[merged["event_id"] == manual_id].iloc[0]
    assert str(manual_row["severity_reviewed"]) == "1", "人工复核结论必须原样保留"
    new_row = merged.loc[merged["event_id"] == new_extracted_id].iloc[0]
    assert pd.isna(new_row["severity_reviewed"]) or str(new_row["severity_reviewed"]) == "", "抽取行没有复核列时留空"
    # 写回 CSV 再按 remediation 的消费形状读：复核结论仍是字面 "1"，解锁不会因 dtype 漂移失效。
    reread = pd.read_csv(io.StringIO(merged.to_csv(index=False)), dtype=str, keep_default_na=False)
    assert reread.loc[reread["event_id"] == manual_id, "severity_reviewed"].iloc[0] == "1"


def test_merge_without_extra_columns_keeps_exact_event_columns():
    """放行对照：现有表没有额外列时，输出列恰好等于 EVENT_COLUMNS，不多不少。"""
    existing = pd.DataFrame([
        {"event_id": "M-2026-001", "date": "2026-09-01", "type": "sanctions", "severity": 2,
         "region": "global", "source_url": "https://example.gov/a", "available_at": "2026-09-01T08:00:00+08:00"},
    ])
    merged, _ = merge_with_manual(existing, pd.DataFrame(columns=EVENT_COLUMNS))
    assert list(merged.columns) == EVENT_COLUMNS
    empty, _ = merge_with_manual(pd.DataFrame(columns=EVENT_COLUMNS), pd.DataFrame(columns=EVENT_COLUMNS))
    assert list(empty.columns) == EVENT_COLUMNS


def test_review_exclusions_drop_only_the_named_rows():
    """复核排除表按哈希点名：被点名的误报不产出事件，其他同窗口的行照常抽取。

    夹具模拟 2026-10-20「家乐福下调财务目标 上周末法国门店发生抗议」这类转述误报：
    两条标题都含关键词，只排掉被人工复核判死的那一条（哈希点名），另一条照常出事件。
    """
    named = "sha256:" + "a" * 64
    rules = _rules(review_exclusions=[{"content_hash": named, "reason": "公司财报转述，误报"}])
    news = _news([
        {"published_at": "2026-10-20T09:00:00+08:00", "title": "家乐福下调财务目标 上周末法国门店发生抗议",
         "content_hash": named},
        {"published_at": "2026-10-21T09:00:00+08:00", "title": "美国宣布对伊朗实施新一轮制裁",
         "content_hash": "sha256:" + "b" * 64},
    ])
    events = extract_events(news, rules)
    assert len(events) == 1
    assert events.iloc[0]["type"] == "sanctions"
    assert events.iloc[0]["source_url"].endswith("b" * 64)
    assert excluded_rows(news, rules) == 1

    # 对照：同一批新闻不配排除表时两条都抽出来——排除表不是「顺手少一条」。
    assert len(extract_events(news, _rules())) == 2
    assert excluded_rows(news, _rules()) == 0


def test_review_exclusions_reject_malformed_entries():
    """排除表写错必须加载期报错：哈希格式、缺理由、重复、非映射四种各一格。"""
    good = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="格式不对"):
        load_rules({**BASE_RULES, "review_exclusions": [{"content_hash": "sha256:abc", "reason": "x"}]})
    with pytest.raises(ValueError, match="缺 reason"):
        load_rules({**BASE_RULES, "review_exclusions": [{"content_hash": good, "reason": "   "}]})
    with pytest.raises(ValueError, match="重复"):
        load_rules({**BASE_RULES, "review_exclusions": [
            {"content_hash": good, "reason": "x"}, {"content_hash": good, "reason": "y"},
        ]})
    with pytest.raises(ValueError, match="必须是映射"):
        load_rules({**BASE_RULES, "review_exclusions": [good]})
    assert _rules(review_exclusions=[]).excluded_hashes == frozenset()
