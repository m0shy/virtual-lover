# -*- coding: utf-8 -*-
"""表情包后处理（SPEC §8.4）—— &&tag&& 识别-校验-取图-清理（clean-room 行为实现）。

行为规格（SPEC §8.4，参数实测自 meme_manager_config.json 与 local/ 数据）：
- 严格匹配 ``&&[^&]+&&``（TAG_RE；从 LLM 回复文本中提取全部标记）；
- 白名单校验：白名单来自三份实测数据（均可由配置覆盖）——
  a) 现网 high_confidence_emotions 的 19 个 tag（与本地图库目录一一对应，
     实测 local/shared_plugin_data/meme_manager/memes/ 共 19 个 tag 子目录 398 张图）；
  b) 历史 assistant 实际输出的 10 个有效基础 tag（data_v4.db 全量扫描实测：
     blush/happy/hug/meow/sad/wronged/heart/kawaii/shy/kiss——其中多数指向
     旧栈图床路径，stardots key 全空从未配置成功，属"从未生效的能力"）；
  白名单 = a ∪ b；发送时再校验本地图库目录实际存在该 tag 子目录，
  无图 tag 命中时按"非法处理"清理标记只发文字；
  （R3 起 tag 归一化：历史 _jpg 变体 &&blush_jpg&& 剥掉后缀即按基础 tag
  blush 参与校验——白名单本就含 blush，图库 R3 补了占位目录，能立即出图）；
- 命中率 60%（emotions_probability=60 实测）：整条回复一次概率判定，命中才发图；
- 单条上限 1 张（max_emotions_per_message=1 且 strict=true 实测）；
- 独立消息发送：表情包图不拼进文字消息，单独作为一条图片消息发出，
  文本中所有 &&tag&& 标记（含合法与非法）一律清除；
- 残缺 && 清洗：未闭合的 ``&&...``、孤立 ``&&``、空标记 ``&&&&`` 等残迹全部清除
  （对应现网 content_cleanup_rule="&&[a-zA-Z]*&&" 的清理意图，并覆盖残缺形态）。

依赖注入：图库目录（config 带入）、rng（测试固定随机）、图选择回调。
本模块只依赖标准库。
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("virtuallover.soul.memes")

# ---------------------------------------------------------------------------
# 严格匹配与清洗正则（SPEC §8.4：&&tag&& 严格匹配 &&[^&]+&&）
# ---------------------------------------------------------------------------

# 合法形态的标记：&& 与 && 之间至少 1 个非 & 字符
TAG_RE = re.compile(r"&&([^&\n]+?)&&")

# 残缺形态：未闭合的 &&...（到行尾无第二个 &&）、孤立 &&、空标记 &&&&
# 清洗顺序：先完整匹配替换/清除，再清除一切残留的 && 字符（含残缺形态）。
LEFTOVER_AMP_RE = re.compile(r"&&+")

# 默认白名单：实测带出（模块 docstring 的 a ∪ b 口径）
# a) 现网 high_confidence_emotions 19 个（=本地图库 19 个 tag 目录，398 张图）
DEFAULT_WHITELIST: frozenset[str] = frozenset(
    {
        "angry", "baka", "color", "confused", "cpu", "fool", "givemoney",
        "happy", "like", "meow", "morning", "reply", "sad", "see", "shy",
        "sigh", "sleep", "surprised", "work",
    }
)
# b) 历史 assistant 实际输出的 10 个有效基础 tag（data_v4.db 实测）
HISTORY_TAGS: frozenset[str] = frozenset(
    {"blush", "happy", "hug", "meow", "sad", "wronged", "heart", "kawaii", "shy", "kiss"}
)

# 实测参数（meme_manager_config.json）
DEFAULT_PROBABILITY = 60          # emotions_probability=60（命中率 60%）
DEFAULT_MAX_PER_MESSAGE = 1       # max_emotions_per_message=1 且 strict=true

# 支持的图片扩展名（实测图库目录内为 .jpg/.png/.gif）
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def normalize_tag(tag: str) -> str:
    """tag 归一化（R3）：剥掉历史形态的 ``_jpg`` 后缀并去首尾空白。

    历史 assistant 真实输出过 &&blush_jpg&& 这类带图床后缀的变体（旧栈
    data_v4.db 实测），后缀使其 100% 落在白名单外出不了图；剥掉后即恢复为
    基础 tag（blush_jpg → blush）参与白名单校验与取图。
    """
    return (tag or "").strip().removesuffix("_jpg")


def find_tags(text: str) -> list[str]:
    """提取文本中全部 &&tag&& 标记的 tag 名（严格匹配 &&[^&]+&&，按出现顺序）。

    R3 起提取结果经 :func:`normalize_tag` 归一化（``_jpg`` 历史后缀剥除）；
    归一化后为空的标记（如 &&_jpg&&）视为残迹丢弃。
    """
    tags = [normalize_tag(m.group(1)) for m in TAG_RE.finditer(text or "")]
    return [tag for tag in tags if tag]


def strip_all_markers(text: str) -> str:
    """清除文本中所有 && 标记与残迹（合法/非法/残缺一律清），并收敛多余空白。

    清洗步骤：
    1) TAG_RE 清掉完整标记（&&xxx&&）；
    2) LEFTOVER_AMP_RE 清掉残留的 && 字符（未闭合 &&xxx、孤立 &&、&&&& 等）；
       ——只清 && 残迹本身，标记内的词是正文文字，保留不删（保守清洗）；
    3) 收敛标记清除后留下的连续空白与首尾空格。
    """
    cleaned = TAG_RE.sub("", text or "")
    cleaned = LEFTOVER_AMP_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def build_whitelist(extra_tags: list[str] | None = None,
                    include_history_tags: bool = True) -> frozenset[str]:
    """组装白名单：默认 19 图库 tag（+ 历史实测 10 tag），可再叠加配置 tag。"""
    tags = set(DEFAULT_WHITELIST)
    if include_history_tags:
        tags |= HISTORY_TAGS
    for tag in extra_tags or []:
        if str(tag).strip():
            tags.add(str(tag).strip())
    return frozenset(tags)


def pick_image(memes_dir: str | Path, tag: str,
               rng: random.Random | None = None) -> Path | None:
    """从图库目录的对应 tag 子目录随机取一张图；目录不存在/为空返回 None。

    memes_dir 结构与旧栈一致：<memes_dir>/<tag>/<图片文件>（实测 19 个 tag 目录）。
    """
    base = Path(memes_dir) / tag
    if not base.is_dir():
        return None
    try:
        files = [
            p for p in base.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        ]
    except OSError as exc:
        logger.warning("扫描表情图目录失败（tag=%s）: %s", tag, exc)
        return None
    if not files:
        return None
    return (rng or random).choice(files)


@dataclass
class MemePlan:
    """一条回复的表情包处理结果（供发送层按"独立消息"语义执行）。"""

    text: str                      # 清除全部标记后的纯文本（正常路径发文字消息）
    image_path: Path | None        # 命中白名单且有图时的本地图片路径（发独立图片消息）
    tag: str | None = None         # 命中的 tag（诊断用）
    dropped_tags: list[str] = field(default_factory=list)  # 非法/无图被清理的 tag


def process_reply(
    text: str,
    *,
    memes_dir: str | Path,
    whitelist: frozenset[str] | None = None,
    probability: int = DEFAULT_PROBABILITY,
    max_images: int = DEFAULT_MAX_PER_MESSAGE,
    rng: random.Random | None = None,
) -> MemePlan:
    """对一条 LLM 回复做表情包后处理（识别-校验-取图-清理，SPEC §8.4 全链路）。

    - 概率判定在前（命中率 60%）：未命中时只清理标记不发图（与现网
      "每条消息 60% 概率、最多 1 张"行为一致）；
    - 多个合法 tag 时按出现顺序取第一个进入发送（单条上限 1 张）；
    - 非法 tag（不在白名单）与"合法但图库无图"的 tag 都只清理标记、不阻塞文字；
    - 返回 MemePlan：text 永远是清理后的纯文本，image_path 为待发独立图片消息。
    """
    rng = rng or random.Random()
    allowed = whitelist if whitelist is not None else build_whitelist()

    tags = find_tags(text)
    image_path: Path | None = None
    chosen_tag: str | None = None
    dropped: list[str] = []
    taken = 0  # 本条已取图张数（单条上限 max_images）

    # 命中率判定：概率通过且本条确有标记时才取图
    want_image = bool(tags) and rng.randint(1, 100) <= max(0, min(100, int(probability)))
    for tag in tags:
        if want_image and taken < max(0, max_images) and tag in allowed:
            img = pick_image(memes_dir, tag, rng)
            if img is not None:
                image_path = img
                chosen_tag = tag
                taken += 1
                logger.info("表情包命中：tag=%s -> %s", tag, img.name)
                continue
            # 合法但图库无图（历史 tag 多属此类）：清理标记，不视为错误
            logger.info("tag=%s 在白名单但图库无图，仅清理标记。", tag)
        dropped.append(tag)

    cleaned = strip_all_markers(text)
    return MemePlan(text=cleaned, image_path=image_path, tag=chosen_tag, dropped_tags=dropped)
