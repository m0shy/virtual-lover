# -*- coding: utf-8 -*-
"""brain/llm.py —— OpenAI 兼容 chat + embedding 双客户端（SPEC §4）。

行为要点（全部出自 SPEC §4，数值默认值取现网配置实测）：
- chat：非流式 POST {base}/chat/completions；读超时 120s（provider_sources.timeout
  实测）；指数退避重试 ≥3 次（不含首次共 4 次尝试）；HTTP 429 可配重试且优先
  尊重 Retry-After；重试耗尽后按 fallback 客户端列表依次兜底（初版为空）；
  收到 tool_calls 容错忽略（记警告，不影响文本内容提取）。
- embedding：独立配置（ark endpoint、独立 key、model、dim=2048、timeout=20s 实测），
  与 chat 分字段配置、互不共享连接。
- think 处理：请求侧把 assistant 历史 think 段剥离不回传（分段列表 type=="think"
  与文本 <think>...</think> 两种形态都剥）；响应侧 reasoning_content 不入库
  不回发——仅随 LLMReply.reasoning 暴露，落盘存档与否由调用方决定。
- content 双形态：字符串与分段列表（[{type: text|think, ...}]）都接受，
  扁平化为纯文本（只取 text 段，与 data_v4.db 实测历史结构对齐）。
- 日志打码：请求日志只含 URL/model/消息条数与字符数；任何异常日志经过
  mask_secrets 打码，绝不出现 api_key / Authorization / 消息体原文。

配置消费契约（duck typing，避免与骨架 config.py 的字段树强耦合）：
- chat 配置对象需有属性：api_base / api_key / model / timeout，
  可选 max_retries（默认 3）、retry_on_429（默认 True）、fallback_provider_ids
  （本类不直接消费，fallback 客户端由装配方按该列表显式构造并经 fallbacks 注入）、
  thinking / bg_thinking / timeout_main（PERF_SPEC §2 O1 双实例装配，
  build_chat_clients 消费）；
- embedding 配置对象需有属性：api_base / api_key / model / dim / timeout。

本模块同步阻塞实现（标准库 urllib.request），urlopen/sleep 均可注入便于测试；
异步调用方经 chat_async / embed_async（asyncio.to_thread 包装）使用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from virtuallover.net import smart_urlopen as _default_urlopen

logger = logging.getLogger("virtuallover.brain.llm")

# ---------------------------------------------------------------------------
# 常量与默认值（数值均为现网配置实测，详见 SPEC §4）
# ---------------------------------------------------------------------------

CHAT_TIMEOUT_SECONDS = 120.0
EMBEDDING_TIMEOUT_SECONDS = 20.0
EMBEDDING_DIM = 2048

CHAT_MAX_RETRIES = 3          # 指数退避重试次数下限（SPEC §4「≥3」）
EMBEDDING_MAX_RETRIES = 2     # embedding 无 SPEC 硬性要求，保守给 2 次
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0

# embedding 向量缓存内存上限（PERF_SPEC §3-O7）：64MB；dim=2048 float32 单条
# 恰 8KB → 即 8192 条 LRU。随实例构造（禁模块级全局），0=禁用缓存。
EMBEDDING_CACHE_MAX_BYTES = 64 * 1024 * 1024

# 触发重试的服务端状态码（429 单独受 retry_on_429 开关控制）
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

# 响应 content 分段列表中已知段类型：text 取文本、think 剥离；其余忽略
_SEGMENT_TYPE_TEXT = "text"
_SEGMENT_TYPE_THINK = "think"

# 文本形态 think 标签（deepseek 风格：<think>…</think> 出现在 content 字符串里）
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_TAG_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)  # 未闭合残段兜底

# 日志打码：键名小写化后含这些子串的值一律替换为占位符
_SECRET_KEY_MARKS = ("key", "token", "secret", "password", "authorization")
_SECRET_PLACEHOLDER = "<masked>"

UrlOpen = Callable[..., Any]
SleepFn = Callable[[float], None]

# 真探活观测面常量（五轮修缮 R2）
#: recent_failure 的默认回看窗口：10 分钟内的失败才算「最近」（与控制台
#: brain 行黄灯判定同窗）
RECENT_FAILURE_WINDOW_SECONDS = 600.0
_LAST_ERROR_MAX_CHARS = 300  # last_error 串上限（观测值，防异常串无限长）
_RECENT_FAILURE_WINDOW = RECENT_FAILURE_WINDOW_SECONDS

# embedding 熔断常量（五轮修缮 R2）：连续 3 次调用级失败（每次调用已含内部
# 重试）→ 跳过请求 10 分钟，期间记忆检索按「降级不注入」继续，不拖垮对话
EMBED_BREAKER_THRESHOLD = 3
EMBED_BREAKER_OPEN_SECONDS = 600.0


# ---------------------------------------------------------------------------
# 数据模型与异常
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMReply:
    """一次 chat 调用的规整结果。

    content       扁平化后的纯文本回复（think 已剥离），可能为空串；
    reasoning     响应侧 reasoning_content 原样保留——调用方不得将其写入
                  历史或回发，仅可用于本地存档（SPEC §4「落盘存档可选」）；
    finish_reason 上游原值，观测用；
    provider_id   实际成功的客户端标识（主客户端或某个 fallback）；
    attempts      本次成功（或最终失败）累计发起的 HTTP 请求次数。
    """

    content: str
    reasoning: str | None = None
    finish_reason: str | None = None
    provider_id: str = ""
    attempts: int = 0
    raw: dict | None = None


class LLMError(RuntimeError):
    """LLM 调用最终失败（重试与 fallback 均已耗尽，或遇到不可重试错误）。"""

    def __init__(self, message: str, *, status: int | None = None,
                 attempts: int = 0, retriable: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.attempts = attempts
        self.retriable = retriable
        self.retry_after = retry_after


class EmbeddingDimError(LLMError):
    """embedding 返回维度与配置 dim 不一致（入库前必须拦下，防向量库污染）。"""


# ---------------------------------------------------------------------------
# 纯函数：content 双形态扁平化 / think 剥离 / 日志打码
# ---------------------------------------------------------------------------


def content_to_text(content: Any) -> str:
    """把 OpenAI 兼容消息的 content 扁平化为纯文本（SPEC §4 双形态）。

    - 字符串原样返回；
    - 分段列表只取 {"type": "text", "text": ...} 段拼接（think/图片等其余段
      一律忽略），与 data_v4.db 实测历史结构（text/think 段）对齐；
    - 兼容带 get_text() / .text 属性的段对象；
    - None 与其他类型返回空串或字符串化结果，绝不抛异常。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # 单段 dict 形态（{"type":"text","text":...}）
        if content.get("type") in (None, _SEGMENT_TYPE_TEXT):
            return str(content.get("text", ""))
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for seg in content:
            if isinstance(seg, dict):
                if seg.get("type") in (None, _SEGMENT_TYPE_TEXT):
                    parts.append(str(seg.get("text", "")))
            elif hasattr(seg, "get_text"):
                parts.append(str(seg.get_text()))
            elif hasattr(seg, "text"):
                parts.append(str(getattr(seg, "text", "")))
            elif isinstance(seg, str):
                parts.append(seg)
        return "".join(parts)
    return str(content)


