# -*- coding: utf-8 -*-
"""GPT-SoVITS-Inference（GSVI）HTTP 客户端 v2（VOICE_SPEC §1，改造自 SPEC §9 客户端）。

三块职责：
- ``synthesize(text, emotion=..., speed_factor=...)``：GET /tts 合成。契约沿用
  SPEC §9 实测裁定：别名参数成对携带（cha_name/character、character_emotion/
  emotion、text_language/lang、format=wav）、3 次尝试、60s 超时、响应体 RIFF
  校验、失败返回 None（调用方回退纯文字）。情绪/语速参数由调用方传入映射结果
  （speed_factor=None 不传，服务端按默认 1.0）；
- ``character_list()``：GET /character_list 注册表（角色 → 注册情绪列表）。
  **只拉一次缓存**（``force`` 显式刷新除外）——服务端每次调用都会重写
  character_info.json（voice-engine/Inference/src/load_infer_info.py:304），
  禁止轮询；就绪后拉一次、恢复后可强制刷新；
- ``probe()``：TCP connect 探测（1s 超时）。就绪判定只用 TCP——
  character_list 每调写盘不可作探测（VOICE_SPEC §0 终裁）。

GSVI 怪癖（源码核实，Inference/src/tts_backend.py）：
- string 参数统一 ``unquote(...).lower()``（:125）——角色/情绪大小写在服务端
  不敏感，注册表成员比较一律小写化；
- 未注册情绪**静默**按默认情绪出 200+wav（客户端侧映射回退链负责拦）；
- 未注册角色返回 400 + JSON ``{"error": ...}``（:154）——按合成失败处理。

依赖策略：框架仅允许 websockets+numpy 两个非标准库依赖，HTTP 走标准库
urllib（同步）+ ``asyncio.to_thread`` 包装（单用户低并发下线程池开销可忽略）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

__all__ = ["GsviClient", "GptSoVitsClient", "GSVI_AUTH_USER"]

logger = logging.getLogger("virtuallover.voice.gsvi")

# wav 帧头（RIFF/WAVE）：服务端出错时可能以 200 + JSON 报错体返回，用帧头判真伪
_WAV_MAGIC = b"RIFF"

# 引擎侧 Basic 认证用户名（FUSION_SPEC §3.1：口令单源 vl.toml [tts].
# tts_auth_password，用户名固定常量——lifecycle 拉起前把 {GSVI_AUTH_USER: 口令}
# 注入引擎 Inference/config.json 的 user 表，两侧同源即通）
GSVI_AUTH_USER = "vl"


def tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    """一次 TCP connect 探测（同步；调用方负责放线程）。成功即端口有主。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class GsviClient:
    """GSVI HTTP 客户端：/tts 合成 + /character_list 注册表缓存 + TCP 探测。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5000",  # 宿主机直连（VOICE_SPEC §1/§5 归一化基线）
        character: str = "",
        emotion: str = "",        # 兜底情绪（映射结果缺省时使用；旧字段语义保留）
        text_lang: str = "zh",
        audio_format: str = "wav",
        timeout: float = 60.0,    # SPEC §9：60s
        retries: int = 3,         # SPEC §9：3 次尝试
        backoff_base: float = 0.5,   # 第 n 次失败后睡 backoff_base * 2^(n-1) 秒
        probe_timeout: float = 1.0,  # VOICE_SPEC §0：TCP connect 1s
        auth_password: str = "",  # Basic 认证口令（vl.toml [tts].tts_auth_password；空=不带认证头）
    ) -> None:
        if retries < 1:
            raise ValueError("retries 至少为 1（首次尝试）")
        self.base_url = base_url.rstrip("/")
        self.character = character
        self.emotion = emotion
        self.text_lang = text_lang
        self.audio_format = audio_format
        self.timeout = timeout
        self.retries = retries
        self.backoff_base = backoff_base
        self.probe_timeout = probe_timeout
        self.auth_password = str(auth_password or "")
        # 注册表缓存：None=尚未拉取；拉取成功后常驻（服务端禁轮询，仅显式刷新）
        self._registry: "Optional[dict[str, list[str]]]" = None
        self._registry_fetched_at: float = 0.0

    # ------------------------------------------------------------------ 探测

    def _auth_headers(self) -> "dict[str, str]":
        """Basic 认证头（FUSION_SPEC §3.1）：配置了口令即携带——服务端
        enable_auth=false 时 flask_httpauth 直接放行、头被忽略，因此客户端
        无条件带（口令在）与「引擎侧开关状态」解耦，谁先重启都对齐。"""
        if not self.auth_password:
            return {}
        token = base64.b64encode(
            f"{GSVI_AUTH_USER}:{self.auth_password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base_url).hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        return urllib.parse.urlsplit(self.base_url).port or 5000

    def probe(self, timeout: "Optional[float]" = None) -> bool:
        """TCP connect 一次（同步阻塞，async 侧经 to_thread 调用）。"""
        return tcp_probe(self.host, self.port,
                         timeout=self.probe_timeout if timeout is None else timeout)

    async def probe_async(self, timeout: "Optional[float]" = None) -> bool:
        return await asyncio.to_thread(self.probe, timeout)

    # ------------------------------------------------------------------ 注册表

    async def character_list(self, *, force: bool = False) -> "Optional[dict[str, list[str]]]":
        """角色 → 注册情绪列表；缓存命中直接返回，未拉取/force 才出网。

        拉取失败返回 None（缓存保持原状）；注册表不可用时调用方按
        「不做成员校验」处理（GSVI 对未知情绪静默兜底，行为可预期）。
        """
        if self._registry is not None and not force:
            return self._registry
        try:
            registry = await asyncio.to_thread(self._get_character_list_once)
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError, ValueError) as exc:
            logger.warning("[voice] /character_list 拉取失败（沿用既有缓存态）：%s", exc)
            return self._registry
        if registry is None:
            return self._registry
        self._registry = registry
        self._registry_fetched_at = asyncio.get_running_loop().time()
        logger.info("[voice] 注册表已拉取：%d 个角色（%s）",
                    len(registry), ", ".join(sorted(registry)))
        return self._registry

    def registry_for(self, character: str) -> "Optional[list[str]]":
        """某角色的注册情绪列表（小写不敏感匹配键）；注册表未拉取返回 None。"""
        if self._registry is None:
            return None
        want = str(character).strip().lower()
        for name, emotions in self._registry.items():
            if str(name).strip().lower() == want:
                return list(emotions)
        return None

    def _get_character_list_once(self) -> "Optional[dict[str, list[str]]]":
        url = f"{self.base_url}/character_list"
        req = urllib.request.Request(url, method="GET", headers=self._auth_headers())
        # GSVI 在本机 127.0.0.1:5000——net.smart_urlopen 禁代理直连（回环流量
        # 走系统代理会被劫持，语音引擎探测/合成全部假死）
        from ..net import smart_urlopen
        with smart_urlopen(req, timeout=max(self.timeout, self.probe_timeout)) as resp:
            if resp.status != 200:
                raise ValueError(f"/character_list 返回 HTTP {resp.status}")
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("/character_list 响应不是 dict")
        registry: "dict[str, list[str]]" = {}
        for name, emotions in data.items():
            if isinstance(emotions, list):
                registry[str(name)] = [str(e) for e in emotions]
            else:
                registry[str(name)] = []
        return registry

    # ------------------------------------------------------------------ 合成

    async def synthesize(
        self, text: str, *, emotion: "Optional[str]" = None,
        speed_factor: "Optional[float]" = None,
    ) -> "Optional[bytes]":
        """合成语音，成功返回 wav 字节；全部尝试失败返回 None。

        emotion：本次合成使用的音色注册情绪（映射模块产出）；None 时回落
        构造时的默认情绪。
        speed_factor：语速倍率（mapping.mood_speed 产出；服务端非经典模式原生
        支持，voice-engine/app.py:76,124）；None=不传该参（服务端按默认 1.0），
        给定则原样写进 /tts 请求。
        空/纯空白文本直接抛 ValueError（调用方应在发送管线里先过滤）。
        """
        if not text or not text.strip():
            raise ValueError("synthesize(text) 收到空文本：发送侧应先过滤空回复")
        use_emotion = self.emotion if emotion is None else emotion
        last_err: "Exception | None" = None
        for attempt in range(1, self.retries + 1):
            try:
                return await asyncio.to_thread(
                    self._get_once, text, use_emotion, speed_factor)
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError, ValueError) as exc:
                last_err = exc
                logger.warning("TTS 第 %d/%d 次尝试失败（text 长度 %d）：%s",
                               attempt, self.retries, len(text), exc)
                if attempt < self.retries:
                    await asyncio.sleep(self.backoff_base * (2 ** (attempt - 1)))
        logger.error("TTS 全部 %d 次尝试失败，回退纯文字（最后错误：%r）",
                     self.retries, last_err)
        return None

    # -------------------------------------------------------------- 内部实现

    def _get_once(
        self, text: str, emotion: str, speed_factor: "Optional[float]" = None
    ) -> bytes:
        """同步执行一次 GET /tts。任何失败以异常表达（URLError/HTTPError/ValueError）。"""
        params = {
            # 别名参数成对携带：服务端按其注册名取用（SPEC §9）
            "text": text,
            "cha_name": self.character,
            "character": self.character,
            "character_emotion": emotion,
            "emotion": emotion,
            "text_language": self.text_lang,
            "lang": self.text_lang,
            "format": self.audio_format,
        }
        if speed_factor is not None:
            # 语速倍率（None 不传——键缺位即服务端默认 1.0，与既有行为逐字节兼容）
            params["speed_factor"] = speed_factor
        url = f"{self.base_url}/tts?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, method="GET", headers=self._auth_headers())
        from ..net import smart_urlopen  # 同上：GSVI 回环直连
        with smart_urlopen(req, timeout=self.timeout) as resp:
            if resp.status != 200:
                raise ValueError(f"TTS 服务返回 HTTP {resp.status}")
            body = resp.read()
        if len(body) < 44 or not body.startswith(_WAV_MAGIC):
            # 200 但不是 wav（多为服务端 JSON 报错体）→ 视为失败，进入重试
            raise ValueError("TTS 响应不是 wav（缺少 RIFF 帧头），判定为服务端报错")
        return body


# 旧名兼容：SPEC §9 时代的类名（tests/test_gptsovits.py 与既有管线引用）
GptSoVitsClient = GsviClient
