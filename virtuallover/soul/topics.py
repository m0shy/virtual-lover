# -*- coding: utf-8 -*-
"""soul/topics.py —— 每日一题（主动陪伴升级：主动消息不再问重复的问题）。

问题在哪（任务书）：主动消息内容浅，问过的问题过几天又会原样再问一遍——
她缺一个「今天聊什么」的固定素材源。

方案：内置 ``DEFAULT_TOPICS``（约 40 条日常深浅搭配的通用问题，不涉任何
私人信息），:func:`pick_topic` 保证——

- **同一天同一题**：状态落 ``<state_dir>/daily_topic_state.json``，一天内
  重复调用返回同一条（主动消息一天可能发多条，都围绕同一话题展开）；
- **每天推进**：日期变化时 pos +1，取洗牌序列的下一条；
- **用尽重洗**：pos 走到尽头时重洗一轮（cycle +1 换种子），一轮之内
  40 条问题每条只出现一次，绝不短期内重复。

确定性：洗牌顺序用以「日期+轮次」为种子的 ``random.Random`` 生成并整体
持久化在状态文件里——同一天无论重启多少次、按什么顺序调用，结果一致，
宿主机 pytest 直测零随机。

状态形态::

    {"date": "2026-10-14",      # 当前日期（推进判定的锚）
     "order": [17, 3, ...],     # 本轮洗牌后的索引数组（0..n-1 的排列）
     "pos": 5,                  # 本轮已推进到的位置
     "cycle": 0}                # 已重洗的轮数（种子成分，防每轮同序）

本模块只依赖标准库；时间由调用方注入，直测友好。
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("virtuallover.soul.topics")

# 状态文件名（落在 <storage.data_dir>，即 local/vl_data/ 下）
STATE_FILENAME = "daily_topic_state.json"

# 内置话题池：通用日常内容，深浅搭配，绝不携带任何真实人设/私人信息。
DEFAULT_TOPICS: list[str] = [
    "今天有没有哪个瞬间突然想到我？",
    "今天吃了什么？有没有踩雷或者特别好吃的？",
    "今天有没有遇到什么好笑的事，讲给我听听？",
    "最近在追什么剧或者番？看到哪了？",
    "昨晚睡得好吗？有没有做什么奇怪的梦？",
    "今天有没有偷懒摸鱼的时刻？都在干嘛？",
    "最近有没有一首歌让你单曲循环？",
    "周末有什么安排吗？想不想出去走走？",
    "最近有没有特别想吃的东西？我记下来哦。",
    "今天累不累？有没有什么烦心事想倒一倒？",
    "你最近一次开怀大笑是因为什么？",
    "有没有哪顿饭让你到现在还念念不忘？",
    "最近刷到过什么有趣的段子或者视频吗？",
    "如果明天能睡到自然醒，你想几点起？",
    "你小时候有没有什么可爱的糗事？挑一件讲讲嘛。",
    "最近有没有买到什么好用的东西，想推荐给我的？",
    "今天有没有被人夸过？或者你夸过谁？",
    "最近有没有什么小事让你觉得特别治愈？",
    "你最近在玩什么游戏？带我一起玩呗。",
    "今天走路多不多？有没有好好活动一下？",
    "有没有什么地方是你一直想去但还没去的？",
    "最近有没有喝到特别好喝的饮品？奶茶咖啡都算。",
    "今天有没有什么计划被打乱了？后来怎么办的？",
    "你最近一次熬夜是为了什么？",
    "有没有什么老歌，一听到前奏就想起从前？",
    "最近有没有学到什么新东西？再小的事也算。",
    "今天路上有没有看到什么有趣的人或事？",
    "如果现在可以立刻出发去一个地方，你想去哪？",
    "你最近有没有好好吃早餐呀？",
    "有没有什么爱好是想重新捡起来的？",
    "今天有没有遇到什么让人生气的事？说给我听，我帮你一起骂。",
    "最近有没有给家里打电话呀？家里人都好吗？",
    "你觉得自己最近的状态怎么样？满分十分打几分？",
    "有没有什么电影是你想看第二遍的？",
    "今天有没有准时下班（下课）呀？",
    "最近有没有拍到什么好看的照片？给我看看呗。",
    "天气好的时候你最想做什么？",
    "有没有什么小心愿？不用很大，说出来我记着。",
    "你觉得今天过得快还是慢？",
    "今晚说晚安之前，还有什么想跟我说的吗？",
]

# 洗牌种子前缀（与日期+轮次拼成完整种子，进程无关、可复现）
_SEED_PREFIX = "vl-daily-topic"


def _shuffled_order(count: int, seed: str) -> list[int]:
    """以 seed 生成 0..count-1 的确定性排列（random.Random 独立实例，无全局态）。"""
    indices = list(range(max(0, int(count))))
    random.Random(seed).shuffle(indices)
    return indices


def _seed_for(date_str: str, cycle: int) -> str:
    return f"{_SEED_PREFIX}-{date_str}-{int(cycle)}"


def _valid_order(raw: object, count: int) -> "list[int] | None":
    """校验持久化的 order 是不是 0..count-1 的排列；不是（topics 增删/损坏）返回 None。"""
    if not isinstance(raw, list) or len(raw) != count:
        return None
    try:
        values = [int(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if sorted(values) != list(range(count)):
        return None
    return values


def _load_state(path: Path) -> dict:
    """读每日话题状态；缺失/损坏返回空 dict（不抛异常，从零开始）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    """原子落盘（tmp + os.replace）；失败只告警（当次话题照常返回）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("每日话题状态写入失败: %s", exc)


def pick_topic(now: datetime, state_dir: "str | Path") -> str:
    """取「今天的一题」：同一天同一题，每天推进一条，用尽重洗一轮。

    - now：注入的当前时间（同一天内任意时刻调用结果一致）；
    - state_dir：状态目录（生产= <storage.data_dir>，测试= tmp_path），
      状态文件 ``daily_topic_state.json`` 缺失/损坏时从当天重新起算；
    - 返回值永远有效：任何状态异常都会回退到重新洗牌的合法位置。
    """
    topics = DEFAULT_TOPICS
    count = len(topics)
    today = now.date().isoformat()
    path = Path(state_dir) / STATE_FILENAME
    state = _load_state(path)

    order = _valid_order(state.get("order"), count)
    cycle_raw = state.get("cycle", 0)
    try:
        cycle = max(0, int(cycle_raw))
    except (TypeError, ValueError):
        cycle = 0
    date_raw = str(state.get("date", "") or "")
    try:
        pos = int(state.get("pos", 0))
    except (TypeError, ValueError):
        pos = 0

    if order is None:
        # 首次运行或状态损坏：以当天+轮次为种子重排，从第 0 条开始
        order = _shuffled_order(count, _seed_for(today, cycle))
        pos = 0
    elif date_raw != today:
        # 跨天推进一条；走到尽头则重洗一轮（cycle 入种子，防每轮同序）
        pos += 1
        if pos >= count:
            cycle += 1
            pos = 0
            order = _shuffled_order(count, _seed_for(today, cycle))

    pos = max(0, min(pos, count - 1))
    new_state = {"date": today, "order": order, "pos": pos, "cycle": cycle}
    _save_state(path, new_state)
    return topics[order[pos]]
