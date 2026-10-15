# -*- coding: utf-8 -*-
"""soul/story.py —— 故事模式（人设硬约束的临时豁免开关，独立小模块）。

背景（FUSION_SPEC 人设硬约束）：日常人格带「禁止描写动作心理 / 1-3 句 /
四种标点」三条硬约束——讲故事在结构上被这三条封死。本模块给故事模式一个
**本轮临时许可**的接缝：

1. **触发识别**：:data:`STORY_TRIGGERS` 正则命中「讲(个|一个)?(睡前)?故事 /
   编个故事 / 来个故事 / 一起编 / 角色扮演」类意图（管线侧消费，命中后
   activate 开窗）；
2. **豁免块**：:func:`escape_block` 返回注入本轮 prompt 的临时许可文本
   （豁免动作心理与句数限制、350 字上限、第一人称、2-3 条消息、讲完回日常）；
3. **窗口状态**：``<state_dir>/story_state.json``（默认 local/vl_data/ 下，
   与 proactive_state.json 同目录）记 ``turns_left``（还剩几轮）与
   ``activated_at``（激活时刻）——管线每讲一轮 :func:`consume_turn` 递减，
   归零自动回日常；另加 **2 小时绝对过期兜底**（is_active 判定，防状态
   残留导致永久豁免）。

依赖注入：状态目录与时钟全部由调用方传入（测试用 tmp_path + 固定 now），
本模块只依赖标准库，不发任何请求。
状态读写沿用 soul/anniversary.py 的容错模式（损坏/缺失视为无状态，不抛异常）
与原子落盘（tmp + os.replace）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("virtuallover.soul.story")

# 状态落盘目录（相对运行根，与 proactive_state.json / anniversary.json 同目录）；
# 实际文件由调用方传 state_dir，本模块在其下固定文件名落盘。
DEFAULT_STATE_DIR = "local/vl_data"
STATE_FILENAME = "story_state.json"

# 故事窗口默认轮数（/story 激活一次可讲几轮）
STORY_TURNS_DEFAULT = 2

# 绝对过期兜底：激活满 2 小时一律视为过期（防状态残留导致永久豁免）
STORY_EXPIRY_SECONDS = 2 * 3600.0

# 故事触发正则（管线侧消费）：讲(个|一个)?(睡前)?故事 / 编个故事 / 来个故事 /
# 一起编 / 角色扮演
STORY_TRIGGERS = re.compile(r"讲(个|一个)?(睡前)?故事|编个故事|来个故事|一起编|角色扮演")

# 本轮临时许可块（escape_block 原文；产品默认文案，不含任何人设名）
_ESCAPE_BLOCK_TEXT = (
    "【故事模式·本轮临时许可】本次回复暂时豁免「禁止描写动作心理」与"
    "「1-3 句」限制：写一段不超过 350 字的第一人称小故事，分 2-3 条消息发出，"
    "保持她的口吻与两人关系，不要旁白腔；讲完自动回到日常模式。"
)


# ---------------------------------------------------------------------------
# 状态读写（anniversary.py 同款容错与原子落盘）
# ---------------------------------------------------------------------------


def state_path(state_dir: "str | Path") -> Path:
    """状态文件路径：``<state_dir>/story_state.json``。"""
    return Path(state_dir) / STATE_FILENAME


def load_state(state_dir: "str | Path") -> dict:
    """读故事窗口状态；缺失/损坏返回空 dict（不阻塞、不抛异常）。

    形态::

        {"turns_left": 2,          # 还剩几轮故事（0=窗口已用完）
         "activated_at": 1765000000.0}   # 激活时刻（epoch 秒，过期兜底基准）
    """
    try:
        raw = state_path(state_dir).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_dir: "str | Path", state: dict) -> None:
    """原子落盘（tmp + os.replace）；OSError 只告警不抛（状态丢失宁关窗）。"""
    path = state_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("故事模式状态写入失败: %s", exc)


# ---------------------------------------------------------------------------
# 触发与豁免块
# ---------------------------------------------------------------------------


def triggered(text: str) -> bool:
    """用户消息是否命中故事触发正则（管线侧据此 activate 开窗）。"""
    return bool(STORY_TRIGGERS.search(str(text or "")))


def escape_block() -> str:
    """本轮临时许可块：注入 prompt 豁免人设硬约束（动作心理 / 1-3 句）。"""
    return _ESCAPE_BLOCK_TEXT


# ---------------------------------------------------------------------------
# 窗口生命周期：activate / consume_turn / is_active
# ---------------------------------------------------------------------------


def activate(
    state_dir: "str | Path",
    turns: int = STORY_TURNS_DEFAULT,
    *,
    now: "float | None" = None,
) -> dict:
    """激活故事模式：写入 turns_left 与 activated_at 并落盘；返回新状态。

    - turns 钳到 ≥1（0/负数按 1 计——activate 的语义就是「开窗」）；
    - 重复 activate 即重开窗口（轮数与时钟都重置）。
    """
    now_ts = time.time() if now is None else float(now)
    state = {
        "turns_left": max(1, int(turns)),
        "activated_at": now_ts,
    }
    save_state(state_dir, state)
    logger.info("[story] 故事模式已激活（turns_left=%d）", state["turns_left"])
    return state


def consume_turn(state_dir: "str | Path") -> int:
    """消耗一轮：turns_left 递减并落盘；返回递减后的剩余轮数。

    - 无状态/已归零：返回 0（幂等，不建档）；
    - 递减到 0 后状态保留（is_active 据此关窗），下轮 activate 重开。
    """
    state = load_state(state_dir)
    if not state:
        return 0
    try:
        turns_left = max(0, int(state.get("turns_left") or 0) - 1)
    except (TypeError, ValueError):
        turns_left = 0
    state["turns_left"] = turns_left
    save_state(state_dir, state)
    logger.info("[story] 故事轮消耗：turns_left=%d", turns_left)
    return turns_left


def is_active(state_dir: "str | Path", now: "float | None" = None) -> bool:
    """故事窗口是否有效：turns_left > 0 且未过 2 小时绝对过期兜底。

    - 无状态 / turns_left≤0 → False（讲完自动回日常模式）；
    - activated_at 缺失/非法 → 视为无有效窗口（防御面）；
    - 激活满 STORY_EXPIRY_SECONDS（2h）→ 一律 False（绝对过期兜底，
      即便 turns_left 还有剩）。
    """
    state = load_state(state_dir)
    if not state:
        return False
    try:
        turns_left = int(state.get("turns_left") or 0)
    except (TypeError, ValueError):
        return False
    if turns_left <= 0:
        return False
    try:
        activated_at = float(state.get("activated_at") or 0)
    except (TypeError, ValueError):
        return False
    if activated_at <= 0:
        return False
    now_ts = time.time() if now is None else float(now)
    return (now_ts - activated_at) < STORY_EXPIRY_SECONDS
