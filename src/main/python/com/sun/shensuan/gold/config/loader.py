"""配置加载、schema 校验与 config_hash。

优先级：CLI > 环境变量 > YAML > 默认值（P1 只实现 YAML 与默认值）。
缺键/类型错必须启动失败，不得用默认值蒙混。

职责：读取 ``application/data_sources/strategy/llm`` 四份 YAML，用 pydantic
做 schema 校验，计算 ``config_hash``，并把相对路径解析集中到 ``LoadedConfig``。

关键不变量：
- 四份文件缺一即 ``FileNotFoundError``；未知键、缺键、类型错即
  ``ValidationError``——不允许“补个默认值先跑起来”（AGENTS §15.6）。
- ``config_hash`` 只由四份 YAML 的解析结果决定：取值变则哈希变，
  注释、缩进、键顺序不影响；它用于决策产物审计与配置回滚比对。
- ``strategy`` 保持原始 dict（P1 只要求含 ``weights``），避免把尚未实现的
  P2/P3 判据提前固化成 schema。
- 相对路径一律以 ``repo_root`` 为基准，不写死绝对路径。

不负责：环境变量/CLI 覆盖（P1 未实现，优先级表只在文档中声明）、读取
``.env`` 或密钥、校验 ``strategy`` 的深层结构与数值合理性（P2 策略层负责）。
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..common.hashing import hash_obj
from ..common.paths import CONFIG_DIR, REPO_ROOT
from ..common.timeutil import AVAILABLE_AT_RULES, CALENDAR_BASES

# 加载清单与 config_hash 的键集合都由这里决定；新增配置文件必须同批改
# LoadedConfig、测试与 AGENTS 的目录说明。
CONFIG_FILES = ("application", "data_sources", "strategy", "llm")


class PathsConfig(BaseModel):
    """``application.yaml`` 的 ``paths`` 段：仓库内各数据目录。

    10 个键默认都是相对路径，由 ``LoadedConfig.path`` 拼到 ``repo_root``；
    也可以填绝对路径（此时 ``path`` 原样返回）。

    Attributes:
        data: 数据根目录。
        raw: 原始快照（append-only，抓取后不得覆盖）。
        normalized: 规范化长表（幂等重写）。
        manual: 手工 CSV 目录。
        reference: 参考数据目录（交易日历等非行情、非决策产物）。
        decisions: 每日决策产物与 ``pipeline_status.json``。
        paper: 纸面账本。
        live: 实盘回录；gitignored，只在本地。
        logs: JSONL 日志。
        report: 日报与图表。

    Notes:
        ``extra="forbid"``：路径键拼错会直接 ValidationError，而不是悄悄写到
        默认目录，避免决策产物落错地方。
    """

    model_config = ConfigDict(extra="forbid")
    data: str = "data"
    raw: str = "data/raw"
    normalized: str = "data/normalized"
    manual: str = "data/manual"
    reference: str = "data/reference"
    decisions: str = "data/decisions"
    paper: str = "data/paper"
    live: str = "data/live"
    logs: str = "logs"
    report: str = "report"


class UiConfig(BaseModel):
    """``application.yaml`` 的 ``ui`` 段：Streamlit 绑定参数。

    Attributes:
        host: 监听地址，默认 ``127.0.0.1``；AGENTS §14.2 禁止对公网开放。
        port: 监听端口，默认 8501。

    Notes:
        P1 不启动 UI，本段只加载、校验并计入 ``config_hash``。
    """

    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = 8501


class LoggingConfig(BaseModel):
    """``application.yaml`` 的 ``logging`` 段：日志级别与保留期。

    Attributes:
        level: 日志级别名（``logging`` 模块可识别的字符串）。
        retention_days: 在线日志保留天数，``>=1``，默认 7（AGENTS §15.7）。
        file_prefix: 预留的文件名前缀。

    Notes:
        ``logging_setup.setup_logging`` 当前固定写 ``app-<日期>.jsonl``，
        尚未读取 ``file_prefix``；改前缀必须同批改 ``LOG_PATTERN`` 与清理逻辑。
    """

    model_config = ConfigDict(extra="forbid")
    level: str = "INFO"
    retention_days: int = Field(default=7, ge=1)
    file_prefix: str = "app"


class IngestConfig(BaseModel):
    """``application.yaml`` 的 ``ingest`` 段：抓取重试、超时与 PIT 起点。

    Attributes:
        retries: 单个数据源失败后的重试次数，0–5，默认 2（AGENTS §6.7）。
        backoff_s: 指数退避基数（秒）；第 k 次失败后等待 ``backoff_s * 2**k``
            （k 从 0 起），即首次重试等一个 ``backoff_s``。
        request_timeout_s: 单次下载默认超时秒数，``>=10``。
        history_start: 首次全量抓取的起点日期（``YYYY-MM-DD``）。
        cutoff_time: 事实包冻结时刻（本地时区 ``HH:MM``，默认 07:00）。

    Notes:
        P1 实际读取前三个键（``run_ingest`` 与 ``status`` 使用）；适配器另有
        内置起点兜底。``history_start``/``cutoff_time`` 先随配置加载并计入
        哈希，留给后续阶段接线，避免口径散落在代码里。
    """

    model_config = ConfigDict(extra="forbid")
    retries: int = Field(default=2, ge=0, le=5)
    backoff_s: float = Field(default=0.5, ge=0)
    request_timeout_s: int = Field(default=600, ge=10)
    history_start: str = "2021-10-19"
    cutoff_time: str = "07:00"


class PreflightConfig(BaseModel):
    """``application.yaml`` 的 ``preflight`` 段：启动前的磁盘与内存闸。

    Attributes:
        min_disk_free_gb: 仓库磁盘剩余下限（GiB），低于即 preflight 判 RED。
        llm_min_free_gb: 起 LLM 前 ``MemAvailable`` 下限（GiB）；初始占位 9.5，
            P5 前按 9b 实测峰值重定（AGENTS §15.10）。P1 不启 LLM，只展示。

    Notes:
        阈值是硬闸，改值必须同批更新 AGENTS、看板与测试（AGENTS §0.6）。
    """

    model_config = ConfigDict(extra="forbid")
    min_disk_free_gb: float = Field(default=5.0, ge=0)
    llm_min_free_gb: float = Field(default=9.5, ge=0)


class AcceptanceConfig(BaseModel):
    """``application.yaml`` 的 ``acceptance`` 段：P1 阶段验收闸的程序化阈值。

    Attributes:
        min_weekday_coverage: 日频源工作日覆盖率下限（0–1）。交易日历未接入前，
            分母用周一至周五近似，因此阈值只作下限闸（AGENTS §6.6）。
        max_stale_days: 核心源最近一行相对验收基准日允许的最大滞后（自然日）；
            超过即验收 FAIL，非核心源只披露不阻断。
        weekly_rows_per_year_min/weekly_rows_per_year_max: 周频源的行数/年合理区间，
            用于「不把周频误报成缺口」（AGENTS §6.6 的 40–60 行/年口径）。
        replay_kinds: 参与「原始快照重放对表」的数据集 kind；P1 只重放数值型
            （price/series），news/events 的批次字段本来就会变，不纳入。

    Notes:
        这些阈值是判据的一部分，改值必须同批更新 AGENTS §18.6、看板与负对照测试
        （AGENTS §0.6）；注释不进 ``config_hash``，改注释不改取值不会改变哈希。
    """

    model_config = ConfigDict(extra="forbid")
    min_weekday_coverage: float = Field(default=0.80, ge=0, le=1)
    max_stale_days: int = Field(default=5, ge=1)
    weekly_rows_per_year_min: float = Field(default=40.0, ge=1)
    weekly_rows_per_year_max: float = Field(default=60.0, ge=1)
    replay_kinds: list[str] = Field(default_factory=lambda: ["price", "series"])

    @field_validator("weekly_rows_per_year_max")
    @classmethod
    def _weekly_window_direction(cls, value: float, info) -> float:
        """校验周频行数区间的方向（上限必须不低于下限）。

        Args:
            value: YAML 中的 ``weekly_rows_per_year_max``。
            info: pydantic 校验上下文，含同批次其它字段值。

        Returns:
            float: 通过校验的原值。

        Raises:
            ValueError: 上限小于下限——这种配置会让每条周频判据恒 FAIL，
                必须在加载期拒绝，而不是拖到验收跑到一半。
        """
        low = info.data.get("weekly_rows_per_year_min")
        if low is not None and value < low:
            raise ValueError(f"weekly_rows_per_year_max={value} 必须 >= min={low}")
        return value

    @field_validator("replay_kinds")
    @classmethod
    def _replay_kinds_allowed(cls, value: list[str]) -> list[str]:
        """校验重放 kind 只允许已支持的取值。

        Args:
            value: YAML 中的 ``replay_kinds`` 列表。

        Returns:
            list: 通过校验的原列表。

        Raises:
            ValueError: 出现 ``price/series/news/events`` 之外的值——拼错键会让
                重放判据静默失效，必须在加载期拒绝。
        """
        allowed = {"price", "series", "news", "events"}
        bad = [v for v in value if v not in allowed]
        if bad:
            raise ValueError(f"replay_kinds 含不支持的取值 {bad}；允许 {sorted(allowed)}")
        return value


class ApplicationConfig(BaseModel):
    """``application.yaml`` 的完整结构。

    Attributes:
        schema_version: 配置 schema 版本（字符串，如 ``"1"``）。
        timezone: 展示与日期口径时区，默认 ``Asia/Shanghai``。
        mode: 运行模式（``LLM_IN_BAND/DETERMINISTIC/DEGRADED/SHADOW``）。
        paths/ui/logging/ingest/preflight/acceptance: 各子段；缺省时用默认工厂。

    Notes:
        ``mode`` 是声明式标记：P1 不据此分支，但仍计入 ``config_hash``。
        顶层未知键会因 ``extra="forbid"`` 直接失败。
        ``acceptance`` 是阶段验收闸的阈值来源；判据代码不写魔法数（§15.5）。
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: str
    timezone: str
    mode: str
    paths: PathsConfig = Field(default_factory=PathsConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)
    acceptance: AcceptanceConfig = Field(default_factory=AcceptanceConfig)


