"""数据适配器包。

导入本包即完成注册；适配器按需在函数内部 import akshare，离线测试不触发网络。
"""
from . import geo, macro, market  # noqa: F401
from .base import AdapterContext, available_adapters, get_adapter, register

__all__ = ["AdapterContext", "available_adapters", "get_adapter", "register"]
