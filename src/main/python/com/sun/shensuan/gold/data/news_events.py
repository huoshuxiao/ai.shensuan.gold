"""新闻→地缘事件抽取：把已入库快讯标题按配置规则映射成 `geo_events` 结构化行。

职责：从 `news_cls / news_sina / news_shmet` 的标题按关键词表产出
`event_id/date/type/severity/region/source_url/available_at`，写回事件日历输入文件。
不负责：读快讯之外的任何外部数据、调用 LLM、判定事件对金价的影响、
代替人工复核 severity（§6.2 硬规则：severity 必须人工复核；§7.3：
`GEO_EVENT_SEVERITY=0` 只表示未识别到事件，不表示无地缘风险）。
本模块也不写质量档——事件行进事实包前由 `normalize`/质量闸继续判。

口径边界：
- 匹配只在**标题**上做，规则表来自 `config/strategy.yaml` 的 `geo.news_events`，
  代码不写关键词与 severity 魔法数；`rules_version` 参与 `event_id`，规则一变
  事件号就变，历史抽取可对表。
- 同 `(type, region)` 在 `dedupe_window_days` 冷却期内只留最早发布的一条；地区未
  点明（`global`）的报道会并入同窗口内已有具体地区的同类事件——连续多日报道
  不得数成一串独立事件（两个不同具体地区仍各算一个）。
- `source_url` 写「接口名#内容哈希」，因为快讯接口不返回原文链接；哈希能在本地
  新闻表里定位到原始标题，不伪造 URL、不把原文外发。
- 手工补录的行（`event_id` 不带抽取前缀）原样保留，本模块只替换自己生成的行。
- 人工复核判死的误报进 `review_exclusions`（按 `content_hash` 精确排除，附理由与
  复核日期）：关键词规则无法区分「公司财报顺带提到骚乱」这类转述，给规则加排除词
  又会误伤真事件；用哈希点名只删被复核的那一条，重跑不会把它捞回来。

失败行为：规则表缺失或非法（severity 不在 1/2/3、关键词为空、type 重复）、
复核排除表缺哈希/缺理由/哈希格式不对，抛 `ValueError`，不静默按空规则跑出
「今天没有事件」。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

EXTRACT_PREFIX = "XGE"
EVENT_COLUMNS = ["event_id", "date", "type", "severity", "region", "source_url", "available_at"]
SEVERITY_LEVELS = (1, 2, 3)


@dataclass(frozen=True)
class EventRule:
    """一条抽取规则：命中任一关键词即产出该类型事件。

    Attributes:
        type: 事件类型代码（如 `armed_conflict`）。
        severity: 1=局部、2=区域冲突/制裁、3=全球战争/大国直接冲突，必须人工复核。
        keywords: 标题子串匹配关键词（不做分词、不用模型）。
        excludes: 排除短语；某次关键词命中位置上，若标题从该位置起能匹配到
            任一排除短语（如「爆炸性」包含「爆炸」），这次命中作废。
            没有这一步，「黄金爆炸性上涨」会被抽成爆炸事件。
    """

    type: str
    severity: int
    keywords: tuple[str, ...]
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewsEventRules:
    """`geo.news_events` 段的解析结果。

    Attributes:
        rules_version: 规则版本号，进 `event_id`，规则改动即换号。
        min_title_chars: 短于此长度的标题视为噪声，直接跳过。
        dedupe_window_days: 同 `(type, region)` 的冷却期（自然日）。
        rules: 事件规则列表。
        regions: 地区关键词表；命中即定地区，否则 `global`。
        excluded_hashes: 人工复核判死的快讯 `content_hash` 集合；命中的行不参与
            抽取。它记录的是**复核结论**（谁、何时、为什么），不是关键词规则，
            所以不改 `rules_version`；新增条目必须写 `reason`，加载期拒绝空理由。
    """

    rules_version: str
    min_title_chars: int
    dedupe_window_days: int
    rules: tuple[EventRule, ...]
    regions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    excluded_hashes: frozenset[str] = field(default_factory=frozenset)


def load_rules(strategy_section: dict | None) -> NewsEventRules:
    """解析 `strategy.yaml` 的 `geo.news_events` 段为规则对象。

    Args:
        strategy_section: `config.strategy` 里的 `geo.news_events` 映射；None 或
            空视为配置缺失。

    Returns:
        NewsEventRules: 校验通过的规则。

    Raises:
        ValueError: 缺 `rules_version`、缺规则、severity 越界、关键词为空、
            type 重复，或复核排除表里的哈希格式不对/缺理由/重复。
            空规则跑出「今天没有事件」会被误读成地缘平静，必须报错而不是降级；
            排除表写错同理——它会静默把该留的事件删掉或把该删的留下。
    """
    if not isinstance(strategy_section, dict):
        raise ValueError("geo.news_events 缺失：事件抽取规则必须由配置给出")
    version = str(strategy_section.get("rules_version") or "").strip()
    if not version:
        raise ValueError("geo.news_events.rules_version 缺失")
    raw_rules = strategy_section.get("rules") or []
    if not raw_rules:
        raise ValueError("geo.news_events.rules 为空：不允许按空规则冒充无事件")
    rules: list[EventRule] = []
    seen: set[str] = set()
    for item in raw_rules:
        if not isinstance(item, dict):
            raise ValueError(f"事件规则必须是映射，收到 {type(item).__name__}")
        event_type = str(item.get("type") or "").strip()
        if not event_type:
            raise ValueError("事件规则缺少 type")
        if event_type in seen:
            raise ValueError(f"事件规则 type 重复：{event_type}")
        seen.add(event_type)
        try:
            severity = int(item.get("severity"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{event_type} 的 severity 必须是 1/2/3，收到 {item.get('severity')!r}") from exc
        if severity not in SEVERITY_LEVELS:
            raise ValueError(f"{event_type} 的 severity 越界：{severity}")
        keywords = tuple(str(word).strip() for word in (item.get("keywords") or []) if str(word).strip())
        if not keywords:
            raise ValueError(f"{event_type} 的 keywords 为空")
        excludes = tuple(str(word).strip() for word in (item.get("excludes") or []) if str(word).strip())
        orphan = [word for word in excludes if not any(keyword in word for keyword in keywords)]
        if orphan:
            raise ValueError(
                f"{event_type} 的 excludes {orphan} 不包含任何本规则关键词：排除短语只在关键词命中位置上生效，"
                "写错了会静默失效，这里直接拒绝"
            )
        rules.append(EventRule(type=event_type, severity=severity, keywords=keywords, excludes=excludes))
    regions_raw = strategy_section.get("regions") or {}
    regions = {
        str(name).strip(): tuple(str(word).strip() for word in (words or []) if str(word).strip())
        for name, words in regions_raw.items()
        if str(name).strip()
    }
    return NewsEventRules(
        rules_version=version,
        min_title_chars=int(strategy_section.get("min_title_chars") or 1),
        dedupe_window_days=int(strategy_section.get("dedupe_window_days") or 1),
        rules=tuple(rules),
        regions=regions,
        excluded_hashes=_load_review_exclusions(strategy_section.get("review_exclusions")),
    )


def _load_review_exclusions(items: object) -> frozenset[str]:
    """解析人工复核排除表 `geo.news_events.review_exclusions`。

    Args:
        items: YAML 里的排除列表；每项需含 `content_hash`（`sha256:<64 位小写十六进制>`）
            与 `reason`，可附 `decided_at`/`decided_by` 供审计。None 或空列表表示没有排除。

    Returns:
        frozenset[str]: 被排除的 `content_hash` 集合（与新闻表 `content_hash` 逐字比较）。

    Raises:
        ValueError: 条目不是映射、哈希格式不对、缺理由或哈希重复。排除表写错会让
            「该删的没删 / 该留的被删」两种错都安静发生，必须在加载期拒绝。
    """
    if not items:
        return frozenset()
    if not isinstance(items, list):
        raise ValueError(f"review_exclusions 必须是列表，收到 {type(items).__name__}")
    hashes: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"review_exclusions 条目必须是映射，收到 {type(item).__name__}")
        digest = str(item.get("content_hash") or "").strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"review_exclusions.content_hash 格式不对：{digest!r}（应为 sha256:<64 位小写十六进制>）")
        if not str(item.get("reason") or "").strip():
            raise ValueError(f"review_exclusions {digest[:19]}… 缺 reason：复核结论必须写清为什么删")
        if digest in hashes:
            raise ValueError(f"review_exclusions 重复声明：{digest}")
        hashes.add(digest)
    return frozenset(hashes)


def _keyword_hits(text: str, rule: EventRule) -> bool:
    """判断一条规则是否在标题上真命中（排除短语按命中位置抵消）。

    Args:
        text: 标题原文。
        rule: 事件规则。

    Returns:
        bool: 任一关键词在某个位置上未被排除短语覆盖 → True。
            「黄金爆炸性上涨」里「爆炸」的每个命中位置后面都跟着「性」，
            被 `excludes: [爆炸性]` 抵消，因此不算爆炸事件。
    """
    for keyword in rule.keywords:
        start = text.find(keyword)
        while start != -1:
            if not any(text.startswith(exclude, start) for exclude in rule.excludes):
                return True
            start = text.find(keyword, start + 1)
    return False


def match_rule(title: str, rules: NewsEventRules) -> list[tuple[EventRule, str]]:
    """对一条标题做关键词匹配，返回命中的规则与地区。

    Args:
        title: 快讯标题原文（只在标题上匹配，不读正文）。
        rules: 已加载规则。

    Returns:
        list: `[(EventRule, region), ...]`；同一条标题可命中多个类型。
            标题长度不足 `min_title_chars` 时返回空列表。
    """
    text = str(title or "").strip()
    if len(text) < rules.min_title_chars:
        return []
    hits: list[tuple[EventRule, str]] = []
    for rule in rules.rules:
        if _keyword_hits(text, rule):
            hits.append((rule, detect_region(text, rules.regions)))
    return hits


def detect_region(text: str, regions: dict[str, tuple[str, ...]]) -> str:
    """按关键词表判定地区代码。

    Args:
        text: 标题原文。
        regions: `{地区代码: 关键词元组}`。

    Returns:
        str: 命中的地区代码；多区命中按配置顺序取第一个；都没命中返回 `global`。
    """
    for name, words in (regions or {}).items():
        if any(word in text for word in words):
            return name
    return "global"


def build_event_id(event_date: date, event_type: str, region: str, source_ref: str, version: str) -> str:
    """生成稳定、可复核的事件编号。

    组成：抽取前缀 + 规则版本 + 日期 + 类型 + 地区 + 来源指纹前 8 位。同一标题
    重跑得到同一个 id；换 `rules_version` 后整批 id 变化，便于历史对表。

    Args:
        event_date: 事件日期（取快讯发布日）。
        event_type: 事件类型代码。
        region: 地区代码。
        source_ref: 来源定位串（接口名 + 内容哈希）。
        version: `rules_version`。

    Returns:
        str: 事件编号。
    """
    digest = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:8]
    slug = re.sub(r"[^A-Za-z0-9]+", "", f"{event_type}{region}") or "EV"
    return f"{EXTRACT_PREFIX}-{version}-{event_date.isoformat()}-{slug}-{digest}"


def extract_events(news: pd.DataFrame, rules: NewsEventRules) -> pd.DataFrame:
    """从新闻表抽取事件行。

    Args:
        news: 规范化新闻表，需含 `published_at/title/source/content_hash`；
            缺列时抛错（列契约由 `normalize` 保证，不在这里兜底）。
        rules: 已加载规则。

    Returns:
        DataFrame: `EVENT_COLUMNS` 顺序的事件表；无命中时返回带列名的空表。
            `available_at` 取快讯发布时间（带时区 ISO-8601），与 news 的 PIT 口径一致。
            `review_exclusions` 里点名的快讯整行跳过，不参与匹配。

    Raises:
        ValueError: 缺少必需列，或发布时间不可解析。
    """
    required = {"published_at", "title", "source", "content_hash"}
    missing = sorted(required - set(news.columns))
    if missing:
        raise ValueError(f"新闻表缺列，无法抽取事件：{missing}")
    candidates: list[dict] = []
    for row in news.to_dict("records"):
        if str(row.get("content_hash") or "") in rules.excluded_hashes:
            continue
        published = _parse_published(row.get("published_at"))
        if published is None:
            continue
        source_ref = f"{row.get('source')}#{row.get('content_hash')}"
        for rule, region in match_rule(str(row.get("title") or ""), rules):
            candidates.append(
                {
                    "date": published.date(),
                    "type": rule.type,
                    "severity": rule.severity,
                    "region": region,
                    "source_url": source_ref,
                    "available_at": published.isoformat(),
                    "published_at": published,
                }
            )
    kept = _dedupe(candidates, rules.dedupe_window_days)
    rows = []
    for item in kept:
        rows.append(
            {
                "event_id": build_event_id(
                    item["date"], item["type"], item["region"], item["source_url"], rules.rules_version
                ),
                "date": item["date"].isoformat(),
                "type": item["type"],
                "severity": item["severity"],
                "region": item["region"],
                "source_url": item["source_url"],
                "available_at": item["available_at"],
            }
        )
    frame = pd.DataFrame(rows, columns=EVENT_COLUMNS)
    if not frame.empty:
        frame = frame.sort_values(["date", "severity", "event_id"], ascending=[True, False, True]).reset_index(drop=True)
    return frame


def excluded_rows(news: pd.DataFrame, rules: NewsEventRules) -> int:
    """统计被复核排除表拦下的快讯行数，供 CLI 读数（只读，不参与抽取）。

    Args:
        news: 规范化新闻表；可为空。
        rules: 已加载规则（使用其 `excluded_hashes`）。

    Returns:
        int: `content_hash` 落在排除表里的行数；0 表示本次没有命中排除项。
            单独报这个数，是为了让「排除表生效了」与「排除表写错了但恰好没命中」
            在日志里能分开看。
    """
    if news.empty or not rules.excluded_hashes or "content_hash" not in news.columns:
        return 0
    return int(news["content_hash"].astype(str).isin(rules.excluded_hashes).sum())


def _parse_published(value: object) -> datetime | None:
    """把 `published_at` 解析成带时区的 datetime。

    Args:
        value: 原始值（带时区 ISO-8601 字符串或 datetime）。

    Returns:
        datetime | None: 解析成功返回带时区时间；空值/坏格式返回 None（跳过该行）。

    Raises:
        ValueError: 解析出来是 naive 时间——PIT 口径禁止无时区时间戳冒充上海时间。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"published_at 不可解析：{text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"published_at 缺少时区：{parsed.isoformat()}")
    return parsed


