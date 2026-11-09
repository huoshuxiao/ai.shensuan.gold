"""项目异常类型。失败必须显式，禁止静默吞掉。"""
from __future__ import annotations


class GoldError(Exception):
    """所有项目异常的基类。"""


class ConfigError(GoldError):
    """配置缺失、类型错误或哈希不符。"""


class AdapterError(GoldError):
    """数据源适配器失败。"""


class DegradedSourceError(AdapterError):
    """数据源可用但质量降级（限流、空响应、单位异常等）。"""


class ManualDataError(GoldError):
    """手工 CSV 缺字段或取值非法。"""


class QualityError(GoldError):
    """质量闸判定为 RED。"""
