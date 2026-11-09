"""阶段验收包：把 AGENTS §18 的阶段放行判据编成可执行程序。

本包只做「判据计算与汇总」，不做抓取、不写业务数据、不生成决策卡。
P1 的判据实现见 `com.sun.shensuan.gold.acceptance.p1`。
"""
from __future__ import annotations

from .p1 import CheckResult, run_p1_acceptance

__all__ = ["CheckResult", "run_p1_acceptance"]
