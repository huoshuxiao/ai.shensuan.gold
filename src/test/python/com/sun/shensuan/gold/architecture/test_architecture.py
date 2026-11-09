"""架构层：仓库结构硬闸的静态检查。

覆盖场景：main 不导入 test 树、main/test 目录存在、.gitignore 排除数据目录、
requirements 固定版本、非 `__init__.py` 的 docstring 齐全、四份 YAML 配置注释齐全、
测试按层分目录、每个 Python 文件在 PROJECT.md 中登记。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[8]  # 位于 architecture/ 下，比测试根目录多一层
MAIN_ROOT = REPO_ROOT / "src" / "main"
TEST_ROOT = REPO_ROOT / "src" / "test"
MAIN_PYTHON = MAIN_ROOT / "python" / "com" / "sun" / "shensuan" / "gold"
TEST_PYTHON = TEST_ROOT / "python" / "com" / "sun" / "shensuan" / "gold"
PROJECT_MD = REPO_ROOT / "PROJECT.md"
CONFIG_DIR = REPO_ROOT / "config"
CONFIG_FILES = ("application.yaml", "data_sources.yaml", "strategy.yaml", "llm.yaml")


def test_main_does_not_import_test_tree():
    """验证 src/main 下无文件导入 test 树或引用 src/test 路径；命中即违反架构硬闸。"""
    pattern = re.compile(r"(from\s+com\.sun\.shensuan\.gold\.test|import\s+com\.sun\.shensuan\.gold\.test|src[./]test)")
    offenders = []
    for path in MAIN_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_main_and_test_trees_exist():
    """验证 main 的 cli.py 与 test 的 conftest.py 存在，防止路径常量漂移后检查空转。"""
    assert (MAIN_ROOT / "python" / "com" / "sun" / "shensuan" / "gold" / "cli.py").exists()
    assert (TEST_ROOT / "python" / "com" / "sun" / "shensuan" / "gold" / "conftest.py").exists()


def test_no_live_data_in_gitignore():
    """验证 .gitignore 覆盖 data/live、raw/normalized/decisions 与 logs，避免数据或台账入库。"""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("data/live/", "data/raw/", "data/normalized/", "data/decisions/", "logs/"):
        assert pattern in text


def test_requirements_pins_versions():
    """验证 requirements.txt 每行都用 == 固定版本，且包含 akshare 与 pandas；不锁版本会破坏可复现。"""
    lines = [
        line.strip()
        for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    assert all("==" in line for line in lines)
    assert any(line.startswith("akshare==") for line in lines)
    assert any(line.startswith("pandas==") for line in lines)


def _python_files(root: Path):
    """枚举 root 下全部 Python 文件；不存在时返回空列表，避免路径漂移后静默空转。"""
    return sorted(root.rglob("*.py")) if root.exists() else []


def _walk_nodes(tree: ast.AST):
    """遍历模块内的类与函数节点（含方法），供 docstring 审计复用。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_non_init_modules_have_docstrings():
    """验证 main/test 中非 `__init__.py` 的模块、类、函数都有 docstring；缺失即违反注释硬要求。"""
    missing: list[str] = []
    checked = 0
    for path in _python_files(MAIN_PYTHON) + _python_files(TEST_PYTHON):
        if path.name == "__init__.py":
            continue
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if ast.get_docstring(tree) is None:
            missing.append(f"{path.relative_to(REPO_ROOT)}: module")
        for node in _walk_nodes(tree):
            if ast.get_docstring(node) is None:
                missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name}")
    assert checked > 0
    assert missing == []


def _previous_non_empty(lines: list[str], index: int) -> str:
    """返回 index 之前最近的非空行；没有则返回空串，供注释检查复用。"""
    return next((lines[i] for i in range(index - 1, -1, -1) if lines[i].strip()), "")


def _comment_gaps(name: str, lines: list[str]) -> list[str]:
    """检查配置文本的文件头、顶层键与数据集条目前是否有注释；返回缺失位置。"""
    gaps: list[str] = []
    first = next((line for line in lines if line.strip()), "")
    if not first.lstrip().startswith("#"):
        gaps.append(f"{name}: 缺少文件头注释")
    for index, line in enumerate(lines):
        previous = _previous_non_empty(lines, index)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:", line):
            key = line.split(":", 1)[0]
            if not (previous.lstrip().startswith("#") or re.search(r"\s#", line)):
                gaps.append(f"{name}:{index + 1} 顶层键 {key} 前缺少注释")
        if name == "data_sources.yaml" and re.match(r"^  [a-z0-9_]+:\s*$", line):
            entry = line.strip().rstrip(":")
            if not previous.lstrip().startswith("#"):
                gaps.append(f"{name}:{index + 1} 数据集 {entry} 前缺少注释")
    return gaps


def test_config_files_have_maintenance_comments():
    """验证四份 YAML 都有文件头、顶层键与数据集注释；负对照证明检查器能报出缺失。"""
    gaps: list[str] = []
    for name in CONFIG_FILES:
        path = CONFIG_DIR / name
        assert path.exists(), f"缺少配置文件：{name}"
        gaps.extend(_comment_gaps(name, path.read_text(encoding="utf-8").splitlines()))
    assert gaps == []
    assert _comment_gaps("synthetic.yaml", ["# 文件头", "alpha: 1", "", "beta: 2"]) == [
        "synthetic.yaml:4 顶层键 beta 前缺少注释"
    ]
    assert _comment_gaps("data_sources.yaml", ["# 文件头", "datasets:", "  alpha:", "    enabled: true"]) == [
        "data_sources.yaml:3 数据集 alpha 前缺少注释"
    ]


def test_tests_are_layered():
    """验证测试根目录无平铺 `test_*.py`，且 unit/contract/integration/architecture 四层都有测试。"""
    assert sorted(p.name for p in TEST_PYTHON.glob("test_*.py")) == []
    for layer in ("unit", "contract", "integration", "architecture"):
        layer_dir = TEST_PYTHON / layer
        assert layer_dir.is_dir(), f"缺少测试层目录：{layer}"
        assert list(layer_dir.rglob("test_*.py")), f"测试层为空：{layer}"


def test_all_python_files_listed_in_project_md():
    """验证每个非 `__init__.py` 的 Python 文件都在 PROJECT.md 中登记，防止结构文档过期。"""
    text = PROJECT_MD.read_text(encoding="utf-8")
    unlisted: list[str] = []
    checked = 0
    for python_root in (MAIN_ROOT / "python", TEST_ROOT / "python"):
        for path in _python_files(python_root):
            if path.name == "__init__.py":
                continue
            checked += 1
            relative = path.relative_to(python_root).as_posix()
            if relative not in text:
                unlisted.append(relative)
    assert checked > 0
    assert unlisted == []
