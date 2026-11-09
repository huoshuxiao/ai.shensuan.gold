"""仓库路径推导。所有路径从仓库根派生，不写死绝对路径。

职责：集中推导仓库根 ``REPO_ROOT`` 与配置目录 ``CONFIG_DIR``，并提供
“相对路径按指定根拼接”的工具函数，供配置、预检、存储等模块复用。

关键不变量：
- ``REPO_ROOT`` 由 ``__file__`` 的固定深度（``parents[8]``）推导，不读环境变量、
  不依赖 cwd；移动本文件必须同步改深度。
- ``CONFIG_DIR`` 固定为 ``<repo>/config``，不随 cwd 改变。
- 本模块不创建目录、不检查存在性、不解析符号链接；这些由调用方负责。

不负责：读取配置内容（见 ``config.loader``）、创建数据目录（见 CLI/preflight）、
处理用户传入的任意相对路径语义（见 ``resolve_repo_path`` 的说明）。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[8]
CONFIG_DIR = REPO_ROOT / "config"


def resolve_repo_path(value: str | Path, repo_root: Path | None = None) -> Path:
    """把相对路径补成仓库内路径；绝对路径原样返回。

    Args:
        value: 相对或绝对路径。相对路径以 ``repo_root`` 为基准。
        repo_root: 可选的根目录覆盖（测试常传临时目录）；默认使用模块级
            ``REPO_ROOT``。

    Returns:
        Path: 未做 ``resolve()`` 的路径对象。若 ``value`` 是绝对路径，
        返回其本身；否则返回 ``(repo_root or REPO_ROOT) / value``。

    Raises:
        无：不检查路径存在性，也不校验越界。

    Notes:
        - 不做 ``.resolve()``：保留调用方传入的相对片段与符号链接形态，
          便于日志/报告原样展示；
        - 不限制 ``..`` 越界：是否允许写到仓库外由调用方判断；
        - 传入绝对路径时不强制塞回仓库根，便于部署时挂载仓库外数据目录。

    单位/时区：与文件系统路径无关，不涉及时区换算。
    """
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root or REPO_ROOT) / path
