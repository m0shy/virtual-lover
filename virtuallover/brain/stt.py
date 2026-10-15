# -*- coding: utf-8 -*-
"""brain/stt.py —— 语音转文字（STT）客户端：OpenAI 兼容 /audio/transcriptions。

多模态能力的语音侧（照片/语音/联网搜索三件之一）：QQ 语音消息（record 段）
经桥 ``get_record`` 取到 base64 音频后，交给本模块转写成文字，替换桥降级时
打的 ``[语音]`` 占位——LLM 看到的就是文字，走正常文本管线（人格/情绪/记忆/
分段/语音回复全部原样复用，不新增任何旁路）。

两层语义（任务定稿）：
- 云端 STT（优先）：``[chat].stt_provider`` 配置了（如 'doubao'/'whisper'）即
  启用，POST ``{[chat].api_base}/audio/transcriptions``（OpenAI 兼容协议，
  multipart/form-data：model + file 音频字节），响应 ``{"text": "..."}``；
- 本地降级（零依赖）：无 provider、或取音频/转写失败时，语音文本降级为
  ``[语音消息]`` 占位（与桥 ``[图片]`` 同款语义）——绝不引第三方依赖、绝不
  让一条听不懂的语音拖垮对话。

实现约定（对齐 brain/llm.py 同门手法）：
- 同步阻塞实现（标准库 urllib.request），urlopen 可注入便于测试；异步调用方
  经 ``transcribe_async``（asyncio.to_thread 包装）使用；
- 端点基址沿用 ``[chat].api_base``；密钥取 ``[chat].stt_api_key``，留空回退
  ``[chat].api_key``（ark 同账号 ASR 共 key 场景）——敏感值只存在
  local/config/vl.toml 与内存，日志一律不携带 key/音频原文；
- 单次尝试不重试：转写失败立即降级占位，重试只会让回复迟到（对话场景
  「慢而无 content」不如「快而承认没听清」）。

配置消费契约（duck typing，只读这些属性）：
- ``stt_provider``：非空=启用；``stt_model``：转写模型名（必填才有意义）；
- ``stt_api_key``（可空回退 api_key）；``api_base``（端点基址）；
- ``api_key``（回退密钥）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import uuid
import urllib.error
import urllib.request
from typing import Any, Callable

from virtuallover.net import smart_urlopen as _default_urlopen

logger = logging.getLogger("virtuallover.brain.stt")

__all__ = [
    "STTError", "SttClient", "build_stt_client",
    "PLACEHOLDER_VOICE", "PLACEHOLDER_RECORD",
    "STT_TIMEOUT_SECONDS", "STT_MAX_AUDIO_BYTES",
    "iter_record_files", "replace_voice_placeholder",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: STT 读超时（秒）：30s——语音消息一般几十 KB～几 MB，云端转写秒级返回；
#: 超时即降级占位，不为一条语音把对话挂太久。
STT_TIMEOUT_SECONDS = 30.0
#: 单条音频解码后字节上限（与桥 MAX_FRAME_BYTES 同量纲 32MiB；超限直接降级）
STT_MAX_AUDIO_BYTES = 32 * 1024 * 1024
#: 本地降级占位（无云端 STT / 转写失败时，替换 [语音] 占位的最终形态）
PLACEHOLDER_VOICE = "[语音消息]"
#: 桥侧降级占位（textutil.PLACEHOLDER_RECORD 的本地副本，防桥内部调整拼写漂移；
#: 替换逻辑以本常量为准）
PLACEHOLDER_RECORD = "[语音]"
#: 上传文件名/类型：get_record(out_format='mp3') 产出的就是 mp3
_AUDIO_FILENAME = "audio.mp3"
_AUDIO_MIME = "audio/mpeg"

UrlOpen = Callable[..., Any]


class STTError(RuntimeError):
    """语音转写失败（未启用/取参非法/网络/HTTP/响应解析）。文案中文、不带敏感值。"""


# ---------------------------------------------------------------------------
# 纯函数：段收集与占位替换（管线与测试共用的稳定小面）
# ---------------------------------------------------------------------------


def iter_record_files(segments: "list[dict[str, Any]] | None") -> "list[str]":
    """从桥事件段列表里按出现序提取 record 段的 file 标识（get_record 入参）。

    - record 段形如 ``{"type": "record", "data": {"file": "xxx.mp3"}}``；
    - 缺 file / 非 dict 段容错跳过；text/image/face 等其他段一律忽略；
    - 返回新列表，绝不改入参。
    """
    files: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("type") != "record":
            continue
        data = seg.get("data")
        file_id = data.get("file") if isinstance(data, dict) else None
        if isinstance(file_id, str) and file_id.strip():
            files.append(file_id.strip())
    return files


def replace_voice_placeholder(text: str, heard: str) -> str:
    """把文本里的第一个 ``[语音]`` 占位替换为转写文本（heard 为空/空白时降级占位）。

    - 转写成功：``[语音]`` → 转写原文（strip 后非空才用）；
    - 转写不可用/为空：``[语音]`` → ``[语音消息]``（本地降级占位，与 [图片] 同款）；
    - 文本里没有 ``[语音]`` 占位时原样返回（调用方短路后误调也无害）。
    """
    out = str(text or "")
    if PLACEHOLDER_RECORD not in out:
        return out
    heard_clean = str(heard or "").strip()
    replacement = heard_clean if heard_clean else PLACEHOLDER_VOICE
    return out.replace(PLACEHOLDER_RECORD, replacement, 1)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class SttClient:
    """OpenAI 兼容语音转写客户端（单次尝试，失败抛 STTError）。

    cfg 契约见模块 docstring；urlopen 可注入（测试用）。``enabled`` 为 False
    （未配 stt_provider）时 ``transcribe`` 直接抛 STTError——调用方（管线）
    应先看 ``enabled`` 走本地降级路径。
    """

    def __init__(
        self,
        chat_cfg: Any,
        *,
        urlopen: UrlOpen = _default_urlopen,
        timeout: float = STT_TIMEOUT_SECONDS,
    ) -> None:
        cfg = chat_cfg
        self.provider = str(getattr(cfg, "stt_provider", "") or "").strip()
        self.model = str(getattr(cfg, "stt_model", "") or "").strip()
        # 密钥：stt_api_key 优先，留空回退 chat api_key（同账号 ASR 共 key）
        self.api_key = (str(getattr(cfg, "stt_api_key", "") or "").strip()
                        or str(getattr(cfg, "api_key", "") or ""))
        self.api_base = str(getattr(cfg, "api_base", "") or "").strip().rstrip("/")
        self.timeout = float(timeout)
        self._open = urlopen
        #: 配了 provider 但没配 model：启用判定为 False（降级占位），
        #: 但打一次性告警把配置缺口亮出来（修好后无需改代码即生效）
        if self.provider and not self.model:
            logger.warning(
                "[stt] stt_provider=%r 已配置但 stt_model 为空，语音转写不可用——"
                "请在 vl.toml [chat] 补配 stt_model（如 'whisper-1' 或 doubao ASR "
                "模型 ID），此前语音消息一律降级为 %s 占位",
                self.provider, PLACEHOLDER_VOICE)
        if self.provider and not self.api_base:
            logger.warning(
                "[stt] stt_provider=%r 已配置但 [chat] api_base 为空，无端点可用——"
                "语音消息降级为 %s 占位", self.provider, PLACEHOLDER_VOICE)

    @property
    def enabled(self) -> bool:
        """云端 STT 是否可用（provider+model+端点齐备才为 True）。"""
        return bool(self.provider and self.model and self.api_base and self.api_key)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def transcribe(self, audio_b64: str) -> str:
        """同步转写一段 base64 音频（裸 base64，不含 data:/base64:// 前缀）。

        成功返回转写文本（可能为空串=上游没听出内容，调用方降级占位）；
        失败抛 STTError。单次尝试不重试（见模块 docstring 裁决）。
        """
        if not self.enabled:
            raise STTError(
                "云端 STT 未启用（需同时配置 [chat] stt_provider / stt_model，"
                "且 [chat] api_base 与密钥可用）")
        audio = self._decode_audio(audio_b64)
        url = f"{self.api_base}/audio/transcriptions"
        body, content_type = _multipart_body(self.model, audio)
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": content_type,
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "virtuallover-brain/1.0",
            },
            method="POST",
        )
        logger.info(
            "[stt] -> %s model=%s（%d 字节音频，provider=%s）",
            url, self.model, len(audio), self.provider)
        try:
            with self._open(request, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", None) or resp.getcode())
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 —— 错误响应体读不动就按空处理
                raw = b""
            raise STTError(
                f"语音转写 HTTP {exc.code}：{_error_summary(_decode_json(raw))}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 —— URLError/超时/连接拒绝
            raise STTError(f"语音转写网络错误（{type(exc).__name__}）：{exc}") from exc
        if status != 200:
            raise STTError(
                f"语音转写 HTTP {status}：{_error_summary(_decode_json(raw))}")
        data = _decode_json(raw)
        text = data.get("text") if isinstance(data, dict) else None
        if text is None:
            raise STTError(f"语音转写响应缺少 text 字段：{_error_summary(data)}")
        heard = str(text).strip()
        logger.info("[stt] 转写完成（provider=%s model=%s，%d 字符）",
                    self.provider, self.model, len(heard))
        return heard

    async def transcribe_async(self, audio_b64: str) -> str:
        """transcribe 的 asyncio 包装：阻塞 HTTP 移入工作线程，不卡事件循环。"""
        return await asyncio.to_thread(self.transcribe, audio_b64)

    # ------------------------------------------------------------------

    @staticmethod
    def _decode_audio(audio_b64: str) -> bytes:
        """base64 → 音频字节（容忍空白字符；空/坏 base64/超限一律 STTError）。"""
        cleaned = re.sub(r"\s+", "", str(audio_b64 or ""))
        if not cleaned:
            raise STTError("语音音频为空（get_record 未返回 base64）")
        try:
            audio = base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise STTError(f"语音音频 base64 解码失败：{type(exc).__name__}") from exc
        if not audio:
            raise STTError("语音音频解码后为 0 字节")
        if len(audio) > STT_MAX_AUDIO_BYTES:
            raise STTError(
                f"语音音频 {len(audio)} 字节超过 {STT_MAX_AUDIO_BYTES} 上限")
        return audio


def build_stt_client(chat_cfg: Any, **kwargs: Any) -> SttClient:
    """从 [chat] 配置构造 SttClient（未配置 provider 也照常构造，enabled=False）。"""
    return SttClient(chat_cfg, **kwargs)


# ---------------------------------------------------------------------------
# HTTP 小件（multipart 编码 / JSON 解码 / 安全错误摘要，全部标准库）
# ---------------------------------------------------------------------------


def _multipart_body(model: str, audio: bytes) -> "tuple[bytes, str]":
    """构造 OpenAI 兼容 /audio/transcriptions 的 multipart/form-data 请求体。

    两个 part：``model``（文本字段）与 ``file``（音频二进制，filename 固定
    audio.mp3）。返回 (body, Content-Type 头值)。boundary 用 uuid4 随机生成
    （防音频字节里恰好出现边界串）。
    """
    boundary = f"vl-stt-{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def _field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    parts.append(_field("model", model))
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{_AUDIO_FILENAME}"\r\n'
            f"Content-Type: {_AUDIO_MIME}\r\n\r\n"
        ).encode("utf-8")
        + audio
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def _decode_json(raw: bytes) -> Any:
    """响应体 JSON 解码；失败时返回占位 dict（只记长度，原文不进日志）。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_non_json_body": f"<{len(raw)} bytes>"}


def _error_summary(data: Any) -> str:
    """错误响应的保守摘要：只取 error.type/code/param 枚举性字段。

    转写错误体可能回显密钥或上游内部信息，自由文本（message 等）与未知
    结构一律不进日志（手法对齐 brain/llm._error_summary）。
    """
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            parts = [str(error[k]) for k in ("type", "code", "param") if error.get(k)]
            if parts:
                return "error." + "/".join(parts)
            return "error（未提供 type/code）"
        if data.get("_non_json_body") is not None:
            return "非 JSON 响应体"
    return "响应体结构未识别"
