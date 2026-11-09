"""把 src/main/python 挂到 sys.path，供直接运行的入口脚本使用。

职责：在缺失时把 ``src/main/python`` 插入 ``sys.path`` 首位，使以文件路径直接
运行的入口（如 ``python src/main/python/com/sun/shensuan/gold/cli.py``）也能用
``com.sun.shensuan.gold.*`` 平铺导入，而不必先安装包或依赖相对导入。

关键不变量：
- 只做路径注入：不读配置、不建目录、不写文件，导入本模块不产生任何文件副作用。
- 两个根目录都由 ``__file__`` 推导，与当前工作目录（cwd）无关。
- 幂等：源码根已在 ``sys.path`` 时不重复插入，避免路径列表无限增长。

不负责：安装依赖、设置环境变量、创建 ``data/`` 等目录（由 CLI/preflight 负责）。

注意：本文件位于包根 ``gold/`` 下，``parents[4]``/``parents[7]`` 与文件深度绑定；
移动本文件必须同步改索引，否则路径会静默指错（AGENTS §15.4）。
"""
from __future__ import annotations

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[4]
REPO_ROOT = Path(__file__).resolve().parents[7]


def bootstrap() -> Path:
    """确保源码根位于 ``sys.path`` 中，并返回仓库根目录。

    导入路径：``SOURCE_ROOT`` = ``src/main/python``（``__file__`` 的 ``parents[4]``）；
    ``REPO_ROOT`` = 仓库根（``parents[7]``）。两者都在模块导入时按绝对路径算好，
    因此调用本函数不依赖 cwd。

    Returns:
        Path: 仓库根目录，供调用方定位 ``config/``、``data/``、``logs/`` 等相对路径。

    Raises:
        无：纯内存操作，不会抛业务异常。

    Notes:
        仅在缺失时插入索引 0，优先于 site-packages，避免同名包遮蔽仓库代码；
        若源码根已存在（即使在列表后面）不会重排。比较用 ``str(SOURCE_ROOT)``，
        因为 ``sys.path`` 存放的是字符串而非 Path。
    """
    source = str(SOURCE_ROOT)
    if source not in sys.path:
        sys.path.insert(0, source)
    return REPO_ROOT
