# -*- coding: utf-8 -*-
"""brain/sender.py —— 分段回复与发送速率限制（SPEC §5 + PERF_SPEC §2 O2a）。

行为三件套（参数全部从 config 读，默认值取现网实测）：
1. segmented_reply 分段：回复总长 ≤ words_count_threshold 不拆；超过按
   split_mode 拆段——"regex" 模式用配置正则逐段匹配（finditer group(0)，
   兼容带捕获组的写法），"words" 模式按 split_words 切分并保留标点；
2. 段间间隔：interval_method="log" 用对数分布采样（偏向短间隔、带长尾，
   base=log_base；base<=1 退化为均匀分布），其余按均匀分布采样；
   区间从 interval 原文解析（兼容 "1.5-3.5" 与 "1.5, 3.5" 两种迁移写法），
   解析失败回退 (min_gap, max_gap)；采样结果统一 clamp 到 ≥1.2s 下限
   （PERF_SPEC §2 O2a：拟人底线，防配置把连发变成轰炸）；
   打字节奏（typing_cps>0 且调用方给定 next_len 时叠加）：间隔 =
   clamp(base + 段长 × typing_cps × [0.8,1.2] 抖动, 1.2s, 10s)——长段多等
   （她在打字）、短段（≤4 字）按 1.2s 底线秒回；typing_cps=0 或 next_len=None
   完全保持现行分布（向后兼容，旧调用方零变化）；
3. 速率限制 10 条/30s stall：滑动窗口计满后等待最早一条出窗再发
   （stall 等待而非丢弃），clock/sleeper 可注入。

首段拟人抖动（PERF_SPEC §2 O2a + §7.1-1 并行语义）：
- first_gap 区间（默认 "0.8-2.2"）经 sample_first_gap 采样后，由调用方折算
  成首段最早可发时刻 ``first_send_at = 消息到达 + first_gap`` 传入 send_reply；
- 首段发送时刻 = max(LLM 完成, first_send_at)——抖动不叠加延迟，只保拟人
  下限（LLM 完成晚于抖动窗口则零等待）；first_gap 配空串=关闭抖动。

配置消费契约（duck typing，字段对齐骨架 config.SegmentedReplyConfig /
config.RateLimitConfig，装配方直接传 cfg.reply.segmented / cfg.reply.rate_limit）：
- 分段参数需有：enable / words_count_threshold / split_mode / regex /
  split_words / interval_method / interval / log_base / min_gap / max_gap /
  first_gap（缺属性按关闭抖动处理，兼容旧参数对象）；
- 限流参数需有：max_count / window_seconds。

异步接口：send_reply / RateLimiter.acquire 均为协程；阻塞仅发生在
asyncio.sleep（可注入替换），不占用工作线程。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger("virtuallover.brain.sender")

SendFn = Callable[[str], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]

# interval 原文的数字对解析：兼容 "1.5-3.5" / "1.5,3.5" / "1.5, 3.5"
_INTERVAL_PAIR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~,]\s*(\d+(?:\.\d+)?)")

# 段间隔硬下限（秒）：PERF_SPEC §2 O2a「段间间隔维持 1.5-3.5s 分布（≥1.2s 下限）」，
# 对任何来源的段间采样结果统一生效（配置写了更小的值也压到 1.2s）
MIN_SEGMENT_GAP_SECONDS = 1.2

# 打字节奏上限（秒）：typing_cps 节奏下「打字耗时」封顶——防超长段把下一段
# 推迟到分钟级（长段等 10s 已是拟人上限，再长像掉线）
MAX_SEGMENT_GAP_SECONDS = 10.0

# 打字节奏短段阈值（字）：≤4 字的短段不装打字，按底线间隔直接发（秒回感）
SHORT_SEGMENT_CHARS = 4

# 打字耗时抖动区间：段长 × typing_cps 再乘 [0.8, 1.2] 均匀抖动（匀速打字像机器）
_TYPING_JITTER_LO, _TYPING_JITTER_HI = 0.8, 1.2

# regex 模式兜底：配置正则为空或编译失败时按整条发送（绝不因配置问题丢内容）


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------


def parse_interval(
    interval_text: str, min_gap: float, max_gap: float
) -> "tuple[float, float]":
    """解析间隔区间原文，失败回退 (min_gap, max_gap)（骨架契约的兜底语义）。"""
    matched = _INTERVAL_PAIR_RE.search(str(interval_text or ""))
    if not matched:
        return float(min_gap), float(max_gap)
    lo, hi = float(matched.group(1)), float(matched.group(2))
    if lo > hi:
        lo, hi = hi, lo
    if lo <= 0:
        lo, hi = float(min_gap), float(max_gap)
    return lo, hi


def _typing_enabled(params: Any, next_len: "int | None") -> bool:
    """打字节奏是否生效：typing_cps>0 且调用方给定下一段长度。

    next_len=None（旧调用方/不关心段长）一律 False——现行分布零变化（向后兼容）。
    """
    if next_len is None:
        return False
    return float(getattr(params, "typing_cps", 0.0) or 0.0) > 0.0


def _typing_gap(params: Any, base: float, next_len: int, rng: "random.Random") -> float:
    """打字节奏间隔：clamp(base + 段长 × typing_cps × [0.8,1.2] 抖动, 1.2s, 10s)。

    - 段长 ≤ SHORT_SEGMENT_CHARS 的短段：不叠加打字耗时，直接 1.2s 底线
      （「嗯」「来了」这类秒回，硬等反而假）；
    - 抖动防匀速：匀速打字像机器，每次乘 [0.8, 1.2] 均匀系数。
    """
    length = int(next_len)
    if length <= SHORT_SEGMENT_CHARS:
        return MIN_SEGMENT_GAP_SECONDS
    cps = float(getattr(params, "typing_cps", 0.0) or 0.0)
    jitter = rng.uniform(_TYPING_JITTER_LO, _TYPING_JITTER_HI)
    gap = base + length * cps * jitter
    return min(max(gap, MIN_SEGMENT_GAP_SECONDS), MAX_SEGMENT_GAP_SECONDS)


def sample_gap(
    params: Any, rng: "random.Random | None" = None, next_len: "int | None" = None
) -> float:
    """采样两条分段消息之间的间隔秒数（结果 ≥ MIN_SEGMENT_GAP_SECONDS）。

    新签名（打字节奏，PERF_SPEC「发送节奏」项）::

        sample_gap(params, rng=None, next_len=None) -> float

    - next_len：下一段长度（字数）。typing_cps>0 且给定时间隔 =
      clamp(base + next_len × typing_cps × [0.8,1.2] 抖动, 1.2s, 10s)——
      段越长等越久（她在打字），≤4 字短段直接 1.2s（秒回感）；
    - next_len=None 或 typing_cps=0：完全保持现行分布（向后兼容）；
    - base 采样：log 分布 t = lo + (hi-lo) * (base^u - 1) / (base - 1)，u∈[0,1)；
      base>1 时分布偏向低端（多数间隔短、偶尔长），对齐「log」语义；
      其余 interval_method：[lo, hi] 均匀分布；
    - 出口统一 clamp 到 ≥1.2s 硬下限（PERF_SPEC §2 O2a：拟人底线）。
    """
    rng = rng or random
    lo, hi = parse_interval(
        getattr(params, "interval", ""),
        float(getattr(params, "min_gap", 1.5)),
        float(getattr(params, "max_gap", 3.5)),
    )
    gap = lo
    if hi > lo:
        method = str(getattr(params, "interval_method", "") or "").strip().lower()
        if method == "log":
            base = float(getattr(params, "log_base", 2.6) or 2.6)
            if base > 1.0:
                u = rng.random()
                gap = lo + (hi - lo) * (base ** u - 1.0) / (base - 1.0)
            else:
                gap = rng.uniform(lo, hi)
        else:
            gap = rng.uniform(lo, hi)
    if _typing_enabled(params, next_len):
        return _typing_gap(params, gap, int(next_len), rng)
    return max(gap, MIN_SEGMENT_GAP_SECONDS)


def sample_first_gap(
    params: Any, rng: "random.Random | None" = None, next_len: "int | None" = None
) -> float:
    """采样首段拟人抖动秒数（PERF_SPEC §2 O2a + §7.1-1 并行语义）。

    新签名（打字节奏）::

        sample_first_gap(params, rng=None, next_len=None) -> float

    - next_len：首段长度（字数）。typing_cps>0 且给定时间隔 =
      clamp(base + next_len × typing_cps × [0.8,1.2] 抖动, 1.2s, 10s)
      （首段本身也要「打」，段长参与节奏；≤4 字短段直接 1.2s）；
    - next_len=None 或 typing_cps=0：完全保持现行分布（向后兼容）；
    - base 采样：从 params.first_gap 区间原文（默认 "0.8-2.2"）均匀采样；
    - first_gap 缺属性/空串/纯空白 = 关闭抖动（返回 0.0，调用方折算出的
      first_send_at 不晚于消息到达时刻，首段零等待）；打字节奏不越过该关闭
      语义——first_gap 关闭时即便给了 next_len 也返回 0.0；
    - 解析失败（无数字对）同样按关闭处理——抖动是增益项，绝不因配置问题
      阻塞发送。
    """
    raw = str(getattr(params, "first_gap", "") or "").strip()
    if not raw:
        return 0.0
    matched = _INTERVAL_PAIR_RE.search(raw)
    if not matched:
        return 0.0
    lo, hi = float(matched.group(1)), float(matched.group(2))
    if lo > hi:
        lo, hi = hi, lo
    lo, hi = max(lo, 0.0), max(hi, 0.0)
    rng = rng or random
    gap = lo if hi <= lo else rng.uniform(lo, hi)
    if _typing_enabled(params, next_len):
        return _typing_gap(params, gap, int(next_len), rng)
    return gap


def segment_reply(text: str, params: Any) -> list[str]:
    """按分段参数把回复拆成待发送的段列表（至少返回一段）。

    - enable=False 或总长 ≤ words_count_threshold：整条一段；
    - split_mode="regex"：配置正则逐段匹配（group(0)）；正则为空、编译失败
      或无匹配时整条发送；
    - split_mode="words"：按 split_words 切分并保留标点；
    - 每段 strip 后丢弃空段。
    """
    text = str(text or "").strip()
    if not text:
        return []
    threshold = int(getattr(params, "words_count_threshold", 0) or 0)
    if not getattr(params, "enable", True) or (threshold > 0 and len(text) <= threshold):
        return [text]

    mode = str(getattr(params, "split_mode", "") or "").strip().lower()
    segments: "list[str]" = []
    if mode == "regex":
        pattern_text = str(getattr(params, "regex", "") or "")
        if pattern_text.strip():
            try:
                pattern = re.compile(pattern_text, re.DOTALL)
            except re.error as exc:
                logger.warning("[sender] 分段正则非法(%s)，整条发送：%r", exc, pattern_text)
                return [text]
            segments = [m.group(0).strip() for m in pattern.finditer(text)]
    elif mode == "words":
        words = [str(w) for w in (getattr(params, "split_words", None) or []) if str(w)]
        if words:
            splitter = re.compile("(" + "|".join(re.escape(w) for w in words) + ")")
            pieces = splitter.split(text)
            merged: "list[str]" = []
            for piece in pieces:
                if piece in words:  # 分隔符：拼回上一段（保留标点）
                    if merged:
                        merged[-1] += piece
                elif piece:
                    merged.append(piece)
            segments = [seg.strip() for seg in merged]

    segments = [seg for seg in segments if seg]
    if not segments:
        return [text]
    return segments


# ---------------------------------------------------------------------------
# 速率限制
# ---------------------------------------------------------------------------


class RateLimiter:
    """滑动窗口限流：窗口内最多 max_count 条，超限 stall 等待出窗（SPEC §5）。"""

    def __init__(
        self,
        *,
        max_count: int = 10,
        window_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: "SleepFn | None" = None,
    ) -> None:
        self.max_count = int(max_count)
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._sleep = sleeper or asyncio.sleep
        self._stamps: "list[float]" = []

    async def acquire(self) -> None:
        """取一个发送名额；窗口满时等待最早一条出窗（stall，不丢弃）。"""
        while True:
            now = self._clock()
            self._stamps = [
                ts for ts in self._stamps if now - ts < self.window_seconds
            ]
            if len(self._stamps) < self.max_count:
                self._stamps.append(now)
                return
            wait = self.window_seconds - (now - self._stamps[0])
            if wait > 0:
                logger.info(
                    "[sender] 限流触发（%d 条/%.0fs），stall 等待 %.2fs",
                    self.max_count, self.window_seconds, wait,
                )
                await self._sleep(wait + 0.01)
            # 等待后重走清理与判定（防止时钟抖动导致贴线发放）


# ---------------------------------------------------------------------------
# 发送编排
# ---------------------------------------------------------------------------


async def send_reply(
    text: str,
    send: SendFn,
    *,
    params: Any,
    limiter: "RateLimiter | None" = None,
    rng: "random.Random | None" = None,
    sleeper: "SleepFn | None" = None,
    first_send_at: "float | None" = None,
    clock: "ClockFn | None" = None,
) -> "list[str]":
    """分段 + 限流 + 逐段发送，返回实际发送的段列表。

    - send：单段发送回调（bridge 层提供，逐条顺序调用，SPEC §3.8 多条顺序发送）；
    - 每段发送前过限流器，段与段之间 sleep 采样间隔（最后一段后不等待）；
    - first_send_at：首段最早可发时刻（time.monotonic 基准，PERF_SPEC §7.1-1
      并行语义——调用方传「消息到达 + first_gap 抖动」），首段发送前补足差值
      等待，实现首段发送时刻 = max(LLM 完成, 到达+抖动)；None/已过期 = 零等待；
    - sleeper / clock 可注入（测试断言间隔序列），缺省 asyncio.sleep / time.monotonic。
    """
    segments = segment_reply(text, params)
    do_sleep = sleeper or asyncio.sleep
    now = clock or time.monotonic
    for index, segment in enumerate(segments):
        if index == 0 and first_send_at is not None:
            delay = first_send_at - now()
            if delay > 0:
                logger.debug("[sender] 首段拟人抖动：%.2fs 后发首段（first_send_at 未到）", delay)
                await do_sleep(delay)
        if limiter is not None:
            await limiter.acquire()
        await send(segment)
        if index < len(segments) - 1:
            gap = sample_gap(params, rng)
            logger.debug("[sender] 段 %d/%d 发送完毕，%.2fs 后发下一段", index + 1, len(segments), gap)
            await do_sleep(gap)
    return segments
