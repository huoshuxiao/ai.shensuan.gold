"""单元层：config.loader 的配置加载、schema 校验与配置哈希。

覆盖场景：仓库配置关键口径、哈希稳定性与变更敏感性、缺键/未知键即失败、
配置四件套声明。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from com.sun.shensuan.gold.config.loader import CONFIG_FILES, load_config


def test_load_repo_config(repo_config):
    """验证仓库配置关键口径：20 个数据集、3 个核心源、9 个 LLM 角色、模型清单、GPR 修订标记与汇率取价列。"""
    assert len(repo_config.data_sources.datasets) == 20
    assert {name for name, spec in repo_config.data_sources.datasets.items() if spec.core} == {
        "etf_518880",
        "sge_au9999",
        "shfe_au0",
    }
    assert len(repo_config.llm.roles) == 9
    assert repo_config.llm.model == "qwen2.5:7b"
    assert repo_config.llm.models == ["qwen2.5:7b"]
    assert repo_config.application.logging.retention_days == 7
    assert repo_config.config_hash.startswith("sha256:")
    assert repo_config.application.paths.live == "data/live"
    gpr = repo_config.data_sources.datasets["gpr"]
    assert gpr.revised_only is True
    assert gpr.available_at_rule == "fetched_at"
    usdcny = repo_config.data_sources.datasets["usdcny"]
    assert usdcny.options["price_column"] == "中行折算价"
    # 长历史中间价是独立数据集：与折算价两套报价，禁止在同一条特征里拼接。
    usdcny_mid = repo_config.data_sources.datasets["usdcny_mid"]
    assert usdcny_mid.source == "safe.rmb_mid"
    assert usdcny_mid.options == {}
    assert usdcny_mid.history_start == "1994-01-01"
    # 中间价 09:15 发布后当天固定（T-023 同日兜底条款）：允许当天转正，
    # 但观察期若发现当日行缺失，应连同注释降回 same_day_1530。
    assert usdcny_mid.available_at_rule == "same_day_0915"
    # 价格下限只能是正数：AGENTS §6.6 规定价格 ≤0 整行剔除，配置写负数等于
    # 声明一条代码永远不会执行的规则（曾出现 wti_cl: -100.0 的死档）。
    assert repo_config.data_sources.datasets["wti_cl"].price_min > 0


def _copied_config(tmp_path: Path) -> Path:
    """测试辅助：把仓库 config/ 复制到临时目录，返回副本目录供就地改坏。"""
    source = Path(__file__).resolve().parents[9] / "config"  # 位于 unit/config/ 下，比测试根目录多两层
    target = tmp_path / "config"
    shutil.copytree(source, target)
    return target


def _rewrite_dataset(target: Path, name: str, **changes) -> None:
    """测试辅助：改坏 data_sources.yaml 中某个数据集的指定字段并写回。"""
    path = target / "data_sources.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["datasets"][name].update(changes)
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")


def test_negative_price_floor_fails(tmp_path: Path):
    """负向验证 price_min<0 在加载期即失败：负价行会被 normalize 的 close<=0 先剔除，
    允许负下限的配置只会误导读者以为负价被放行（wti_cl 曾写 -100.0）。"""
    target = _copied_config(tmp_path)
    _rewrite_dataset(target, "wti_cl", price_min=-100.0)
    with pytest.raises(ValidationError):
        load_config(target, repo_root=tmp_path)


def test_inverted_price_range_fails(tmp_path: Path):
    """负向验证 price_min > price_max 加载即失败：区间倒置会把每一行都判成越界。"""
    target = _copied_config(tmp_path)
    _rewrite_dataset(target, "etf_518880", price_min=10.0, price_max=1.0)
    with pytest.raises(ValidationError):
        load_config(target, repo_root=tmp_path)


def test_key_column_typo_fails(tmp_path: Path):
    """负向验证 key_columns 拼错（as_off）加载即失败。

    主键列拼错时 store.merge 取不到该列、全部行落进同一个键，历史会被静默
    合并成 1 行；必须在加载期拦住，而不是等数据丢了才发现。
    """
    target = _copied_config(tmp_path)
    _rewrite_dataset(target, "etf_518880", key_columns=["as_off"])
    with pytest.raises(ValidationError):
        load_config(target, repo_root=tmp_path)


def test_unknown_available_at_rule_fails(tmp_path: Path):
    """负向验证 available_at_rule 写成未知值加载即失败：非法规则此前要等到
    生成 PIT 时才抛 ValueError，整条日链已经跑了一半。"""
    target = _copied_config(tmp_path)
    _rewrite_dataset(target, "etf_518880", available_at_rule="next_day_0630")
    with pytest.raises(ValidationError):
        load_config(target, repo_root=tmp_path)


def _rewrite_llm(target: Path, **changes) -> None:
    """测试辅助：改 llm.yaml 的顶层字段并写回。"""
    path = target / "llm.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://api.example.com/v1"},
        {"base_url": "http://192.168.1.10:11434"},
        {"think": True},
        {"num_gpu": 1},
        {"mode": "whatever"},
    ],
    ids=["cloud-url", "lan-url", "think-on", "gpu-offload", "unknown-mode"],
)
def test_llm_unsafe_settings_fail(tmp_path: Path, changes):
    """负向验证云端地址、开启思考、GPU 卸载与未知模式在加载期即失败。

    AGENTS §11.3/§15.6/§1.3：LLM 只允许本机回环、``think=false``、``num_gpu=0``，
    运行模式仅 ``influence/shadow/off``；这些值若放行，P5 会把模型请求发到公网
    或长时间无正文，属于安全边界问题，不能等到调用时才失败。
    """
    target = _copied_config(tmp_path)
    _rewrite_llm(target, **changes)
    with pytest.raises(ValidationError):
        load_config(target, repo_root=tmp_path)


def test_config_hash_is_stable(repo_config):
    """验证同一配置重复加载得到相同 config_hash，保证决策可复现。"""
    again = load_config()
    assert repo_config.config_hash == again.config_hash


def test_config_hash_changes_with_value(tmp_path: Path):
    """验证改动 strategy.yaml 权重会改变 config_hash；哈希不变则配置篡改无法被审计发现。"""
    source = Path(__file__).resolve().parents[9] / "config"  # 位于 unit/config/ 下，比测试根目录多两层
    target = tmp_path / "config"
    shutil.copytree(source, target)
    document = yaml.safe_load((target / "strategy.yaml").read_text(encoding="utf-8"))
    document["weights"]["trend"] = 0.31
    (target / "strategy.yaml").write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    changed = load_config(target, repo_root=tmp_path)
    baseline = load_config()
    assert changed.config_hash != baseline.config_hash


def test_missing_required_key_fails(tmp_path: Path):
    """负向验证 application.yaml 缺 timezone 时加载失败（ValidationError），禁止用默认值蒙混。"""
    source = Path(__file__).resolve().parents[9] / "config"  # 位于 unit/config/ 下，比测试根目录多两层
    target = tmp_path / "config"
    shutil.copytree(source, target)
    document = yaml.safe_load((target / "application.yaml").read_text(encoding="utf-8"))
    del document["timezone"]
    (target / "application.yaml").write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(target, repo_root=tmp_path)


def test_unknown_dataset_key_fails(tmp_path: Path):
    """负向验证数据集下出现未知键（typo_key）时报 ValidationError，防止拼写错误被静默忽略。"""
    source = Path(__file__).resolve().parents[9] / "config"  # 位于 unit/config/ 下，比测试根目录多两层
    target = tmp_path / "config"
    shutil.copytree(source, target)
    document = yaml.safe_load((target / "data_sources.yaml").read_text(encoding="utf-8"))
    document["datasets"]["etf_518880"]["typo_key"] = 1
    (target / "data_sources.yaml").write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(target, repo_root=tmp_path)


def test_config_files_declared():
    """验证 CONFIG_FILES 四件套清单固定，避免漏加载某份配置导致哈希与运行口径不一致。"""
    assert CONFIG_FILES == ("application", "data_sources", "strategy", "llm")
