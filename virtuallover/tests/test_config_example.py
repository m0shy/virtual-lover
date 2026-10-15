# -*- coding: utf-8 -*-
"""vl.toml.example 与配置树的对拍测试（五轮修缮 R4 任务 ①）。

防漂移三道闸：
1. 键集对拍：example 解析出的键路径集合 == VlConfig dataclass 全字段树
   （逐层按骨架走，节内多余键 / 缺键都算失败）——代码加删字段必须重新
   生成 example，否则本文件红；
2. 值对拍：example 的 TOML 值 == dump_config(build_fresh_config()) 产物
   （example 主体必须机械生成，禁手抄漂移）；
3. dump 豁免：已删除的死键（timezone / [chat].fallback_provider_ids）
   不出现在任何新配置 dump 与 example 里。

安全规约：example 只含 PENDING_SETUP_* / 号段 10000 占位（tools/init_config
安全规约同款），本测试额外断言无真实密钥形态、token 为空。
"""

from __future__ import annotations

import dataclasses
import tomllib
import typing
from pathlib import Path

from virtuallover.config import VlConfig, dump_config, load_config
from virtuallover.tools.init_config import build_fresh_config

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "deploy" / "config-samples" / "vl.toml.example"

# 已删除的死键（R3 time_anchor / R4 timezone 与 fallback_provider_ids）：
# 任何新配置 dump 与 example 都不得再出现
DELETED_KEYS = ("timezone", "fallback_provider_ids")


# ---------------------------------------------------------------------------
# 工具：dataclass 字段树 / TOML 键路径（同一骨架两种走法）
# ---------------------------------------------------------------------------

def _dataclass_paths(cls: type, prefix: tuple = ()) -> set[str]:
    """dataclass 字段树的叶子路径集合（嵌套 dataclass 递归；dict/List 是叶子）。"""
    hints = typing.get_type_hints(cls)
    paths: set[str] = set()
    for f in dataclasses.fields(cls):
        path = prefix + (f.name,)
        if dataclasses.is_dataclass(hints[f.name]):
            paths |= _dataclass_paths(hints[f.name], path)
        else:
            paths.add(".".join(path))
    return paths


def _toml_paths_by_skeleton(tbl: dict, cls: type, prefix: tuple = ()) -> set[str]:
    """按 dataclass 骨架遍历 TOML 表：缺键 / 骨架外多余键当场断言失败。

    emotion_map / keyword_groups 等动态键 dict 在 dataclass 侧是叶子，
    这里同样整值视作叶子（不展开），两侧口径一致。
    """
    hints = typing.get_type_hints(cls)
    fields = dataclasses.fields(cls)
    paths: set[str] = set()
    for f in fields:
        path = prefix + (f.name,)
        if dataclasses.is_dataclass(hints[f.name]):
            sub = tbl.get(f.name)
            assert isinstance(sub, dict), f"example 缺节 [{'.'.join(path)}]"
            paths |= _toml_paths_by_skeleton(sub, hints[f.name], path)
        else:
            assert f.name in tbl, f"example 缺键 {'.'.join(path)}"
            paths.add(".".join(path))
    extra = set(tbl) - {f.name for f in fields}
    assert not extra, f"example 含配置树之外的键：{sorted(extra)}"
    return paths


def _parse_example() -> dict:
    raw = EXAMPLE.read_bytes()
    return tomllib.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# 1) 键集对拍（防漂移主闸）
# ---------------------------------------------------------------------------

def test_example_key_set_equals_vlconfig_field_tree():
    """example 键集 == VlConfig 全字段集（任务 ① 对拍口径；防字段漂移）。"""
    assert EXAMPLE.is_file(), "deploy/config-samples/vl.toml.example 缺失"
    parsed = _parse_example()
    assert _toml_paths_by_skeleton(parsed, VlConfig) == _dataclass_paths(VlConfig)


# ---------------------------------------------------------------------------
# 2) 值对拍（example 主体必须 = dump_config(build_fresh_config()) 机械产物）
# ---------------------------------------------------------------------------

def test_example_values_match_fresh_config_dump():
    """example 解析值 == 全新安装配置树的 dump 产物（占位值，无敏感）。"""
    fresh = tomllib.loads(dump_config(build_fresh_config()))
    assert _parse_example() == fresh


def test_example_loads_through_load_config():
    """example 可直接过启动校验（token 留空合法；必填占位齐备）——
    复制为 local/config/vl.toml 即可被 vl 加载。"""
    cfg = load_config(EXAMPLE)
    assert cfg.persona.name and cfg.session_id  # 占位必填项在位
    assert cfg.bridge.token == ""


# ---------------------------------------------------------------------------
# 3) dump 豁免检查（任务 ⑤：删除的键不再出现在新配置）
# ---------------------------------------------------------------------------

def test_dump_exempts_deleted_keys():
    """新配置 dump 产物零死键：timezone / fallback_provider_ids 不复活。"""
    dumped = dump_config(VlConfig())
    for key in DELETED_KEYS:
        assert key not in dumped, f"死键 {key} 在新配置 dump 中复活"
    fresh = dump_config(build_fresh_config())
    for key in DELETED_KEYS:
        assert key not in fresh, f"死键 {key} 在全新安装配置中复活"


def test_example_exempts_deleted_keys():
    """example 文本里死键零残留（作为键出现即失败）。"""
    text = EXAMPLE.read_text(encoding="utf-8")
    for key in DELETED_KEYS:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 注释里的历史说明不算键
            assert not stripped.startswith(key + " "), \
                f"example 残留死键行：{stripped!r}"


# ---------------------------------------------------------------------------
# 4) 占位安全（无敏感值入库）
# ---------------------------------------------------------------------------

def test_example_has_no_real_secrets():
    """example 只允许占位形态：无真实密钥（sk- 前缀等）、token 为空。"""
    fresh = _parse_example()
    assert fresh["bridge"]["token"] == ""
    for section in ("chat", "embedding"):
        key = fresh[section]["api_key"]
        assert key.startswith("PENDING_SETUP_"), f"{section}.api_key 应为占位值，实为 {key[:12]}…"
        assert not key.lower().startswith("sk-")
    # 号段全部为明显占位 10000（QQ 真实号不进库）
    assert fresh["admins"] == ["10000"]
    assert fresh["whitelist"] == ["10000"]
    assert fresh["bridge"]["self_id"] == "10000"
