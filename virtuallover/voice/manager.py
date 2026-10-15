# -*- coding: utf-8 -*-
"""voice/manager.py —— VoiceManager：语音管线门面（VOICE_SPEC §2）。

分工（与并行落地的 §1 ``voice/engine.py`` 的书面约定一致）：
- **本类 = §2 管线门面**：每用户有序队列后台 worker（一段一语音，文字路径零改动、
  绝不 await 合成）；段间间隔复用 brain.sender.sample_gap；语音发送前过共享限流
  （10 条/30s）；dual_output 双轨——主回复文字必发+语音附加，proactive 成功只发
  语音/失败回退文字（对齐 soul/proactive.py ⑧ 现有实现）；trigger_probability 按
  回复整体抽样一次（种子 rng，DEBUG 记录）；引擎 waiting/degraded 期间降级纯文字。
- **§1 引擎栈**（串行化锁/情绪映射/磁盘缓存/生命周期门的权威实现点）在
  ``voice/engine.py``：本类经惰性 ``ctx.voice_engine`` 接缝消费其
  ``synthesize(text, mood_snapshot) -> bytes | None`` 契约——引擎缺位时（测试/
  过渡形态）退化为注入 synth + 本地单飞锁 + 可注入状态门。

mood 快照：调用方在 persona 装配时刻取同一有效状态传入——
``soul.mood.effective_mood(state, now)`` 返回的 ``(mood, intensity)`` 元组
（τ=4h 衰减 + <0.15 回落 calm，与文字注入同一数学）；本模块只透传给引擎，
禁止任何一方解析注入字符串。

语速链（发送节奏与语音质感项）：mood 快照在 :meth:`VoiceManager.synthesize`
的引擎路径同一处经 ``voice.mapping.speed_for_snapshot`` 产出语速倍率，经
``speed_factor=`` 关键字透传引擎（签名探测兼容旧两参契约，绝不试错出网）；
引擎侧接缝 ``cache.make_key(speed=…)`` 与 ``client.synthesize(speed_factor=…)``
已就绪。首次合成成功落 ``<storage.data_dir>/first_voice.json``（{ts, text 截断
50 字}，已存在不覆盖）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..brain import sender as sender_mod
from ..soul import textclean
from .mapping import speed_for_snapshot

__all__ = ["VoiceManager", "VoiceJob", "setup"]

logger = logging.getLogger("virtuallover.voice.manager")

# R2 队列有界：每用户语音队列上限（满丢最旧+告警；语音是文字轨的附加品，
# 积压的旧语音比丢弃更伤——发出来时对话早翻篇了）
VOICE_QUEUE_MAX_ITEMS = 20

# 注入接缝类型（仅作注释约定，运行期 duck typing）：
#   EngineProvider = () -> engine | None（engine.synthesize(text, mood_snapshot) 契约）
#   SynthFn        = async (text, mood_snapshot|None) -> bytes | None   （裸形态）
#   SendVoiceFn    = async (user_id: str, audio: bytes) -> bool


def _accepts_speed_factor(synth: Any) -> bool:
    """合成引擎契约是否收 ``speed_factor`` 关键字（inspect 签名探测，不试错调用）。

    引擎栈（voice.engine.VoiceEngine.synthesize）声明该参后语速透传即自动生效；
    旧两参契约 / 签名不可查的内建或奇形对象一律按不接受处理——绝不为探测而
    重复出网。
    """
    try:
        inspect.signature(synth).bind("_", None, speed_factor=1.0)
        return True
    except (TypeError, ValueError):
        return False


@dataclass
class VoiceJob:
    """一条回复的语音任务：分段列表 + 该回复的 mood 快照（整体一次抽样通过后入队）。"""

    segments: "list[str]"
    mood_snapshot: "tuple[str, float] | None" = None
    enqueued_at: float = 0.0  # 入队时刻（time.time()），排队时长观测用


class VoiceManager:
    """语音管线门面：每用户有序 worker + dual_output 双轨 + trigger 抽样 + 降级。

    注入约定（duck typing，生产装配见 ``setup``；测试可只注 synth/send_voice）：
    - engine_provider：惰性取 §1 引擎栈（ctx.voice_engine）——存在时其 synthesize
      自带全局单飞锁/映射/缓存/生命周期门/HANG 喂入，本类只透传；
    - synth：裸形态合成引擎 ``(text, mood_snapshot) -> bytes|None``（GptSoVits
      单参契约也兼容）；
    - send_voice：桥发送面 ``(user_id, wav_bytes) -> bool``（内部转 base64://record）；
    - limiter：与文字共用的发送限流器（brain.sender.RateLimiter，10 条/30s stall）；
    - gap_params：段间间隔参数（cfg.reply.segmented，sample_gap 消费）；None 则不等待；
    - trigger_probability：回复级语音触发概率 [0,1]，越界值收敛到边界；
    - engine_gate：``() -> bool``，False=引擎 waiting/degraded（裸形态下跳过合成
      直接降级；引擎栈就位时门在引擎内实现，此参不消费）；
    - engine_cleanup：引擎自有清理（终止自有 GSVI），注册进本类 cleanup 链；
    - first_voice_path：首次语音纪念落盘路径（local/vl_data/first_voice.json，
      setup 注入）；None=不记录；
    - clock：first_voice 时间戳时钟（可注入，缺省 time.time）。
    """

    def __init__(
        self,
        *,
        send_voice: Callable[[str, bytes], Awaitable[bool]],
        synth: "Callable[..., Awaitable[bytes | None]] | None" = None,
        engine_provider: "Callable[[], Any] | None" = None,
        limiter: Any = None,
        gap_params: Any = None,
        trigger_probability: float = 1.0,
        engine_gate: "Callable[[], bool] | None" = None,
        engine_cleanup: "Callable[[], None] | None" = None,
        first_voice_path: "str | Path | None" = None,
        clock: "Callable[[], float] | None" = None,
        rng: "random.Random | None" = None,
    ) -> None:
        self._synth = synth
        self._engine_provider = engine_provider
        self._send_voice = send_voice
        self._limiter = limiter
        self._gap_params = gap_params
        self._engine_gate = engine_gate
        self._engine_cleanup = engine_cleanup
        self._first_voice_path = Path(first_voice_path) if first_voice_path else None
        self._clock = clock or time.time
        self._rng = rng or random.Random()
        # 裸形态单飞锁（引擎栈就位时权威锁在 engine._synth_lock，此锁不消费）
        self._synth_lock = asyncio.Lock()
        # 每用户有序队列 + 后台 worker（懒创建：首个语音回复到达时启动）
        self._queues: "dict[str, asyncio.Queue]" = {}
        self._workers: "dict[str, asyncio.Task]" = {}
        self.trigger_probability = min(1.0, max(0.0, float(trigger_probability)))
        # 运行观测计数（控制台语音页可直接读）
        self.stats: "dict[str, int]" = {
            "jobs": 0,            # 入队的语音回复数
            "segments_sent": 0,   # 成功发送的语音段
            "segments_failed": 0, # 合成失败/发送失败的段
            "degraded_skips": 0,  # 引擎未就绪/degraded 冷却期跳过的合成次数
            "queue_dropped": 0,   # 队列满丢弃的最旧作业数（R2 队列有界观测面）
            "dirty_skips": 0,     # R3-2b：清洗后为空跳过合成的段（emoji/碎片段）
        }

    # ------------------------------------------------------------------
    # 状态卡（console/voice.py 鸭类型契约；R2 补基线实现）
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """控制台语音页状态卡数据（console/voice.py 契约键，扁平结构）。

        分层语义：
        - §1 引擎栈就位：voice.engine.setup 会把引擎的 status/preview/start/stop
          转发（setattr）到本门面实例上——生产路径状态卡数据源是引擎全量状态
          （lifecycle/cache/pid 等），本方法被转发覆盖、不再被调用；
        - 引擎缺位（裸形态/装配前/转发前）：本基线实现兜底——门面自身可观测的
          计数器与队列数据如实上报，lifecycle 按引擎栈缺失报 "not_started"，
          绝不假装在线。
        """
        engine = self._engine()
        if engine is not None and callable(getattr(engine, "status", None)):
            try:
                return dict(engine.status())
            except Exception:  # noqa: BLE001 —— 引擎状态读失败退回基线
                logger.warning("[voice] 引擎状态读取失败，退回门面基线状态", exc_info=True)
        queue_lens = [q.qsize() for q in self._queues.values()]
        return {
            "lifecycle": "not_started",
            "ownership": None,
            "character": "",
            "registered_emotions": [],
            "stats": dict(self.stats),
            "queue_count": len(self._queues),
            "queue_depth_total": int(sum(queue_lens)),
            "queue_max_items": VOICE_QUEUE_MAX_ITEMS,
        }

    # ------------------------------------------------------------------
    # 合成（生产=透传 §1 引擎栈；裸形态=本地门 + 单飞锁）
    # ------------------------------------------------------------------

    async def synthesize(
        self, text: str, mood_snapshot: "tuple[str, float] | str | None" = None
    ) -> "bytes | None":
        """合成一段文本的语音；引擎不可用/失败返回 None（调用方降级纯文字）。

        - §1 引擎栈就位：直接透传——waiting/degraded 门、情绪映射回退链、磁盘
          缓存、全局单飞锁、HANG 喂入全部在引擎内实现（权威实现点）；mood 快照
          在此同一处一并产出语速（``mapping.speed_for_snapshot``）经
          ``speed_factor=`` 关键字透传引擎（见 :meth:`_synth_via_engine`）；
        - 裸形态：引擎状态门 False 时跳过合成直接 None（§2：degraded 冷却期跳过
          重试），否则在本地单飞锁内调用注入 synth；
        - 任一路径合成成功且为本次会话第一段语音时，落 first_voice.json
          （见 :meth:`_record_first_voice`）。
        """
        if not text or not text.strip():
            raise ValueError("synthesize(text) 收到空文本：发送侧应先过滤空段")
        engine = self._engine()
        if engine is not None and callable(getattr(engine, "synthesize", None)):
            audio = await self._synth_via_engine(engine, text, mood_snapshot)
        else:
            if self._engine_gate is not None and not self._engine_allowed():
                self.stats["degraded_skips"] += 1
                logger.info("[voice] 引擎未就绪/degraded 冷却期，跳过合成直接降级（text 长度 %d）",
                            len(text))
                return None
            async with self._synth_lock:
                audio = await self._synth_bare(text, mood_snapshot)
        if audio:
            self._record_first_voice(text)
        return audio

    async def _synth_via_engine(
        self, engine: Any, text: str, mood_snapshot: "tuple[str, float] | str | None"
    ) -> "bytes | None":
        """引擎路径合成：mood 快照在解析情绪的同一处一并产出语速并透传。

        - speed = mapping.speed_for_snapshot(mood_snapshot)（与情绪映射同一快照
          口径；None 快照 → None → 不传，引擎内部还有 mood_getter 兜底）；
        - speed 非 None 且引擎契约收 ``speed_factor`` 关键字 → 透传（引擎侧接缝
          ``cache.make_key(speed=…)`` + ``client.synthesize(speed_factor=…)`` 已
          就绪，接住即全链生效）；
        - 现役引擎契约 ``(text, mood_snapshot)`` 尚未收该参时按签名探测退回两参
          调用——语速退化为服务端默认 1.0，语音绝不因语速透传失败（签名探测而非
          试错调用：绝不重复出网）。
        """
        speed = speed_for_snapshot(mood_snapshot)
        try:
            if speed is not None and _accepts_speed_factor(getattr(engine, "synthesize")):
                return await engine.synthesize(text, mood_snapshot, speed_factor=speed)
            return await engine.synthesize(text, mood_snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 引擎故障按失败降级，不炸 worker
            logger.warning("[voice] 引擎合成异常，按失败降级：%s", exc)
            return None

    def _record_first_voice(self, text: str) -> None:
        """首次合成成功落 first_voice.json（{ts, text 截断 50 字}；只记一次）。

        - 已存在不覆盖：首语音时刻是事实不是滑标（「她第一次开口」纪念数据）；
        - 原子写（tmp + os.replace，与 soul/anniversary.save_state 同模式）；
        - OSError 只告警不抛——记录失败绝不影响语音返回。
        """
        path = self._first_voice_path
        if path is None:
            return
        try:
            if path.is_file():
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"ts": float(self._clock()), "text": str(text)[:50]}
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            logger.info("[voice] 首次语音合成已记录（%s）", path)
        except OSError as exc:
            logger.warning("[voice] first_voice.json 记录失败（不影响语音）：%s", exc)

    async def _synth_bare(
        self, text: str, mood_snapshot: "tuple[str, float] | str | None"
    ) -> "bytes | None":
        """裸形态合成：调用注入 synth（兼容只吃 text 的旧单参契约）。"""
        if self._synth is None:
            logger.warning("[voice] 无可用合成引擎（engine_provider/synth 均未注入）")
            return None
        try:
            return await self._synth(text, mood_snapshot)
        except TypeError:
            # 过渡形态：引擎只吃 text（GptSoVits/GsviClient 单参契约）
            return await self._synth(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 合成故障按失败降级，不炸 worker
            logger.warning("[voice] 合成异常，按失败降级：%s", exc)
            return None

    def _engine(self) -> Any:
        """惰性取 §1 引擎栈（装配序晚于本模块：闭包实时读 ctx.voice_engine）。"""
        if self._engine_provider is None:
            return None
        try:
            return self._engine_provider()
        except Exception:  # noqa: BLE001
            logger.warning("[voice] 引擎栈读取失败，按裸形态处理", exc_info=True)
            return None

    def _engine_allowed(self) -> bool:
        """裸形态状态门求值；读不出状态时放行（fail-open，合成自身有超时兜底）。"""
        try:
            return bool(self._engine_gate())
        except Exception:  # noqa: BLE001
            logger.warning("[voice] 引擎状态门读取失败，按允许合成处理", exc_info=True)
            return True

    # ------------------------------------------------------------------
    # 主回复轨：文字先行，语音入每用户有序队列（绝不 await 合成）
    # ------------------------------------------------------------------

    def enqueue_reply(
        self,
        user_id: str,
        segments: "list[str]",
        *,
        mood_snapshot: "tuple[str, float] | None" = None,
    ) -> bool:
        """主回复语音附加（§2 双轨之一）：同步立即返回，绝不在调用方 await 合成。

        - trigger_probability 按回复整体抽样一次（种子 rng，DEBUG 记录掷点）：
          未触发则整条回复（含全部段）都不带语音；
        - 命中后把分段列表放入该用户的有序队列，懒启动后台 worker——段与段之间
          复用 sample_gap 间隔，发送前过共享限流；
        - 返回是否真正入队（概率未命中/空段返回 False）。
        """
        segments = [str(s or "").strip() for s in (segments or [])]
        segments = [s for s in segments if s]
        if not segments:
            return False
        roll = self._rng.random()
        fired = roll < self.trigger_probability
        logger.debug("[voice] trigger 抽样（回复整体一次）：roll=%.4f p=%.2f → %s",
                     roll, self.trigger_probability, "触发" if fired else "不触发")
        if not fired:
            return False
        queue = self._queues.get(user_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=VOICE_QUEUE_MAX_ITEMS)
            self._queues[user_id] = queue
            self._workers[user_id] = asyncio.ensure_future(self._worker(user_id))
            logger.debug("[voice] 用户 %s 的语音 worker 已启动", self._mask(user_id))
        # R2 队列有界：满 20 丢最旧 + 告警 + 计数——语音是文字的附加轨，旧语音
        # 过期无妨（发出来时对话早翻篇），绝不让队列无限积压
        if queue.full():
            try:
                queue.get_nowait()
                self.stats["queue_dropped"] += 1
                logger.warning("[voice] 语音队列已满（%d/%d），丢弃最旧作业（user=%s，"
                               "累计丢弃 %d）", queue.qsize(), VOICE_QUEUE_MAX_ITEMS,
                               self._mask(user_id), self.stats["queue_dropped"])
            except asyncio.QueueEmpty:  # full 与 get 间无让出点，防御性兜底
                pass
        queue.put_nowait(VoiceJob(segments=segments, mood_snapshot=mood_snapshot,
                                  enqueued_at=time.time()))
        self.stats["jobs"] += 1
        logger.debug("[voice] 已入队（user=%s，段数 %d，队列长 %d）",
                     self._mask(user_id), len(segments), queue.qsize())
        return True

    async def _worker(self, user_id: str) -> None:
        """单用户后台 worker：FIFO 消费该用户的语音任务（引擎恢复后自动续上）。"""
        queue = self._queues[user_id]
        while True:
            job = await queue.get()
            try:
                await self._process_job(user_id, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 单任务失败不终止 worker（后台继续等）
                logger.exception("[voice] 语音任务处理失败（worker 继续运行）")
            finally:
                queue.task_done()

    async def _process_job(self, user_id: str, job: VoiceJob) -> None:
        """一段一语音：逐段清洗 → 合成 → 共享限流 → 发送；段间复用 sample_gap 间隔。

        R3-2b：每段合成前过 TTS 文本清洗（emoji/颜文字/(?: 残留）——清洗后为空
        的段直接跳过（她宁可不念这一段，也不念一串乱码）。
        """
        total = len(job.segments)
        for index, segment in enumerate(job.segments):
            spoken = textclean.clean_tts_text(segment)
            if not spoken:
                self.stats["dirty_skips"] += 1
                logger.info("[voice] 段 %d/%d 清洗后为空（emoji/颜文字/碎片），跳过合成"
                            "（user=%s）", index + 1, total, self._mask(user_id))
                continue
            audio = await self.synthesize(spoken, job.mood_snapshot)
            if audio:
                if self._limiter is not None:
                    await self._limiter.acquire()  # §2：语音发送前过共享限流
                sent = False
                try:
                    sent = bool(await self._send_voice(user_id, audio))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[voice] 语音发送异常（user=%s，段 %d/%d）：%s",
                                   self._mask(user_id), index + 1, total, exc)
                if sent:
                    self.stats["segments_sent"] += 1
                else:
                    self.stats["segments_failed"] += 1
                    logger.warning("[voice] 语音发送失败（user=%s，段 %d/%d）——"
                                   "文字轨不受影响", self._mask(user_id), index + 1, total)
            else:
                # 该段降级纯文字：主回复轨文字已先行发出，无需补发（§2 降级语义）
                self.stats["segments_failed"] += 1
                logger.debug("[voice] 段 %d/%d 合成失败/降级，保持纯文字（user=%s）",
                             index + 1, total, self._mask(user_id))
            if index < total - 1:
                gap = 0.0
                if self._gap_params is not None:
                    gap = sender_mod.sample_gap(self._gap_params, self._rng)
                logger.debug("[voice] 段 %d/%d 处理完毕，%.2fs 后处理下一段",
                             index + 1, total, gap)
                if gap > 0:
                    await asyncio.sleep(gap)

    def drop_queued(self, user_id: str) -> int:
        """清空该用户队列里尚未开始处理的语音作业（R3-2c 失败收尾配套）。

        文字轨发送失败短路后，还没轮到的语音段不该再出帧（她在文字上已经
        「没说完」）；正在合成/发送中的段不受影响（自然完成）。返回丢弃数。
        """
        queue = self._queues.get(user_id)
        if queue is None:
            return 0
        dropped = 0
        while True:
            try:
                queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            self.stats["queue_dropped"] += dropped
            logger.info("[voice] 文字轨发送失败短路，丢弃 %d 个排队语音作业（user=%s）",
                        dropped, self._mask(user_id))
        return dropped

    # ------------------------------------------------------------------
    # proactive 轨：⑧TTS 优先（成功只语音、失败回退文字）——语义逐字还原
    # ------------------------------------------------------------------

    async def deliver_proactive(
        self,
        user_id: str,
        text: str,
        *,
        mood_snapshot: "tuple[str, float] | None" = None,
        always_send_text: bool = False,
        send_text: "Callable[[str], Awaitable[bool]] | None" = None,
    ) -> bool:
        """主动消息双轨（soul/proactive.py ⑧ 语义迁移落点，VOICE_SPEC §2）。

        - 合成成功 → 共享限流 → 发语音；``always_send_text=false``（现网实测配置）
          时**成功只发语音**；
        - 合成失败/发送失败 → 回退调 ``send_text`` 发纯文字（分段+限流由回调内部
          处理，对齐 proactive.setup 现有 _send_text）；
        - 返回是否有任一发送成功——proactive ⑦写回历史/③unanswered 计数的判定依据
          与原实现完全一致。
        """
        text = str(text or "").strip()
        if not text:
            return False
        sent_voice = False
        # R3-2b：合成前清洗——清洗后为空则整条跳过合成（直接走文字回退）
        spoken = textclean.clean_tts_text(text)
        audio = await self.synthesize(spoken, mood_snapshot) if spoken else None
        if audio:
            if self._limiter is not None:
                await self._limiter.acquire()  # §2：语音发送前过共享限流
            try:
                sent_voice = bool(await self._send_voice(user_id, audio))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[voice] 主动语音发送异常：%s", exc)
        need_text = (not sent_voice) or always_send_text
        if need_text and send_text is not None:
            try:
                sent_text = bool(await send_text(text))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[voice] 主动消息文字回退异常：%s", exc)
                sent_text = False
            return sent_text or sent_voice
        return sent_voice

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """事件循环存活期的完整收尾：取消全部 worker 并等待退出（_amain 调用）。"""
        tasks = [t for t in self._workers.values() if not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 —— 收尾从宽
                pass
        self._workers.clear()
        self._queues.clear()

    def cleanup(self) -> None:
        """同步收尾（__main__ 清理钩子约定）：尽力取消 worker + 引擎自有清理。

        「终止自有 GSVI」由注入的 engine_cleanup（voice.engine 的 VoiceEngine.cleanup
        → GsviLifecycle.cleanup）执行——PID 记录 + 命令行核验双门（不含
        voice_engine_entry.py 一律不杀），外部收编实例绝不触碰；本类绝不直接持有
        进程句柄。
        """
        for task in list(self._workers.values()):
            try:
                task.cancel()
            except Exception:  # noqa: BLE001 —— 事件循环已关闭时取消是良性 no-op
                pass
        self._workers.clear()
        if self._engine_cleanup is not None:
            engine_cleanup = self._engine_cleanup
            self._engine_cleanup = None  # 幂等：注册钩子可能被调用多次
            try:
                engine_cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("[voice] 引擎自有清理（终止自有 GSVI）失败")

    # ------------------------------------------------------------------

    @staticmethod
    def _mask(user_id: str) -> str:
        """日志里的用户号打码（敏感值规约：QQ 号不出现在日志正文）。"""
        text = str(user_id or "")
        return (text[:2] + "***") if len(text) > 2 else "***"


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def _make_send_voice(ctx: Any) -> "Callable[[str, bytes], Awaitable[bool]]":
    """构造桥发送回调：wav 字节 → base64:// record 段（运行时取 ctx.bridge，
    桥装配序在本模块之后，闭包引用 ctx 保证拿到最终实例——与 proactive 同手法）。"""
    import base64

    async def _send_voice(user_id: str, audio: bytes) -> bool:
        bridge = getattr(ctx, "bridge", None)
        if bridge is None or not audio:
            return False
        b64 = base64.b64encode(audio).decode("ascii")
        result = await bridge.send_record_b64(user_id, b64)
        if not result.ok:
            logger.warning("[voice] 语音发送失败（%s）", result.status)
        return bool(result.ok)

    return _send_voice


def setup(ctx: Any) -> "Callable[[], None] | None":
    """按 vl.toml [tts] 构造 §2 语音管线门面，挂 ctx.voice_manager。

    - tts.enabled=false 时跳过装配（管线纯文字，proactive 经 dual_send 接缝回退文字）；
    - 合成引擎接缝：惰性读 ``ctx.voice_engine``（§1 全栈，voice/engine.py 装配序在
      本模块**之后**——引擎 setup 会再把控制台契约方法转发到本门面实例上）；
    - 引擎状态门：引擎栈就位时门在其内部实现（degraded 冷却期直接 None）；此处
      保留裸形态门接缝供测试/替换引擎注入；
    - 返回清理钩子：取消 worker + 引擎自有清理（终止自有 GSVI，核验链在 lifecycle）。
    """
    tts_cfg = ctx.cfg.tts
    if not tts_cfg.enabled:
        logger.info("[voice] TTS 已禁用（tts.enabled=false），VoiceManager 不装配")
        return

    # 首次语音纪念落盘路径：<storage.data_dir>/first_voice.json（默认
    # local/vl_data/first_voice.json，与 voice-engine.pid 等运行态同目录，
    # 路径解析与 voice/engine.setup 同模式）
    data_dir = Path(ctx.repo_root) / ctx.cfg.storage.data_dir
    manager = VoiceManager(
        send_voice=_make_send_voice(ctx),
        synth=None,  # 引擎缺位时由 engine_provider 兜底判定（都缺=纯文字并告警）
        engine_provider=lambda: getattr(ctx, "voice_engine", None),
        limiter=getattr(ctx, "rate_limiter", None),   # 与文字共用的 10 条/30s stall 限流
        gap_params=ctx.cfg.reply.segmented,           # 段间间隔复用 sample_gap 参数
        trigger_probability=float(getattr(tts_cfg, "trigger_probability", 1.0)),
        first_voice_path=data_dir / "first_voice.json",
    )
    ctx.voice_manager = manager
    logger.info("[voice] VoiceManager 装配完成（trigger_probability=%.2f，"
                "每用户有序队列+dual_output 双轨就绪）", manager.trigger_probability)
    return manager.cleanup
