"""pytest 共享夹具：全部离线，不访问网络。

覆盖层：跨层共用（unit/contract/integration 均可能引用）；提供仓库配置、
临时沙箱配置、合成 ETF 行情、固定抓取时间与 FetchResult 构造辅助。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(REPO_ROOT / "src" / "main" / "python"))

from com.sun.shensuan.gold.common.timeutil import SHANGHAI, now_shanghai  # noqa: E402
from com.sun.shensuan.gold.config.loader import LoadedConfig, load_config  # noqa: E402
from com.sun.shensuan.gold.data.manual import ensure_templates  # noqa: E402
from com.sun.shensuan.gold.data.models import FetchResult  # noqa: E402


@pytest.fixture
def repo_config() -> LoadedConfig:
    """函数级夹具：返回按仓库真实 config/ 加载的 LoadedConfig，供只读断言使用。"""
    return load_config()


@pytest.fixture
def tmp_config(tmp_path: Path, repo_config: LoadedConfig) -> LoadedConfig:
    """函数级夹具：返回指向 tmp_path 的隔离配置，预建数据/日志/报告目录并生成手工模板，避免写入真实仓库。"""
    config = load_config()
    config.repo_root = tmp_path
    for key in ("data", "raw", "normalized", "manual", "decisions", "paper", "live", "logs", "report"):
        config.path(key).mkdir(parents=True, exist_ok=True)
    ensure_templates(config.path("manual"))
    return config


@pytest.fixture
def etf_frame() -> pd.DataFrame:
    """函数级夹具：返回 5 行合成 ETF 日线（OHLCV + amount），作为不依赖网络的价格样本。"""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
            ),
            "open": [4.80, 4.82, 4.81, 4.85, 4.88],
            "high": [4.86, 4.85, 4.84, 4.90, 4.92],
            "low": [4.78, 4.79, 4.78, 4.83, 4.86],
            "close": [4.84, 4.81, 4.83, 4.89, 4.90],
            "volume": [100000, 120000, 90000, 130000, 110000],
            "amount": [484000.0, 578400.0, 434700.0, 635700.0, 539000.0],
        }
    )


@pytest.fixture
def fetched_at() -> datetime:
    """函数级夹具：返回固定的 Asia/Shanghai 抓取时刻，保证 PIT 断言可复现。"""
    return datetime(2024, 1, 8, 16, 0, tzinfo=SHANGHAI)


def make_fetch(frame: pd.DataFrame, source: str = "test.source", fetched_at: datetime | None = None) -> FetchResult:
    """测试辅助：把给定 DataFrame 包成 FetchResult；未传 fetched_at 时用当前上海时间。"""
    return FetchResult(
        frame=frame,
        source=source,
        fetched_at=fetched_at or now_shanghai(),
    )