class DatasetConfig(BaseModel):
    """``data_sources.yaml`` 中单个数据集的口径声明。

    Attributes:
        enabled: 是否参与日链抓取；False 的数据集不出现在默认抓取列表。
        source: 数据源标识，写入 PIT 的 ``source`` 列，供追溯。
        adapter: ``data/adapters`` 注册表中的适配器名称。
        symbol: 传给数据源的代码/参数（如 ``sh518880``、``Au99.99``、``GC``）。
        kind: 数据集类型，``price/series/news/events``；决定规范化与质量分支。
        core: 是否核心序列；核心缺失按 AGENTS §6.6 应升级为 ``DEGRADED``。
        required_columns: 规范化后必须存在的列；缺列即 RED。
        timezone: 该数据集的日期/时间口径时区，默认 ``Asia/Shanghai``。
        calendar: 连续性判据的分母口径，``cn_sse``（上交所官方交易日）或
            ``weekday``（周一至周五近似，默认）。境内股票/期货/现货用 ``cn_sse``；
            外汇报价日与美国交易日没有官方直连日历，必须留在 ``weekday`` 并接受
            读数里「近似」的披露（AGENTS §6.6）。
        available_at_rule: PIT 可见时点规则，取值见 ``timeutil.available_at_for``。
        stale_days: 陈旧阈值（自然日），超过即 ``STALE``。
        key_columns: 规范化层合并去重的主键列；必须来自
            ``required_columns ∪ {"as_of"}``（``as_of`` 是派生列，不在源表契约里）。
        history_start: 首次全量抓取起点；``None`` 时由适配器内置兜底。
        price_min/price_max: 收盘价的合理区间（单位随数据集），越界判 RED；
            ``price_min >= 0`` 且不得大于 ``price_max``（AGENTS §6.6：价格 ≤0 必须剔除）。
        zero_fields: 源结构性给 0 的字段；真实 0 标 ``ZERO_<COL>``，缺失保持空值并标
            ``MISSING_<COL>``，两者都把数据集降级（AGENTS §6.6：零持仓不得当真实
            流动性；§2.1-5：禁止填 0 冒充真实值）。
        positive_columns: 必须为正的数值列（当前只有 AU0 的 ``settlement``）；出现
            ≤0 时整行剔除并留档（原因 ``NONPOSITIVE_<COL>``），不猜一个值出来。
            必须是 ``required_columns`` 的子集；只对显式声明它的数据集生效。
        drop_all_zero_rows: series 数据集是否把「全部必需数值列同时为 0」的行整行
            剔除（原因 ``ALL_ZERO_IN_REQUIRED``）。默认关闭：GPR 这类指数的 0 可以是
            真实平静日（本库实测 2 行 ``gprd=0``），全局强推会删掉真实读数；
            只有 CFTC 这种「多空净仓全 0 = 源没给值」的数据集才开启。
        authoritative_snapshot: 输入是否本身就是这张表的全部真值（当前只有
            ``geo_events``：手工/抽取生成的日历文件每次整份重写）。True 时规范化层
            按完整快照替换、不做历史并集——否则被删掉的误报会从旧行里复活；
            历史完整性由源文件负责。False（默认）时合并只增不删。
        revised_only: 无 vintage 的修订序列；True 时整表标 ``REVISED_ONLY``。
        manual_path: 手工 CSV 相对 ``paths.manual`` 的文件名。
        options: 适配器私有参数（如 ``price_column``、``url``）；禁止放密钥。

    Notes:
        ``extra="forbid"``：``data_sources.yaml`` 拼错键会直接失败，避免某条
        质量规则因键名笔误而静默失效。跨字段口径（``available_at_rule`` 合法值、
        主键列归属、价格区间方向）都在加载阶段校验：这些错误若拖到日链中途，
        会把半条链的节点留在 RUNNING，且质量闸已经按错误口径跑过。
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    source: str
    adapter: str
    symbol: str | None = None
    kind: str
    core: bool = False
    required_columns: list[str]
    timezone: str = "Asia/Shanghai"
    calendar: str = "weekday"
    available_at_rule: str
    stale_days: int = Field(ge=1)
    key_columns: list[str]
    history_start: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    zero_fields: list[str] = Field(default_factory=list)
    positive_columns: list[str] = Field(default_factory=list)
    drop_all_zero_rows: bool = False
    authoritative_snapshot: bool = False
    revised_only: bool = False
    manual_path: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _kind_allowed(cls, value: str) -> str:
        """校验 ``kind`` 属于支持的四类。

        Args:
            value: YAML 中的 ``kind`` 字符串。

        Returns:
            str: 通过校验的原值。

        Raises:
            ValueError: 不在 ``price/series/news/events`` 中。
        """
        allowed = {"price", "series", "news", "events"}
        if value not in allowed:
            raise ValueError(f"kind 必须是 {sorted(allowed)}，收到 {value!r}")
        return value

    @field_validator("calendar")
    @classmethod
    def _calendar_basis_allowed(cls, value: str) -> str:
        """校验 ``calendar`` 属于 ``timeutil.CALENDAR_BASES``。

        Args:
            value: YAML 中的 ``calendar`` 字符串。

        Returns:
            str: 通过校验的原值。

        Raises:
            ValueError: 不在合法口径中。拼错的口径若被静默当成 ``weekday``，
                境内源会重新按自然工作日算缺口，节假日又变回假缺口（§6.6）。
        """
        if value not in CALENDAR_BASES:
            raise ValueError(f"calendar 必须是 {list(CALENDAR_BASES)}，收到 {value!r}")
        return value

    @field_validator("available_at_rule")
    @classmethod
    def _available_at_rule_allowed(cls, value: str) -> str:
        """校验 PIT 规则名属于 ``timeutil.AVAILABLE_AT_RULES``。

        Args:
            value: YAML 中的 ``available_at_rule`` 字符串。

        Returns:
            str: 通过校验的原值。

        Raises:
            ValueError: 不在 ``AVAILABLE_AT_RULES``（当前 7 种）合法规则中。
                此前该错误要等生成 PIT 时才抛，整条日链已经跑了一半；
                加载阶段拒绝可避免半截运行。
        """
        if value not in AVAILABLE_AT_RULES:
            raise ValueError(f"available_at_rule 必须是 {list(AVAILABLE_AT_RULES)}，收到 {value!r}")
        return value

    @field_validator("price_min")
    @classmethod
    def _price_floor_non_negative(cls, value: float | None) -> float | None:
        """拒绝负的 ``price_min``。

        Args:
            value: 收盘价下界；``None`` 表示不设下界。

        Returns:
            float | None: 通过校验的原值。

        Raises:
            ValueError: ``value < 0``。AGENTS §6.6 规定价格 ≤0 必须判 RED 并
                剔除该行，配置却允许负价的源会让质量闸永远抓不到这类坏值。
        """
        if value is not None and value < 0:
            raise ValueError(f"price_min 必须 >= 0，收到 {value}")
        return value

    @model_validator(mode="after")
    def _check_cross_field_consistency(self) -> "DatasetConfig":
        """校验主键列归属、价格区间方向与正值列归属。

        三类错误都是“配置写着能跑、实际把数据写坏”：
        ``key_columns`` 拼错（如 ``as_off``）会让合并找不到主键，退化成整表
        覆盖；``price_min > price_max`` 则让任何价格都越界，整源被判 RED；
        ``positive_columns`` 拼错或不在 ``required_columns`` 里，会让「非正值整行
        剔除」静默不生效，源把缺值写成 0 时又混回主表。

        Returns:
            DatasetConfig: 通过校验的自身。

        Raises:
            ValueError: 主键列不在 ``required_columns ∪ {"as_of"}`` 中、价格下界
                大于上界、``positive_columns`` 不在 ``required_columns`` 中或有重复。
        """
        allowed = set(self.required_columns) | {"as_of"}
        stray = [column for column in self.key_columns if column not in allowed]
        if stray:
            raise ValueError(
                f"key_columns {stray} 不在 required_columns ∪ {{as_of}} 中；"
                "主键列拼错会让合并退化为整表覆盖"
            )
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError(f"price_min={self.price_min} 不能大于 price_max={self.price_max}")
        stray_positive = [column for column in self.positive_columns if column not in set(self.required_columns)]
        if stray_positive:
            # 拼错的列名不会报错，只会让「≤0 整行剔除」静默失效——正是 D3 要堵的那类假绿。
            raise ValueError(
                f"positive_columns {stray_positive} 不在 required_columns 中；"
                "拼错的列名会让非正值剔除静默失效"
            )
        duplicated = sorted({column for column in self.positive_columns if self.positive_columns.count(column) > 1})
        if duplicated:
            raise ValueError(f"positive_columns 重复声明：{duplicated}")
        return self


class DataSourcesConfig(BaseModel):
    """``data_sources.yaml`` 的顶层结构。

    Attributes:
        schema_version: 配置 schema 版本。
        datasets: 数据集名 → ``DatasetConfig``；至少一个。

    Notes:
        ``datasets`` 为空直接 ValidationError：没有数据集的日链没有意义，
        宁可启动失败也不空跑。
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: str
    datasets: dict[str, DatasetConfig]

    @field_validator("datasets")
    @classmethod
    def _non_empty(cls, value: dict[str, DatasetConfig]) -> dict[str, DatasetConfig]:
        """拒绝空 ``datasets`` 映射。

        Args:
            value: 数据集名到配置的映射。

        Returns:
            dict[str, DatasetConfig]: 通过校验的原映射。

        Raises:
            ValueError: 映射为空。
        """
        if not value:
            raise ValueError("datasets 不能为空")
        return value