def _dedupe(candidates: list[dict], window_days: int) -> list[dict]:
    """同 `(type, region)` 在冷却期内只留最早发布的一条。

    Args:
        candidates: 命中事件的候选行。
        window_days: 冷却期（自然日）；<=1 表示不做跨日合并。

    Returns:
        list: 去重后的候选行，按发布时间升序。

    Notes:
        严重度不参与“替换”：同一 `type` 的 severity 由规则表一一对应（`load_rules`
        禁止同一 type 出现两次），所以同类型重复报道的 severity 必然相同，
        这里只做「窗口内合并」，不做「取更高值」。
        地区按「具体优先」合并：一条报道没点明地区时判为 `global`，它会被并入
        同窗口内已有具体地区的同类事件（如 10-19「以色列空袭加沙」与 10-20
        「以军继续空袭」算同一个事件），避免同一件事因为地区写法不稳定被数成两条。
        两个不同具体地区仍各算一个事件。
    """
    ordered = sorted(candidates, key=lambda item: item["published_at"])
    kept: list[dict] = []
    window = max(window_days, 1)
    for item in ordered:
        cooling = any(
            existing["type"] == item["type"]
            and (
                existing["region"] == item["region"]
                or existing["region"] == "global"
                or item["region"] == "global"
            )
            and (item["date"] - existing["date"]).days < window
            for existing in kept
        )
        if not cooling:
            kept.append(item)
    return sorted(kept, key=lambda item: (item["published_at"], item["type"], item["region"]))


