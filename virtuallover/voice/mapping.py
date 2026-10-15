# -*- coding: utf-8 -*-
"""情绪→音色情绪映射（VOICE_SPEC §1）：14 态表（原 7 态+丰富化 7 态）+ 注册表回退链打点。

职责边界（终裁口径）：
- 输入是 MoodEngine 的**有效状态**（``soul.mood.effective_mood`` 的返回值——
  τ=4h 衰减 + <0.15 回落 calm，与文字注入同源）。本模块**不解析注入字符串**，
  也不再看强度：终裁「intensity 仅经衰减语义参与」，衰减已在 getter 内完成；
- 输出是最终写进 /tts ``character_emotion`` 参数的音色注册情绪；
- 回退链：映射值 ∉ 注册表 → 配置默认情绪 → "default"，每跳以
  ``degraded_emotion`` 前缀打 warning（控制台/日志可 grep）。

为什么必须客户端校验情绪（GSVI 怪癖，源码核实）：
- voice-engine/Inference/src/tts_backend.py 的 string 参数统一
  ``unquote(...).lower()``（:125），**未注册情绪不会报错**——静默按默认情绪出
  200+wav。映射表写错一个字，联动就无声失效，故在发送前用 /character_list
  注册表（``voice.gsvi`` 缓存）做成员校验并回退。

本模块只依赖标准库（7 态键名以字符串常量内置，测试侧与 soul.mood.VALID_MOODS
对账），避免 config→mapping→soul 的任何导入环。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Collection, Optional

__all__ = [
    "DEFAULT_EMOTION_MAP", "MOOD_KEYS", "FALLBACK_KEY", "DEFAULT_DEFAULT_EMOTION",
    "EmotionResolution", "resolve_emotion", "emotion_for_snapshot",
    "mood_speed", "speed_for_snapshot",
]

logger = logging.getLogger("virtuallover.voice.mapping")

# ---------------------------------------------------------------------------
# 默认情绪表（VOICE_SPEC §1 终裁 + 情绪丰富化 7 态；vl.toml [tts].emotion_map
# 可逐行覆盖）
#
# 值全部取自用户自备多情绪音色 Huohua 的实测注册表
# （voice-engine/trained/character_info.json）：
#   default / 平常的 / 慢速病娇 / 傻白甜 / 平静的 / 疯批 / 聊天
#
# 原 7 态（五轮修缮第三轮 R3 按参谋听感实测重排三行；审计实证：calm 是常态
# 15/16，旧映射把最常出现的心情唱成了冷御姐调）：
#   calm    「平静的」→「聊天」（平静的 实听偏冷御姐，与她的软绵人设错配）
#   shy     「聊天」→「傻白甜」（害羞时用聊天调听感平淡，傻白甜更贴）
#   excited 「疯批」→「傻白甜」（兴奋≠发疯，疯批调只在 angry 保留）
# sad→慢速病娇（终裁：多数意见）。
#
# 情绪丰富化 7 态（soul.mood.MOOD_ENRICHED，与该模块同批并入默认表——
# 与运行态 vl.toml 一致，不再走 fallback 键统一降级）：
#   wronged(委屈)/lost(失落)→慢速病娇；jealous(吃醋)→疯批；
#   expect(期待)/sweet(甜蜜)/tsundere(傲娇)→傻白甜；secure(安心)→聊天。
# fallback 键承接表外情绪（键序固定最后一行）。
# ---------------------------------------------------------------------------

DEFAULT_EMOTION_MAP = {
    "happy": "傻白甜",
    "excited": "傻白甜",
    "angry": "疯批",
    "sad": "慢速病娇",
    "lonely": "平常的",
    "shy": "傻白甜",
    "calm": "聊天",
    "wronged": "慢速病娇",
    "lost": "慢速病娇",
    "jealous": "疯批",
    "expect": "傻白甜",
    "sweet": "傻白甜",
    "tsundere": "傻白甜",
    "secure": "聊天",
    "fallback": "聊天",
}

# 表内 14 个真实情绪键（= soul.mood.ALL_MOODS，测试侧对账；
# fallback 是伪键，不属于 MoodEngine 枚举）
MOOD_KEYS = frozenset(k for k in DEFAULT_EMOTION_MAP if k != "fallback")
FALLBACK_KEY = "fallback"

# 配置默认情绪（回退链第二跳；与 fallback 键默认值一致，vl.toml 可改）
DEFAULT_DEFAULT_EMOTION = "聊天"


@dataclass(frozen=True)
class EmotionResolution:
    """一次情绪解析的完整结果（供日志/控制台/测试消费）。"""

    emotion: str          # 最终写入 /tts 的情绪参数
    source: str           # map | map_fallback | config_default | default
    hops: "tuple[str, ...]" = ()   # 回退链每一跳的人类可读说明
    degraded: bool = False         # 是否发生过回退（≥1 跳）


def _registry_lookup(registry: "Optional[Collection[str]]") -> "Optional[set[str]]":
    """注册表情绪集合（小写化——服务端对 string 参数统一 .lower()）。

    registry 为 None 表示注册表不可用（未拉取/拉取失败）：此时**不做**成员校验，
    按映射值直接发送（GSVI 对未知情绪静默用默认情绪，行为可预期）。
    """
    if registry is None:
        return None
    return {str(e).strip().lower() for e in registry if str(e).strip()}


def resolve_emotion(
    mood: str,
    *,
    emotion_map: "Optional[dict]" = None,
    default_emotion: str = DEFAULT_DEFAULT_EMOTION,
    registry: "Optional[Collection[str]]" = None,
    character: str = "",
) -> EmotionResolution:
    """把一个有效情绪解析为音色注册情绪（回退链 + 每跳 degraded_emotion 打点）。"""
    mood = str(mood or "").strip() or "calm"
    table = dict(DEFAULT_EMOTION_MAP if emotion_map is None else emotion_map)
    registry_set = _registry_lookup(registry)
    hops: "list[str]" = []

    # 第 0 跳：表内直查；表外情绪走 fallback 键（同样是回退，打点）
    if mood in table and str(table[mood]).strip():
        emotion = str(table[mood]).strip()
        source = "map"
    else:
        emotion = str(table.get(FALLBACK_KEY) or DEFAULT_EMOTION_MAP[FALLBACK_KEY]).strip()
        source = "map_fallback"
        hops.append(f"情绪 {mood!r} 不在映射表，走 fallback 键 → {emotion!r}")
        logger.warning("degraded_emotion: %s", hops[-1])

    # 注册表不可用：不再校验，直接用映射值（GSVI 静默兜底行为）
    if registry_set is None or emotion.lower() in registry_set:
        return EmotionResolution(emotion, source, tuple(hops), bool(hops))

    # 第 1 跳：映射值 ∉ 注册表 → 配置默认情绪
    hops.append(
        f"映射值 {emotion!r} 不在角色 {character!r} 的注册情绪里，回退配置默认情绪"
    )
    logger.warning("degraded_emotion: %s", hops[-1])
    configured = str(default_emotion or "").strip()
    if configured and configured.lower() in registry_set:
        return EmotionResolution(configured, "config_default", tuple(hops), True)

    # 第 2 跳：配置默认情绪也 ∉ 注册表（或未配置）→ "default"（GSVI 一定注册）
    hops.append(
        f"配置默认情绪 {configured!r} 同样不在注册表，最终回退 \"default\""
    )
    logger.warning("degraded_emotion: %s", hops[-1])
    return EmotionResolution("default", "default", tuple(hops), True)


def emotion_for_snapshot(
    snapshot: "Optional[tuple[str, float]]",
    **kwargs,
) -> EmotionResolution:
    """从有效状态快照 (mood, intensity) 解析情绪。

    snapshot 为 None（无情绪上下文，如主动消息装配线没有 persona 快照）时按
    calm 处理——MoodState 的中性地面状态。intensity 不在此消费：终裁
    「intensity 仅经衰减语义参与」，衰减已在 ``soul.mood.effective_mood`` 完成。
    """
    mood = "calm" if snapshot is None else str(snapshot[0])
    return resolve_emotion(mood, **kwargs)


# ---------------------------------------------------------------------------
# 情绪→语速档位（GSVI /tts speed_factor，服务端非经典模式原生支持：
# voice-engine/app.py:76,124 → Inference/src/load_infer_info.py:208,267）
#
# 档位 = (基准偏移, 强度斜率)：speed = round(1.0 + 基准 + 斜率 × intensity, 2)。
# 任一强度下档位值都落在任务裁定的区间内——
#   happy/excited → 1.10~1.15（开心语速略快）；sad/lonely → 0.90~0.95
#   （低落语速略慢）；calm/其余（含表外情绪）→ 恒 1.0（原速）。
# 与情绪映射表同源键；强度经 ``soul.mood.effective_mood`` 衰减后的快照值
# 线性放大偏移（强度 0 → 档位近原速端、1 → 档位极值端），越界强度收敛到 [0,1]。
# ---------------------------------------------------------------------------

_SPEED_TIERS = {
    "happy": (0.10, 0.05),
    "excited": (0.10, 0.05),
    "sad": (-0.05, -0.05),
    "lonely": (-0.05, -0.05),
}

# 快照缺强度时的默认值（与 voice.engine._normalize_snapshot 的字符串快照一致）
_DEFAULT_INTENSITY = 0.3


def mood_speed(mood: str, intensity: float = 0.5) -> float:
    """情绪→语速倍率（/tts ``speed_factor``；1.0=原速，>1 加快、<1 放慢）。

    - happy/excited → 1.10~1.15、sad/lonely → 0.90~0.95、calm/其余 → 1.0；
    - intensity（越界收敛到 [0,1]）线性放大偏移：0 → 档位近原速端、1 → 极值端；
    - 结果 round 两位——缓存键（voice.cache.make_key 的 speed 维度）与 /tts
      参数同用该值，等价类稳定；
    - 表外情绪/空串一律 1.0（语速是增益项，绝不因未知情绪改变行为）。
    """
    base, slope = _SPEED_TIERS.get(str(mood or "").strip().lower(), (0.0, 0.0))
    try:
        level = min(1.0, max(0.0, float(intensity)))
    except (TypeError, ValueError):
        level = _DEFAULT_INTENSITY
    return round(1.0 + base + slope * level, 2)


def speed_for_snapshot(snapshot: Any) -> "Optional[float]":
    """从有效状态快照产出语速倍率（manager/engine 透传链的取数口）。

    与 ``emotion_for_snapshot`` 同一快照口径（同一处一并产出情绪与语速）：
    - None（无情绪上下文）→ None（调用方不传 speed_factor，服务端默认 1.0）；
    - (mood, intensity) 元组 → mood_speed(mood, intensity)；
    - 7 态键/中文名等字符串 → mood_speed(mood, 0.3)（缺强度默认，
      与 voice.engine._normalize_snapshot 同值）。
    """
    if snapshot is None:
        return None
    if isinstance(snapshot, (tuple, list)) and len(snapshot) >= 1:
        mood = str(snapshot[0] or "").strip() or "calm"
        intensity = snapshot[1] if len(snapshot) > 1 else _DEFAULT_INTENSITY
        try:
            return mood_speed(mood, float(intensity))
        except (TypeError, ValueError):
            return mood_speed(mood, _DEFAULT_INTENSITY)
    return mood_speed(str(snapshot).strip())
