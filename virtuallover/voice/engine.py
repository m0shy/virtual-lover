# -*- coding: utf-8 -*-
"""voice/engine.py —— VoiceEngine：§1 语音引擎栈（VOICE_SPEC §1 全部模块的组合体）。

命名说明（重要，非本模块自作主张）：VOICE_SPEC §1 的 manager.py 槽位（全局单飞
synthesize）已被并行的 §2 管线门面 ``voice/manager.py`` 占用——其 setup 声明
「串行化契约在其类内兑现、消费 ``ctx.voice_engine`` 接缝（§1 全栈：gsvi/
lifecycle/cache/mapping 的 synthesize(text, mood_snapshot) 契约面）」。故本模块
以 ``voice/engine.py`` 落地该接缝：组合 gsvi.py（HTTP/注册表）+ lifecycle.py
（进程状态机）+ cache.py（磁盘 LRU）+ mapping.py（情绪回退链），挂
``ctx.voice_engine``，并把控制台契约（§3 的 status/preview/start/stop/
reload_mapping）转发到 ctx.voice_manager 门面实例上。

职责逐条（VOICE_SPEC §1）：
- **全局单飞锁**：本类持有的 asyncio.Lock 是 /tts 串行化的权威实现点——聊天
  worker（§2 门面）与控制台试听共用；任一时刻至多一个 in-flight /tts；
- **情绪映射**：输入用 MoodEngine 有效状态（``soul.mood.effective_mood`` 结构化
  getter，与文字注入同源；禁止解析注入字符串）→ 7 态表（默认表 ← vl.toml ←
  local/vl_data/voice/emotion_map.json 覆盖层）→ 注册表回退链（每跳
  degraded_emotion 打点）；
- **缓存**：命中先行（键含 character|emotion|lang|format|归一化 text），未命中
  才进单飞合成，双检后写盘；
- **生命周期门**：degraded 冷却期跳过重试直接 None；waiting/down 期间该条降级
  纯文字（后台 run_forever 继续探测，恢复后自动回语音）；
- **HANG 喂入**：合成失败喂 lifecycle.report_synth_failure（TCP 探不到的挂起型
  故障），连续 2 次判死走同一死亡处理链。

依赖策略：仅 websockets+numpy 两个非标准库依赖（本模块全标准库）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from virtuallover.soul.mood import MOOD_ZH, VALID_MOODS, effective_mood

from .cache import VoiceCache
from .gsvi import GsviClient
from .lifecycle import (
    GsviLifecycle,
    LifecycleParams,
    STATE_DEGRADED,
    STATE_NOT_STARTED,
)
from .mapping import DEFAULT_EMOTION_MAP, EmotionResolution, emotion_for_snapshot

__all__ = ["VoiceEngine", "setup"]

logger = logging.getLogger("virtuallover.voice.engine")

# 映射覆盖层文件（控制台映射表卡的写落点，与 voice-engine.pid 同目录；console/voice.py 同约定）
OVERLAY_REL_PATH = "local/vl_data/voice/emotion_map.json"

# 中文字 → 7 态键（防御性归一化：调用方传中文情绪名时反向对齐，绝不解析注入字符串）
_ZH_TO_MOOD = {zh: key for key, zh in MOOD_ZH.items()}


def _normalize_snapshot(
    mood_snapshot: Any,
) -> "tuple[str, float] | None":
    """mood 快照归一化：接受 (mood, intensity) 元组 / 7 态键 / 中文情绪名 / None。

    - 元组（soul.mood.effective_mood 的返回值——规范输入）原样透传；
    - 字符串只接受 7 态键（happy/sad/...）或 MOOD_ZH 中文名（开心/...）——
      这两者是**枚举值**而非注入文本；其余一律按未知处理（回退链 fallback），
      绝不解析注入字符串（VOICE_SPEC §1 禁令）；
    - None（无情绪上下文）→ None，由映射层按 calm 处理。
    """
    if mood_snapshot is None:
        return None
    if isinstance(mood_snapshot, (tuple, list)) and len(mood_snapshot) >= 1:
        mood = str(mood_snapshot[0] or "").strip().lower()
        intensity = mood_snapshot[1] if len(mood_snapshot) > 1 else 0.3
        try:
            return mood, float(intensity)
        except (TypeError, ValueError):
            return mood, 0.3
    text = str(mood_snapshot).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in VALID_MOODS:
        return lowered, 0.3
    if text in _ZH_TO_MOOD:
        return _ZH_TO_MOOD[text], 0.3
    return lowered, 0.3  # 未知串交给回退链（fallback 键 → 聊天），打点可见


class VoiceEngine:
    """语音引擎栈：单飞合成 + 情绪映射 + 缓存 + 生命周期的组合门面。"""

    def __init__(
        self,
        *,
        client: GsviClient,
        cache: VoiceCache,
        lifecycle: GsviLifecycle,
        emotion_map: "Optional[dict]" = None,
        default_emotion: str = "聊天",
        mood_getter: "Callable[[], tuple[str, float] | None] | None" = None,
        overlay_path: "str | Path | None" = None,
    ) -> None:
        self.client = client
        self.cache = cache
        self.lifecycle = lifecycle
        self.character = client.character
        self.default_emotion = str(default_emotion or "聊天")
        self.mood_getter = mood_getter
        self.overlay_path = Path(overlay_path) if overlay_path else None
        self._config_map = dict(emotion_map or {})
        self._overlay_map: "dict[str, str]" = {}
        self._load_overlay()
        # 全局单飞锁（§1 串行化的权威实现点；聊天与试听共用）
        self._synth_lock = asyncio.Lock()
        # 主事件循环引用（preview 从控制台线程 run_coroutine_threadsafe 回主循环）
        self._loop: "Optional[asyncio.AbstractEventLoop]" = None
        self._bg_task: "Optional[asyncio.Task]" = None
        # 运行观测（状态卡附加数据）
        self.stats: "dict[str, int]" = {
            "synth_requests": 0,   # synthesize 调用数
            "cache_hits": 0,       # 缓存命中数
            "synth_failures": 0,   # 合成失败数（回退纯文字）
            "degraded_skips": 0,   # 熔断/未就绪跳过数
        }

    # ------------------------------------------------------------------
    # 映射表：默认 ← vl.toml ← 覆盖层（console/voice.py 同一三层口径）
    # ------------------------------------------------------------------

    def _load_overlay(self) -> None:
        """读覆盖层 emotion_map.json（损坏按缺失处理，只读不修——console 同约定）。"""
        self._overlay_map = {}
        if self.overlay_path is None or not self.overlay_path.is_file():
            return
        try:
            data = json.loads(self.overlay_path.read_text(encoding="utf-8"))
            raw = data.get("map") if isinstance(data, dict) else data
            if isinstance(raw, dict):
                self._overlay_map = {
                    str(k): str(v) for k, v in raw.items() if str(v).strip()
                }
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[voice] 映射覆盖层读取失败，按缺失处理：%s", exc)

    def effective_map(self) -> "dict[str, str]":
        """生效映射表：默认表 ← vl.toml [tts].emotion_map ← 覆盖层文件。"""
        merged = dict(DEFAULT_EMOTION_MAP)
        merged.update(self._config_map)
        merged.update(self._overlay_map)
        return merged

    def reload_mapping(self) -> dict:
        """控制台映射表落盘后的热加载钩子：重读覆盖层，返回新生效表。"""
        self._load_overlay()
        logger.info("[voice] 映射表已热加载（覆盖层 %d 行）", len(self._overlay_map))
        return self.effective_map()

    # ------------------------------------------------------------------
    # 生命周期接线（__main__ / 控制台）
    # ------------------------------------------------------------------

    def start_background(self) -> None:
        """主事件循环内调用：捕获 loop 引用并启动 lifecycle 常驻探测任务。

        幂等；--smoke 与测试不调用（绝不意外拉起 GSVI 子进程）。
        """
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("[voice] start_background 需在事件循环内调用（本次忽略）")
                return
        if self._bg_task is not None and not self._bg_task.done():
            return
        self._bg_task = asyncio.ensure_future(self.lifecycle.run_forever())
        logger.info("[voice] 生命周期后台任务已启动（adopt-first + 30s 探测）")

    def start(self) -> dict:
        """控制台启停卡「启动」（同步方法）：threadsafe 回主循环执行状态机操作。

        必须同步的原因与 :meth:`preview` 一致——控制台线程经 ``asyncio.run``
        驱动协程会新建临时循环：``start_background`` 的 ``get_running_loop()``
        会把 ``_loop`` 污染成该临时循环（后台任务随 asyncio.run 结束而死，
        ``preview`` 从此永久降级），且 lifecycle 状态机会与主循环常驻
        ``run_forever`` 跨线程并发改写。无主循环（--smoke/装配未完成）时如实
        返回当前状态，绝不另起循环抢绑。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("[voice] 启动请求不可用：主事件循环未就绪（框架启动后可用）")
            return {"state": self.lifecycle.state, "ownership": self.lifecycle.ownership}
        future = asyncio.run_coroutine_threadsafe(self._start_async(), loop)
        timeout = float(self.lifecycle.params.ready_budget) + 30.0
        try:
            return future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 —— 超时/异常如实返回当前状态
            logger.warning("[voice] 启动请求失败（%s）", exc)
            return {"state": self.lifecycle.state, "ownership": self.lifecycle.ownership}

    async def _start_async(self) -> dict:
        """主循环内执行启动：后台任务缺失则补启，再按当前状态分支推进。

        - not_started：走 adopt_or_start（收编外部实例或按 auto_start 拉起）；
        - degraded：走 maybe_recover（冷却未过原样返回，冷却已过半开重试一次）
          ——绝不绕过熔断冷却直接拉起；
        - starting/restarting/ready/suspected_down：后台循环已在管，插手即与
          ``run_forever`` 竞态，原样返回当前状态。
        """
        if self._bg_task is None or self._bg_task.done():
            self.start_background()
        state = self.lifecycle.state
        if state == STATE_NOT_STARTED:
            state = await self.lifecycle.adopt_or_start()
        elif state == STATE_DEGRADED:
            await self.lifecycle.maybe_recover()
            state = self.lifecycle.state
        return {"state": state, "ownership": self.lifecycle.ownership}

    def stop(self) -> dict:
        """控制台启停卡「停止」（同步方法）：核验链终止自有进程。

        与 :meth:`start_sync` 同理由：主循环在跑时 threadsafe 回主循环执行
        ``stop_engine``（其中 ``_respawn_task.cancel()`` 只能由属主线程调用）；
        无主循环（测试/无后台形态，不存在并发循环竞态）时 ``asyncio.run`` 直驱。
        """
        loop = self._loop
        if loop is not None and not loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(
                self.lifecycle.stop_engine(), loop)
            try:
                return future.result(timeout=60.0)
            except Exception as exc:  # noqa: BLE001 —— 如实返回当前状态
                logger.warning("[voice] 停止请求失败（%s）", exc)
                return {"state": self.lifecycle.state,
                        "ownership": self.lifecycle.ownership}
        return asyncio.run(self.lifecycle.stop_engine())

    def state_name(self) -> str:
        """§2 引擎状态门消费：仅 \"ready\" 放行。"""
        return self.lifecycle.state

    def cleanup(self) -> None:
        """同步收尾（__main__ 清理钩子）：终止自有 GSVI + 取消后台任务。"""
        if self._bg_task is not None:
            self._bg_task.cancel()
            self._bg_task = None
        self.lifecycle.cleanup()

    # ------------------------------------------------------------------
    # 合成主入口（§1 契约：synthesize(text, mood_snapshot) -> wav | None）
    # ------------------------------------------------------------------

    async def synthesize(
        self, text: str, mood_snapshot: Any = None, *, wait: bool = False
    ) -> "bytes | None":
        """合成一段文本：映射 → 缓存 → 单飞合成；失败/降级返回 None。

        - 熔断冷却期：跳过重试直接 None（§2）；未就绪：None（wait=True 供试听
          场景在预算内等就绪；聊天路径绝不 await 引擎冷启）；
        - 全程持有全局单飞锁的只有「未命中缓存的出网合成」——缓存命中不占锁
          （不产生 /tts 请求，无需串行）；
        - 空/纯空白文本抛 ValueError（调用方发送侧应先过滤）。
        """
        if not text or not text.strip():
            raise ValueError("synthesize(text) 收到空文本：发送侧应先过滤空段")
        self.stats["synth_requests"] += 1
        lc = self.lifecycle

        # 0) 熔断冷却：跳过重试直接降级（§2）
        if lc.state_name() == "degraded":
            self.stats["degraded_skips"] += 1
            logger.info("[voice] 引擎熔断冷却期，本条直接降级纯文字（text 长度 %d）", len(text))
            return None
        # 1) 就绪门：未就绪不阻塞文字路径（wait=True 仅试听/控制台使用）
        if lc.state_name() != "ready":
            if not wait or not await lc.wait_ready():
                self.stats["degraded_skips"] += 1
                logger.info("[voice] 引擎未就绪（%s），本条降级纯文字（text 长度 %d）",
                            lc.state_name(), len(text))
                return None

        # 2) 情绪解析（回退链；注册表按需拉一次，之后常驻——禁轮询）
        resolution = await self._resolve_emotion(mood_snapshot)
        logger.debug("[voice] 情绪解析：mood=%r → emotion=%r（source=%s，degraded=%s）",
                     mood_snapshot, resolution.emotion, resolution.source,
                     resolution.degraded)

        # 3) 缓存先行（读不占锁）
        key = self.cache.make_key(
            character=self.character, emotion=resolution.emotion,
            text_language=self.client.text_lang,
            audio_format=self.client.audio_format, text=text,
        )
        hit = self.cache.get(key)
        if hit is not None:
            self.stats["cache_hits"] += 1
            logger.debug("[voice] 缓存命中（key=%s…，%d 字节）", key[:12], len(hit))
            return hit

        # 4) 全局单飞：任一时刻至多一个 in-flight /tts（§1 串行化契约）
        async with self._synth_lock:
            hit = self.cache.get(key)  # 双检：等锁期间可能已被并行请求填入
            if hit is not None:
                self.stats["cache_hits"] += 1
                return hit
            audio = await self.client.synthesize(text, emotion=resolution.emotion)
            if audio is None:
                self.stats["synth_failures"] += 1
                await self.lifecycle.report_synth_failure()  # HANG 型喂入（后台判死）
                return None
            self.lifecycle.report_synth_success()
            self.cache.put(key, audio)
            return audio

    # ------------------------------------------------------------------
    # 试听（控制台 §3：同步方法——内部 threadsafe 回主循环，共用缓存与单飞锁）
    # ------------------------------------------------------------------

    def preview(self, text: str, emotion: str) -> "bytes | None":
        """控制台试听：指定注册情绪直接合成（不走 mood 映射），共享缓存与单飞锁。

        必须保持同步：控制台线程调用，经 run_coroutine_threadsafe 回主循环
        （asyncio.Lock 绑定主循环，绝不在控制台线程另起 asyncio.run 争锁）。
        引擎未就绪/主循环未知/合成失败一律返回 None（降级语义）。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("[voice] 试听不可用：主事件循环未就绪（框架启动后可用）")
            return None
        future = asyncio.run_coroutine_threadsafe(
            self._preview_async(text, emotion), loop)
        timeout = float(self.client.timeout) * int(self.client.retries) + 30.0
        try:
            return future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 —— 超时/取消/异常统一降级
            logger.warning("[voice] 试听合成失败（%s）", exc)
            return None

    async def _preview_async(self, text: str, emotion: str) -> "bytes | None":
        if not text or not str(text).strip():
            return None
        if self.lifecycle.state_name() != "ready":
            return None
        emotion = str(emotion or "").strip() or "default"
        key = self.cache.make_key(
            character=self.character, emotion=emotion,
            text_language=self.client.text_lang,
            audio_format=self.client.audio_format, text=text,
        )
        hit = self.cache.get(key)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit
        async with self._synth_lock:  # 与聊天共用单飞锁（§3：共用缓存与串行锁）
            hit = self.cache.get(key)
            if hit is not None:
                self.stats["cache_hits"] += 1
                return hit
            audio = await self.client.synthesize(str(text), emotion=emotion)
            if audio is None:
                self.stats["synth_failures"] += 1
                return None
            self.lifecycle.report_synth_success()
            self.cache.put(key, audio)
            return audio

    # ------------------------------------------------------------------
    # 情绪解析 / 状态卡
    # ------------------------------------------------------------------

    async def _resolve_emotion(self, mood_snapshot: Any) -> EmotionResolution:
        """有效状态 → 注册情绪（回退链打点在 mapping 层）。"""
        snapshot = _normalize_snapshot(mood_snapshot)
        if snapshot is None and self.mood_getter is not None:
            try:
                snapshot = self.mood_getter()
            except Exception:  # noqa: BLE001 —— 情绪源故障不阻塞语音
                logger.exception("[voice] mood getter 异常（按无快照处理）")
                snapshot = None
        registry = None
        try:
            registry = await self.client.character_list()
        except Exception:  # noqa: BLE001 —— 注册表不可用按「不校验」处理
            logger.exception("[voice] 注册表拉取异常（映射值直接发送）")
            registry = None
        emotions = self.client.registry_for(self.character) if registry else None
        return emotion_for_snapshot(
            snapshot,
            emotion_map=self.effective_map(),
            default_emotion=self.default_emotion,
            registry=emotions,
            character=self.character,
        )

    def status(self) -> dict:
        """控制台状态卡数据（console/voice.py 契约键，扁平结构）。"""
        lc_status = self.lifecycle.status()
        cache_stats = self.cache.stats()
        registry = self.client._registry
        registered: Any
        own = self.client.registry_for(self.character)
        if own is not None:
            registered = {self.character: list(own)}
        elif registry is not None:
            registered = dict(registry)
        else:
            registered = []
        return {
            "lifecycle": lc_status["lifecycle"],
            "ownership": lc_status["ownership"],
            "character": self.character,
            "registered_emotions": registered,
            "cache_count": cache_stats["entries"],
            "cache_bytes": cache_stats["bytes"],
            "cache_max_entries": cache_stats["max_entries"],
            "cache_max_bytes": cache_stats["max_bytes"],
            "pid": lc_status["pid"],
            "started_at": lc_status["started_at"],
            "uptime_s": lc_status.get("uptime_s"),  # lifecycle 内部同钟求差，控制台直用
            "auto_start": lc_status["auto_start"],
            "base_url": self.client.base_url,
            "log_path": lc_status["log_path"],
            "last_error": lc_status["last_error"],
            "stats": dict(self.stats),
            "effective_emotion_map": self.effective_map(),
        }


# 默认映射表来源（engine.effective_map 的第一层；从 mapping 取，避免第二份拷贝）
from .mapping import DEFAULT_EMOTION_MAP as DEFAULT_EMOTION_MAP_SOURCE  # noqa: E402


class _EngineTtsAdapter:
    """proactive ⑧ 兼容面：``ctx.tts_client.synthesize(text)`` 走引擎全栈。

    proactive 现有回调只吃 text——经此适配后自动获得单飞串行、缓存、生命周期
    门与情绪映射（mood 由引擎的 mood_getter 取会话用户当前有效状态）。
    """

    def __init__(self, engine: VoiceEngine) -> None:
        self._engine = engine

    async def synthesize(self, text: str) -> "bytes | None":
        return await self._engine.synthesize(text)


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def setup(ctx: Any) -> "Callable[[], None] | None":
    """按 vl.toml [tts] 构造 VoiceEngine 挂 ctx.voice_engine。

    - tts.enabled=false：跳过装配（管线纯文字）；
    - 装配序要求本模块排在 voice.manager（§2 门面）**之后**——门面 setup 先读
      ctx.voice_engine 接缝，本 setup 再把控制台契约方法转发到门面实例上；
    - ctx.tts_client 换成引擎适配器（proactive ⑧ 回调不变，自动获得全栈行为）；
    - 返回清理钩子：终止自有 GSVI（外部实例绝不碰）。
    """
    tts_cfg = ctx.cfg.tts
    if not tts_cfg.enabled:
        logger.info("[voice] TTS 已禁用（tts.enabled=false），语音引擎不装配")
        return None

    repo = Path(ctx.repo_root)
    data_dir = repo / ctx.cfg.storage.data_dir

    client = GsviClient(
        base_url=tts_cfg.base_url,
        character=tts_cfg.character,
        emotion=tts_cfg.emotion,
        text_lang=tts_cfg.text_lang,
        audio_format=tts_cfg.audio_format,
        timeout=float(tts_cfg.timeout),
        retries=int(tts_cfg.retries),
        # R7（FUSION_SPEC §3.1）：与 lifecycle 注入同源的 Basic 口令——引擎侧
        # enable_auth=true 时客户端凭此通过认证；空=两侧都不武装（旧行为）
        auth_password=str(getattr(tts_cfg, "tts_auth_password", "") or ""),
    )
    cache = VoiceCache(
        data_dir / "voice_cache",
        max_entries=int(getattr(tts_cfg, "cache_max_entries", 500)),
        max_bytes=int(getattr(tts_cfg, "cache_max_bytes", 128 * 1024 * 1024)),
    )
    lifecycle = GsviLifecycle(
        LifecycleParams(
            auto_start=bool(getattr(tts_cfg, "auto_start", True)),
            gsvi_root=str(getattr(tts_cfg, "gsvi_root", "voice-engine")),
            host=client.host,
            port=client.port,
            pid_path=data_dir / "voice" / "voice-engine.pid",   # R6 中性化（原 gsvi.pid）
            log_path=data_dir / "logs" / "voice-engine.log",    # R6 中性化（原 gsvi.log）
            # 同一口令单源：_spawn 拉起前注入引擎 config.json（enable_auth+user）
            auth_password=str(getattr(tts_cfg, "tts_auth_password", "") or ""),
        ),
        repo_root=repo,
    )

    # mood 有效状态 getter：与文字注入同源（soul.mood.effective_mood，禁止解析注入串）
    def mood_getter() -> "tuple[str, float] | None":
        mood_engine = getattr(ctx, "mood_engine", None)  # 装配序晚于本模块，闭包实时取
        if mood_engine is None:
            return None
        try:
            user_id = str(ctx.cfg.session_id or "").rsplit(":", 1)[-1]
            state = mood_engine.storage.get(user_id)
            return effective_mood(state, now=time.time())
        except Exception:  # noqa: BLE001 —— 情绪源故障按无快照处理
            logger.exception("[voice] mood 有效状态读取失败")
            return None

    engine = VoiceEngine(
        client=client,
        cache=cache,
        lifecycle=lifecycle,
        emotion_map=dict(getattr(tts_cfg, "emotion_map", None) or {}),
        default_emotion=str(getattr(tts_cfg, "default_emotion", "聊天")),
        mood_getter=mood_getter,
        overlay_path=data_dir / "voice" / "emotion_map.json",
    )
    ctx.voice_engine = engine

    # 控制台契约（§3）转发到 §2 门面（若已装配且未实现）——控制台只认 voice_manager。
    # 装配序兜底：voice.manager 先于本模块装配时读不到 voice_engine/tts_client
    # 接缝会跳过门面装配——此时接缝已就绪，补跑一次其 setup 再转发。
    facade = getattr(ctx, "voice_manager", None)
    if facade is None and tts_cfg.enabled:
        try:
            from virtuallover.voice import manager as manager_mod

            manager_mod.setup(ctx)
            facade = getattr(ctx, "voice_manager", None)
            if facade is not None:
                logger.info("[voice] §2 门面补装配完成（voice_engine 接缝就绪后重跑）")
        except Exception:  # noqa: BLE001 —— 门面缺失不阻塞引擎装配
            logger.exception("[voice] §2 门面补装配失败（控制台语音页将按未装配降级）")
    if facade is not None:
        forwarded = []
        for name in ("status", "preview", "start", "stop", "reload_mapping"):
            if not callable(getattr(facade, name, None)) and callable(getattr(engine, name, None)):
                setattr(facade, name, getattr(engine, name))
                forwarded.append(name)
        if forwarded:
            logger.info("[voice] 控制台契约方法已转发到 voice_manager 门面：%s",
                        ", ".join(forwarded))

    # proactive ⑧ 兼容面：tts_client.synthesize(text) → 引擎全栈
    ctx.tts_client = _EngineTtsAdapter(engine)

    logger.info(
        "[voice] VoiceEngine 装配完成（base_url=%s，character=%s，auto_start=%s，"
        "缓存上限 %d 条/%d MB）",
        client.base_url, client.character, lifecycle.params.auto_start,
        cache.max_entries, cache.max_bytes // (1024 * 1024),
    )
    return engine.cleanup