class LLMConfig(BaseModel):
    """``llm.yaml`` 的完整结构；P5 才真正调用，P1 只加载/校验/计哈希。

    Attributes:
        schema_version: 配置 schema 版本。
        provider: 推理后端，当前为 ``ollama``。
        base_url: 本地推理服务地址（仅 localhost，禁止云端）。
        model: 默认模型名，如 ``qwen3.5:9b``。
        models: 允许切换的模型清单。
        think: 是否开启思考；必须 False，否则可能长时间无正文。
        temperature: 采样温度，0–2。
        num_ctx: 上下文窗口。
        num_gpu: 卸载到 GPU 的层数；2GB 显存下为 0（CPU 推理）。
        num_thread: CPU 线程数。
        keep_alive: 模型驻留时长，如 ``45m``。
        timeout_round1_s/timeout_round2_s: 两轮辩论的单角色超时（秒）。
        total_budget_min: 委员会总预算（分钟）。
        concurrency: 并发数，强制为 1（单机只允许一个本地实例）。
        mode: ``influence/shadow/off``。
        roles: 9 个角色名，必须唯一。

    Notes:
        ``roles`` 数量被硬校验为 9，与 AGENTS §11 的角色数一致；改角色数必须
        同批改配置、文档、看板与测试（AGENTS §0.6）。安全边界类字段同样在加载期
        硬校验：``base_url`` 只允许本机回环，``think`` 必须 False，``num_gpu``
        必须 0，``mode`` 只能是 ``influence/shadow/off``（AGENTS §11.3、§15.6）。
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: str
    provider: str
    base_url: str
    model: str
    models: list[str]
    think: bool
    temperature: float = Field(ge=0, le=2)
    num_ctx: int
    num_gpu: int
    num_thread: int
    keep_alive: str
    timeout_round1_s: int
    timeout_round2_s: int
    total_budget_min: int
    concurrency: int = Field(ge=1, le=1)
    mode: str
    roles: list[str]

    @field_validator("base_url")
    @classmethod
    def _localhost_only(cls, value: str) -> str:
        """校验推理服务地址只指向本机回环。

        Args:
            value: ``base_url`` 字符串，如 ``http://localhost:11434``。

        Returns:
            str: 通过校验的原字符串（不改写、不补端口）。

        Raises:
            ValueError: 缺少 http(s) 前缀或主机名，或主机不是 ``localhost``/
                回环 IP（``127.0.0.0/8``、``::1``）。

        Notes:
            AGENTS §1.3/§15.6 禁止云端或网页 LLM：配置放行公网地址等于把事实包
            发到外部服务，必须在加载期拦住，而不是等 P5 调用时才失败。
        """
        parsed = urlparse(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host:
            raise ValueError(f"base_url 必须是 http(s)://host[:port]，收到 {value!r}")
        if host == "localhost":
            return value
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(f"base_url 只允许本机回环，收到 {host!r}") from exc
        if not address.is_loopback:
            raise ValueError(f"base_url 只允许本机回环，收到 {host!r}")
        return value

    @field_validator("think")
    @classmethod
    def _think_must_be_off(cls, value: bool) -> bool:
        """校验 ``think`` 关闭；开启后本地模型可能长时间无正文（AGENTS §11.3）。"""
        if value:
            raise ValueError("think 必须为 false")
        return value

    @field_validator("num_gpu")
    @classmethod
    def _cpu_only(cls, value: int) -> int:
        """校验 ``num_gpu=0``；2GB 显存不足以卸载模型层（AGENTS §11.3、§15.10）。"""
        if value != 0:
            raise ValueError("num_gpu 必须为 0（2GB 显存，CPU 推理）")
        return value

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        """校验运行模式只能是 ``influence/shadow/off``（AGENTS §11.10）。"""
        if value not in {"influence", "shadow", "off"}:
            raise ValueError(f"mode 只能是 influence/shadow/off，收到 {value!r}")
        return value

    @field_validator("roles")
    @classmethod
    def _nine_roles(cls, value: list[str]) -> list[str]:
        """校验角色清单恰好 9 个且不重复。

        Args:
            value: ``roles`` 列表。

        Returns:
            list[str]: 通过校验的原列表。

        Raises:
            ValueError: 数量不是 9，或存在重复角色名。

        Notes:
            9 是 AGENTS §11 的硬口径；任何增删角色都要同批更新配置、文档、
            看板与测试，不能只改 YAML。
        """
        if len(value) != 9:
            raise ValueError(f"roles 必须是 9 个，收到 {len(value)}")
        if len(set(value)) != len(value):
            raise ValueError("roles 不得重复")
        return value


class LoadedConfig:
    """已加载并校验的配置集合；日链各模块的唯一配置入口。

    Attributes:
        application: ``application.yaml`` 的强类型模型。
        data_sources: ``data_sources.yaml`` 的强类型模型。
        strategy: ``strategy.yaml`` 的原始 dict（P1 只要求含 ``weights``，
            具体键由 P2 策略层解释）。
        llm: ``llm.yaml`` 的强类型模型。
        raw_documents: 四份 YAML 的原始解析结果（文件名不含后缀 → 内容）；
            既是审计留档，也是 ``config_hash`` 的唯一输入。
        repo_root: 路径基准；测试会把它替换成临时目录，因此是可变属性。
        config_hash: ``sha256:`` 前缀的规范化哈希，覆盖四份 YAML 的全部取值
            （注释与键顺序不影响）。

    Notes:
        本类不做校验，校验在 ``load_config`` 中完成；直接构造实例只用于测试。
    """

    def __init__(
        self,
        application: ApplicationConfig,
        data_sources: DataSourcesConfig,
        strategy: dict[str, Any],
        llm: LLMConfig,
        raw_documents: dict[str, Any],
        repo_root: Path,
    ) -> None:
        """组装配置对象并计算 ``config_hash``。

        Args:
            application: 已校验的 application 配置。
            data_sources: 已校验的 data_sources 配置。
            strategy: strategy 原始字典。
            llm: 已校验的 llm 配置。
            raw_documents: 四份 YAML 的原始字典（文件名 → 内容）。
            repo_root: 仓库根目录，供 ``path`` 拼接相对路径。

        Raises:
            无：入参已由调用方校验；本方法只赋值与取哈希。

        Notes:
            ``config_hash`` 只对 ``raw_documents`` 取哈希：任何取值改动都会
            改变哈希，而注释、缩进、键顺序不会；这与“配置变更必须可审计”的
            口径一致（AGENTS §4.4）。
        """
        self.application = application
        self.data_sources = data_sources
        self.strategy = strategy
        self.llm = llm
        self.raw_documents = raw_documents
        self.repo_root = repo_root
        self.config_hash = hash_obj(raw_documents)

    def path(self, key: str) -> Path:
        """把 ``application.paths`` 中的键解析成路径。

        Args:
            key: ``PathsConfig`` 的字段名，如 ``"raw"``、``"logs"``。

        Returns:
            Path: 绝对路径原样返回；相对路径拼到 ``repo_root``。

        Raises:
            AttributeError: ``key`` 不是 ``PathsConfig`` 的字段。

        Notes:
            不创建目录、不检查存在性；需要写盘的调用方自行 ``mkdir``
            （preflight 会逐个探测可写性）。
        """
        value = getattr(self.application.paths, key)
        path = Path(value)
        return path if path.is_absolute() else self.repo_root / path

    def dataset_manual_path(self, spec: DatasetConfig) -> Path | None:
        """返回某数据集手工 CSV 的期望路径。

        Args:
            spec: 数据集配置；``manual_path`` 为空（None/""）时返回 None。

        Returns:
            Path | None: ``path("manual") / spec.manual_path``；文件不存在也照常返回。

        Raises:
            无。

        Notes:
            ``manual_path`` 若写成绝对路径，pathlib 的 ``/`` 会丢弃左侧目录，
            从而绕出 ``paths.manual``；配置应始终写相对文件名。
        """
        if not spec.manual_path:
            return None
        return self.path("manual") / spec.manual_path


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取单个 YAML 文件并要求顶层是映射。

    Args:
        path: YAML 文件路径。

    Returns:
        dict[str, Any]: 顶层键值映射；嵌套结构保持原样。

    Raises:
        FileNotFoundError: 文件不存在；消息带完整路径便于定位。
        yaml.YAMLError: YAML 语法错误（由 ``yaml.safe_load`` 抛出）。
        ValueError: 顶层不是映射（空文件解析为 None 也落在这里）。

    Notes:
        使用 ``safe_load``，禁止 YAML 自定义构造器执行任意 Python 对象。
    """
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件：{path}")
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"配置文件必须是映射：{path}")
    return document


