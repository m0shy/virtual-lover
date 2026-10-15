# -*- coding: utf-8 -*-
"""brain/vision.py —— 图片识别（云 vision 模型 + 占位降级，多模态能力①）。

链路（多模态任务书）：
- QQ 桥收到 image 段时已有 ``data.url``（NapCat 媒体 URL；桥侧 ``_spawn_media_saves``
  另做后台转存，两边各自独立下载，互不依赖）；
- 管线（brain/pipeline.py）对带图消息改调 :func:`process_image`：``[chat].supports_vision``
  =true 时下载图片 → base64 → 构造 OpenAI 多模态消息
  ``{role:"user", content:[{type:"text"},{type:"image_url", image_url:{url:"data:…;base64,…"}}]}``
  发给 chat 客户端分析，返回的中文描述替换消息文本里的 ``[图片]`` 占位；
- ``supports_vision=false``（默认）→ 维持 ``[图片]`` 占位（与旧版行为零差异），
  并一次性日志提示「配了视觉模型后可打开」；
- 控制台聊天（console/core.py）粘贴图片 URL / data URI base64 → 同一链路
  （:func:`extract_image_refs` 提取 + :func:`process_image` / :func:`process_image_bytes`）。

设计约束：
- 只用标准库 urllib + 既有 ``net.smart_urlopen``（回环/私网直连、公网走系统
  代理，与桥媒体转存同一网络策略），零新依赖；
- 图片识别是增强能力，**任何失败一律降级为 ``[图片]`` 占位**，绝不阻断对话；
- base64 只在内存流转，绝不写日志（大小上限 32 MiB 与桥媒体转存对齐）；
- 下载经 ``asyncio.to_thread`` 进工作线程，事件循环零阻塞。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from virtuallover.brain.llm import content_to_text
from virtuallover.net import smart_urlopen

logger = logging.getLogger("virtuallover.brain.vision")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 与桥 textutil.PLACEHOLDER_IMAGE 同语义的占位文本（降级兜底，唯一出口）
PLACEHOLDER_IMAGE = "[图片]"

#: 图片下载超时（秒；对齐桥 MEDIA_DOWNLOAD_TIMEOUT）
DEFAULT_DOWNLOAD_TIMEOUT = 30.0

#: 单张图片字节上限（对齐桥 MAX_MEDIA_BYTES / 帧上限 32 MiB）
MAX_IMAGE_BYTES = 32 * 1024 * 1024

#: 无附言时视觉模型的默认指令
DESCRIBE_PROMPT = "请用简洁的中文描述这张图片的内容。"

#: 有附言时追加的指令（附言本身作为 user 文本，让模型结合图片回应）
CAPTION_SUFFIX = "（请同时用简洁的中文描述这张图片）"

#: URL 后缀 → MIME（魔数嗅探失败时的兜底）
_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

#: 默认 MIME（OpenAI 兼容端点最普遍接受的形态）
_DEFAULT_MIME = "image/jpeg"

# ---------------------------------------------------------------------------
# 控制台侧图片引用提取（粘贴 URL / base64 两种形态）
# ---------------------------------------------------------------------------

# data URI：data:image/png;base64,<payload>——payload 不含空白（粘贴形态连续）
_DATA_URI_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=]+)")
# http(s) 图片 URL：以常见图片后缀结尾（可带查询串）
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s<>\"']+\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?[^\s<>\"']*)?",
    re.IGNORECASE)
# 裸 base64：≥256 字符的连续 b64 字符集（无 data: 前缀的粘贴形态）；
# 256 下限把普通英文单词/短串排除在外（误报率可忽略）
_BARE_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{256,}={0,2}(?![A-Za-z0-9+/=])")

#: 单条消息最多识别的图片数（防滥用；超出部分按占位处理）
MAX_IMAGES_PER_MESSAGE = 3


def extract_image_refs(message: str) -> "tuple[str, list[tuple[str, str]]]":
    """从消息文本提取图片引用，返回（清理后文本, 引用列表）。

    引用形态（按优先级）：``("b64", payload)``（data URI）/ ``("url", url)``
    / ``("b64", payload)``（裸 base64）。清理文本把引用替换为空格并压掉
    多余空白，作为图片附言（caption）使用。纯函数，不改入参。
    """
    refs: list[tuple[str, str]] = []
    text = str(message or "")

    def _sub_data(match: "re.Match[str]") -> str:
        refs.append(("b64", match.group(1)))
        return " "

    def _sub_url(match: "re.Match[str]") -> str:
        refs.append(("url", match.group(0)))
        return " "

    def _sub_b64(match: "re.Match[str]") -> str:
        refs.append(("b64", match.group(0)))
        return " "

    # 顺序关键：data URI 里的 payload 也是合法裸 b64，必须先吃掉
    text = _DATA_URI_RE.sub(_sub_data, text)
    text = _IMAGE_URL_RE.sub(_sub_url, text)
    text = _BARE_B64_RE.sub(_sub_b64, text)
    return " ".join(text.split()), refs


# ---------------------------------------------------------------------------
# 图片下载与编码
# ---------------------------------------------------------------------------

def download_image(
    url: str,
    *,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
    max_bytes: int = MAX_IMAGE_BYTES,
    urlopen: Callable[..., Any] = smart_urlopen,
) -> bytes:
    """下载图片 → bytes（标准库 urllib + smart_urlopen，同步阻塞）。

    超过 ``max_bytes`` 抛 ValueError；网络错误原样上抛（调用方降级为占位）。
    异步调用方经 ``asyncio.to_thread`` 包装使用（process_image 内已包装）。
    """
    request = urllib_request(url)
    with urlopen(request, timeout=timeout) as resp:
        payload = resp.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"图片超过 {max_bytes} 字节上限，放弃识别")
    return payload


def urllib_request(url: str) -> Any:
    """构造图片下载请求（独立成函数便于测试打桩）。"""
    import urllib.request

    return urllib.request.Request(
        url, headers={"User-Agent": "virtuallover-vision/1.0"})


def sniff_image_mime(data: bytes, url: str = "") -> str:
    """按魔数嗅探图片 MIME；嗅探不出时回退 URL 后缀，再回退 image/jpeg。

    只认视觉模型普遍支持的五种位图形态；其余一律按 image/jpeg 兜底（上游
    对错误 MIME 的容忍度远高于对缺失 data: 前缀的容忍度）。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    ext = Path(urlparse(url).path).suffix.lower() if url else ""
    return _EXT_MIME.get(ext, _DEFAULT_MIME)