def strip_think_tags(text: str) -> str:
    """剥离文本形态的 think 标签段（<think>…</think> 及未闭合残段）。"""
    if not text or "<think>" not in text:
        return text
    cleaned = _THINK_TAG_RE.sub("", text)
    # 未闭合的 <think>…（流式截断等残段）：从 <think> 起整段丢弃
    if "<think>" in cleaned:
        cleaned = _THINK_TAG_OPEN_RE.sub("", cleaned)
    return cleaned


def strip_assistant_think(messages: Iterable[dict]) -> list[dict]:
    """请求侧 think 剥离（SPEC §4：assistant 历史 think 段一律剥离不回传）。

    - assistant 消息：分段列表里 type=="think" 的段丢弃，文本形态
      <think>…</think> 标签剥掉；reasoning_content 字段整体移除；
    - 其余 role 原样保留（user 内容里的 <think> 是用户的话，不碰）；
    - 返回新列表（含新 dict），不改入参。
    """
    cleaned: list[dict] = []
    for msg in messages:
        item = dict(msg)
        if str(item.get("role", "")) == "assistant":
            item.pop("reasoning_content", None)
            content = item.get("content")
            if isinstance(content, list):
                content = [
                    seg for seg in content
                    if not (isinstance(seg, dict) and seg.get("type") == _SEGMENT_TYPE_THINK)
                ]
                item["content"] = content
            text = content_to_text(content)
            if "<think>" in text:
                text = strip_think_tags(text)
                item["content"] = text
        cleaned.append(item)
    return cleaned


