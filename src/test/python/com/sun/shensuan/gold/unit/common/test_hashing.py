"""单元层：common.hashing 的规范化 JSON 与哈希工具。

覆盖场景：键排序输出、哈希与键序无关、numpy/NaN 值可序列化。
"""
from __future__ import annotations

import numpy as np

from com.sun.shensuan.gold.common.hashing import canonical_json, hash_obj


def test_canonical_json_sorts_keys():
    """验证 canonical_json 按键排序输出紧凑 JSON；哈希链要求同一字典换序后仍得到相同字节串。"""
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_hash_is_order_independent():
    """验证 hash_obj 对键序不敏感：仅顺序不同的两个字典必须得到同一哈希，否则账本链会无故断裂。"""
    assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})


def test_numpy_and_nan_are_jsonable():
    """验证 numpy 标量转为原生 JSON 数值、NaN 序列化为 null；否则事实包与决策哈希会因序列化异常而失败。"""
    payload = {"value": np.float64("nan"), "count": np.int64(3), "ok": np.float64(1.5)}
    text = canonical_json(payload)
    assert text == '{"count":3,"ok":1.5,"value":null}'
