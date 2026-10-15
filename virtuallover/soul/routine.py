# -*- coding: utf-8 -*-
"""soul/routine.py —— 早晚安锚点（主动陪伴升级：她没有定点早安晚安 → 现在有了）。

两件小事，天天不落（任务书）：

1. **锚点判定** :func:`tick`——早安 ``morning_time ± morning_jitter``、
   晚安 ``night_time ± night_jitter`` 各一次。抖动偏移不是随机数：以
   ``sha256(日期 + 键)`` 派生的确定性偏移（±jitter 分钟内），同一天无论
   重启多少次、重算多少遍结果一致（幂等不漂移）；到点返回 ``["morning"]``
   / ``["night"]``（同刻两者都到点则都返回）。
2. **当日幂等**：状态落 ``<state_dir>/routine.json``
   ``{"morning_sent": "YYYY-MM-DD", "night_sent": "YYYY-MM-DD"}``，
   同一天第二次调用一律安静——判定与落账都在 tick 内完成。

发送不在本模块：``__main__._routine_tick`` 消费 tick 的返回值，把各自的
prompt（:func:`render_morning_prompt` / :func:`render_night_prompt`）经
``ProactiveService.generate_and_send(template_override=...)`` 走完整主动
消息链路（安静时段/活跃避让/质量门/dual_send 语音双轨/表情包/历史写回
全部同源）。早安 prompt 注入 ``soul.weather.get_weather_line`` 的一行
天气事实（失败自然省略）；晚安 prompt 带 ``&&sleep&&``、早安带
``&&morning&&`` 表情标记（tag 在 memes 白名单里，发送路径统一处理）。

本模块只依赖标准库；时间与配置均由调用方注入（cfg 鸭子类型，字段名与
``config.RoutineConfig`` 对齐），宿主机 pytest 直测。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .diary import parse_hhmm

logger = logging.getLogger("virtuallover.soul.routine")

# 状态文件名（落在 <storage.data_dir>，即 local/vl_data/ 下）
STATE_FILENAME = "routine.json"

# 锚点解析失败时的回退默认（与 config.RoutineConfig 出厂值一致）
_DEFAULT_ANCHORS: dict[str, tuple[int, int]] = {"morning": (7, 30), "night": (22, 50)}

# 锚点键的固定顺序：同刻都到点时早安在前（先迎新一天，再道晚安不适用同刻，
# 顺序只为结果稳定可测）
_ANCHOR_KEYS: "tuple[str, ...]" = ("morning", "night")

# 早安 prompt（{now} 占位；天气行与表情标记由 render_* 追加）
_MORNING_PROMPT = (
    "[系统任务：主动对话] 现在是 {now}。\n"
    "到早安时刻了：用一两句符合你的人设的话向他道早安，"
    "像刚睡醒的自己顺手发的一条消息，自然口语化，别像播报。"
)

# 晚安 prompt
_NIGHT_PROMPT = (
    "[系统任务：主动对话] 现在是 {now}。\n"
    "到晚安时刻了：用一两句符合你的人设的话向他道晚安，"
    "顺口聊聊今天、轻轻叮嘱他早点休息，自然收尾，别像播报。"
)


# ---------------------------------------------------------------------------
# 确定性抖动（hash(日期+键)，幂等重算不漂移）
# ---------------------------------------------------------------------------

def jitter_offset(date_str: str, key: str, jitter_minutes: int) -> int:
    """某天某锚点的确定性抖动偏移（分钟，含正负）。

    - jitter_minutes<=0 → 0（关闭抖动）；
    - 偏移由 sha256(日期:键) 前 4 字节均匀映射到 [-j, +j]——内置 hash() 对
      str 有进程级盐（PYTHONHASHSEED），重启即变，绝不能用作跨进程锚点；
      sha256 与进程无关，同一天重算一万次也是同一个值。
    """
    j = max(0, int(jitter_minutes or 0))
    if j == 0:
        return 0
    digest = hashlib.sha256(f"{date_str}:{key}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big")
    return value % (2 * j + 1) - j


def anchor_datetime(
    day: date, time_str: str, jitter_minutes: int, key: str
) -> datetime:
    """某天某锚点的实际触发时刻（配置时刻 + 确定性抖动）。

    time_str 非法（非 "HH:MM"）回退该键的出厂锚点（morning 07:30 / night 22:50）。
    """
    parsed = parse_hhmm(time_str) if time_str else None
    if parsed is None:
        parsed = _DEFAULT_ANCHORS.get(key, (7, 30))
        if time_str:
            logger.warning("routine.%s_time 配置非法（按出厂锚点 %02d:%02d 处理）。",
                           key, parsed[0], parsed[1])
    hour, minute = parsed
    base = datetime(day.year, day.month, day.day, hour, minute)
    return base + timedelta(minutes=jitter_offset(day.isoformat(), key, jitter_minutes))


# ---------------------------------------------------------------------------
# 状态持久化（当日幂等）
# ---------------------------------------------------------------------------

def _state_path(state_dir: "str | Path") -> Path:
    return Path(state_dir) / STATE_FILENAME


def load_state(state_dir: "str | Path") -> dict:
    """读早晚安状态；缺失/损坏返回空 dict（不抛异常，从零起算）。"""
    try:
        data = json.loads(_state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_dir: "str | Path", state: dict) -> None:
    """原子落盘（tmp + os.replace）；OSError 只告警（幂等性宁可重发不误发）。"""
    try:
        path = _state_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("早晚安状态写入失败: %s", exc)


def mark_sent(
    now: datetime,
    state_dir: "str | Path",
    keys: "str | Iterable[str]",
) -> dict:
    """把键（morning/night）的已发送标记记为当日并落盘；返回更新后的状态。"""
    today = now.date().isoformat()
    state = load_state(state_dir)
    for key in (keys if isinstance(keys, str) else keys):
        state[f"{key}_sent"] = today
    save_state(state_dir, state)
    return state


# ---------------------------------------------------------------------------
# 锚点判定（tick）
# ---------------------------------------------------------------------------

def tick(now: datetime, cfg: Any, state_dir: "str | Path") -> "list[str]":
    """早晚安锚点判定：到点且当日未发 → 返回 ["morning"]/["night"]（或两者）。

    - cfg：``config.RoutineConfig`` 或等价鸭子类型（enabled / *_time /
      *_jitter_minutes）；enabled=false 恒空；
    - state_dir：状态目录（生产= <storage.data_dir>，测试= tmp_path）；
    - 判定与当日落账在本函数内一次完成（幂等：同一天重复调用安静）；
      发送由 __main__._routine_tick 按返回值经主动消息通道执行。
    """
    if not getattr(cfg, "enabled", True):
        return []
    day = now.date()
    today = day.isoformat()
    state = load_state(state_dir)
    due: list[str] = []
    for key in _ANCHOR_KEYS:
        if str(state.get(f"{key}_sent", "") or "") == today:
            continue
        target = anchor_datetime(
            day,
            str(getattr(cfg, f"{key}_time", "") or ""),
            int(getattr(cfg, f"{key}_jitter_minutes", 0) or 0),
            key,
        )
        if now >= target:
            due.append(key)
    if due:
        mark_sent(now, state_dir, due)
        logger.info("早晚安锚点触发：%s（当日落账，幂等）。", ",".join(due))
    return due


# ---------------------------------------------------------------------------
# prompt 渲染（发送侧 __main__._routine_tick 消费）
# ---------------------------------------------------------------------------

def render_morning_prompt(now_dt: datetime, weather_line: "str | None" = None) -> str:
    """早安 prompt：问候指令（+ 可选一行天气事实 + &&morning&& 表情标记）。"""
    parts = [_MORNING_PROMPT.format(now=now_dt.strftime("%Y-%m-%d %H:%M"))]
    line = str(weather_line or "").strip()
    if line:
        parts.append("今天的天气实况（可自然揉进问候里，别原样照抄成天气预报）：\n" + line)
    parts.append("合适的话在消息里带上 &&morning&& 表情标记（至多一次，也可以不用）。")
    return "\n\n".join(parts)


def render_night_prompt(now_dt: datetime) -> str:
    """晚安 prompt：道晚安指令 + &&sleep&& 表情标记。"""
    parts = [_NIGHT_PROMPT.format(now=now_dt.strftime("%Y-%m-%d %H:%M"))]
    parts.append("合适的话在消息里带上 &&sleep&& 表情标记（至多一次，也可以不用）。")
    return "\n\n".join(parts)


# 供测试/诊断枚举锚点键
ANCHOR_KEYS: "Sequence[str]" = _ANCHOR_KEYS