def mask_secrets(value: Any) -> Any:
    """递归打码 dict/list 中键名含敏感子串的值（返回新结构，不改入参）。

    用于异常日志中的响应体/请求体片段：保证 api_key、token、Authorization
    等字段绝不进入日志（SPEC §4「请求日志对 Authorization 与消息体密钥字段
    打码」）。
    """
    if isinstance(value, dict):
        masked: dict = {}
        for key, val in value.items():
            key_l = str(key).lower()
            if any(mark in key_l for mark in _SECRET_KEY_MARKS):
                masked[key] = _SECRET_PLACEHOLDER
            else:
                masked[key] = mask_secrets(val)
        return masked
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def _error_summary(data: Any) -> str:
    """错误响应的保守日志摘要：只取结构字段，不带原文。

    聊天场景的错误体可能回显密钥（如「Incorrect API key provided: sk-…」）
    或用户正文，因此只提取 error.type / error.code / error.param 等枚举性
    字段供排障；message 等自由文本与未知结构一律不进日志。
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


# ---------------------------------------------------------------------------
# chat 客户端
# ---------------------------------------------------------------------------


class ChatClient:
    """OpenAI 兼容 chat 客户端（非流式），带重试 / 429 退避 / fallback 链。

    cfg 契约见模块 docstring。urlopen 与 sleep 可注入（测试用）；
    fallbacks 为 ChatClient 序列，主客户端重试耗尽后依次尝试，
    每个 fallback 自带各自的超时与重试策略。

    ``extra_body``（PERF_SPEC §2 O1）：客户端级默认附加字段（如 ark 的
    ``{"thinking": {"type": "disabled"}}``），在 _request_once 组装请求体时
    与当次 payload 合并（调用方 extra_body 优先）。合并在**本实例**的请求层
    而非 chat() 的公共 payload 层——fallback 客户端复用同一 payload 重发时
    绝不继承主实例的 ark 专属字段（各 fallback 用各自的 extra_body）。
    """

    def __init__(
        self,
        cfg: Any,
        *,
        provider_id: str = "chat",
        # 本机端点感知 urlopen（virtuallover/net.py）：本地大脑 127.0.0.1:8002
        # 直连（本机系统代理会劫持回环流量返 404，她整段哑火的第二层根因），
        # 公网云端保持系统默认（用户可能靠代理出网）
        urlopen: UrlOpen = _default_urlopen,
        sleep: SleepFn = time.sleep,
        fallbacks: "Sequence[ChatClient] | None" = None,
        extra_body: "dict | None" = None,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self.api_base = str(cfg.api_base).rstrip("/")
        self.api_key = str(cfg.api_key)
        self.model = str(cfg.model)
        self.timeout = float(getattr(cfg, "timeout", CHAT_TIMEOUT_SECONDS))
        self.max_retries = int(getattr(cfg, "max_retries", CHAT_MAX_RETRIES))
        self.retry_on_429 = bool(getattr(cfg, "retry_on_429", True))
        self.provider_id = provider_id or "chat"
        self.extra_body: dict = dict(extra_body or {})
        self._open = urlopen
        self._sleep = sleep
        self.fallbacks: list[ChatClient] = list(fallbacks or [])
        # ---- 真探活观测面（五轮修缮 R2：ark 欠费 403 哑火不可见问题）----
        # wall-clock 基准（time.time）：控制台按「距上次失败多久」渲染黄灯，
        # 跨线程读（asyncio.to_thread 工作线程写、控制台线程读）——单属性
        # 赋值在 CPython 下原子，无需加锁。
        self._clock = clock or time.time
        #: 最近一次最终失败的原因串（LLMError 的 str，含 HTTP code 与
        #: error.code 摘要——_error_summary 已保证不带密钥/正文）；成功清零。
        self.last_error: str = ""
        #: 最近一次失败的 wall-clock 时刻（秒）；0=无失败记录。
        self.last_error_ts: float = 0.0
        #: 最近一次成功的 wall-clock 时刻（秒）；0=从未成功。
        self.last_ok_ts: float = 0.0

    # ------------------------------------------------------------------
    # 真探活观测面（控制台 brain 行黄灯消费）
    # ------------------------------------------------------------------

    def _record_success(self) -> None:
        """调用成功：清失败痕迹并记成功时刻（探活语义「成功清零」）。"""
        self.last_error = ""
        self.last_error_ts = 0.0
        self.last_ok_ts = self._clock()

    def _record_failure(self, exc: "LLMError | Exception") -> None:
        """调用最终失败：记原因串与时刻（重试/fallback 全耗尽才走到这里）。"""
        self.last_error = str(exc)[:_LAST_ERROR_MAX_CHARS]
        self.last_error_ts = self._clock()

    def recent_failure(self, window_seconds: float = _RECENT_FAILURE_WINDOW) -> str:
        """window 秒内有失败记录则返回原因串，否则空串（控制台黄灯判定）。

        语义：探活探的是「端口可达」，她哑没哑看的是「最近调用成没成」——
        ark 欠费 403 时端口照应答（可达全绿），但本方法会持续吐出
        「chat HTTP 403：error.Forbidden/AccountOverdueError」直到下一次
        成功调用清零。
        """
        if not self.last_error:
            return ""
        if self._clock() - self.last_error_ts > window_seconds:
            return ""
        return self.last_error

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
        should_retry: "Callable[[], bool] | None" = None,
    ) -> LLMReply:
        """同步发起一次对话补全。失败时重试 → fallback → 抛 LLMError。

        messages 为 {"role","content"} 序列；内部先做 assistant think 剥离
        并把 content 扁平化为纯文本（content 字符串形态对 OpenAI 兼容 API
        兼容面最广），再发请求。
        真探活观测（R2）：成败都记录到本实例——主客户端与 fallback 共用
        本实例的观测面（控制台只读主客户端），成功清零、失败记
        「HTTP code + error.code 摘要」（_error_summary 保证无密钥/正文）。

        should_retry（五轮修缮 R5）：每次**重试/fallback 切换前**回调一次，
        返回 False 即放弃本次调用（抛当前 LLMError，不再耗退避与 fallback）。
        用途：打断轮（superseded 代）的调用方在轮被取代后停止无效重试——
        修前被弃代会带着 1s/2s/4s 退避继续打满重试额度（本地大脑多 slot
        被僵尸请求占满后新代挤不进，实测 burst 全静默）；成功路径不受影响。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _flatten_messages(strip_assistant_think(messages)),
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_body:
            payload.update(extra_body)

        try:
            reply = self._chat_with_retry(payload, should_retry)
        except LLMError as primary_error:
            self._record_failure(primary_error)
            for fallback in self.fallbacks:
                if should_retry is not None and not should_retry():
                    logger.info(
                        "[llm] %s 失败后调用方已放弃（被弃轮），不再切 fallback %s",
                        self.provider_id, fallback.provider_id)
                    break
                logger.warning(
                    "[llm] 主客户端 %s 失败（%s），切换 fallback %s",
                    self.provider_id, primary_error, fallback.provider_id,
                )
                try:
                    fallback_reply = fallback._chat_with_retry(payload, should_retry)
                except LLMError as fallback_error:
                    # fallback 的失败同样记到本实例（观测面归主客户端）
                    self._record_failure(fallback_error)
                    logger.warning(
                        "[llm] fallback %s 也失败：%s", fallback.provider_id, fallback_error
                    )
                else:
                    self._record_success()
                    return fallback_reply
            raise primary_error from None
        else:
            self._record_success()
            return reply

    async def chat_async(self, messages: Sequence[dict], **kwargs: Any) -> LLMReply:
        """chat 的 asyncio 包装：阻塞 HTTP 调用移入工作线程，不卡事件循环。"""
        return await asyncio.to_thread(self.chat, messages, **kwargs)

    # ------------------------------------------------------------------
    # 请求与重试
    # ------------------------------------------------------------------

    def _chat_with_retry(
        self, payload: dict,
        should_retry: "Callable[[], bool] | None" = None,
    ) -> LLMReply:
        """重试主循环：可重试失败指数退避，429 优先尊重 Retry-After。

        should_retry 在每次进入退避等待前回调：False = 调用方已放弃
        （被弃轮），直接抛当前异常止损（R5，见 chat() 注释）。"""
        attempts = 0
        last_error: LLMError | None = None
        while True:
            attempts += 1
            try:
                return self._request_once(payload, attempts)
            except LLMError as exc:
                last_error = exc
                if not exc.retriable or attempts > self.max_retries:
                    raise
                if should_retry is not None and not should_retry():
                    logger.info(
                        "[llm] %s 第 %d 次失败后调用方已放弃（被弃轮），停止重试：%s",
                        self.provider_id, attempts, exc)
                    raise
                delay = self._backoff_delay(exc, attempts - 1)
                logger.warning(
                    "[llm] %s 第 %d 次请求失败（status=%s），%.2fs 后重试：%s",
                    self.provider_id, attempts, exc.status, delay, exc,
                )
                self._sleep(delay)

    def _backoff_delay(self, exc: LLMError, retry_index: int) -> float:
        """计算第 retry_index 次重试（0 起）前的等待秒数。

        - 429 且响应带数字 Retry-After：直接采用（bounded 到退避上限）；
        - 其余：base * 2^retry_index 指数退避，封顶 max。
        """
        retry_after = exc.retry_after
        if exc.status == 429 and retry_after is not None and retry_after > 0:
            return min(float(retry_after), BACKOFF_MAX_SECONDS)
        delay = BACKOFF_BASE_SECONDS * (2 ** retry_index)
        return min(delay, BACKOFF_MAX_SECONDS)

    def _request_once(self, payload: dict, attempts: int) -> LLMReply:
        """发一次请求并解析响应；网络层/状态码错误统一抛 LLMError。

        请求体组装：本实例 extra_body 先铺底、当次 payload 覆盖（调用方的
        extra_body 参数在 chat() 里已并入 payload，优先级最高）。合并在本层
        做是 O1 的 fallback 隔离点：payload 对 fallback 是共享的，而实例级
        默认字段不共享——fallback 客户端用自己的（空）extra_body。
        """
        url = f"{self.api_base}/chat/completions"
        if self.extra_body:
            merged = dict(self.extra_body)
            merged.update(payload)
            payload = merged
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        logger.info(
            "[llm] %s -> %s model=%s messages=%d chars=%d",
            self.provider_id, url, self.model,
            len(payload.get("messages", [])), sum(
                len(m.get("content") or "") for m in payload.get("messages", [])
            ),
        )
        status, headers, data = _post_json(
            self._open, url, body, self.api_key, self.timeout
        )
        if status != 200:
            detail = _error_summary(data)
            retriable = status in RETRYABLE_STATUS or (status == 429 and self.retry_on_429)
            raise LLMError(
                f"chat HTTP {status}：{detail}",
                status=status,
                attempts=attempts,
                retriable=retriable,
                retry_after=_parse_retry_after(headers),
            )
        self._log_usage(data, payload)
        return self._parse_reply(data, attempts)

    def _log_usage(self, data: Any, payload: dict) -> None:
        """落 usage 观测日志（PERF_SPEC §3-O6）：cached_tokens 与 prompt_cache_key
        同行输出——验收「连发第 3 条起 cached>0 或 burst 内出现」靠这条日志对账；
        缺失字段打 "-"，绝不因响应缺 usage 而报错。
        """
        if not isinstance(data, dict):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        details = usage.get("prompt_tokens_details")
        cached = None
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
        if cached is None:
            cached = usage.get("cached_tokens")  # 旧版兼容形态
        logger.info(
            "[llm] %s usage: prompt_tokens=%s completion_tokens=%s "
            "cached_tokens=%s prompt_cache_key=%s",
            self.provider_id,
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
            cached if cached is not None else "-",
            payload.get("prompt_cache_key") or "-",
        )

    def _parse_reply(self, data: dict, attempts: int) -> LLMReply:
        """解析 chat 响应：取 choices[0].message，tool_calls 容错忽略。"""
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(
                f"chat 响应缺少 choices：{_error_summary(data)}",
                status=200, attempts=attempts, retriable=False,
            )
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls")
        if tool_calls:
            # SPEC §4：本框架砍掉 function-calling，模型若回 tool_calls 容错忽略，
            # 只取文本内容；记警告便于发现上游行为漂移。
            logger.warning(
                "[llm] %s 响应携带 tool_calls（%d 项），已按规格忽略",
                self.provider_id, len(tool_calls),
            )
        content = content_to_text(message.get("content"))
        reasoning = message.get("reasoning_content")
        if reasoning:
            # SPEC §4：reasoning_content 不入库不回发，只随 LLMReply 暴露供存档。
            reasoning = str(reasoning)
        return LLMReply(
            content=strip_think_tags(content).strip(),
            reasoning=reasoning or None,
            finish_reason=choices[0].get("finish_reason"),
            provider_id=self.provider_id,
            attempts=attempts,
            raw=data,
        )


