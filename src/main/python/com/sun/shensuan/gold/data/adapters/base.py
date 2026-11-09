"""适配器注册表与上下文。

职责：把「数据集配置里的 adapter 名称」映射到具体抓取函数，并为适配器提供
统一运行上下文（手工 CSV 目录、请求超时、预留选项）。

关键不变量：
- 注册表是进程级全局字典；同一名称只能注册一次，重复注册立即抛
  ``ValueError``，避免两个实现争抢同名数据集而静默覆盖。
- 适配器签名固定为 ``(DatasetConfig, start, end, AdapterContext) -> FetchResult``；
  失败必须抛异常，不得返回空表冒充成功（空表由规范化层统一标
  ``EMPTY_SOURCE`` / RED）。
- ``available_adapters()`` 始终返回排序后的名称，输出稳定、便于 diff。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from ...config.loader import DatasetConfig
from ..models import FetchResult


@dataclass
class AdapterContext:
    """适配器运行时上下文。

    Attributes:
        manual_dir: 手工 CSV 所在目录（默认 ``data/manual``），适配器只读。
        request_timeout_s: 单次下载的默认超时秒数，来自
            ``application.yaml`` 的 ``ingest.request_timeout_s``；具体适配器可用
            ``spec.options["download_timeout_s"]`` 覆盖。
        options: 预留的进程级扩展参数（P1 未使用）；禁止放密钥或个人数据。
    """

    manual_dir: Path
    request_timeout_s: int = 600
    options: dict = field(default_factory=dict)


# 适配器函数类型：(数据集配置, 起始日, 结束日, 上下文) -> 抓取结果
AdapterFn = Callable[[DatasetConfig, date | None, date | None, AdapterContext], FetchResult]
_REGISTRY: dict[str, AdapterFn] = {}


def register(name: str):
    """把适配器函数按名称登记到全局注册表（装饰器用法）。

    Args:
        name: 数据集配置中 ``adapter`` 字段使用的名称，如 ``"etf"``、``"gpr"``。

    Returns:
        装饰器：原样返回被装饰函数，因此注册不改变调用方式。

    Raises:
        ValueError: 同名适配器已注册；在导入期立即失败，便于尽早发现重复定义。
    """

    def decorator(function: AdapterFn) -> AdapterFn:
        """登记 ``function`` 并原样返回；重复名称由外层 ``register`` 统一报错。"""
        if name in _REGISTRY:
            raise ValueError(f"适配器重复注册：{name}")
        _REGISTRY[name] = function
        return function

    return decorator


def get_adapter(name: str) -> AdapterFn:
    """按名称取出适配器函数。

    Args:
        name: 已注册的适配器名称。

    Returns:
        对应的适配器函数。

    Raises:
        KeyError: 名称未注册；错误信息附带当前可用名称，便于定位配置拼写错误。
    """
    if name not in _REGISTRY:
        raise KeyError(f"未注册的适配器：{name}；可用：{sorted(_REGISTRY)}")
    return _REGISTRY[name]


def available_adapters() -> list[str]:
    """返回全部已注册适配器名称（排序后），用于 CLI 展示与配置核对。"""
    return sorted(_REGISTRY)
