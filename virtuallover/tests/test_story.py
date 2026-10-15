# -*- coding: utf-8 -*-
"""soul/story.py 契约测试（故事模式：人设硬约束的临时豁免开关）。

覆盖点：
- 触发正则 STORY_TRIGGERS：讲(个|一个)?(睡前)?故事 / 编个故事 / 来个故事 /
  一起编 / 角色扮演 各形态命中；普通聊天不误触；
- escape_block：豁免块含关键约束字样（动作心理 / 1-3 句 / 350 字 / 2-3 条）；
- 窗口生命周期：activate 开窗 → is_active True → consume_turn 递减 →
  归零自动关窗；缺状态 consume 幂等回 0；
- 2 小时绝对过期兜底：窗口内有效、满 2h 一律失效（turns_left 有剩也关窗）；
- 状态容错：文件损坏视为无状态（不抛异常）；落盘位置 = state_dir/
  story_state.json。

时钟一律注入固定 now；状态目录用 tmp_path；不写任何真实人设名/QQ 号。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.soul import story  # noqa: E402

# 固定时钟基准（epoch 秒；任意确定值，不依赖真实时间）
T0 = 1765000000.0


# ---------------------------------------------------------------------------
# 触发正则
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "给我讲个故事吧",
    "讲个故事",
    "讲一个故事",
    "讲个睡前故事",
    "讲睡前故事",
    "编个故事给我听",
    "来个故事",
    "我们一起编个故事好不好",
    "一起编",
    "来玩角色扮演吧",
])
def test_triggered_hits(text):
    """各种故事意图变体命中触发正则。"""
    assert story.triggered(text) is True
    assert story.STORY_TRIGGERS.search(text) is not None


@pytest.mark.parametrize("text", [
    "今天天气真好",
    "这个故事会今晚开播",   # 只有「故事」二字、无讲/编/来动作
    "你喜欢看书吗",
    "晚安",
])
def test_triggered_misses(text):
    """普通聊天不误触（不吞话）。"""
    assert story.triggered(text) is False


# ---------------------------------------------------------------------------
# escape_block：豁免块内容
# ---------------------------------------------------------------------------


def test_escape_block_contains_relief_clauses():
    """豁免块点名豁免的两条硬约束 + 350 字上限 + 分条要求。"""
    block = story.escape_block()
    assert "【故事模式·本轮临时许可】" in block
    assert "禁止描写动作心理" in block
    assert "1-3 句" in block
    assert "350" in block
    assert "2-3 条消息" in block
    assert "回到日常模式" in block


def test_escape_block_is_stable():
    """豁免块是纯文本常量：多次调用结果一致（无随机/时钟成分）。"""
    assert story.escape_block() == story.escape_block()


# ---------------------------------------------------------------------------
# 窗口生命周期：activate / consume_turn / is_active
# ---------------------------------------------------------------------------


def test_activate_opens_window_and_persists(tmp_path):
    """activate 开默认 2 轮窗口并落盘 state_dir/story_state.json。"""
    state = story.activate(tmp_path, now=T0)
    assert state["turns_left"] == story.STORY_TURNS_DEFAULT == 2
    assert state["activated_at"] == T0
    on_disk = json.loads(
        (tmp_path / "story_state.json").read_text(encoding="utf-8"))
    assert on_disk["turns_left"] == 2
    assert story.is_active(tmp_path, T0 + 1) is True


def test_activate_custom_turns_and_clamp(tmp_path):
    """自定义轮数生效；非正数钳到 1（activate 的语义就是开窗）。"""
    assert story.activate(tmp_path, turns=5, now=T0)["turns_left"] == 5
    assert story.activate(tmp_path, turns=0, now=T0)["turns_left"] == 1


def test_consume_turn_decrements_until_closed(tmp_path):
    """讲一轮消耗一轮：2→1（还开着）→0（自动回日常）。"""
    story.activate(tmp_path, turns=2, now=T0)
    assert story.consume_turn(tmp_path) == 1
    assert story.is_active(tmp_path, T0 + 60) is True
    assert story.consume_turn(tmp_path) == 0
    assert story.is_active(tmp_path, T0 + 60) is False
    on_disk = json.loads(
        (tmp_path / "story_state.json").read_text(encoding="utf-8"))
    assert on_disk["turns_left"] == 0


def test_consume_turn_without_state_is_zero(tmp_path):
    """无状态时 consume 幂等回 0，不建档。"""
    assert story.consume_turn(tmp_path) == 0
    assert not (tmp_path / "story_state.json").exists()


def test_reactivate_resets_window(tmp_path):
    """重复 activate 即重开窗口（轮数与时钟都重置）。"""
    story.activate(tmp_path, turns=2, now=T0)
    story.consume_turn(tmp_path)
    story.consume_turn(tmp_path)
    assert story.is_active(tmp_path, T0 + 60) is False
    story.activate(tmp_path, turns=2, now=T0 + 120)
    assert story.is_active(tmp_path, T0 + 121) is True
    assert story.consume_turn(tmp_path) == 1


# ---------------------------------------------------------------------------
# 2 小时绝对过期兜底
# ---------------------------------------------------------------------------


def test_absolute_expiry_boundary(tmp_path):
    """激活满 2 小时一律失效（turns_left 有剩也关窗）；差 1 秒仍有效。"""
    story.activate(tmp_path, turns=2, now=T0)
    assert story.is_active(tmp_path, T0 + story.STORY_EXPIRY_SECONDS - 1) is True
    assert story.is_active(tmp_path, T0 + story.STORY_EXPIRY_SECONDS) is False


def test_expiry_not_extended_by_consume(tmp_path):
    """过期基准是 activated_at：消耗轮数不续期（绝对过期，防永久豁免）。"""
    story.activate(tmp_path, turns=2, now=T0)
    story.consume_turn(tmp_path)          # T0+0 消耗
    late = T0 + story.STORY_EXPIRY_SECONDS + 10
    assert story.is_active(tmp_path, late) is False


# ---------------------------------------------------------------------------
# 状态容错与路径口径
# ---------------------------------------------------------------------------


def test_corrupt_state_is_treated_as_absent(tmp_path):
    """状态文件损坏（非法 JSON）：is_active False、consume 回 0、不抛异常。"""
    (tmp_path / "story_state.json").write_text("{broken", encoding="utf-8")
    assert story.is_active(tmp_path, T0) is False
    assert story.consume_turn(tmp_path) == 0
    assert story.load_state(tmp_path) == {}


def test_is_active_without_state(tmp_path):
    """空目录（从未激活）：一律 False。"""
    assert story.is_active(tmp_path, T0) is False


def test_state_file_named_story_state_json(tmp_path):
    """状态文件固定名 story_state.json（vl_data 目录口径，与 proactive 同居）。"""
    assert story.state_path(tmp_path).name == "story_state.json"
    assert story.DEFAULT_STATE_DIR == "local/vl_data"