def load_config(config_dir: Path | None = None, repo_root: Path | None = None) -> LoadedConfig:
    """加载并校验四份 YAML，返回带 ``config_hash`` 的配置对象。

    读取顺序固定为 ``CONFIG_FILES``；``application``/``data_sources``/``llm``
    走 pydantic 校验，``strategy`` 仅要求是含 ``weights`` 的映射（P2 再细化）。

    Args:
        config_dir: 配置目录；默认 ``<repo_root>/config``。
        repo_root: 仓库根；默认由 ``paths.REPO_ROOT`` 推导，测试可指向临时目录。

    Returns:
        LoadedConfig: 含四份原始文档、强类型模型、``repo_root`` 与 ``config_hash``。

    Raises:
        FileNotFoundError: 任一份 YAML 缺失。
        pydantic.ValidationError: 缺键、类型错或出现未知键。
        ValueError: strategy 不是映射或缺少 ``weights``。

    Notes:
        - 默认路径不依赖 cwd；但显式传入相对 ``config_dir`` 时，
          ``Path.resolve()`` 会按当前工作目录解析，测试请传绝对路径；
        - ``resolve()`` 只做规范化，不要求目录存在；存在性由 ``_read_yaml``
          逐个文件检查并报出具体文件名。
    """
    root = (repo_root or REPO_ROOT).resolve()
    # config_dir 显式给出时以它为准；repo_root 只影响 path() 的相对路径基准。
    directory = (config_dir or (root / "config" if config_dir is None else config_dir)).resolve()
    documents = {name: _read_yaml(directory / f"{name}.yaml") for name in CONFIG_FILES}
    application = ApplicationConfig.model_validate(documents["application"])
    data_sources = DataSourcesConfig.model_validate(documents["data_sources"])
    llm = LLMConfig.model_validate(documents["llm"])
    # strategy 保持原始 dict：P1 不解释 P2/P3 判据，避免把未实现口径固化成 schema。
    strategy = documents["strategy"]
    if not isinstance(strategy, dict) or "weights" not in strategy:
        # 缺 weights 说明拿错文件或结构被改坏；直接失败，不带着空策略启动。
        raise ValueError("strategy.yaml 缺少 weights")
    return LoadedConfig(application, data_sources, strategy, llm, documents, root)


def default_config() -> LoadedConfig:
    """用仓库自带 ``config/`` 加载默认配置（忽略 cwd）。

    Returns:
        LoadedConfig: 以模块推导的仓库根为基准的配置。

    Raises:
        同 ``load_config``：文件缺失、schema 不符或 strategy 缺 ``weights``。
    """
    return load_config(CONFIG_DIR, REPO_ROOT)
