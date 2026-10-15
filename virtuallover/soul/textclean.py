# -*- coding: utf-8 -*-
"""soul/textclean.py —— 共享文本清洗原语（五轮修缮 R3）。

单一来源，两个消费出口：
- ``clean_proactive_text``：主动消息**发出去的文字**（soul/proactive.py 消费，
  质量门后的最后一道清洗——剥碎片 token/emoji/括号/围栏/包裹引号）；
- ``clean_tts_text``：**念出来的语音**（voice/manager.py 消费，合成前清洗——
  emoji/颜文字/(?: 残留念出来是灾难；清洗后为空由调用方跳过该段）；
- ``strip_mood_tags``：剥 LLM 回复里的 ``<<mood:名字:强度>>`` 自评情绪标签
  （出站兜底——情绪引擎按 soul.mood.parse_mood_tag 严格解析并消费该标签，
  剥除交给出站路径，保证标签形态的任何残留都不发给用户）。

审计动机（R3 实证）：
- 主动消息裸 prompt 会产出 ``(?:`` 家族正则碎片（质量门只负责触发重生成一次，
  重生成仍可能残留，清洗兜底）；
- Huohua 音库把 emoji/颜文字直接念出来（或静默吞字），发前必须剥干净。

设计约束：
- 纯函数、只依赖标准库、无 IO——proactive 与 voice 两侧都可宿主机直测；
- 不剥句末标点（。？！…是 TTS 韵脚与聊天语感的载体，只在截断时作锚点）；
- neutralize（全角化）不做：本模块只做「剥除」，不改写用户可见正文语义。
"""

from __future__ import annotations

import re

__all__ = [
    "clean_proactive_text",
    "clean_tts_text",
    "has_emoji",
    "has_fragment_token",
    "has_llm_artifacts",
    "strip_mood_tags",
]

# ---------------------------------------------------------------------------
# LLM 输出里的脏东西（两出口共用）
# ---------------------------------------------------------------------------

# 代码块围栏：整体包裹形态与残留独立围栏行（soul/diary.clean_diary_text 同手法）
_FENCE_LINE_RE = re.compile(r"^\s*`{3,}")
_WRAPPED_FENCE_RE = re.compile(
    r"^\s*`{3,}[^\S\n]*[^\n]*\n(?P<body>.*?)\n?\s*`{3,}\s*$",
    re.DOTALL,
)

# 成对包裹引号：剥掉 LLM 输出最外层多余的引号（最多两层）
_QUOTE_PAIRS = {
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
}

# 正则碎片 token（审计实证：裸 prompt 产 "(?:" 家族）——(?:...)/(?i)/(?= 等全部
# 命名/非命名组头；含裸 "(?:" 字面兜底
_FRAGMENT_TOKEN_RE = re.compile(r"\(\?[a-zA-Z]*[:=!][^)\n]*\)?|\(\?")

# emoji 与符号修饰：主表情块/杂项符号/旗帜/箭头星形/补充表情，外加变体选择符、
# 零宽连接符与键帽符（孤立残留也要剥）
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 扑克/麻将/表情/交通/补充符号等主块
    "\U00002600-\U000027BF"   # 杂项符号 + 装饰符号（☀✂✅…）
    "\U0001F1E6-\U0001F1FF"   # 区域指示符（旗帜）
    "\U00002B00-\U00002BFF"   # 箭头与星形（⭐⬆…）
    "\U0001F900-\U0001F9FF"   # 补充表情符号
    "\U00002190-\U000021FF"   # 箭头（←↑→…）
    "\uFF66-\uFF9F"           # 半角片假名（颜文字家族 ﾉ(￣ω￣)ﾉ 的散件）
    "\uFE0F\u200D\u20E3"      # 变体选择符/零宽连接符/键帽符
    "]+"
)

# 颜文字括号组：内容不含汉字/字母/数字的短括号组（(｡･ω･｡)／(╯°□°）╯ 的核心形态）
_KAOMOJI_GROUP_RE = re.compile(r"[（(][^（）()a-zA-Z0-9\u4e00-\u9fff]{1,24}[)）]")

# 残余括号字符（输出约束禁括号；剥完组后落单的括号也清掉）
_BRACKET_CHARS_RE = re.compile(r"[()（）\[\]【】\{\}「」『』《》<>]")

# 句末标点（截断锚点）
_SENTENCE_ENDERS = "。！？!?…~；;"