# ---------------------------------------------------------------------------
# embedding 客户端
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """OpenAI 兼容 embedding 客户端（独立配置，SPEC §4 + PERF_SPEC §3-O7 LRU）。

    现网指向 ark endpoint：POST {base}/embeddings，请求体 {"model", "input"}，
    响应 data[i].embedding 为 dim=2048 的向量。返回 np.ndarray(float32)，
    维度与配置不符时抛 EmbeddingDimError（memory/store 建库校验依赖该约定）。

    向量 LRU 缓存（PERF_SPEC §3-O7，默认开）：
    - 实例级：缓存随本客户端构造/重建而生灭，禁模块级全局（避免跨配置串向量）；
    - 键 = (model, style, dim, text)——四元组任一变化即不同键，杜绝换模型后
      命中旧向量；
    - 内存上限 cache_max_bytes（默认 64MB；dim=2048 float32 单条 8KB → 8192 条），
      OrderedDict LRU 淘汰最旧；
    - 失败不缓存：请求异常不写缓存，等待中的同文本线程醒来自行重试；
    - in-flight 同文本单飞：并发请求同文本时只有第一个线程真正触网，
      其余等结果（threading.Event 协调，embed 经 asyncio.to_thread 在线程池跑）。
    """

    def __init__(
        self,
        cfg: Any,
        *,
        provider_id: str = "embedding",
        urlopen: UrlOpen = _default_urlopen,  # 本机端点感知（net.py，同 ChatClient）
        sleep: SleepFn = time.sleep,
        cache_max_bytes: int = EMBEDDING_CACHE_MAX_BYTES,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self.api_base = str(cfg.api_base).rstrip("/")
        self.api_key = str(cfg.api_key)
        self.model = str(cfg.model)
        self.dim = int(getattr(cfg, "dim", EMBEDDING_DIM))
        self.timeout = float(getattr(cfg, "timeout", EMBEDDING_TIMEOUT_SECONDS))
        self.max_retries = int(getattr(cfg, "max_retries", EMBEDDING_MAX_RETRIES))
        self.style = str(getattr(cfg, "style", "openai") or "openai")
        if self.style not in ("openai", "multimodal_single", "ark_multimodal"):
            self.style = "openai"
        if self.style == "ark_multimodal":  # R6：旧取值内部映射为中性名（存量 vl.toml 兼容）
            self.style = "multimodal_single"
        self.provider_id = provider_id or "embedding"
        self._open = urlopen
        self._sleep = sleep
        # ---- 熔断状态（五轮修缮 R2：ark 欠费时别再每条消息都去撞 403）----
        # monotonic 基准（只做时长运算，不受系统对时影响）；测试可注入假钟
        self._clock = clock or time.monotonic
        self._fail_streak = 0           # 连续调用级失败数（成功清零）
        self._breaker_open_until = 0.0  # 熔断开路截止时刻（monotonic；0=常闭）
        # ---- O7 向量 LRU（实例级）----
        self._cache_max_bytes = max(0, int(cache_max_bytes))
        self._cache: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = threading.Lock()
        self._inflight: "dict[tuple, threading.Event]" = {}
        # 观测计数（read-only 属性供测试/控制台读）
        self.cache_hits = 0
        self.cache_misses = 0

    # ------------------------------------------------------------------
    # O7 缓存路径
    # ------------------------------------------------------------------

    @property
    def cache_bytes(self) -> int:
        """当前缓存占用字节数（观测用）。"""
        with self._cache_lock:
            return self._cache_bytes

    @property
    def cache_entries(self) -> int:
        """当前缓存条数（观测用）。"""
        with self._cache_lock:
            return len(self._cache)

    def _cache_key(self, text: str) -> tuple:
        """缓存键=(model, style, dim, text)（PERF_SPEC §3-O7）。"""
        return (self.model, self.style, self.dim, text)

    def _cache_lookup(self, key: tuple) -> "np.ndarray | None":
        """锁内查缓存：命中晋升 LRU 并返回副本（防调用方原地改污染缓存）。"""
        with self._cache_lock:
            vec = self._cache.get(key)
            if vec is not None:
                self._cache.move_to_end(key)
                self.cache_hits += 1
                return vec.copy()
            self.cache_misses += 1
        return None

    def _cache_store(self, key: tuple, vec: np.ndarray) -> None:
        """锁内写缓存并按字节上限 LRU 淘汰（失败路径绝不调用本方法）。

        淘汰护栏 ``len > 1``：上限被配得比单条向量还小时仍保留最新一条，
        避免「写入即淘汰」让缓存永久空转（正常 64MB 上限下单条 8KB 不会触发）。
        """
        with self._cache_lock:
            if key in self._cache:  # 单飞竞态下另一线程已写入：保持不动
                return
            self._cache[key] = vec
            self._cache_bytes += vec.nbytes
            while self._cache_max_bytes > 0 and self._cache_bytes > self._cache_max_bytes \
                    and len(self._cache) > 1:
                _old_key, old_vec = self._cache.popitem(last=False)
                self._cache_bytes -= old_vec.nbytes

    def _embed_cached(self, text: str) -> np.ndarray:
        """带缓存与 in-flight 单飞的单条向量化（O7 主管道）。

        同文本并发：后到线程持 Event 等待首请求完成；首请求失败（不缓存）
        时等待者醒来重新竞争（由本循环自然接棒），绝不把失败结果缓存下来。
        """
        if self._cache_max_bytes <= 0:
            return self._embed_uncached(text)[0]
        key = self._cache_key(text)
        while True:
            hit = self._cache_lookup(key)
            if hit is not None:
                return hit
            with self._cache_lock:
                event = self._inflight.get(key)
                owner = event is None
                if owner:
                    self._inflight[key] = threading.Event()
            if not owner:
                event.wait()  # 首请求完成（成败）后唤醒，回到循环重查缓存
                continue
            try:
                vec = self._embed_uncached([text])[0]
            finally:
                with self._cache_lock:
                    done = self._inflight.pop(key, None)
                    if done is not None:
                        done.set()
            self._cache_store(key, vec)
            return vec.copy()

    # ------------------------------------------------------------------
    # 请求路径（uncached）
    # ------------------------------------------------------------------

    def embed(self, text: str) -> np.ndarray:
        """向量化单条文本，返回 shape=(dim,) 的 float32 ndarray（带 LRU 缓存）。"""
        return self._embed_cached(text)

    def embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        """向量化一批文本，返回 shape=(n, dim) 的 float32 ndarray。

        multimodal_single 风格端点（原 ark_multimodal）只接受单条输入，逐条请求后堆叠；
        openai 风格走标准批量 /embeddings。
        缓存开时先做整批命中拆分：命中部分直接回填，未命中的合并为一次
        请求（批量不进 in-flight 单飞——批量方为顺序回填场景，无并发同文本）。
        """
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self._cache_max_bytes <= 0:
            return self._embed_uncached(texts)
        keys = [self._cache_key(str(t)) for t in texts]
        resolved: "dict[int, np.ndarray]" = {}
        miss_idx: list[int] = []
        for i, key in enumerate(keys):
            hit = self._cache_lookup(key)
            if hit is not None:
                resolved[i] = hit
            else:
                miss_idx.append(i)
        if miss_idx:
            miss_texts = [str(texts[i]) for i in miss_idx]
            miss_vecs = self._embed_uncached(miss_texts)
            for j, i in enumerate(miss_idx):
                resolved[i] = miss_vecs[j]
                self._cache_store(keys[i], miss_vecs[j])
        # np.vstack 恒分配新内存：返回值与缓存对象天然隔离，调用方可原地改
        return np.vstack([resolved[i] for i in range(len(texts))])

    # ------------------------------------------------------------------
    # R2 熔断：连续 3 次调用级失败 → 10 分钟跳过请求（半开由时钟自然到达）
    # ------------------------------------------------------------------

    @property
    def fail_streak(self) -> int:
        """当前连续失败数（观测面；成功即清零）。"""
        return self._fail_streak

    def breaker_open(self) -> bool:
        """熔断是否开路（控制台「记忆检索降级中」黄灯判定）。

        只看时钟：窗口过后自然回到半开（下一次请求真实放行，成败由
        _record_embed_* 收口）——不需要额外的半开状态位。
        """
        return self._clock() < self._breaker_open_until

    def breaker_remaining_seconds(self) -> float:
        """熔断剩余秒数（闭路恒 0；控制台展示用）。"""
        return max(0.0, self._breaker_open_until - self._clock())

    def _record_embed_failure(self) -> None:
        """一次调用级失败：连败计数 +1；达阈值即开路（日志给中文指引）。"""
        self._fail_streak += 1
        if self._fail_streak >= EMBED_BREAKER_THRESHOLD \
                and not self.breaker_open():
            self._breaker_open_until = self._clock() + EMBED_BREAKER_OPEN_SECONDS
            logger.error(
                "[llm] embedding 连续 %d 次失败，熔断 %.0f 分钟（期间跳过请求，"
                "记忆检索降级为不注入；对话与日记不受影响）",
                self._fail_streak, EMBED_BREAKER_OPEN_SECONDS / 60,
            )

    def _record_embed_success(self) -> None:
        """一次调用级成功：连败清零、熔断复位。"""
        self._fail_streak = 0
        self._breaker_open_until = 0.0

    def _embed_uncached(self, texts: "Sequence[str] | str") -> np.ndarray:
        """熔断包装：开路直接抛（跳过请求）；闭合放行并记成败（R2）。

        本方法是全部真实网络请求的唯一咽喉（openai 批量与多模态逐条都在
        _embed_request 内），熔断在这里收口即覆盖所有路径；缓存命中不经过
        本方法——已缓存的向量在熔断期照常可用。
        入参归一：_embed_cached 的禁缓存快路径会直接传字符串（历史形态），
        list(str) 会按字拆分——这里统一包成列表（顺带修掉存量快路径 bug）。
        """
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self.breaker_open():
            raise LLMError(
                f"embedding 熔断中：连续 {self._fail_streak} 次失败，"
                f"{self.breaker_remaining_seconds():.0f}s 后自动半开（本次跳过请求）",
                retriable=False,
            )
        try:
            vectors = self._embed_request(texts)
        except LLMError:
            self._record_embed_failure()
            raise
        self._record_embed_success()
        return vectors

    def _embed_request(self, texts: Sequence[str]) -> np.ndarray:
        """无缓存的向量请求：多模态端点逐条 / openai 批量（重试循环原样保留）。"""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self.style == "multimodal_single":  # 旧值 ark_multimodal 已在构造时映射
            return np.vstack([self._embed_one_ark(text) for text in texts])
        payload = {"model": self.model, "input": list(texts)}
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._request_once(payload, attempts)
            except LLMError as exc:
                if not exc.retriable or attempts > self.max_retries:
                    raise
                delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), BACKOFF_MAX_SECONDS)
                logger.warning(
                    "[llm] embedding 第 %d 次请求失败（status=%s），%.2fs 后重试：%s",
                    attempts, exc.status, delay, exc,
                )
                self._sleep(delay)

    def _embed_one_ark(self, text: str) -> np.ndarray:
        """火山方舟多模态嵌入端点（单条）：POST /embeddings/multimodal。

        实测：input 为 [{"type":"text","text":...}]；响应 data 为 dict，
        其 embedding 即 2048 维向量；纯文本输入可用（SPEC §4 style=multimodal_single，R6 前名 ark_multimodal）。
        """
        payload = {"model": self.model, "input": [{"type": "text", "text": text}]}
        attempts = 0
        while True:
            attempts += 1
            try:
                url = f"{self.api_base}/embeddings/multimodal"
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                logger.info(
                    "[llm] %s -> %s model=%s（multimodal 单条，%d 字符）",
                    self.provider_id, url, self.model, len(text),
                )
                status, _headers, data = _post_json(
                    self._open, url, body, self.api_key, self.timeout
                )
                if status != 200:
                    detail = _error_summary(data)
                    retriable = status in RETRYABLE_STATUS or status == 429
                    raise LLMError(
                        f"embedding HTTP {status}：{detail}",
                        status=status, attempts=attempts, retriable=retriable,
                    )
                block = data.get("data")
                if not isinstance(block, dict):
                    raise LLMError(
                        f"embedding(multimodal) 响应 data 不是对象：{_error_summary(data)}",
                        status=200, attempts=attempts, retriable=False,
                    )
                vec = block.get("embedding")
                if not isinstance(vec, (list, tuple)) or len(vec) != self.dim:
                    got = len(vec) if isinstance(vec, (list, tuple)) else None
                    raise EmbeddingDimError(
                        f"embedding 维度不符：期望 {self.dim}，实际 {got}（model={self.model}）",
                        status=200, attempts=attempts, retriable=False,
                    )
                return np.asarray(vec, dtype=np.float32)
            except LLMError as exc:
                if not exc.retriable or attempts > self.max_retries:
                    raise
                delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), BACKOFF_MAX_SECONDS)
                logger.warning(
                    "[llm] embedding(multimodal) 第 %d 次请求失败（status=%s），%.2fs 后重试：%s",
                    attempts, exc.status, delay, exc,
                )
                self._sleep(delay)

    async def embed_async(self, text: str) -> np.ndarray:
        """embed 的 asyncio 包装（记忆检索在事件循环内调用）。"""
        return await asyncio.to_thread(self.embed, text)

    async def embed_batch_async(self, texts: Sequence[str]) -> np.ndarray:
        """embed_batch 的 asyncio 包装（历史回填批量写入用）。"""
        return await asyncio.to_thread(self.embed_batch, texts)

    def _request_once(self, payload: dict, attempts: int) -> np.ndarray:
        url = f"{self.api_base}/embeddings"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        logger.info(
            "[llm] %s -> %s model=%s inputs=%d",
            self.provider_id, url, self.model, len(payload["input"]),
        )
        status, _headers, data = _post_json(
            self._open, url, body, self.api_key, self.timeout
        )
        if status != 200:
            detail = _error_summary(data)
            retriable = status in RETRYABLE_STATUS or status == 429
            raise LLMError(
                f"embedding HTTP {status}：{detail}",
                status=status, attempts=attempts, retriable=retriable,
            )
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != len(payload["input"]):
            raise LLMError(
                f"embedding 响应 data 条数与输入不符：{_error_summary(data)}",
                status=200, attempts=attempts, retriable=False,
            )
        vectors = [row.get("embedding") for row in rows]
        for i, vec in enumerate(vectors):
            if not isinstance(vec, (list, tuple)) or len(vec) != self.dim:
                got = len(vec) if isinstance(vec, (list, tuple)) else None
                raise EmbeddingDimError(
                    f"embedding 维度不符：期望 {self.dim}，实际 {got}（model={self.model}）",
                    status=200, attempts=attempts, retriable=False,
                )
        return np.asarray(vectors, dtype=np.float32)


