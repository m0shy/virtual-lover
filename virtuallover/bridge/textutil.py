# -*- coding: utf-8 -*-
"""CQ 码/消息段 → 纯文本 单向降级工具。

本模块是 OneBot 桥的文本出口，方向严格单向：只把 NapCat 发来的消息
（array 段列表或字符串形态的 CQ 码）降级成可供 LLM/日志阅读的纯文本，
**绝不提供任何"纯文本→CQ 码"或"纯文本→消息段"的构造器**——发送侧一律
由 onebot_server 构造数组段，永不拼 CQ 码（SPEC §3 第 8 条）。

降级规则（SPEC §3 第 6 条：段类型 text/face/at/reply/image/record，
未知类型降级占位文本+日志）：

======================  ==============================
段类型                  纯文本形态
======================  ==============================
text                    原文（array 段不做 CQ 反转义，见下）
face(id 在表情表内)     ``[微笑]`` 这类名称
face(id 未知)           ``[表情]``
at(qq=123)              ``@123``
at(qq=all)              ``@全体成员``
reply                   ``[回复]``
image                   ``[图片]``
record                  ``[语音]``
其他/未知                ``[CQ:<type>]`` 占位 + 日志
======================  ==========================

CQ 码反转义只适用于**字符串形态**的消息（如 get_msg 回查返回的旧格式
message）：转义对 ``&amp;``→``&``、``&#91;``→``[``、``&#93;``→``]``、
``&#44;``→``,``，按单趟扫描替换（替换结果不参与二次替换，保证
``&amp;#91;`` 解码为字面量 ``&#91;`` 而不是 ``[``）。array 格式的 text 段
data.text 是原始文本、不经 CQ 转义，因此走 segments_to_plain 时**不做**
反转义。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .faces import FACE_NAMES

logger = logging.getLogger("virtuallover.bridge.textutil")

# ---------------------------------------------------------------------------
# 占位文本（收侧降级专用；发送侧禁止使用任何"构造回去"的逻辑）
# ---------------------------------------------------------------------------

PLACEHOLDER_FACE = "[表情]"
PLACEHOLDER_REPLY = "[回复]"
PLACEHOLDER_IMAGE = "[图片]"
PLACEHOLDER_RECORD = "[语音]"
PLACEHOLDER_AT_ALL = "@全体成员"

# ---------------------------------------------------------------------------
# 字符串形态 CQ 码解析
# ---------------------------------------------------------------------------

# CQ 码形如 [CQ:type,key=value,...]；参数值中的 ] 一定被转义为 &#93;，
# 因此 [^\]]* 是安全的（非规范消息只会被截断降级，不会误吞后文）。
_CQ_CODE_RE = re.compile(r"\[CQ:([A-Za-z0-9_.\-]+)((?:,[^\[\]]*)?)\]")

# 单趟反转义：&amp; 必须与数值转义并列处理，替换文本不再被二次扫描。
_CQ_UNESCAPE_MAP = {"&amp;": "&", "&#91;": "[", "&#93;": "]", "&#44;": ","}
_CQ_UNESCAPE_RE = re.compile(r"&(?:amp|#91|#93|#44);")

# 允许的 key 名（用于参数容错；未知 key 忽略不致命）
_PARAM_RE = re.compile(r"^[^=]+=")


def _cq_unescape(text: str) -> str:
    """单趟解码 CQ 码转义序列。"""
    return _CQ_UNESCAPE_RE.sub(lambda m: _CQ_UNESCAPE_MAP[m.group(0)], text)


def _parse_cq_params(raw: str) -> dict[str, str]:
    """解析 CQ 码参数段 `",k1=v1,k2=v2"`（不含开头的逗号已由正则捕获）。"""
    params: dict[str, str] = {}
    if not raw:
        return params
    for item in raw.lstrip(",").split(","):
        if not item:
            continue
        if _PARAM_RE.match(item):
            key, _, value = item.partition("=")
            params[key] = _cq_unescape(value)
        else:  # 无 = 的残缺参数：忽略键、记空值，不中断整体降级
            params.setdefault(item, "")
    return params


def _face_placeholder(data: dict[str, Any]) -> str:
    """face 段的占位文本：id 在常用表情表内 → ``[微笑]`` 这类名称。

    此前 face 段一律降级 ``[表情]`` 丢 id——她完全"看不懂"小黄脸。
    现在 id 命名（faces.FACE_NAMES，40 个常用经典 id）；表外/残缺 id
    回落通用占位 ``[表情]``，绝不编造名称。
    """
    face_id = _id_to_str(data.get("id")).strip()
    if face_id.isdigit():
        name = FACE_NAMES.get(int(face_id))
        if name is not None:
            return f"[{name}]"
    return PLACEHOLDER_FACE


def _cq_code_placeholder(ctype: str, params: dict[str, str]) -> str:
    """单个字符串形态 CQ 码的占位文本。"""
    if ctype == "face":
        return _face_placeholder(params)
    if ctype == "image":
        return PLACEHOLDER_IMAGE
    if ctype == "record":
        return PLACEHOLDER_RECORD
    if ctype == "reply":
        return PLACEHOLDER_REPLY
    if ctype == "at":
        qq = params.get("qq", "?")
        return PLACEHOLDER_AT_ALL if qq == "all" else f"@{qq}"
    return f"[CQ:{ctype}]"


def cq_to_plain(text: str) -> str:
    """字符串形态的 CQ 码消息 → 纯文本（单向）。

    文本段原样保留（含反转义），CQ 码逐个替换为占位文本；未闭合或
    非规范的 ``[CQ:...`` 片段不被正则命中，按普通文本保留。
    """
    if not text:
        return ""
    parts: list[str] = []
    pos = 0
    for m in _CQ_CODE_RE.finditer(text):
        parts.append(_cq_unescape(text[pos : m.start()]))
        parts.append(_cq_code_placeholder(m.group(1), _parse_cq_params(m.group(2))))
        pos = m.end()
    parts.append(_cq_unescape(text[pos:]))
    return "".join(parts)


# ---------------------------------------------------------------------------
# array 段降级（SPEC §3 第 6 条主路径）
# ---------------------------------------------------------------------------


def _at_placeholder(data: dict[str, Any]) -> str:
    qq = data.get("qq", "?")
    if qq == "all":
        return PLACEHOLDER_AT_ALL
    return f"@{_id_to_str(qq)}"


def _id_to_str(value: Any) -> str:
    """Number|String 双形态 ID 统一为字符串（SPEC §3 第 6 条）。"""
    if value is None:
        return ""
    if isinstance(value, bool):  # bool 是 int 子类，排除掉
        return str(value).lower()
    return str(value)


def segments_to_plain(segments: list[dict[str, Any]] | None) -> str:
    """array 格式消息段列表 → 纯文本（单向）。

    未知段类型降级为 ``[CQ:<type>]`` 占位并记 debug 日志；残缺段
    （非 dict / 缺 data）容错跳过或按空段处理，绝不抛异常。
    """
    if not segments:
        return ""
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            logger.debug("跳过非 dict 消息段：%r", seg)
            continue
        stype = seg.get("type")
        data = seg.get("data")
        if not isinstance(data, dict):
            data = {}
        if stype == "text":
            # array 格式 text 段是原始文本，不做 CQ 反转义
            parts.append(str(data.get("text", "")))
        elif stype == "face":
            parts.append(_face_placeholder(data))
        elif stype == "at":
            parts.append(_at_placeholder(data))
        elif stype == "reply":
            parts.append(PLACEHOLDER_REPLY)
        elif stype == "image":
            parts.append(PLACEHOLDER_IMAGE)
        elif stype == "record":
            parts.append(PLACEHOLDER_RECORD)
        else:
            logger.debug("未知段类型 %r，降级为占位文本", stype)
            parts.append(f"[CQ:{stype}]")
    return "".join(parts)


def find_reply_id(segments: list[dict[str, Any]] | None) -> str | None:
    """从 array 段中提取第一个 reply 段的被引用消息 id（Number|String 兼容）。

    返回字符串 id；没有 reply 段时返回 None。get_msg 回查（SPEC §3 第 7 条）
    由 onebot_server 用这个 id 发起。
    """
    if not segments:
        return None
    for seg in segments:
        if isinstance(seg, dict) and seg.get("type") == "reply":
            reply_id = (seg.get("data") or {}).get("id")
            if reply_id is not None:
                return _id_to_str(reply_id)
            return None
    return None