# LLM 自评情绪标签的**宽松剥除形态**：<<mood: 开头、>> 结尾、内部不含尖括号/
# 换行的短标签。刻意比解析形态（soul.mood.parse_mood_tag 的严格正则：合法
# 心情名 + 数值强度）宽——非法名/坏强度的残缺标签也一律剥掉，出站兜底只求
# 「标签形态的字符不发给用户」，不做任何解析。长度上限防灾难回溯。
MOOD_TAG_RE = re.compile(r"<<\s*mood\s*:[^\n<>]{0,120}>>", re.IGNORECASE)


def has_emoji(text: str) -> bool:
    """文本是否含 emoji/符号修饰字符。"""
    return bool(_EMOJI_RE.search(str(text or "")))


def has_fragment_token(text: str) -> bool:
    """文本是否残留正则碎片 token（"(?:" 家族）。"""
    raw = str(text or "")
    return "(?:" in raw or bool(_FRAGMENT_TOKEN_RE.search(raw))


def has_llm_artifacts(text: str) -> bool:
    """质量门判定：碎片 token 或 emoji 任一在场即为不合格裸输出。"""
    return has_fragment_token(text) or has_emoji(text)


def strip_mood_tags(text: str) -> str:
    """剥除 LLM 回复里的 ``<<mood:名字[:强度]>>`` 自评情绪标签（只剥不解析）。

    - 宽松形态（MOOD_TAG_RE）：心情名不校验、强度可以残缺——凡是标签形状的
      片段都剥，保证任何出站路径都不会把标签发给用户；
    - 严格的「解析 + 取最后一个合法标签」在 soul.mood.parse_mood_tag；
    - 纯函数：没有标签时原样返回（不收敛空白、不动正文）。
    """
    return MOOD_TAG_RE.sub("", str(text or ""))


def _strip_fences_and_quotes(text: str) -> str:
    """剥整体代码围栏 + 残留围栏行 + 最外层成对包裹引号（≤2 层）。"""
    cleaned = text.strip()
    wrapped = _WRAPPED_FENCE_RE.match(cleaned)
    if wrapped:
        cleaned = wrapped.group("body").strip()
    if "`" in cleaned:
        cleaned = "\n".join(
            ln for ln in cleaned.splitlines() if not _FENCE_LINE_RE.match(ln)
        ).strip()
    for _ in range(2):
        if len(cleaned) >= 2 and (cleaned[0], cleaned[-1]) in _QUOTE_PAIRS:
            cleaned = cleaned[1:-1].strip()
        else:
            break
    return cleaned


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """超出 max_chars 时优先在句末标点处截断（找不到锚点则硬截）。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    for idx in range(len(window) - 1, 0, -1):
        if window[idx] in _SENTENCE_ENDERS:
            return window[: idx + 1]
    return window


def _clean_common(text: str, *, strip_brackets: bool) -> str:
    """两出口共用的清洗主体：围栏/引号 → 碎片 token → emoji → 空白收敛。"""
    if not text:
        return ""
    cleaned = _strip_fences_and_quotes(str(text))
    cleaned = _FRAGMENT_TOKEN_RE.sub("", cleaned)
    cleaned = _EMOJI_RE.sub("", cleaned)
    if strip_brackets:
        # 颜文字组整体剥除（内容无实义字符），再清落单括号字符
        cleaned = _KAOMOJI_GROUP_RE.sub("", cleaned)
        cleaned = _BRACKET_CHARS_RE.sub("", cleaned)
    return cleaned


def clean_proactive_text(text: str, max_chars: int = 120) -> str:
    """主动消息正文清洗（R3-1f，质量门之后的兜底）。

    - 剥代码围栏 / 成对包裹引号 / 正则碎片 token / emoji / 括号（输出约束禁括号）；
    - 多行收敛为单行（聊天消息不该带换行），空白收敛；
    - 超过 max_chars 时按句末标点截断（max_chars<=0 不截）；
    - 清洗后无有效内容返回 ""（调用方按生成失败处理）。
    """
    cleaned = _clean_common(text, strip_brackets=True)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _truncate_at_sentence(cleaned, max_chars)


def clean_tts_text(text: str) -> str:
    """TTS 朗读文本清洗（R3-2b，合成前最后一道）。

    - 与 clean_proactive_text 同源规则：围栏/引号/碎片 token/emoji/颜文字/括号；
    - **不截断**（段长由分段层负责）、**不剥句末标点**（TTS 韵脚载体）；
    - 换行收敛为单个空格；
    - 清洗后为空返回 ""——调用方跳过该段合成（她宁可不念，也不念一串乱码）。
    """
    cleaned = _clean_common(text, strip_brackets=True)
    return re.sub(r"\s+", " ", cleaned).strip()