# ---------------------------------------------------------------------------
# HTTP 底座（chat 与 embedding 共用）
# ---------------------------------------------------------------------------


def _post_json(
    urlopen: UrlOpen, url: str, body: bytes, api_key: str, timeout: float
) -> "tuple[int, Any, Any]":
    """POST JSON 并返回 (status, headers, 解析后的 JSON 或原文)。

    - HTTPError（4xx/5xx）返回其状态码与打码后的响应体，不抛出；
    - URLError / 超时 / 连接失败 → LLMError(retriable=True)。
    日志只记录 URL 与状态码，不记录 Authorization 头与请求体。
    """
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "virtuallover-brain/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as resp:
            status = int(getattr(resp, "status", None) or resp.getcode())
            raw = resp.read()
            headers = resp.headers
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 —— 错误响应体读不动就按空处理
            raw = b""
        return int(exc.code), exc.headers, _decode_json(raw)
    except Exception as exc:  # noqa: BLE001 —— URLError/Timeout/连接拒绝一律可重试
        raise LLMError(
            f"网络错误（{type(exc).__name__}）：{exc}", retriable=True
        ) from exc
    return status, headers, _decode_json(raw)


def _decode_json(raw: bytes) -> Any:
    """响应体 JSON 解码；失败时返回占位 dict（只记长度，原文不进日志）。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_non_json_body": f"<{len(raw)} bytes>"}


def _parse_retry_after(headers: Any) -> float | None:
    """解析 Retry-After 响应头（只支持数字秒；HTTP-date 等格式忽略）。"""
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_multimodal_content(content: Any) -> bool:
    """content 分段列表里是否含图片等多模态段（text/think 之外的 dict 段）。

    多模态能力（brain/vision.py）：带 image_url 段的 user 消息必须原样发给
    上游——扁平化会把图片段丢成空串，模型就真的「看不见」了。
    """
    if not isinstance(content, list):
        return False
    return any(
        isinstance(seg, dict) and seg.get("type") not in (None, _SEGMENT_TYPE_TEXT, _SEGMENT_TYPE_THINK)
        for seg in content
    )


def _flatten_messages(messages: Iterable[dict]) -> list[dict]:
    """把 {"role","content"} 序列的 content 统一扁平化为纯文本字符串。

    例外（多模态）：content 是含 image_url 等多模态段的分段列表时原样保留，
    只剥 think 段与字符串形态照旧扁平化（strip_assistant_think 已先行处理
    assistant 侧；此处守的是 user 侧的图片段）。
    """
    out: list[dict] = []
    for msg in messages:
        item = dict(msg)
        content = item.get("content")
        if _is_multimodal_content(content):
            out.append(item)   # 多模态分段列表原样透传（vision 消息）
            continue
        item["content"] = content_to_text(content)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------

# 思考三态 → ark/doubao extra_body 参数（tmp/probe_ark_thinking.py 实证格式，
# PERF_SPEC §2 O1：参数走既有 extra_body 通道）
_THINKING_BODY_TYPES = frozenset({"disabled", "enabled", "auto"})


def thinking_extra_body(mode: str) -> dict:
    """把 thinking 三态词转成请求体附加字段；非法/空值返回 {}（不带字段=上游默认）。

    格式实证：``{"thinking": {"type": "disabled"}}`` 直接并入 /chat/completions
    请求体（非流式 has_reasoning=False，见 scripts/bench/smoke_ark_thinking.py）。
    """
    mode = str(mode or "").strip().lower()
    if mode not in _THINKING_BODY_TYPES:
        return {}
    return {"thinking": {"type": mode}}


def _with_timeout_main(chat_cfg: Any) -> Any:
    """复制 chat 配置并把 timeout 覆写为 timeout_main（构造期覆写，§7.1-4）。

    dataclass 走 replace；duck typing 配置对象（测试桩/装配方自备）走浅拷贝
    + setattr，两者都不改用户内存外的任何配置。
    """
    import copy
    import dataclasses

    override = {"timeout": int(getattr(chat_cfg, "timeout_main", 30) or 30)}
    if dataclasses.is_dataclass(chat_cfg) and not isinstance(chat_cfg, type):
        return dataclasses.replace(chat_cfg, **override)
    copied = copy.copy(chat_cfg)
    for key, val in override.items():
        setattr(copied, key, val)
    return copied


def build_chat_clients(chat_cfg: Any) -> "tuple[ChatClient, ChatClient]":
    """按 PERF_SPEC §2 O1 + §7.1-4 构造主/后台双 chat 客户端。

    - 主客户端（ctx.chat_client，主回复 + proactive）：thinking 取
      ``cfg.chat.thinking``（默认 disabled）、读超时覆写为 ``timeout_main``
      （默认 30s，压住被弃线程——30s 只降主客户端，后台保持 120s）；
    - 后台客户端（ctx.chat_client_bg，日记 + 记忆总结）：thinking 取
      ``bg_thinking``（默认 enabled）、读超时保持 ``timeout``（120s，
      后台思考实测 11.5s+，日记生产 prompt 更大）。
    """
    main_cfg = _with_timeout_main(chat_cfg)
    # 本地 provider（llama.cpp/Ollama/vLLM，brain_switch.py 本地大脑）没有
    # ark 的 thinking 协议——按 api_base 门控不注入，避免严格实现 400
    base = str(getattr(chat_cfg, "api_base", "") or "")
    local = "://127.0.0.1" in base or "://localhost" in base
    extra_main = {} if local else thinking_extra_body(getattr(chat_cfg, "thinking", "disabled"))
    extra_bg = {} if local else thinking_extra_body(getattr(chat_cfg, "bg_thinking", "enabled"))
    main_client = ChatClient(main_cfg, provider_id="chat", extra_body=extra_main)
    bg_client = ChatClient(chat_cfg, provider_id="chat_bg", extra_body=extra_bg)
    return main_client, bg_client


def setup(ctx: Any) -> None:
    """按 vl.toml 构造 chat / embedding 客户端挂到 ctx。

    - ctx.chat_client：ChatClient（主回复 + proactive；thinking 默认 disabled，
      timeout_main 默认 30s——PERF_SPEC §2 O1 + §7.1-4）；
    - ctx.chat_client_bg：ChatClient（日记 + 记忆总结；bg_thinking 默认
      enabled，timeout 默认 120s）；
    - ctx.embedding_client：EmbeddingClient（cfg.embedding，独立配置互不共享）；
      [embedding] enabled=false 时不构造（槽位留 None）——记忆库装配见
      memory.store.setup 的同款开关，聊天/日记不受影响；
    - [chat] fallback_enabled=true 时再由 BrainRouter 包住主/后台客户端
      （五轮修缮第二轮智能降级，_wire_brain_router）。
    客户端本身惰性建连（首次请求才触网），装配失败面只剩配置缺失——那由
    config.load_config 的启动校验先行拦截。
    """
    ctx.chat_client, ctx.chat_client_bg = build_chat_clients(ctx.cfg.chat)
    if getattr(ctx.cfg.embedding, "enabled", True):
        ctx.embedding_client = EmbeddingClient(ctx.cfg.embedding)
    else:
        ctx.embedding_client = None
        logger.info("[llm] embedding 未启用（[embedding] enabled=false）——"
                    "记忆功能暂闭（检索/注入/写入），聊天与日记不受影响")
    main_thinking = (ctx.chat_client.extra_body.get("thinking") or {}).get("type") or "上游默认"
    bg_thinking = (ctx.chat_client_bg.extra_body.get("thinking") or {}).get("type") or "上游默认"
    logger.info(
        "[llm] 客户端装配完成（chat=%s model=%s thinking=%s timeout_main=%.0fs；"
        "chat_bg thinking=%s timeout=%.0fs；embedding model=%s dim=%d）",
        ctx.cfg.chat.api_base, ctx.cfg.chat.model,
        main_thinking, ctx.chat_client.timeout,
        bg_thinking, ctx.chat_client_bg.timeout,
        ctx.cfg.embedding.model, ctx.cfg.embedding.dim,
    )
    # 智能降级接线放在装配日志之后：Router 包住后 ctx.chat_client 不再是
    # ChatClient（无 extra_body 属性），上面的生效值日志读的是裸客户端
    _wire_brain_router(ctx)


def _brain_notice_sink(ctx: Any) -> "Callable[[str], None]":
    """构造降级提示投递闭包：经 MessagePipeline 公开入队口发到会话用户。

    pipeline 的消费面（chat_async）零改动，提示走 O4 统一发送 FIFO
    （pipeline.enqueue_text_send——proactive/diary 同款入口，分段/限流/语音
    规则自动继承）。装配序上 brain.llm 先于 pipeline，故闭包动态解析
    ctx.message_pipeline，未装配时降级为告警日志。
    """
    def _sink(text: str) -> None:
        pipeline = getattr(ctx, "message_pipeline", None)
        user_id = str(getattr(ctx.cfg, "session_id", "") or "").rsplit(":", 1)[-1]
        if pipeline is None or not user_id:
            logger.warning("[llm] 降级提示无法投递（pipeline 未装配或 session_id 缺用户段）：%s",
                           text)
            return
        try:
            pipeline.enqueue_text_send(user_id, [text], ctx.cfg.reply.segmented)
        except Exception:  # noqa: BLE001 —— 提示失败绝不影响降级主流程
            logger.exception("[llm] 降级提示投递失败（不影响降级）")
    return _sink


def _wire_brain_router(ctx: Any) -> None:
    """智能降级接线（五轮修缮第二轮）：fallback_enabled=true 时 Router 包住主/后台客户端。

    - pipeline/proactive/diary/记忆总结继续读 ctx.chat_client(_bg)，零改动换用；
    - 主/后台路由共享同一 DegradationState 与 LocalBrainManager（一个大脑一个
      状态），diary 走的后台实例同样受黏性保命约束；
    - ctx.brain_router 挂主路由（/brain 命令与 console 面板的观测/操作入口）；
    - 延迟 import brain.router（router 反向 import 本模块的 ChatClient，
      顶层 import 会成环）。
    """
    from virtuallover.brain.router import build_brain_router

    chat_cfg = ctx.cfg.chat
    main_router = build_brain_router(
        ctx.chat_client, chat_cfg,
        provider_id="chat",
        repo_root=str(getattr(ctx, "repo_root", ".")),
        notice_sink=_brain_notice_sink(ctx),
    )
    if main_router is None:
        return  # fallback_enabled=false：装配零行为变化（云端用户不装本地也安全）
    ctx.chat_client = main_router
    bg_router = build_brain_router(ctx.chat_client_bg, chat_cfg,
                                   provider_id="chat_bg", shared=main_router)
    if bg_router is not None:
        ctx.chat_client_bg = bg_router
    ctx.brain_router = main_router
    local = (main_router.status().get("local") or {})
    logger.info(
        "[llm] 智能降级已接线（本地大脑 %s:%s model=%s ctx=%d；主客户端 max_retries→1；"
        "spawn=%s）",
        local.get("host") or "?", local.get("port") or "?",
        chat_cfg.fallback_model, chat_cfg.fallback_max_context_tokens,
        "开" if chat_cfg.fallback_auto_spawn else "关（只探测不拉起）",
    )
