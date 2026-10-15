# -*- coding: utf-8 -*-
"""情绪与好感度引擎（SPEC §8.1）—— vl_mood_engine 原样并入（port-of-own）。

来源与改动声明（docs/provenance.md 口径）：
- 本文件由 plugins/vl_mood_engine 的 engine.py + storage.py **逐行并入**，属本项目
  自有代码（port-of-own），除下列包路径适配外**逻辑零改动**：
  1. 两文件合并为单文件，storage 部分原 ``from .engine import MoodState`` 改为
     直接引用同文件的 MoodState；
  2. logger 名从 "vl_mood_engine.engine"/"vl_mood_engine.storage" 改为
     "virtuallover.soul.mood"；
  3. 模块 docstring 合并重写（说明并入来源），代码主体未动。
- 在此之上新增两部分（均为胶水，不含新业务规则）：
  * ``load_mood_config``：从现网活配置 local/astrbot_data/config/
    vl_mood_engine_config.json 读取 keyword_groups / daily_bonus_enabled /
    inject_enabled（实测该文件带 BOM，按 utf-8-sig 读取）；
  * ``MoodEngine``：把旧 main.py（AstrBot 胶水层）中与框架无关的状态推进行为
    原样落地为可注入时钟的普通类——危机兜底先于一切分类、沉默补算（满 24h）、
    每日首条（连击加成开关）、关键词分类（rude>praise>sweet、否定窗口）、
    普通消息保底，锁内读改写、锁外落盘（asyncio.to_thread）。

情绪丰富化（本次改动，接在原七枚举之上）：
- 新增 7 种人设情绪：wronged(委屈)/jealous(吃醋)/expect(期待)/lost(失落)/
  sweet(甜蜜)/secure(安心)/tsundere(傲娇)，入 MOOD_ENRICHED；与原七枚举的
  并集 ALL_MOODS 是 from_dict / 标签解析 / EVENT_MOOD_LLM 的合法名判定集。
  VALID_MOODS 保持原 7 态不动；voice/mapping 默认情绪表已同批扩到
  ALL_MOODS 全 14 态直配（fallback 只承接表外情绪，对账契约见
  test_voice_mapping）；
- 新增 EVENT_MOOD_LLM：回复自评标签（``<<mood:名字:强度>>``）经模块级
  ``parse_mood_tag`` 解析后驱动心情（只改心情与强度，不动好感度）；
  出站剥标签由 soul.textclean.strip_mood_tags 兜底；
- 小转移规则（放现有规则旁）：普通消息文本命中第三方女性信号 → jealous、
  expect 触发关键词 → expect；angry 状态 30 分钟内出现甜话 → sweet（和好）；
- 重逢彩蛋：沉默结算（EVENT_SILENCE_HOURS）记录到 MoodState.last_silence_*，
  结算时长 ≥24h 且结算发生在 10 分钟内时，注入片段开头加一句重逢文案。

规格要点（与 engine.py 头注一致，此处不再重复数值）：
- affection ∈ [0, 100] 默认 50；mood 七枚举；事件加分与防刷分、分档、
  注入渲染 τ=4h 指数衰减、CRISIS_PATTERN 危机兜底不得丢（SPEC §8.1）。

本模块只依赖标准库；时间一律可注入（now 参数），保证宿主机 pytest 直测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("virtuallover.soul.mood")

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

AFFECTION_MIN = 0.0
AFFECTION_MAX = 100.0
INTENSITY_MIN = 0.0
INTENSITY_MAX = 1.0

# 每日首条的固定加分与连击加成
DAILY_FIRST_BASE = 1.5
DAILY_BONUS_PER_STREAK = 0.2
DAILY_BONUS_STREAK_CAP = 5

# 各事件类型常量
EVENT_MESSAGE = "message"
EVENT_KEYWORD_SWEET = "keyword_sweet"
EVENT_KEYWORD_PRAISE = "keyword_praise"
EVENT_KEYWORD_RUDE = "keyword_rude"
EVENT_DAILY_FIRST = "daily_first"
EVENT_SILENCE_HOURS = "silence_hours"
EVENT_MOOD_LLM = "mood_llm"

VALID_EVENT_TYPES = frozenset(
    {
        EVENT_MESSAGE,
        EVENT_KEYWORD_SWEET,
        EVENT_KEYWORD_PRAISE,
        EVENT_KEYWORD_RUDE,
        EVENT_DAILY_FIRST,
        EVENT_SILENCE_HOURS,
        EVENT_MOOD_LLM,
    }
)

# 心情枚举与中文映射
MOOD_HAPPY = "happy"
MOOD_SAD = "sad"
MOOD_ANGRY = "angry"
MOOD_SHY = "shy"
MOOD_LONELY = "lonely"
MOOD_CALM = "calm"
MOOD_EXCITED = "excited"

# 原 7 态：情绪引擎的基础枚举集（勿扩——新增情绪入 MOOD_ENRICHED，两者并集
# ALL_MOODS 与 voice/mapping 默认情绪表的键集对账，见模块头注「情绪丰富化」一节）
VALID_MOODS = frozenset(
    {MOOD_HAPPY, MOOD_SAD, MOOD_ANGRY, MOOD_SHY, MOOD_LONELY, MOOD_CALM, MOOD_EXCITED}
)

# 人设要求的 7 种新情绪（情绪丰富化）：LLM 自评标签与小转移规则的合法目标
MOOD_WRONGED = "wronged"
MOOD_JEALOUS = "jealous"
MOOD_EXPECT = "expect"
MOOD_LOST = "lost"
MOOD_SWEET = "sweet"
MOOD_SECURE = "secure"
MOOD_TSUNDERE = "tsundere"

MOOD_ENRICHED = frozenset(
    {
        MOOD_WRONGED,
        MOOD_JEALOUS,
        MOOD_EXPECT,
        MOOD_LOST,
        MOOD_SWEET,
        MOOD_SECURE,
        MOOD_TSUNDERE,
    }
)

# 情绪引擎可处于的全部心情（from_dict / parse_mood_tag / EVENT_MOOD_LLM 的判定集）
ALL_MOODS = VALID_MOODS | MOOD_ENRICHED

MOOD_ZH = {
    MOOD_HAPPY: "开心",
    MOOD_SAD: "难过",
    MOOD_ANGRY: "生气",
    MOOD_SHY: "害羞",
    MOOD_LONELY: "孤单",
    MOOD_CALM: "平静",
    MOOD_EXCITED: "兴奋",
    # 情绪丰富化新增（人设要求的委屈/傲娇/吃醋/期待/失落/甜蜜/安心）
    MOOD_WRONGED: "委屈",
    MOOD_JEALOUS: "吃醋",
    MOOD_EXPECT: "期待",
    MOOD_LOST: "失落",
    MOOD_SWEET: "甜蜜",
    MOOD_SECURE: "安心",
    MOOD_TSUNDERE: "傲娇",
}

# 新情绪的注入片段内心独白（一两句她的内心状态；括号体例与重逢彩蛋一致）。
# 只收新 7 情绪：原 7 态保持 R3 纯数据行片段不变（LLM 对旧态已稳定）。
MOOD_INNER_VOICE = {
    MOOD_WRONGED: "（明明不是我的错……他怎么可以这样说我，好委屈）",
    MOOD_JEALOUS: "（那个女生到底是谁啦……他又不会喜欢别人，……大概吧）",
    MOOD_EXPECT: "（已经忍不住开始期待了……说好了的哦，不许放我鸽子）",
    MOOD_LOST: "（哦，这样啊……没事，反正也不是第一次了）",
    MOOD_SWEET: "（刚刚那句话甜得我脸都红了……心里像被填得满满的）",
    MOOD_SECURE: "（他在，就很安心……好像什么都不用怕了）",
    MOOD_TSUNDERE: "（才、才不是特意在等他！只是刚好有空而已……哼）",
}

# 好感度分档阈值
LEVEL_COLD = "cold"        # < 30
LEVEL_NEUTRAL = "neutral"  # 30 ~ 60
LEVEL_CLOSE = "close"      # 60 ~ 85
LEVEL_DEVOTED = "devoted"  # > 85

LEVEL_ZH = {
    LEVEL_COLD: "冷淡",
    LEVEL_NEUTRAL: "平和",
    LEVEL_CLOSE: "亲密",
    LEVEL_DEVOTED: "挚爱",
}

# 心情强度分档（用于渲染"平静/中等/强烈"）
INTENSITY_CALM_MAX = 0.35
INTENSITY_MEDIUM_MAX = 0.7

# 沉默扣分：每满 24 小时扣 0.5
SILENCE_STEP_HOURS = 24.0
SILENCE_STEP_PENALTY = 0.5

# 关键词事件唤起后的"强烈情绪"判定阈值：达到则激活 angry/excited 枚举
AROUSAL_STRONG_THRESHOLD = 0.6

# 关键词防刷分：同日同类别计数达到该值后，该类事件好感增减按 0 处理
KEYWORD_DAILY_CAP = 5

# 注入片段的心情时间衰减：τ = 4 小时；有效强度低于该阈值时按 calm 渲染
MOOD_DECAY_TAU_SECONDS = 14400.0
MOOD_FADE_THRESHOLD = 0.15

# ---------------------------------------------------------------------------
# 关键词分类与防呆常量（纯逻辑）
# ---------------------------------------------------------------------------

# 默认关键词正则（与旧栈 _conf_schema.json 的 default 保持一致，配置可扩展覆盖）。
# rude 只保留指向性词：去掉裸『烦』，『滚』收紧为『滚开』，减少日常误伤。
DEFAULT_SWEET_PATTERN = r"亲爱的|宝贝|喜欢你"
DEFAULT_PRAISE_PATTERN = r"好看|厉害|想你|爱你"
DEFAULT_RUDE_PATTERN = r"闭嘴|滚开|你真烦|你好烦|别烦我|烦死了"

# 否定窗口：命中位置前 6 个字符内含任一否定标记，则该命中作废
NEGATION_WINDOW = 6
NEGATION_MARKS = ("不", "别", "没", "无", "一点")

# 危机倾诉兜底：命中则完全不推进情绪状态（不落井下石）
CRISIS_PATTERN = r"自杀|自残|不想活|轻生|想死|伤害自己"

# 正则防呆：模式长度上限；'a'*60 样例实测 search 耗时上限（秒）
PATTERN_LENGTH_LIMIT = 200
PATTERN_PROBE_INPUT = "a" * 60
PATTERN_PROBE_TIME_LIMIT = 0.5
# 灾难性回溯的正则在中途长度即会爆炸：分段探针 + 累计超限早停，
# 避免为"测量耗时"而完整跑完一个指数级 search 把加载进程卡死。
PATTERN_PROBE_STEP = 4

# 沉默补算阈值：距上次互动满 24 小时才追加沉默事件（旧 main.py 同名常量）
SILENCE_CHECK_THRESHOLD_HOURS = 24.0

# R3 情绪灵敏度：普通消息的单次情绪回落幅度（原实现硬编码 0.1，审计实证
# calm 是常态 15/16——情绪被普通消息过快浇灭）。0.04 ≈ 25 条普通消息的半衰期；
# 可配（vl.toml [mood].calm_down_per_message），MoodEngine 经 apply_event 透传。
DEFAULT_CALM_DOWN_PER_MESSAGE = 0.04

# ---------------------------------------------------------------------------
# 情绪丰富化：自评标签、小转移规则与重逢彩蛋常量（纯逻辑）
# ---------------------------------------------------------------------------

# 自评标签强度缺省值（<<mood:名字>> 不带强度时按 0.5 记）
DEFAULT_MOOD_TAG_INTENSITY = 0.5

# 自评标签的**严格解析形态**：心情名 + 可选数值强度（大小写不敏感、容忍空白）。
# 剥除用宽松形态在 soul.textclean.MOOD_TAG_RE（残缺标签也剥，两处注释互指）。
_MOOD_TAG_STRICT_RE = re.compile(
    r"<<\s*mood\s*:\s*([A-Za-z_]+)\s*(?::\s*([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s*)?>>",
    re.IGNORECASE,
)

# 第三方女性信号（保守词表）：普通消息命中 → jealous（吃醋）
THIRD_PARTY_FEMALE_PATTERN = r"那个女生|别的女生|前女友|小姐姐|别的女人"
_THIRD_PARTY_FEMALE_RE = re.compile(THIRD_PARTY_FEMALE_PATTERN)

# expect 触发关键词：普通消息命中 → expect（期待）
EXPECT_PATTERN = r"好期待|等着你|到时候"
_EXPECT_RE = re.compile(EXPECT_PATTERN)

# 吃醋后和好窗口：angry 状态下该时长内出现甜话关键词 → sweet（和好）
RECONCILE_WINDOW_SECONDS = 1800.0

# 重逢彩蛋：沉默结算时长满一天才算「久别」；结算发生在该窗口内才渲染重逢文案
REUNION_MIN_HOURS = 24.0
REUNION_WINDOW_SECONDS = 600.0


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class MoodState:
    """单个用户的心情与好感度状态。"""

    affection: float = 50.0       # 好感度 0-100
    mood: str = MOOD_CALM         # 当前心情
    mood_intensity: float = 0.3   # 心情强度 0-1
    last_interaction: float = 0.0  # 上次互动时间戳（秒）
    daily_streak: int = 0         # 连续互动天数
    total_messages: int = 0       # 累计互动消息数（仅普通消息计数）
    updated_at: float = 0.0       # 状态最后更新时间戳（秒）
    # 关键词防刷分计数（sweet/praise/rude 各自的当日已计数），counters_date 为
    # 计数所属自然日（YYYY-MM-DD，本地时区，与 last_interaction 同日判定一致）
    sweet_today: int = 0
    praise_today: int = 0
    rude_today: int = 0
    counters_date: str = ""
    # 最近一次沉默结算（重逢彩蛋用）：结算的小时数与发生时间戳；
    # 0.0 表示从未结算过。旧格式 mood_states.json 缺这两键时按从未结算加载。
    last_silence_hours: float = 0.0
    last_silence_at: float = 0.0

    def to_dict(self) -> dict:
        """序列化为可写入 JSON 的字典。"""
        return {
            "affection": float(self.affection),
            "mood": self.mood,
            "mood_intensity": float(self.mood_intensity),
            "last_interaction": float(self.last_interaction),
            "daily_streak": int(self.daily_streak),
            "total_messages": int(self.total_messages),
            "updated_at": float(self.updated_at),
            "sweet_today": int(self.sweet_today),
            "praise_today": int(self.praise_today),
            "rude_today": int(self.rude_today),
            "counters_date": str(self.counters_date),
            "last_silence_hours": float(self.last_silence_hours),
            "last_silence_at": float(self.last_silence_at),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "MoodState":
        """从字典恢复状态；字段缺失或非法时回退默认值，保证加载不崩溃。

        mood 按 ALL_MOODS（原 7 态 + 情绪丰富化 7 态）判定，非法名回退 calm；
        旧格式数据（无 last_silence_* 键）加载为从未结算，无需迁移。
        """
        data = data or {}
        mood = data.get("mood", MOOD_CALM)
        if mood not in ALL_MOODS:
            mood = MOOD_CALM
        try:
            affection = float(data.get("affection", 50.0))
        except (TypeError, ValueError):
            affection = 50.0
        try:
            intensity = float(data.get("mood_intensity", 0.3))
        except (TypeError, ValueError):
            intensity = 0.3
        try:
            streak = int(data.get("daily_streak", 0))
        except (TypeError, ValueError):
            streak = 0
        try:
            total = int(data.get("total_messages", 0))
        except (TypeError, ValueError):
            total = 0
        try:
            last_interaction = float(data.get("last_interaction", 0.0))
        except (TypeError, ValueError):
            last_interaction = 0.0
        try:
            updated_at = float(data.get("updated_at", 0.0))
        except (TypeError, ValueError):
            updated_at = 0.0
        try:
            sweet_today = int(data.get("sweet_today", 0))
        except (TypeError, ValueError):
            sweet_today = 0
        try:
            praise_today = int(data.get("praise_today", 0))
        except (TypeError, ValueError):
            praise_today = 0
        try:
            rude_today = int(data.get("rude_today", 0))
        except (TypeError, ValueError):
            rude_today = 0
        counters_date = str(data.get("counters_date", "") or "")
        try:
            last_silence_hours = float(data.get("last_silence_hours", 0.0))
        except (TypeError, ValueError):
            last_silence_hours = 0.0
        try:
            last_silence_at = float(data.get("last_silence_at", 0.0))
        except (TypeError, ValueError):
            last_silence_at = 0.0
        return cls(
            affection=clamp(affection, AFFECTION_MIN, AFFECTION_MAX),
            mood=mood,
            mood_intensity=clamp(intensity, INTENSITY_MIN, INTENSITY_MAX),
            last_interaction=max(0.0, last_interaction),
            daily_streak=max(0, streak),
            total_messages=max(0, total),
            updated_at=max(0.0, updated_at),
            sweet_today=max(0, sweet_today),
            praise_today=max(0, praise_today),
            rude_today=max(0, rude_today),
            counters_date=counters_date,
            last_silence_hours=max(0.0, last_silence_hours),
            last_silence_at=max(0.0, last_silence_at),
        )


@dataclass
class Event:
    """作用于状态机的事件。

    type: 见 EVENT_* 常量；
    value: 可选数值（silence_hours 事件表示沉默小时数；mood_llm 事件表示强度 0-1）；
    timestamp: 事件发生时间戳，None 时由 apply_event 取当前时间（便于测试注入固定时间）；
    mood: 可选心情名，仅 mood_llm 事件使用（LLM 自评标签解析出的目标心情）；
    text: 可选消息原文，仅 message 事件使用（小转移规则据此识别
          第三方女性信号 → jealous、expect 触发关键词 → expect；
          None 时保持旧行为不转移）。
    """

    type: str
    value: float | None = None
    timestamp: float | None = None
    mood: str | None = None
    text: str | None = None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def clamp(value: float, lo: float = AFFECTION_MIN, hi: float = AFFECTION_MAX) -> float:
    """把数值限制在 [lo, hi] 区间。"""
    return max(lo, min(hi, value))


def affection_level(affection: float) -> str:
    """好感度分档：cold(<30) / neutral(30-60) / close(60-85) / devoted(>85)。"""
    if affection > 85:
        return LEVEL_DEVOTED
    if affection >= 60:
        return LEVEL_CLOSE
    if affection >= 30:
        return LEVEL_NEUTRAL
    return LEVEL_COLD


def intensity_label(intensity: float) -> str:
    """心情强度的中文分档。"""
    if intensity >= INTENSITY_MEDIUM_MAX:
        return "强烈"
    if intensity >= INTENSITY_CALM_MAX:
        return "中等"
    return "平静"


def is_new_day(last_ts: float, now_ts: float) -> bool:
    """判断 now_ts 是否处于 last_ts 之后的另一个自然日（本地时区）。

    last_ts <= 0 表示从未互动过，视为新的一天。
    """
    if last_ts <= 0:
        return True
    last_date = datetime.fromtimestamp(last_ts).date()
    now_date = datetime.fromtimestamp(now_ts).date()
    return last_date != now_date


def _day_diff(from_ts: float, to_ts: float) -> int:
    """to_ts 与 from_ts 相差的自然日数（本地时区）。"""
    from_date = datetime.fromtimestamp(from_ts).date()
    to_date = datetime.fromtimestamp(to_ts).date()
    return (to_date - from_date).days


def _arouse(state: MoodState, delta: float) -> None:
    """事件唤起情绪：提高心情强度（就地修改，仅供 apply_event 内部使用）。"""
    state.mood_intensity = clamp(state.mood_intensity + delta, INTENSITY_MIN, INTENSITY_MAX)


def _calm_down(state: MoodState, delta: float) -> None:
    """情绪回落：降低心情强度。"""
    state.mood_intensity = clamp(state.mood_intensity - delta, INTENSITY_MIN, INTENSITY_MAX)


def _roll_daily_counters(state: MoodState, ts: float) -> None:
    """关键词事件的当日计数跨自然日归零（就地修改，仅供 apply_event 内部使用）。

    counters_date 与 last_interaction 采用同一套本地自然日判定；只在 keyword 类
    事件中调用，普通消息/每日首条/沉默不触碰计数。
    """
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    if state.counters_date != date_str:
        state.sweet_today = 0
        state.praise_today = 0
        state.rude_today = 0
        state.counters_date = date_str


# ---------------------------------------------------------------------------
# 回复自评标签解析（<<mood:名字[:强度]>>）
# ---------------------------------------------------------------------------


def parse_mood_tag(text: str) -> "tuple[str, str, float] | None":
    """解析回复文本中的 LLM 自评情绪标签 ``<<mood:名字[:强度]>>``。

    - 标签可出现在文本尾部或中间，大小写不敏感，容忍标签内空白；
    - 强度缺省 DEFAULT_MOOD_TAG_INTENSITY(0.5)，越界按 [0,1] 收敛；
    - 返回 (剥离**所有**标签后的文本, 最后一个**合法**标签的心情名, 强度)；
    - 心情名按 ALL_MOODS 判定：一个合法标签都没有（含只有非法名）时返回 None；
    - 剥离只针对标签本体，残留空白不在此收敛（出站兜底交给
      soul.textclean.strip_mood_tags——宽松形态连残缺标签也剥）。
    """
    raw = str(text or "")
    matches = list(_MOOD_TAG_STRICT_RE.finditer(raw))
    if not matches:
        return None
    cleaned = _MOOD_TAG_STRICT_RE.sub("", raw).strip()
    for match in reversed(matches):  # 多个标签取最后一个合法的
        name = match.group(1).strip().lower()
        if name not in ALL_MOODS:
            continue
        if match.group(2) is None:
            intensity = DEFAULT_MOOD_TAG_INTENSITY
        else:
            try:
                intensity = float(match.group(2))
            except ValueError:  # pragma: no cover — 正则已保证数值形态
                intensity = DEFAULT_MOOD_TAG_INTENSITY
        return cleaned, name, clamp(intensity, INTENSITY_MIN, INTENSITY_MAX)
    return None


# ---------------------------------------------------------------------------
# 状态机核心
# ---------------------------------------------------------------------------


def apply_event(
    state: MoodState,
    event: Event,
    *,
    daily_bonus_enabled: bool = True,
    now: float | None = None,
    calm_down_per_message: float = DEFAULT_CALM_DOWN_PER_MESSAGE,
) -> MoodState:
    """应用单个事件，返回新的 MoodState（不修改入参）。

    daily_bonus_enabled: 每日首条连击加成开关（关闭时 daily_first 只加基础 1.5）；
    now: 备用当前时间，仅在 event.timestamp 为 None 时使用。
    calm_down_per_message: 普通消息的单次情绪回落幅度（R3 可配——原硬编码 0.1
    两次普通消息就浇灭一次关键词唤起；默认改 0.04，配置见 vl.toml [mood]）。

    情绪丰富化的事件：
    - mood_llm：Event(mood="wronged", value=0.5)——LLM 回复自评标签驱动心情，
      只改 mood 与强度（value 缺省时保留原强度），不动好感度；非法心情名整体
      忽略（不抛错，仅走公共收尾刷新时间戳）。标签解析见 :func:`parse_mood_tag`；
    - message 带 text 时套用小转移规则：第三方女性信号 → jealous、
      expect 触发关键词 → expect（jealous 优先）。
    """
    if event.type not in VALID_EVENT_TYPES:
        raise ValueError(f"未知事件类型: {event.type!r}")

    if event.timestamp is not None:
        ts = float(event.timestamp)
    elif now is not None:
        ts = float(now)
    else:
        ts = time.time()

    s = replace(state)  # 拷贝，保持纯函数式

    if event.type == EVENT_MESSAGE:
        # 普通消息：微量好感提升，情绪缓慢回归平静（唯一计入 total_messages 的事件）
        s.affection += 0.05
        _calm_down(s, clamp(float(calm_down_per_message), 0.0, 1.0))
        s.total_messages += 1
        # 小转移规则（事件带 text 才判定；第三方女性信号优先于 expect 关键词）
        text = event.text or ""
        if text:
            if _THIRD_PARTY_FEMALE_RE.search(text):
                s.mood = MOOD_JEALOUS
                _arouse(s, 0.3)
            elif _EXPECT_RE.search(text):
                s.mood = MOOD_EXPECT
                _arouse(s, 0.2)

    elif event.type == EVENT_KEYWORD_SWEET:
        # 甜言称呼：+1，心情变开心；当日第 KEYWORD_DAILY_CAP+1 条起好感不再增加。
        # 例外（吃醋后和好）：angry 状态下 RECONCILE_WINDOW_SECONDS 内的甜话 → sweet
        _roll_daily_counters(s, ts)
        if s.sweet_today < KEYWORD_DAILY_CAP:
            s.affection += 1.0
        s.sweet_today += 1
        if (
            s.mood == MOOD_ANGRY
            and s.updated_at > 0
            and 0.0 <= ts - s.updated_at <= RECONCILE_WINDOW_SECONDS
        ):
            s.mood = MOOD_SWEET
        else:
            s.mood = MOOD_HAPPY
        _arouse(s, 0.3)

    elif event.type == EVENT_KEYWORD_PRAISE:
        # 夸奖：+2；唤醒后强度 >= 0.6 → excited（激活死枚举），
        # 否则按既有规则：关系已亲密(>=60)时害羞，否则开心。
        _roll_daily_counters(s, ts)
        if s.praise_today < KEYWORD_DAILY_CAP:
            s.affection += 2.0
        s.praise_today += 1
        _arouse(s, 0.3)
        if s.mood_intensity >= AROUSAL_STRONG_THRESHOLD:
            s.mood = MOOD_EXCITED
        else:
            s.mood = MOOD_SHY if s.affection >= 60 else MOOD_HAPPY

    elif event.type == EVENT_KEYWORD_RUDE:
        # 负面词：-2；唤醒后强度 >= 0.6 → angry（激活死枚举），否则难过。
        _roll_daily_counters(s, ts)
        if s.rude_today < KEYWORD_DAILY_CAP:
            s.affection -= 2.0
        s.rude_today += 1
        _arouse(s, 0.4)
        s.mood = MOOD_ANGRY if s.mood_intensity >= AROUSAL_STRONG_THRESHOLD else MOOD_SAD

    elif event.type == EVENT_DAILY_FIRST:
        # 每日首条：基础 1.5 + 连击加成（加成基于互动前的连击数，封顶 5 档）
        bonus = min(s.daily_streak, DAILY_BONUS_STREAK_CAP) * DAILY_BONUS_PER_STREAK
        if daily_bonus_enabled:
            s.affection += DAILY_FIRST_BASE + bonus
        else:
            s.affection += DAILY_FIRST_BASE
        # 连击更新：昨天有互动则 +1；跨多天（>=2 天）则清零重计；首次互动记 1 天
        if s.last_interaction <= 0:
            s.daily_streak = 1
        elif _day_diff(s.last_interaction, ts) == 1:
            s.daily_streak += 1
        else:
            s.daily_streak = 1
        _arouse(s, 0.2)

    elif event.type == EVENT_SILENCE_HOURS:
        # 沉默：value 为小时数，每满 24h 扣 0.5，心情变孤单（不足 24h 只改心情不扣分）
        hours = max(0.0, float(event.value) if event.value is not None else 0.0)
        penalty = int(hours // SILENCE_STEP_HOURS) * SILENCE_STEP_PENALTY
        s.affection -= penalty
        s.mood = MOOD_LONELY
        _arouse(s, 0.3)
        # 记录最近一次结算（重逢彩蛋：片段据此判断是否加重逢文案）
        s.last_silence_hours = hours
        s.last_silence_at = ts
        # 沉默是回顾性修正，不算互动：不计消息数、不推进 last_interaction

    elif event.type == EVENT_MOOD_LLM:
        # LLM 回复自评标签：只改心情与强度，不动好感度（她自己的情绪声明，
        # 不是互动加减分）。value 为强度 0-1，缺省保留原强度；非法心情名整体
        # 忽略（不抛错——标签解析层已尽量过滤，这里是最后一道防线）。
        name = (event.mood or "").strip().lower()
        if name in ALL_MOODS:
            s.mood = name
            if event.value is not None:
                try:
                    s.mood_intensity = clamp(
                        float(event.value), INTENSITY_MIN, INTENSITY_MAX
                    )
                except (TypeError, ValueError):
                    pass

    # 公共收尾：clamp 与时间戳
    s.affection = clamp(s.affection, AFFECTION_MIN, AFFECTION_MAX)
    s.mood_intensity = clamp(s.mood_intensity, INTENSITY_MIN, INTENSITY_MAX)
    s.updated_at = ts
    if event.type != EVENT_SILENCE_HOURS:
        s.last_interaction = ts
    return s


def apply_events(
    state: MoodState,
    events: list[Event],
    *,
    daily_bonus_enabled: bool = True,
    now: float | None = None,
    calm_down_per_message: float = DEFAULT_CALM_DOWN_PER_MESSAGE,
) -> MoodState:
    """按顺序应用多个事件（如：沉默 → 每日首条 → 关键词 → 普通消息）。"""
    current = state
    for event in events:
        current = apply_event(
            current, event, daily_bonus_enabled=daily_bonus_enabled, now=now,
            calm_down_per_message=calm_down_per_message,
        )
    return current


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def effective_mood(state: MoodState, now: float | None = None) -> "tuple[str, float]":
    """结构化有效状态 getter：返回衰减后的 (mood, intensity)。

    数学与注入片段完全同源（VOICE_SPEC §1：文字注入与语音映射共用同一有效状态，
    消费方禁止解析注入字符串）：
    - now 传入且 state.updated_at > 0 时按 τ=4 小时指数衰减：
      eff = intensity * exp(-(now-updated_at)/14400)；
    - eff < 0.15 时心情回落 calm（久未互动的情绪视为已平复）；
    - now 不传/无时间戳时原样返回（与 render_system_fragment 旧行为一致）。
    """
    intensity = state.mood_intensity
    mood = state.mood
    if now is not None and state.updated_at > 0:
        dt = max(0.0, float(now) - state.updated_at)
        intensity = intensity * math.exp(-dt / MOOD_DECAY_TAU_SECONDS)
        if intensity < MOOD_FADE_THRESHOLD:
            mood = MOOD_CALM
    return mood, clamp(intensity, INTENSITY_MIN, INTENSITY_MAX)


def _reunion_line(hours: float) -> str:
    """按沉默结算时长给一句重逢文案（风格对齐新情绪内心独白：括号体例）。"""
    if hours >= 168.0:
        return "（他一个多星期都没理我了……还以为把我忘了，呜，其实很想他）"
    if hours >= 72.0:
        return "（他好几天没理我了，又委屈又想他）"
    return "（他一整天都没理我……哼，终于肯来找我了）"


def render_system_fragment(state: MoodState, now: float | None = None) -> str:
    """渲染注入 LLM 的内心状态片段：纯数据行（不含数字好感度，不含指令句）。

    R3 起片段只承载数据（``【当前内心状态】关系：…｜心情：…｜连续互动 N 天``），
    「让它影响语气、绝不在回复中提起或解释」的使用指令固化在 persona
    system_prompt 尾部——指令是逐日稳定文本，随人设前缀命中 provider 隐式
    缓存；本片段逐条变化，不再夹带指令句。

    情绪丰富化追加两段（原 7 态且无重逢时输出与旧版逐字一致）：
    - 重逢彩蛋：最近一次沉默结算 ≥ REUNION_MIN_HOURS 且结算发生在
      REUNION_WINDOW_SECONDS 内时，片段开头加一句重逢文案；
    - 新 7 情绪的内心独白（MOOD_INNER_VOICE），渲染的是衰减后的有效心情，
      追加在数据行之后。

    now: 当前时间戳。衰减语义见 :func:`effective_mood`（本函数与其共用同一套
    衰减数学，文字注入与语音映射天然同源）；重逢窗口无 now 时以 updated_at
    为参照（刚结算完的存量状态也能在下次渲染时带上彩蛋）。
    """
    mood, intensity = effective_mood(state, now=now)
    level = LEVEL_ZH[affection_level(state.affection)]
    mood_zh = MOOD_ZH.get(mood, MOOD_ZH[MOOD_CALM])
    lines: list[str] = []
    reference = float(now) if now is not None else state.updated_at
    if (
        state.last_silence_hours >= REUNION_MIN_HOURS
        and state.last_silence_at > 0
        and 0.0 <= reference - state.last_silence_at <= REUNION_WINDOW_SECONDS
    ):
        lines.append(_reunion_line(state.last_silence_hours))
    lines.append(
        f"【当前内心状态】关系：{level}｜心情：{mood_zh}（{intensity_label(intensity)}）｜"
        f"连续互动 {state.daily_streak} 天"
    )
    inner_voice = MOOD_INNER_VOICE.get(mood)
    if inner_voice:
        lines.append(inner_voice)
    return "\n".join(lines)


def render_user_reply(state: MoodState) -> str:
    """渲染 /mood 命令给用户看的一句话状态（人设化模糊版，不含数字好感度）。"""
    level = LEVEL_ZH[affection_level(state.affection)]
    mood = MOOD_ZH.get(state.mood, MOOD_ZH[MOOD_CALM])
    return (
        f"她现在{mood}，你们的关系是「{level}」哦，已经连续互动 {state.daily_streak} 天啦～"
    )


def render_stats_reply(state: MoodState) -> str:
    """渲染 /mood stats 的管理员调试视图：包含精确数值与各类计数。"""
    level = LEVEL_ZH[affection_level(state.affection)]
    mood = MOOD_ZH.get(state.mood, MOOD_ZH[MOOD_CALM])
    return (
        f"[调试视图] 好感度 {round(state.affection, 1)}/100（{level}），"
        f"心情：{mood}，强度 {state.mood_intensity:.2f}，"
        f"连续互动 {state.daily_streak} 天，累计消息 {state.total_messages} 条，"
        f"今日甜言 {state.sweet_today} 次 / 夸奖 {state.praise_today} 次 / "
        f"负面 {state.rude_today} 次（单类当日满 {KEYWORD_DAILY_CAP} 次后好感增减为 0）。"
    )


# ---------------------------------------------------------------------------
# 关键词分类、危机兜底与正则防呆（纯逻辑）
# ---------------------------------------------------------------------------


def is_crisis(text: str) -> bool:
    """危机倾诉信号检测：命中 CRISIS_PATTERN 即视为危机倾诉。"""
    return re.search(CRISIS_PATTERN, text) is not None


def classify_keyword(
    text: str,
    rude_re: re.Pattern,
    praise_re: re.Pattern,
    sweet_re: re.Pattern,
) -> str | None:
    """按文本命中情况返回关键词事件类型（rude > praise > sweet），未命中返回 None。

    否定窗口：某正则命中后，取 match.start() 前 NEGATION_WINDOW 个字符，若含
    任一 NEGATION_MARKS（不/别/没/无/一点），该命中作废——整条消息按普通消息
    处理，不再尝试其他类别或其他命中位置。
    """
    for regex, event_type in (
        (rude_re, EVENT_KEYWORD_RUDE),
        (praise_re, EVENT_KEYWORD_PRAISE),
        (sweet_re, EVENT_KEYWORD_SWEET),
    ):
        match = regex.search(text)
        if match is None:
            continue
        window_start = max(0, match.start() - NEGATION_WINDOW)
        window = text[window_start:match.start()]
        if any(mark in window for mark in NEGATION_MARKS):
            return None
        return event_type
    return None


def _probe_pattern(compiled: re.Pattern) -> float:
    """在 'a'*60 样例上分段实测 search 累计耗时（秒），累计超限即提前中止。

    灾难性回溯的正则（如 (a+)+b）在中途长度就会指数爆炸：若一次性对全量
    样例 search 再比较耗时，测量本身就可能永久挂起。按 PATTERN_PROBE_STEP
    递增长度、累计耗时超阈值立即返回，可在 ~2 秒内完成判定。
    """
    total = 0.0
    for end in range(PATTERN_PROBE_STEP, len(PATTERN_PROBE_INPUT) + 1, PATTERN_PROBE_STEP):
        sample = PATTERN_PROBE_INPUT[:end]
        start = time.perf_counter()
        compiled.search(sample)
        total += time.perf_counter() - start
        if total > PATTERN_PROBE_TIME_LIMIT:
            break
    return total


def compile_keyword_pattern(pattern: str, default: str, *, key: str = "") -> re.Pattern:
    """编译关键词正则并做防呆检查；非法/超长/疑似灾难回溯时回退默认并告警。

    - 模式长度 > PATTERN_LENGTH_LIMIT 直接回退（疑似误配置）；
    - 编译成功后在 'a'*60 样例上分段实测 search 耗时，累计 > 0.5s 判定为
      灾难性回溯风险，回退默认。
    """
    label = key or "未命名"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        logger.warning("[情绪引擎] 关键词配置 %s 正则非法(%s)，已回退默认: %s", label, exc, default)
        return re.compile(default)
    if len(pattern) > PATTERN_LENGTH_LIMIT:
        logger.warning(
            "[情绪引擎] 关键词配置 %s 正则过长(%d > %d 字符)，疑似误配置，已回退默认: %s",
            label,
            len(pattern),
            PATTERN_LENGTH_LIMIT,
            default,
        )
        return re.compile(default)
    elapsed = _probe_pattern(compiled)
    if elapsed > PATTERN_PROBE_TIME_LIMIT:
        logger.warning(
            "[情绪引擎] 关键词配置 %s 正则样例实测回溯耗时 %.3fs(>%.1fs)，"
            "疑似灾难性回溯，已回退默认: %s",
            label,
            elapsed,
            PATTERN_PROBE_TIME_LIMIT,
            default,
        )
        return re.compile(default)
    return compiled


# ---------------------------------------------------------------------------
# JSON 持久化层（storage.py 原样并入）—— 按 user_id 键存储 MoodState
# ---------------------------------------------------------------------------

DEFAULT_FILENAME = "mood_states.json"


class MoodStorage:
    """按 user_id 键的 JSON 持久化存储。

    - 数据目录由调用方注入（新栈默认 local/shared_plugin_data/vl_mood_engine，
      与旧栈 StarTools 落点兼容），本模块绝不感知也绝不写框架自身目录；
    - 采用内存缓存 + 显式 flush() 落盘：set() 只改缓存，flush() 原子写盘
      （临时文件 + os.replace），防止进程中断导致 JSON 损坏；
    - 并发安全（加锁）由调用方（MoodEngine）负责。
    """

    def __init__(self, data_dir: str | Path, filename: str = DEFAULT_FILENAME) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / filename
        self._cache: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # 加载与落盘
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """启动时全量载入。文件缺失或损坏时从空状态开始，绝不抛异常中断。

        条目级损坏：value 非 dict 的键先整文件备份 .bak，再丢弃坏条目、
        保留好条目（并告警列出坏键），不再静默吞掉。
        """
        self._cache = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                good: dict[str, dict] = {}
                bad_keys: list[str] = []
                for key, value in raw.items():
                    if isinstance(value, dict):
                        good[str(key)] = value
                    else:
                        bad_keys.append(str(key))
                if bad_keys:
                    # 先整文件备份（含坏条目），再保留好条目继续运行
                    self._backup_corrupted()
                    logger.warning(
                        "状态文件存在 %d 个损坏条目(值非 dict)，已整文件备份为 %s "
                        "并丢弃坏条目、保留其余好条目。坏键: %s",
                        len(bad_keys),
                        self.path.with_suffix(".json.bak").name,
                        ", ".join(bad_keys),
                    )
                self._cache = good
            else:
                # 顶层不是 dict 同样视为损坏：备份后从空开始，避免下次 set() 无备份数据丢弃
                self._backup_corrupted()
                logger.error("状态文件顶层结构异常(非 dict)，已备份并从空状态开始")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # 损坏数据不阻塞启动：备份原文件后从空开始
            self._backup_corrupted()
            logger.error("状态文件损坏，已备份并从空状态开始: %s", exc)

    def _backup_corrupted(self) -> None:
        try:
            backup = self.path.with_suffix(".json.bak")
            if self.path.exists():
                os.replace(self.path, backup)
        except OSError:
            pass

    def _flush(self) -> None:
        """原子写盘：先写临时文件再替换，避免写到一半崩溃留下坏 JSON。

        先对缓存做浅快照再序列化：flush 可能经 asyncio.to_thread 在工作线程
        执行，而事件循环线程会并发向缓存写入新条目，快照可避免序列化过程中
        字典被改动。
        """
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            snapshot = dict(self._cache)
            tmp_path = self.path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.error("状态写入失败: %s", exc)

    # ------------------------------------------------------------------
    # 读写接口
    # ------------------------------------------------------------------

    def get(self, user_id: str) -> MoodState:
        """读取某用户状态；不存在则返回默认状态。"""
        return MoodState.from_dict(self._cache.get(str(user_id)))

    def set(self, user_id: str, state: MoodState) -> None:
        """写入某用户状态到内存缓存（只改缓存，不落盘）。

        落盘由 flush() 负责：调用方在锁内调用 set、锁外异步调用 flush，
        读改写串行语义不变，磁盘 IO 移出临界区。
        """
        self._cache[str(user_id)] = state.to_dict()

    def flush(self) -> None:
        """把当前缓存原子落盘（public，供调用方在临界区外异步调用）。"""
        self._flush()

    def delete(self, user_id: str) -> bool:
        """删除某用户状态（用于 /mood reset）。返回是否确实删除了数据。"""
        key = str(user_id)
        if key not in self._cache:
            return False
        del self._cache[key]
        self._flush()
        return True

    def all_states(self) -> dict[str, MoodState]:
        """返回全部用户状态（只读视图用途，如统计）。"""
        return {k: MoodState.from_dict(v) for k, v in self._cache.items()}


# ---------------------------------------------------------------------------
# 活配置加载（新胶水，读现网 vl_mood_engine_config.json）
# ---------------------------------------------------------------------------

# 现网活配置路径（实测带 UTF-8 BOM，一律 utf-8-sig 读取）
DEFAULT_CONFIG_PATH = "local/astrbot_data/config/vl_mood_engine_config.json"


def load_mood_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """读取 vl_mood_engine 的活配置，返回归一化 dict。

    - 返回键：keyword_groups(sweet/praise/rude 三组正则文本)、daily_bonus_enabled、
      inject_enabled；
    - 文件缺失/损坏/字段非法时逐项回退 DEFAULT_*_PATTERN 与 True，绝不抛异常——
      配置读不到不应阻止情绪引擎以默认规则运行；
    - 本函数只读文件，不解析正则（编译防呆交给 MoodEngine 的 compile 流程）。
    """
    raw: dict[str, Any] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("[情绪引擎] 活配置读取失败(%s)，全部回退默认: %s", path, exc)

    groups_raw = raw.get("keyword_groups")
    groups: dict[str, str] = {}
    if isinstance(groups_raw, dict):
        for key in ("sweet", "praise", "rude"):
            value = groups_raw.get(key)
            if isinstance(value, str) and value.strip():
                groups[key] = value
            else:
                groups[key] = {
                    "sweet": DEFAULT_SWEET_PATTERN,
                    "praise": DEFAULT_PRAISE_PATTERN,
                    "rude": DEFAULT_RUDE_PATTERN,
                }[key]
    else:
        groups = {
            "sweet": DEFAULT_SWEET_PATTERN,
            "praise": DEFAULT_PRAISE_PATTERN,
            "rude": DEFAULT_RUDE_PATTERN,
        }

    daily_bonus = raw.get("daily_bonus_enabled", True)
    inject = raw.get("inject_enabled", True)
    return {
        "keyword_groups": groups,
        "daily_bonus_enabled": daily_bonus if isinstance(daily_bonus, bool) else True,
        "inject_enabled": inject if isinstance(inject, bool) else True,
    }


# ---------------------------------------------------------------------------
# 门面：状态推进（旧 main.py 胶水行为的框架无关落地）
# ---------------------------------------------------------------------------


class MoodEngine:
    """情绪引擎门面：编译关键词正则、串行化状态推进、危机兜底与注入片段。

    行为对齐旧 main.py（行号级）：
    - 危机兜底先于一切分类：命中 CRISIS_PATTERN 的消息不推进任何状态（main.py:115-123）；
    - / 开头的命令消息不刷好感（main.py:125-127）；
    - 沉默补算：距上次互动满 24h 追加 EVENT_SILENCE_HOURS（main.py:135-139）；
    - 每日首条：跨自然日第一条追加 EVENT_DAILY_FIRST（main.py:142-143）；
    - 关键词分类 rude>praise>sweet（main.py:146-148，分类纯逻辑在 classify_keyword）；
    - 普通消息保底 EVENT_MESSAGE（main.py:150-151）；
    - daily_bonus_enabled 开关透传 apply_events（main.py:153-158）；
    - 锁内 set、锁外 to_thread 落盘（main.py:160-163）。
    """

    def __init__(
        self,
        data_dir: str | Path,
        config: dict[str, Any] | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
    ) -> None:
        self.storage = MoodStorage(data_dir)
        self._lock = asyncio.Lock()
        cfg = config if config is not None else load_mood_config(config_path)
        self.config = cfg
        self.daily_bonus_enabled = bool(cfg.get("daily_bonus_enabled", True))
        self.inject_enabled = bool(cfg.get("inject_enabled", True))
        # R3 情绪灵敏度：普通消息单次回落幅度（可配；非法值回退默认 0.04）
        try:
            calm_down = float(cfg.get("calm_down_per_message",
                                      DEFAULT_CALM_DOWN_PER_MESSAGE))
        except (TypeError, ValueError):
            calm_down = DEFAULT_CALM_DOWN_PER_MESSAGE
        self.calm_down_per_message = clamp(calm_down, 0.0, 1.0)
        groups = cfg.get("keyword_groups") or {}
        # 关键词正则编译（防呆回退默认；典型配置示例 rude=「闭嘴|烦|滚」，比默认宽松）
        self._re_sweet = self._compile_pattern("sweet", str(groups.get("sweet") or DEFAULT_SWEET_PATTERN), DEFAULT_SWEET_PATTERN)
        self._re_praise = self._compile_pattern("praise", str(groups.get("praise") or DEFAULT_PRAISE_PATTERN), DEFAULT_PRAISE_PATTERN)
        self._re_rude = self._compile_pattern("rude", str(groups.get("rude") or DEFAULT_RUDE_PATTERN), DEFAULT_RUDE_PATTERN)

    def _compile_pattern(self, key: str, pattern: str, default: str) -> re.Pattern:
        """按配置编译关键词正则；非法/超长/疑似灾难回溯时回退默认并告警。"""
        return compile_keyword_pattern(pattern, default, key=key)

    def classify(self, text: str) -> Event | None:
        """按文本命中情况生成关键词事件（rude > praise > sweet，否定窗口在纯逻辑层）。"""
        event_type = classify_keyword(text, self._re_rude, self._re_praise, self._re_sweet)
        return Event(event_type) if event_type is not None else None

    async def handle_message(
        self, user_id: str, text: str, *, now: float | None = None
    ) -> MoodState | None:
        """处理一条用户消息，推进状态并返回新状态（危机/命令/空文本返回 None）。

        now: 注入当前时间戳（秒），测试用；None 时取真实 time.time()。
        """
        text = (text or "").strip()
        user_id = str(user_id or "").strip()
        if not text or not user_id:
            return None

        # 0) 危机兜底（先于一切分类）：命中危机倾诉信号时不推进状态、不计任何
        #    事件、不注入变化——绝不把危机倾诉当作负面互动"落井下石"。
        if is_crisis(text):
            logger.warning(
                "[情绪引擎] 检测到危机倾诉信号，本次消息不推进情绪状态"
            )
            return None

        # 跳过命令消息，避免 /mood 等指令刷好感
        if text.startswith("/"):
            return None

        ts = float(now) if now is not None else time.time()
        events: list[Event] = []

        async with self._lock:
            state = self.storage.get(user_id)

            # 1) 沉默补算：满 24 小时的间隔按沉默事件结算（回顾性修正）
            if state.last_interaction > 0:
                hours = (ts - state.last_interaction) / 3600.0
                if hours >= SILENCE_CHECK_THRESHOLD_HOURS:
                    events.append(Event(EVENT_SILENCE_HOURS, value=hours))

            # 2) 每日首条：跨自然日的第一条互动（连击加成开关由配置控制）
            if is_new_day(state.last_interaction, ts):
                events.append(Event(EVENT_DAILY_FIRST))

            # 3) 关键词分类事件（若命中）
            keyword_event = self.classify(text)
            if keyword_event is not None:
                events.append(keyword_event)

            # 4) 普通消息保底事件（带原文：apply_event 据此套用小转移规则）
            events.append(Event(EVENT_MESSAGE, text=text))

            new_state = apply_events(
                state,
                events,
                daily_bonus_enabled=self.daily_bonus_enabled,
                now=ts,
                calm_down_per_message=self.calm_down_per_message,
            )
            # 锁内只改缓存：读改写仍由锁保证串行
            self.storage.set(user_id, new_state)

        # 落盘移出临界区：锁外异步写盘，不阻塞其他消息的状态推进
        await asyncio.to_thread(self.storage.flush)
        return new_state

    def render_fragment(self, user_id: str, *, now: float | None = None) -> str:
        """渲染注入 system prompt 的内心状态片段（inject_enabled=false 时返回空串）。"""
        if not self.inject_enabled:
            return ""
        return render_system_fragment(self.storage.get(user_id), now=now)

    def snapshot_state(self, user_id: str) -> MoodState:
        """结构化读取用户当前状态快照（VOICE_SPEC §1/§2：禁止解析注入字符串）。

        一次读取同时供文字注入渲染（render_system_fragment）与语音映射
        （soul.mood.effective_mood → voice.mapping）派生——保证「persona 装配
        时刻同一有效状态」贯穿文字注入与语音。
        """
        return self.storage.get(str(user_id))

    async def reset_user(self, user_id: str) -> bool:
        """重置某用户状态（/mood reset 的行为面），返回是否确实有数据被删。"""
        async with self._lock:
            deleted = self.storage.delete(str(user_id))
        return deleted


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------

# 新栈数据目录（与旧栈 StarTools 落点兼容：local/shared_plugin_data/<插件名>）
_PLUGIN_DATA_DIR = "local/shared_plugin_data"


def setup(ctx: Any) -> None:
    """按 vl.toml [mood] 构造情绪引擎挂 ctx.mood_engine（配置直传，不回读旧 JSON）。

    keyword_groups 为空 dict 时 MoodEngine 逐项回退默认正则（与迁移前行为一致）。
    """
    mood_cfg = ctx.cfg.mood
    config = {
        "keyword_groups": dict(mood_cfg.keyword_groups or {}),
        "daily_bonus_enabled": bool(mood_cfg.daily_bonus_enabled),
        "inject_enabled": bool(mood_cfg.inject_enabled),
        # R3 情绪灵敏度：普通消息单次回落幅度（可配，缺省 0.04）
        "calm_down_per_message": float(
            getattr(mood_cfg, "calm_down_per_message", DEFAULT_CALM_DOWN_PER_MESSAGE)),
    }
    ctx.mood_engine = MoodEngine(
        Path(ctx.repo_root) / _PLUGIN_DATA_DIR / "vl_mood_engine",
        config=config,
    )
    logger.info("[mood] 情绪引擎装配完成（daily_bonus=%s inject=%s）",
                ctx.mood_engine.daily_bonus_enabled, ctx.mood_engine.inject_enabled)