def make_image_data_url(data: bytes, url: str = "") -> str:
    """图片字节 → ``data:<mime>;base64,<b64>``（OpenAI 多模态 image_url 形态）。"""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{sniff_image_mime(data, url)};base64,{b64}"


# ---------------------------------------------------------------------------
# OpenAI 多模态消息构造
# ---------------------------------------------------------------------------

def build_vision_message(caption_text: str, image_data_url: str) -> dict:
    """构造 OpenAI 多模态 user 消息（text 段 + image_url 段）。

    - 有附言：附言做正文、指令做后缀（模型结合图片回应附言并描述图片）；
    - 无附言：默认描述指令（DESCRIBE_PROMPT）。
    """
    caption = str(caption_text or "").strip()
    if caption:
        text = f"{caption}\n{CAPTION_SUFFIX}"
    else:
        text = DESCRIBE_PROMPT
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }


def supports_vision(cfg: Any) -> bool:
    """读 ``[chat].supports_vision``（缺配置/缺键一律 False=占位降级）。"""
    chat_cfg = getattr(cfg, "chat", None)
    return bool(getattr(chat_cfg, "supports_vision", False))


def _has_chat_client(chat_client: Any) -> bool:
    """chat 客户端可用性判定（ChatClient / BrainRouter 同款 duck typing）。"""
    return chat_client is not None and callable(getattr(chat_client, "chat_async", None))


