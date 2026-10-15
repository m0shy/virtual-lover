# -*- coding: utf-8 -*-
"""场景自拍后处理——&&selfie_<scene>&& 识别-校验-取图-清理（形状照抄 soul/memes）。

背景：人设承诺"分享私人照片"，框架却没有任何发照片能力——她只能说谎
找补。本模块把「发自拍」变成真实能力：LLM 在回复里写 ``&&selfie_home&&``
这类标记，发送层剥出标记、按场景从本地图库随机挑一张、以独立图片消息
发出。照片库是用户自己放的照片（local/vl_data/selfies/<scene>/），框架
不生成、不网取任何图像。

行为规格：
- 严格匹配 ``&&selfie_([^&\\n]+?)&&``；场景名 strip+lower 后必须落在
  :data:`SCENES` 内，白名单外场景（如 &&selfie_beach&&）只清理不出图；
- 概率 0.35：整条回复一次概率判定（rng.random() < 0.35），命中才取图；
- 每日上限 3 张：发送计数持久化 local/vl_data/selfie_state.json
  （原子落盘照抄 proactive.save_state；跨天自动清零；文件缺失/损坏
  从零开始，绝不阻塞回复）；
- 单条上限 1 张：多个合法场景按出现顺序取第一个**有图**的场景；
- 独立消息发送：照片不拼进文字消息，文本中所有 selfie 标记（含合法
  与非法）一律清除；
- 残缺标记清理：未闭合的 ``&&selfie_home``、``&&selfie_&&``、孤立
  ``&&`` 等残迹全部清掉（selfie 标记内部没有正文文字，与 memes 的
  保守清洗不同口径——``selfie_`` 永远是语法，不是他要看的话）；
- 库空/场景无图 → ``(清洗后文本, None)``：本模块**不编借口**，"照片
  还没整理好"之类的软拒绝由人格规则兜底。

依赖注入：状态目录（state_dir 带入图库与状态文件路径）、now（datetime
或时间戳，测试固定时钟）、rng（测试固定随机）。本模块只依赖标准库。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("virtuallover.soul.selfies")

# ---------------------------------------------------------------------------
# 场景与图库（照片库 = <state_dir>/selfies/<scene>/ 下任意 .jpg/.png）
# ---------------------------------------------------------------------------

#: 自拍场景（目录名即场景名；README.txt 在 local/vl_data/selfies/ 有中文说明）
SCENES: tuple[str, ...] = ("campus", "home", "date", "food", "night")

#: 状态目录下照片库子目录名
LIBRARY_DIR_NAME = "selfies"
#: 状态文件名（<state_dir>/selfie_state.json）
STATE_FILENAME = "selfie_state.json"

#: 支持的照片扩展名（任务规格：任意 .jpg/.png；大小写不敏感）
PHOTO_EXTS = (".jpg", ".png")

# ---------------------------------------------------------------------------
# 严格匹配与清洗正则（形状对齐 soul/memes：完整标记 → 残迹 → 空白收敛）
# ---------------------------------------------------------------------------

# 合法形态的标记：&&selfie_ 与 && 之间至少 1 个非 & 字符（场景名）
MARKER_RE = re.compile(r"&&selfie_([^&\n]+?)&&")

# 残缺形态：未闭合的 &&selfie_xxx（到行尾无第二个 &&）、&&selfie_&&（空场景）、
# 孤立 &&。selfie 标记内没有正文文字，残迹连词一起清（见模块 docstring）；
# 词尾停在空白/&/行尾，不吞标记后面的正文词。
LEFTOVER_RESIDUE_RE = re.compile(r"&&+selfie_?[^&\n\s]*|&&+")

# ---------------------------------------------------------------------------
# 实测参数（本模块行为规格）
# ---------------------------------------------------------------------------

DEFAULT_PROBABILITY = 0.35   # 同一会话单条回复的出片概率
DEFAULT_DAILY_LIMIT = 3      # 每日最多发 3 张


def normalize_scene(scene: str) -> str:
    """场景名归一化：去首尾空白并转小写（LLM 输出大小写不定）。"""
    return (scene or "").strip().lower()


def find_scenes(text: str) -> list[str]:
    """提取文本中全部 selfie 标记的场景名（严格匹配，按出现顺序）。

    归一化后不在 :data:`SCENES` 内的场景名（白名单外）与空场景一律丢弃。
    """
    scenes = [
        normalize_scene(m.group(1)) for m in MARKER_RE.finditer(text or "")
    ]
    return [s for s in scenes if s in SCENES]


def strip_all_markers(text: str) -> str:
    """清除文本中所有 selfie 标记与残迹（合法/非法/残缺一律清）。

    清洗步骤：
    1) MARKER_RE 清掉完整标记（含白名单外的）；
    2) LEFTOVER_RESIDUE_RE 清掉残缺形态（&&selfie_home、&&selfie_、孤立 &&）；
    3) 收敛标记清除后留下的连续空白与首尾空格。
    """
    cleaned = MARKER_RE.sub("", text or "")
    cleaned = LEFTOVER_RESIDUE_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def pick_photo(selfies_dir: str | Path, scene: str,
               rng: random.Random | None = None) -> Path | None:
    """从照片库对应场景目录随机取一张 .jpg/.png；目录不存在/为空返回 None。

    selfies_dir 结构：<selfies_dir>/<scene>/<照片文件>（README.txt 同款口径）。
    """
    base = Path(selfies_dir) / scene
    if not base.is_dir():
        return None
    try:
        files = [
            p for p in base.iterdir()
            if p.is_file() and p.suffix.lower() in PHOTO_EXTS
        ]
    except OSError as exc:
        logger.warning("扫描自拍照片目录失败（scene=%s）: %s", scene, exc)
        return None
    if not files:
        return None
    return (rng or random).choice(files)


# ---------------------------------------------------------------------------
# 每日计数状态（持久化照抄 proactive.save_state：tmp + os.replace 原子落盘）
# ---------------------------------------------------------------------------


def _as_datetime(now: "datetime | float | int") -> datetime:
    """now 参数双形态兼容：datetime 直用，数字按本地时间戳换算。"""
    if isinstance(now, datetime):
        return now
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now)
    raise TypeError(f"now 需为 datetime 或时间戳，得到 {type(now).__name__}")


def _load_state(state_path: Path, today: str) -> dict:
    """读回当日计数状态；缺失/损坏/跨天一律从零开始（不阻塞回复）。"""
    state = {"date": today, "sent": 0}
    try:
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("date") == today:
                state["sent"] = max(0, int(data.get("sent", 0)))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("自拍计数状态读取失败，当日从零开始: %s", exc)
    return state


def _save_state(state_path: Path, state: dict) -> None:
    """当日计数原子落盘（OSError 只告警不抛——计数丢了不该打断发送）。"""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, state_path)
    except OSError as exc:
        logger.error("自拍计数状态写入失败: %s", exc)


def process_reply(
    text: str,
    *,
    now: "datetime | float | int",
    state_dir: str | Path,
    rng: random.Random | None = None,
    probability: float = DEFAULT_PROBABILITY,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
) -> tuple[str, Path | None]:
    """对一条 LLM 回复做自拍后处理（识别-校验-取图-清理，形状对齐 memes）。

    返回 ``(清洗后文本, 照片绝对路径或 None)``：

    - 无标记 / 概率未命中 / 当日已达上限 / 场景无图 → 只清理标记，
      返回 ``(清洗后文本, None)``（软拒绝话术由人格规则兜底）；
    - 命中且场景有图 → 计数 +1 落盘，返回照片绝对路径（发送层以
      独立图片消息发出）。
    """
    rng = rng or random.Random()
    now_dt = _as_datetime(now)
    today = now_dt.strftime("%Y-%m-%d")

    cleaned = strip_all_markers(text)
    scenes = find_scenes(text)
    if not scenes:
        return cleaned, None

    base = Path(state_dir)
    state = _load_state(base / STATE_FILENAME, today)
    if int(state.get("sent", 0)) >= max(0, int(daily_limit)):
        logger.info("自拍今日已达 %d 张上限，仅清理标记。", daily_limit)
        return cleaned, None
    if rng.random() >= float(probability):
        logger.info("自拍概率（%g）未命中，仅清理标记。", probability)
        return cleaned, None

    for scene in scenes:
        img = pick_photo(base / LIBRARY_DIR_NAME, scene, rng)
        if img is not None:
            state["sent"] = int(state.get("sent", 0)) + 1
            _save_state(base / STATE_FILENAME, state)
            logger.info("自拍命中：scene=%s -> %s（今日第 %d 张）",
                        scene, img.name, state["sent"])
            return cleaned, img.resolve()
        # 合法但该场景无图（照片库还没放图）：清理标记，不编借口
        logger.info("scene=%s 在场景表但照片库无图，仅清理标记。", scene)
    return cleaned, None