def merge_with_manual(existing: pd.DataFrame, extracted: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """把抽取结果并回事件日历，保留人工登记行。

    Args:
        existing: 现有 `geo_events.csv`；空表或 None 表示还没有登记。
        extracted: 本次抽取出的事件行。

    Returns:
        tuple: `(合并后的表, 统计)`；统计含
            `manual_kept/extracted_rows/replaced_extracted_rows`，用于交付说明里
            写清「替换了多少、留了多少」。

    Notes:
        人工行 = `event_id` 不以 `XGE` 开头。每次重跑只替换抽取行，
        不覆盖人工维护的记录（§6.3 手工层由人负责）。
    """
    manual_rows = pd.DataFrame(columns=EVENT_COLUMNS)
    replaced = 0
    if existing is not None and not existing.empty and "event_id" in existing.columns:
        mask = existing["event_id"].astype(str).str.startswith(f"{EXTRACT_PREFIX}-")
        replaced = int(mask.sum())
        manual_rows = existing.loc[~mask].copy()
    merged = pd.concat([manual_rows, extracted], ignore_index=True)
    if merged.empty:
        merged = pd.DataFrame(columns=EVENT_COLUMNS)
    else:
        merged = merged.drop_duplicates(subset=["event_id"], keep="last")
        merged = merged.sort_values(["date", "severity", "event_id"], ascending=[True, False, True]).reset_index(drop=True)
    stats = {
        "manual_kept": int(len(manual_rows)),
        "extracted_rows": int(len(extracted)),
        "replaced_extracted_rows": int(replaced),
    }
    return merged[EVENT_COLUMNS], stats