# ---------------------------------------------------------------------------
# 降级提示（一次性日志：配了视觉模型后可打开开关）
# ---------------------------------------------------------------------------

_vision_hint_logged = False


def _hint_vision_off() -> None:
    """视觉未开启时的一次性指引（INFO；进程内只提示一次，不当刷屏日志）。"""
    global _vision_hint_logged
    if _vision_hint_logged:
        return
    _vision_hint_logged = True
    logger.info(
        "[vision] 当前未开启图片识别（[chat] supports_vision=false），"
        "图片按 %s 占位处理；给 [chat] 换支持视觉的模型（如 doubao-vision / "
        "glm-4v / gpt-4o 系列并把 api_base/key/model 配好后）再把 supports_vision "
        "改为 true，她就能看懂照片了", PLACEHOLDER_IMAGE)


# ---------------------------------------------------------------------------
# 主入口：process_image（URL）/ process_image_bytes（内存字节）
# ---------------------------------------------------------------------------

async def process_image(
    url: str,
    caption_text: str = "",
    *,
    chat_client: Any = None,
    supports_vision_flag: "bool | None" = None,
    cfg: Any = None,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
    download_fn: "Callable[..., bytes] | None" = None,
) -> str:
    """下载并识别一张图片，返回中文描述（任何失败返回 ``[图片]`` 占位）。

    参数：
    - ``url``：图片 URL（NapCat 媒体 URL / 控制台粘贴的任意外链）；
    - ``caption_text``：图片附言（QQ 场景=同条消息的文本，图片占位已剥除）；
    - ``chat_client``：chat 客户端（ChatClient 或 BrainRouter 包装，duck typing）；
    - 开关来源优先级：``supports_vision_flag`` 显式传参 > ``cfg`` 配置读取 >
      False。开关关闭或客户端不可用时零网络调用，直接返回占位（现行为）。

    该函数是管线/控制台共用的唯一入口；实现上先下载（工作线程）再走
    :func:`process_image_bytes` 的分析路径。
    """
    if supports_vision_flag is None:
        supports_vision_flag = supports_vision(cfg) if cfg is not None else False
    if not supports_vision_flag or not _has_chat_client(chat_client):
        _hint_vision_off()
        return PLACEHOLDER_IMAGE
    # download_fn 缺省走模块级 download_image（运行时解析：测试可 monkeypatch
    # 替换下载，管线/控制台侧无需感知注入点）
    downloader = download_fn if download_fn is not None else download_image
    try:
        data = await asyncio.to_thread(downloader, url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— 下载失败降级占位，绝不阻断对话
        logger.warning("[vision] 图片下载失败（%s：%s），按占位处理",
                       type(exc).__name__, str(exc)[:120])
        return PLACEHOLDER_IMAGE
    return await process_image_bytes(
        data, caption_text, chat_client=chat_client)


async def process_image_bytes(
    data: bytes,
    caption_text: str = "",
    *,
    chat_client: Any = None,
    supports_vision_flag: bool = True,
) -> str:
    """识别内存中的图片字节（base64 只在内存流转，绝不落日志）。

    调用方已保证开关与客户端可用（本方法不再重复 hint）；空字节/分析失败/
    空回复一律返回占位。成功返回视觉模型的纯文本描述。
    """
    if not _has_chat_client(chat_client):
        return PLACEHOLDER_IMAGE
    if not data:
        return PLACEHOLDER_IMAGE
    message = build_vision_message(caption_text, make_image_data_url(data))
    try:
        reply = await chat_client.chat_async(
            [message], extra_body={"prompt_cache_key": "vl-vision"})
    except Exception as exc:  # noqa: BLE001 —— 视觉调用失败降级占位
        logger.warning("[vision] 视觉模型分析失败（%s：%s），按占位处理",
                       type(exc).__name__, str(exc)[:120])
        return PLACEHOLDER_IMAGE
    desc = content_to_text(getattr(reply, "content", "")).strip()
    if not desc:
        logger.warning("[vision] 视觉模型返回空描述，按占位处理")
        return PLACEHOLDER_IMAGE
    return desc
