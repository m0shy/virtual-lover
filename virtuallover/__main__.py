# -*- coding: utf-8 -*-
"""python -m virtuallover 入口：装配各模块 → 接线事件流与调度 → 守护运行。

装配协议（各模块在包内暴露 ``setup(ctx)``）：
- 同步函数；返回值可为 None 或 ``callable``（无参清理钩子，进程退出时逆序调用）；
- 模块缺失或 setup 抛异常：告警并跳过，单模块故障不拖垮整个框架；
- ctx 为 :class:`AppContext`（config / bus / 运行根 / stop / 清理清单 + 各模块
  实例槽位），模块自己持有长期资源，不做全局单例。

事件流贯通（SPEC §2 事件总线 + PERF_SPEC §2-O3/O4 改造）::

    NapCat → 桥(白名单门禁，帧路由零业务 await) → EVENT_MESSAGE
      → brain/pipeline.py MessagePipeline（O3/O4：到达即起跑 + 打断地平线）
        → per-session 串行通道：命令分发/mood/ proactive 感知/history user 行
          （到达时刻落库）/memory user 计数——逐条按到达序执行
        → per-user 状态机（IDLE/RUNNING/DRIP）：立即发起 LLM 轮（检索+装配
          +chat，零窗口税）；打断期新消息 → generation-id 丢弃当前轮合并重跑；
          DRIP 分段发送中不可打断；距上条 >2min 冷启动不启用打断期
        → per-user 发送 FIFO：分段发送（log 分布间隔 + 10 条/30s stall 限流
          + 首段拟人抖动）统一收编主回复段/meme 图/proactive 文字/diary 文字
        → 语音双轨（文字完整发完才入队；独立队列不动）
      → （旧内联路径 _handle_user_message 保留：直驱测试/兜底用）

调度贯通（SPEC §8.2/§8.3）：__main__ 的 ticker 每分钟投递 EVENT_SCHEDULED，
proactive.tick（随机间隔/安静时段/暂停/启动触发）与 diary 触发点比对
（compute_next_diary_run + 0-20min 抖动 + 当日存档判重）都由心跳驱动；
两者的 LLM 生成 create_task 后台化 + 复入保护（O4：tick 处理 p95 ≤1s）。

调度贯通（SPEC §8.2/§8.3）：__main__ 的 ticker 每分钟投递 EVENT_SCHEDULED，
proactive.tick（随机间隔/安静时段/暂停/启动触发）与 diary 触发点比对
（compute_next_diary_run + 0-20min 抖动 + 当日存档判重）都由心跳驱动。

启动序：日志 → 配置（缺失/校验失败给中文报错退出 2）→ 桥 token 落盘 → 限流器
→ 逐模块 setup → 接线事件流与调度 → 事件循环内启动桥监听（端口占用报错退出 1）
→ 守护运行，Ctrl+C 优雅退出。``--smoke``：装配与监听就绪即优雅退出（就绪探测用）。
``--init-config``（O12）：全新运行根生成可启动的 vl.toml 模板（token 落盘、
--smoke 即就绪），解决新装无旧栈可迁的"鸡生蛋"。

运行根：默认仓库根；可用环境变量 ``VL_ROOT`` 指向其它目录（冒烟/测试在 tmp
准备 local/config/vl.toml 即可，绝不触碰真实 local/ 数据）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, List, Optional

from virtuallover.brain import persona, sender
from virtuallover.brain.llm import content_to_text
from virtuallover.config import ConfigError, VlConfig, ensure_bridge_token, load_config
from virtuallover.events import (
    EVENT_MESSAGE,
    EVENT_SCHEDULED,
    EventBus,
    tick_forever,
)
from virtuallover.soul import anniversary as anniversary_mod
from virtuallover.soul import diary as diary_mod
from virtuallover.soul import memes as memes_mod
from virtuallover.soul import mood as mood_mod
from virtuallover.soul import routine as routine_mod
from virtuallover.soul import weather as weather_mod
from virtuallover.soul.proactive import is_quiet as _proactive_is_quiet

__all__ = ["AppContext", "main"]

APP_VERSION = "2.0.0a0"

logger = logging.getLogger("virtuallover.main")

# 装配清单（顺序即依赖序）：模块名 → 各自暴露 setup(ctx)
_SETUP_TARGETS: List[str] = [
    "virtuallover.voice.manager",          # 语音管线门面（§2：队列/双轨；消费 ctx.voice_engine 接缝）
    "virtuallover.brain.llm",              # chat/embedding 双客户端（§4）
    "virtuallover.brain.history",          # 会话存储 + 双写镜像（§6）
    "virtuallover.memory.store",           # 向量库 + 写入触发服务（§7）
    "virtuallover.soul.mood",              # 情绪引擎（§8.1，port-of-own）
    "virtuallover.soul.diary",             # 日记服务（§8.2，port-of-own）
    "virtuallover.soul.proactive",         # 主动消息 8 条行为（§8.3）
    "virtuallover.commands",               # /mood /diary /eval 命令面（§8.5）
    "virtuallover.bridge.onebot_server",   # OneBot 反向 WS 服务端（§3，_amain 内启动监听）
    "virtuallover.brain.pipeline",         # O3/O4 对话管线（PERF_SPEC §2：打断地平线 + per-user 双队列；订阅 EVENT_MESSAGE）
    "virtuallover.console",                # 控制台 v2（§10，workbench 移植）
    "virtuallover.voice.engine",           # §1 语音引擎栈（单飞/缓存/生命周期/映射；必须晚于 voice.manager——把控制台契约转发到门面）
]
# 纯函数模块不进装配清单：brain.persona（装配管道）、soul.memes（&&tag&& 后处理）、
# memory.backfill（离线工具）与 memory.benchmark（/eval 评测逻辑）由管线直接调用；
# voice.gptsovits（SPEC §9 客户端）已并入 voice.engine（兼容 shim 保留旧名导入）。

REPO_ROOT = Path(__file__).resolve().parent.parent

# mood 快照片段的注入字节上限（persona.DEFAULT_MAX_MEMORY_BYTES 管记忆块，mood 片段单行很短）
_MEME_DIR_DEFAULT = "local/shared_plugin_data/meme_manager/memes"
_EVAL_REPORT_DIR = "local/shared_plugin_data/vl_eval/reports"
# /eval memory 评测会话后缀（独立于日常会话，报告后整会话清理，SPEC §8.5）
_EVAL_SESSION_SUFFIX = ":EvalMemory"


@dataclass
class AppContext:
    """跨模块共享的装配上下文：配置/总线/运行根 + 各模块实例槽位（setup 填充）。"""

    cfg: VlConfig
    bus: EventBus
    repo_root: Path
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    cleanups: List[Callable[[], None]] = field(default_factory=list)
    # ---- 发送面 ----
    rate_limiter: Any = None            # brain.sender.RateLimiter（全局限流 10 条/30s）
    # ---- 各模块实例槽位（_SETUP_TARGETS 的 setup(ctx) 填充，缺模块保持 None）----
    tts_client: Any = None             # voice 引擎适配器（voice.engine._EngineTtsAdapter；proactive ⑧ 消费）
    voice_manager: Any = None          # voice.manager.VoiceManager（§2 管线门面：队列/双轨）
    voice_engine: Any = None           # voice.engine.VoiceEngine（§1 引擎栈：单飞/缓存/生命周期/映射）
    chat_client: Any = None            # brain.llm.ChatClient（[chat] fallback_enabled=true 时为 brain.router.BrainRouter）
    brain_router: Any = None           # brain.router.BrainRouter（智能降级主路由；/brain 命令与 console 面板入口）
    embedding_client: Any = None       # brain.llm.EmbeddingClient
    history: Any = None                # brain.history.HistoryStore
    memory_store: Any = None           # memory.store.MemoryStore
    memory_service: Any = None         # memory.service.MemoryService
    mood_engine: Any = None            # soul.mood.MoodEngine
    diary_service: Any = None          # soul.diary.DiaryService
    proactive: Any = None              # soul.proactive.ProactiveService
    router: Any = None                 # commands.CommandRouter
    bridge: Any = None                 # bridge.onebot_server.OneBotBridge
    # ---- 调度状态（diary 触发点由心跳驱动时维护）----
    diary_next_ts: float = 0.0         # 下次日记触发时刻（含抖动）；0=未排定
    diary_rng: Any = None              # 抖动随机源（可注入，测试固定）


def _setup_logging(cfg: VlConfig, repo_root: Path) -> None:
    """基础日志：控制台 INFO + 轮转文件 ≤5MB×3（SPEC §13 日志规格）。"""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    console.setFormatter(fmt)
    root.addHandler(console)
    log_dir = repo_root / "local" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fileh = RotatingFileHandler(log_dir / "vl.log", maxBytes=5 * 1024 * 1024,
                                backupCount=3, encoding="utf-8")
    fileh.setLevel(logging.DEBUG)  # 轮询类噪音走 DEBUG 文件留档，控制台不受影响
    fileh.setFormatter(fmt)
    root.addHandler(fileh)
    # 五轮修缮 R2「最近事件面板」：进程内环形缓冲（WARNING+ / 关键 INFO，
    # deque 50）随主日志一起装配——控制台 /api/recent-errors 的数据源。
    from virtuallover.console.recent import attach_global_handler
    attach_global_handler(root)


def _import_setups(ctx: AppContext) -> List[Callable[[], None]]:
    """逐模块 import + setup(ctx)。缺模块=告警跳过；单模块异常不拖垮整体。"""
    import importlib

    cleanups: List[Callable[[], None]] = []
    for target in _SETUP_TARGETS:
        try:
            module = importlib.import_module(target)
        except ImportError as exc:
            logger.warning("模块 %s 尚未就绪，跳过装配（%s）", target, exc)
            continue
        setup = getattr(module, "setup", None)
        if not callable(setup):
            logger.warning("模块 %s 已存在但未暴露 setup(ctx)，跳过装配", target)
            continue
        try:
            result = setup(ctx)
        except Exception:  # noqa: BLE001 —— 单模块装配失败不拖垮整个框架
            logger.exception("模块 %s 的 setup(ctx) 抛出异常，跳过该模块", target)
            continue
        if asyncio.iscoroutine(result):
            logger.warning("模块 %s 的 setup 返回协程：请改用同步 setup；本次不会自动等待",
                           target)
        elif callable(result):
            cleanups.append(result)
        logger.info("模块 %s 装配完成", target)
    return cleanups


# ---------------------------------------------------------------------------
# 对话主管线（SPEC §2 事件流，顺序见模块 docstring）
# ---------------------------------------------------------------------------

def _session_user_id(cfg: VlConfig) -> str:
    """会话目标用户 = session_id 尾段（`<人格名>:FriendMessage:<QQ号>`）。"""
    return str(cfg.session_id or "").rsplit(":", 1)[-1]


def _build_eval_runner(ctx: AppContext) -> "Callable[[int], Any] | None":
    """构造 /eval memory 评测执行器（memory.benchmark 纯逻辑 + 进程内记忆库）。

    - 合成事实写入独立评测会话（不混入日常记忆，SPEC §8.5）；
    - 检索走 MemoryStore.search（余弦 + 关键词重排，与注入同管道）；
    - 报告渲染为 Markdown 回传命令面并存档 reports/ 目录，随后清空评测会话
      （清理失败只告警，评测数据可通过会话 ID 手工删除）。
    """
    store = ctx.memory_store
    service = ctx.memory_service
    if store is None or service is None:
        return None
    from virtuallover.memory import benchmark

    async def runner(n: int) -> List[str]:
        eval_session = str(ctx.cfg.session_id) + _EVAL_SESSION_SUFFIX
        cases = benchmark.build_fact_cases(n)
        write_failures: List[str] = []
        for case in cases:
            try:
                await service.remember(eval_session, case.fact_text, source="eval")
            except Exception as exc:  # noqa: BLE001 —— 单条写入失败并入报告
                write_failures.append(f"{case.marker}: {exc}")
        max_chars = ctx.cfg.mnemosyne.max_prompt_chars

        async def retriever(question: str) -> "list[tuple[str, float]]":
            hits = await store.search(
                eval_session, question[:max_chars],
                top_k=3, candidates=ctx.cfg.mnemosyne.candidate_k,
            )
            return [(hit.content, hit.cosine) for hit in hits]

        report = await benchmark.run_memory_benchmark(
            cases, retriever, top_k=3, write_failures=write_failures)

        async def _delete_session(session_id: str) -> None:
            store.delete_session(session_id)

        cleanup_ok, cleanup_err = await benchmark.cleanup_session_data(
            eval_session, _delete_session)
        markdown = benchmark.render_markdown_report(
            report,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            collection="virtuallover.memory",
            session_id=eval_session,
            write_mode="MemoryService.remember（显式 /remember 通道）",
            archive_note=None,
            cleanup_note=(
                "评测数据已清理。" if cleanup_ok else f"评测数据清理失败：{cleanup_err}"
            ),
        )
        # 报告存档（控制台 /api/eval 可读；目录缺失自建）
        reports_dir = Path(ctx.repo_root) / _EVAL_REPORT_DIR
        try:
            reports_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (reports_dir / f"eval_{stamp}.md").write_text(markdown + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("[eval] 报告存档失败：%s", exc)
        logger.info("[eval] 记忆评测完成：recall@1=%.2f recall@3=%.2f（n=%d）",
                    report.recall_at_1, report.recall_at_3, report.total_cases)
        return [markdown]

    return runner


def _wire_pipeline(ctx: AppContext) -> None:
    """装配后接线：/eval 评测器补挂。

    PERF_SPEC §2-O3/O4：EVENT_MESSAGE 的对话处理改由 brain.pipeline 的
    MessagePipeline 承接（其 setup(ctx) 已在 _import_setups 内订阅总线）——
    到达语义走 per-session 串行通道，LLM 轮走 per-user 打断地平线状态机，
    发送走 per-user FIFO。本函数不再重复订阅（防双路径重复处理）；
    _handle_user_message 保留为单条内联直驱路径（语音 e2e 等直驱测试用）。
    """
    router = ctx.router
    if router is not None and router.eval_runner is None:
        router.eval_runner = _build_eval_runner(ctx)
        if router.eval_runner is not None:
            logger.info("[eval] 评测执行器已装配（memory.benchmark + 进程内向量库）")
    if getattr(ctx, "message_pipeline", None) is None:
        # 管线模块缺失/装配失败时的兜底：退回旧内联订阅，消息处理不中断
        async def _on_message(event) -> None:  # noqa: ANN001 —— events.Event
            await _handle_user_message(ctx, event.payload)

        ctx.bus.on(EVENT_MESSAGE, _on_message)
        logger.warning("O3/O4 管线未装配，EVENT_MESSAGE 退回旧内联主管线")


async def _send_texts(ctx: AppContext, user_id: str, texts: List[str],
                      segmented: Any,
                      first_send_at: "float | None" = None,
                      message_id: str = "-",
                      arrived_mono: "float | None" = None) -> List[str]:
    """按分段参数逐条发送（命令回复与日记共用；失败只告警不抛）。

    返回实际送出的段列表——语音双轨据此「一段一语音」（VOICE_SPEC §2）：
    语音与文字逐段对齐，绝不在本函数 await 合成（文字路径零改动）。

    first_send_at：首段最早可发时刻（time.monotonic 基准，PERF_SPEC §7.1-1
    并行语义——主回复路径传「消息到达 + first_gap 抖动」，抖动不叠加延迟、
    只保拟人下限）；命令/日记路径不传（None），发送行为与旧版一致。

    message_id / arrived_mono：§7.1-6 日志关联——首个 send_private_msg 出帧
    时打一条 ``[perf] 首段已发送`` 行（message_id + 到达→出帧毫秒），判定器
    据此对齐 §7.1-5 首段时点口径；日记等无消息来源路径用默认值 "-"。

    发送失败短路（PERF_SPEC §2-O4）：任一段发送失败即停止本调用内剩余段的
    尝试（修掉「失败只告警、剩余段照发」的现状），返回已送出的段。
    """
    bridge = ctx.bridge
    if bridge is None:
        logger.warning("桥未装配，无法发送（%d 条文本丢弃）", len(texts))
        return []
    seg_no = {"n": 0}

    class _SendFailed(Exception):
        """单段发送失败的短路信号（O4③：剩余段不再尝试）。"""

    async def _send(segment: str):
        result = await bridge.send_private_text(user_id, segment)
        seg_no["n"] += 1
        if seg_no["n"] == 1:
            # 首段出帧即打点（首个 send_private_msg，§7.1-5 时点定义）
            e2e = ("-" if arrived_mono is None
                   else f"{(time.monotonic() - arrived_mono) * 1000:.0f}")
            logger.info("[perf] 首段已发送 message_id=%s e2e_ms=%s len=%d",
                        message_id, e2e, len(segment))
        if not result.ok:
            logger.warning("发送失败（%s）：user=%s 段长度 %d —— 短路剩余段",
                           result.status, user_id[:2] + "***", len(segment))
            raise _SendFailed
        return result

    sent: List[str] = []
    try:
        for text in texts:
            segments = await sender.send_reply(
                text, _send, params=segmented, limiter=ctx.rate_limiter,
                first_send_at=first_send_at)
            sent.extend(segments)
            # 抖动时刻只约束本轮第一条文本的首段；后续文本（多条 reply）不再等待
            first_send_at = None
    except _SendFailed:
        pass  # 短路：返回已送出的段
    return sent


# 记忆检索查询并入的最近历史条数（PERF_SPEC §3-O5 兜底补丁：1-2 条，取 2；
# 含 user/assistant 两侧——她的回复同样是「上文」的一部分）
_RECALL_HISTORY_MESSAGES = 2


def _build_recall_query(text: str, history_msgs: List[dict],
                        max_chars: int) -> str:
    """构造记忆检索查询：最近 1-2 条历史 + 本轮原文，尾部按 max_prompt_chars 截断。

    历史行只取纯文本（content 双形态扁平化、剥 think），空行跳过；不足
    _RECALL_HISTORY_MESSAGES 条时有多少并入多少。查询文本过长会稀释向量
    语义，总长仍受 mnemosyne.max_prompt_chars 约束（截尾部最新内容）。
    """
    parts: List[str] = []
    for msg in history_msgs[-_RECALL_HISTORY_MESSAGES:]:
        line = content_to_text(msg.get("content")).strip()
        if line:
            parts.append(line)
    parts.append((text or "").strip())
    query = "\n".join(part for part in parts if part)
    return query[:max(0, int(max_chars))]


async def _handle_user_message(ctx: AppContext, payload: Any) -> None:
    """一条已过桥侧门禁的私聊消息的完整处理管线（顺序固化，见模块 docstring）。"""
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return
    cfg = ctx.cfg
    user_id = str(payload.get("user_id") or "")
    text = str(payload.get("text") or "").strip()
    session_id = cfg.session_id
    if not user_id or not text:
        logger.debug("空文本/缺 user_id 的消息，跳过管线")
        return
    arrived_mono = time.monotonic()  # 消息到达时刻（O2a 首段抖动的折算基准）
    # 消息关联标记（PERF_SPEC §7.1-6）：判定器靠它把装配/首段发送日志归到消息
    message_id = str(payload.get("message_id") or "-")

    # ---- 1) 命令优先分发（/mood /diary /eval；未知 / 命令返回 None 静默忽略）----
    router = ctx.router
    if router is not None:
        replies = await router.handle(
            text, user_id, admins=cfg.admins, whitelist=cfg.whitelist)
        if replies is not None:
            await _send_texts(ctx, user_id, replies, cfg.reply.segmented,
                              message_id=message_id, arrived_mono=arrived_mono)
            _archive(ctx, session_id, text, "\n".join(replies))
            return

    # ---- 2) mood 更新（危机兜底先行；命令/空文本内部自跳过）----
    if ctx.mood_engine is not None:
        try:
            await ctx.mood_engine.handle_message(user_id, text)
        except Exception:  # noqa: BLE001 —— 情绪推进失败不阻塞对话
            logger.exception("情绪引擎处理失败（对话继续）")

    # ---- 3) 记忆检索（注入开关；失败降级为不注入，对话继续）----
    # 查询文本并入最近 1-2 条历史（PERF_SPEC §3-O5 兜底补丁）：单看本轮原文
    # 常缺指代（"那个怎么样了"），带上文一起向量化显著提升召回；
    # 历史读取因此提前到检索之前（装配步骤共用同一份）。
    history_msgs: List[dict] = []
    if ctx.history is not None:
        history_msgs = ctx.history.get_messages(session_id)
    memories: List[Any] = []
    if ctx.memory_store is not None and cfg.mnemosyne.inject_enabled:
        try:
            recall_query = _build_recall_query(
                text, history_msgs, cfg.mnemosyne.max_prompt_chars)
            memories = await ctx.memory_store.search(
                session_id,
                recall_query,
                top_k=cfg.mnemosyne.top_k,
                candidates=cfg.mnemosyne.candidate_k,
                # 相似度下限（O5 补丁）：低于阈值的命中不注入（0=关=旧行为）
                min_cosine=cfg.mnemosyne.min_similarity,
            )
        except Exception as exc:  # noqa: BLE001 —— 封锁态/未接线都降级
            logger.warning("记忆检索失败，本轮不注入（%s）", exc)

    # ---- 4) persona 装配（begin_dialogs + 历史 + 记忆 prepend + mood + 时间）----
    user_text = text
    reply_text = payload.get("reply_text")
    if not reply_text and payload.get("reply_id"):
        # PERF_SPEC §2-O4 后桥不再内联回查：直驱路径在此补一次 get_msg
        # （生产主管线由 brain/pipeline.py 在 per-user 轮内回查，互不影响）
        bridge = getattr(ctx, "bridge", None)
        if bridge is not None and callable(getattr(bridge, "get_msg_text", None)):
            try:
                reply_text = await asyncio.wait_for(
                    bridge.get_msg_text(payload.get("reply_id")), timeout=10.5)
            except Exception:  # noqa: BLE001 —— 回查失败置空，不阻塞主流程
                logger.warning("reply 回查失败（message_id=%s），引用文本置空",
                               payload.get("reply_id"))
                reply_text = None
    if reply_text:
        # 引用回查文本并入本轮用户消息（get_msg 已降级纯文本）
        user_text = f"[引用消息] {reply_text}\n{text}"
    mood_fragment = ""
    mood_snapshot = None  # persona 装配时刻的有效状态（VOICE_SPEC §2：一次快照贯穿文字注入与语音映射）
    if ctx.mood_engine is not None:
        try:
            now_ts_mood = time.time()
            mood_state = ctx.mood_engine.snapshot_state(user_id)  # 结构化读取（禁解析注入串）
            if ctx.mood_engine.inject_enabled:
                # 与语音映射同一状态对象派生（render_system_fragment 内部同 effective_mood 数学）
                mood_fragment = mood_mod.render_system_fragment(mood_state, now=now_ts_mood)
            mood_snapshot = mood_mod.effective_mood(mood_state, now=now_ts_mood)
        except Exception:  # noqa: BLE001
            logger.exception("mood 快照渲染失败（本轮不注入）")
    assembled = persona.assemble(
                persona_name=cfg.persona.name,
        persona_prompt=cfg.persona.system_prompt,
        user_text=user_text,
        history=history_msgs,
        begin_dialogs=cfg.persona.begin_dialogs,
        memories=memories,
        mood_fragment=mood_fragment,
        inject_mood=cfg.mood.inject_enabled,
        inject_time=True,
        now=time.time(),
        max_context_tokens=cfg.context.max_context_tokens,
        truncate_ratio=cfg.context.truncate_ratio,
        max_memory_items=cfg.mnemosyne.top_k,
        tz_label="CST",
        # O6：mood 片段注入位置（user=默认，资料块拼 user 侧保 system 前缀
        # 稳定以命中 prompt_cache_key；system=旧行为回退键）
        mood_position=cfg.mood.fragment_position,
        # §7.1-6：装配日志带 message_id，判定器据此归因到消息
        message_id=message_id,
    )

    # ---- 5) LLM 对话 ----
    if ctx.chat_client is None:
        logger.error("chat 客户端未装配，无法回复（消息已入情绪/历史管道）")
        _archive(ctx, session_id, user_text, "")
        return
    try:
        # O6 隐式缓存：prompt_cache_key=会话 ID——同会话前缀复用 provider 侧
        # KV 缓存（interrupt 重答复自然同键）；cached_tokens 随 usage 日志落盘
        reply = await ctx.chat_client.chat_async(
            assembled.messages,
            extra_body={"prompt_cache_key": session_id},
        )
    except Exception as exc:  # noqa: BLE001 —— 重试与 fallback 耗尽后的最终失败
        logger.error("LLM 调用最终失败，本轮静默（不回复）：%s", exc)
        _archive(ctx, session_id, user_text, "")
        return
    reply_text_out = reply.content
    if not reply_text_out.strip():
        logger.warning("LLM 返回空回复，本轮静默")
        _archive(ctx, session_id, user_text, "")
        return

    # ---- 6) meme &&tag&& 后处理（白名单 + 命中率 + 残缺清理）----
    memes_cfg = cfg.memes
    if memes_cfg.enable:
        plan = memes_mod.process_reply(
            reply_text_out,
            memes_dir=Path(ctx.repo_root) / (memes_cfg.memes_dir or _MEME_DIR_DEFAULT),
            whitelist=memes_mod.build_whitelist(memes_cfg.extra_tags),
            probability=memes_cfg.probability,
            max_images=memes_cfg.max_images,
            rng=random.Random(),
        )
    else:
        plan = memes_mod.MemePlan(text=reply_text_out, image_path=None)

    # ---- 7) 分段发送（log 分布间隔 + 全局限流 + 首段拟人抖动）----
    bridge = ctx.bridge
    if bridge is None:
        logger.warning("桥未装配，LLM 回复无法送出（长度 %d）", len(plan.text))
    else:
        # O2a 首段并行抖动（§7.1-1）：首段最早可发时刻 = 消息到达 + first_gap；
        # send_reply 内首段前补足差值等待 → 发送时刻 = max(LLM 完成, 到达+抖动)。
        # first_gap 配空串（sample_first_gap 返回 0）时时刻早于现在，零等待=关。
        first_gap = sender.sample_first_gap(cfg.reply.segmented)
        first_send_at = arrived_mono + first_gap if first_gap > 0 else None
        sent_segments = await _send_texts(
            ctx, user_id, [plan.text], cfg.reply.segmented,
            first_send_at=first_send_at,
            message_id=message_id, arrived_mono=arrived_mono)
        # ---- 7a) 语音双轨·主回复（VOICE_SPEC §2）：文字已先行发出；分段语音入
        # 每用户有序队列后台「一段一语音」处理——绝不 await 合成，触发概率按
        # 回复整体抽样一次在 enqueue_reply 内完成；mood 快照取装配时刻同一状态。
        voice_manager = getattr(ctx, "voice_manager", None)
        if voice_manager is not None and sent_segments:
            try:
                voice_manager.enqueue_reply(
                    user_id, sent_segments, mood_snapshot=mood_snapshot)
            except Exception:  # noqa: BLE001 —— 语音轨故障不影响文字回复
                logger.exception("语音队列入队失败（文字回复不受影响）")
        # ---- 7b) 表情包独立图片消息（单条上限 1 张，SPEC §8.4）----
        if plan.image_path is not None:
            try:
                data = await asyncio.to_thread(plan.image_path.read_bytes)
                result = await bridge.send_image_b64(
                    user_id, base64.b64encode(data).decode("ascii"))
                if not result.ok:
                    logger.warning("表情包图片发送失败（%s）", result.status)
            except OSError as exc:
                logger.warning("表情包图片读取失败（%s）：%s", plan.image_path, exc)

    # ---- 8) history 写入（user+assistant 成对，双写镜像）----
    _archive(ctx, session_id, user_text, reply_text_out)

    # ---- 9) 记忆计数（user/assistant 各一次；达阈值后台总结入库）----
    if ctx.memory_service is not None:
        try:
            await ctx.memory_service.on_message(session_id, "user", user_text)
            await ctx.memory_service.on_message(session_id, "assistant", reply_text_out)
        except Exception:  # noqa: BLE001 —— 计数失败不阻塞消息循环
            logger.exception("记忆计数处理失败")

    # ---- 10) proactive 感知用户回复（unanswered 清零 + 解除启动武装 + ⑥序列号）----
    if ctx.proactive is not None:
        try:
            ctx.proactive.on_user_message(time.time())
        except Exception:  # noqa: BLE001
            logger.exception("proactive.on_user_message 失败")


def _archive(ctx: AppContext, session_id: str, user_text: str, assistant_text: str) -> None:
    """本轮收发写历史库并镜像；assistant 为空（LLM 失败）时只落 user 一条。

    写入失败只告警：历史库故障不阻塞对话主流程。
    """
    history = ctx.history
    if history is None:
        return
    try:
        if assistant_text:
            history.append_pair(session_id, user_text, assistant_text)
        else:
            history.append(session_id, "user", user_text)
    except Exception:  # noqa: BLE001
        logger.exception("历史写入失败（对话继续）")


# ---------------------------------------------------------------------------
# 调度贯通（EVENT_SCHEDULED 心跳驱动 proactive / diary）
# ---------------------------------------------------------------------------

def _wire_scheduler(ctx: AppContext) -> None:
    """订阅 EVENT_SCHEDULED：proactive.tick 逐分钟驱动；diary 触发点比对。

    PERF_SPEC §2-O4「调度长活后台化」：proactive/diary 的 LLM 生成
    create_task + 复入保护（proactive 复用自身 _generating + tick 任务守卫；
    diary 补 ctx._diary_running 复入门），tick 处理时长 p95 ≤1s。
    """

    async def _on_scheduled(event) -> None:  # noqa: ANN001
        payload = event.payload if isinstance(event.payload, dict) else {}
        now_ts = float(payload.get("now") or time.time())
        if ctx.proactive is not None:
            in_flight = getattr(ctx, "_proactive_tick_task", None)
            if in_flight is not None and not in_flight.done():
                logger.debug("[proactive] 上一 tick 仍在途（LLM 生成中），本次心跳跳过")
            else:
                ctx._proactive_tick_task = asyncio.create_task(
                    _proactive_tick(ctx, now_ts))
        # R9 纪念日里程碑：同一条心跳上做锚点建立与第 7/30/100 天判定
        #（开关 [proactive] anniversary，默认关；长活后台化 + 复入门同 proactive）
        if getattr(ctx.cfg.proactive, "anniversary", False):
            ann_in_flight = getattr(ctx, "_anniversary_tick_task", None)
            if ann_in_flight is not None and not ann_in_flight.done():
                logger.debug("[anniversary] 上一 tick 仍在途，本次心跳跳过")
            else:
                ctx._anniversary_tick_task = asyncio.create_task(
                    _anniversary_tick(ctx, now_ts))
        # R10 早晚安锚点：同一条心跳上的每日问候判定（开关 [routine].enabled；
        # 长活后台化 + 复入门同 proactive/anniversary）
        r_cfg = getattr(ctx.cfg, "routine", None)
        if r_cfg is not None and getattr(r_cfg, "enabled", False):
            routine_in_flight = getattr(ctx, "_routine_tick_task", None)
            if routine_in_flight is not None and not routine_in_flight.done():
                logger.debug("[routine] 上一 tick 仍在途，本次心跳跳过")
            else:
                ctx._routine_tick_task = asyncio.create_task(
                    _routine_tick(ctx, now_ts))
        await _diary_tick(ctx, now_ts)

    ctx.bus.on(EVENT_SCHEDULED, _on_scheduled)
    logger.info("调度已接线：EVENT_SCHEDULED → proactive.tick + routine 早晚安锚点"
                " + diary 触发点比对（LLM 长活后台化，复入保护）")


async def _proactive_tick(ctx: AppContext, now_ts: float) -> None:
    """proactive tick 后台执行体：LLM 生成不再阻塞心跳（O4 长活后台化）。"""
    try:
        sent = await ctx.proactive.tick(now_ts)
        if sent:
            logger.info("[proactive] 主动消息已发出（长度 %d）", len(sent))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 —— 单次 tick 失败不终止心跳
        logger.exception("[proactive] tick 失败")


async def _anniversary_tick(ctx: AppContext, now_ts: float) -> None:
    """纪念日里程碑 tick（R9，soul/anniversary.py）：锚点建立 + 里程碑判定发送。

    - 锚点：「认识第一天」= 会话库最早一条消息的 created_at，首次计算后原子
      落盘 <storage.data_dir>/anniversary.json，此后只读不再重算；会话库暂无
      消息（新安装）时安静等待，宁缺勿猜；
    - 里程碑：第 7/30/100 天（本机日历日差），每档只发一次（幂等落账）；
    - 发送经 ctx.proactive.generate_and_send(template_override=...)——安静
      时段/活跃避让/质量门/dual_send 语音双轨/历史写回全部与主动消息同源；
      安静时段内不点名也不落账，白天心跳自然重试；
    - ctx.proactive 缺席（proactive.enable=false）时没有发送通道，安静返回。
    """
    service = ctx.proactive
    if service is None:
        return
    try:
        state_path = (Path(ctx.repo_root) / ctx.cfg.storage.data_dir
                      / "anniversary.json")
        state = anniversary_mod.ensure_anchor(
            ctx.history, ctx.cfg.session_id, state_path)
        day = anniversary_mod.anniversary_due(state, now_ts)
        if day is None:
            return
        now_dt = datetime.fromtimestamp(now_ts)
        if _proactive_is_quiet(now_dt, service.quiet_start, service.quiet_end):
            return  # 安静时段不说话、不落账（同日稍后心跳重试）
        prompt = anniversary_mod.render_prompt(
            anniversary_mod.DEFAULT_PROMPT_TEMPLATE, day, now_dt)
        sent = await service.generate_and_send(
            now_ts=now_ts, now_dt=now_dt, template_override=prompt)
        if sent:
            anniversary_mod.mark_sent(state, day, state_path, now_ts=now_ts)
            logger.info("[anniversary] 纪念日消息已发出（认识第 %d 天）。", day)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 —— 单次 tick 失败不终止心跳
        logger.exception("[anniversary] tick 失败")


async def _routine_tick(ctx: AppContext, now_ts: float) -> None:
    """早晚安锚点 tick（R10，soul/routine.py）：到点经主动消息通道送早安/晚安。

    - routine.tick 判定当日锚点（hash(日期+键) 确定性抖动 + 当日幂等落账），
      返回 ["morning"]/["night"]（或同刻两者）；
    - 早安 prompt 注入一行天气实况（[tools].weather_city 配置了才有；
      get_weather_line 是阻塞网络请求 → asyncio.to_thread，失败自然省略）；
    - 发送走 generate_and_send(template_override=...)——安静时段/活跃避让/
      质量门/dual_send 语音双轨/表情包/历史写回全部与主动消息同源；
    - 放弃期（她已对他停联，gave_up_date 在位）连早晚安也停——不对失联的
      人继续定点问候；
    - ctx.proactive 缺席（proactive.enable=false）时没有发送通道，静默返回。
    """
    service = ctx.proactive
    if service is None:
        return
    r_cfg = getattr(ctx.cfg, "routine", None)
    if r_cfg is None or not getattr(r_cfg, "enabled", True):
        return
    if str(getattr(getattr(service, "state", None), "gave_up_date", "") or ""):
        return  # 放弃期：等他回来再恢复问候（避免对失联的人定点轰炸）
    try:
        now_dt = datetime.fromtimestamp(now_ts)
        state_dir = Path(ctx.repo_root) / ctx.cfg.storage.data_dir
        due = routine_mod.tick(now_dt, r_cfg, state_dir)
        for key in due:
            weather_line = None
            if key == "morning":
                tools_cfg = getattr(ctx.cfg, "tools", None)
                city = str(getattr(tools_cfg, "weather_city", "") or "").strip()
                if city:
                    try:
                        weather_line = await asyncio.to_thread(
                            weather_mod.get_weather_line,
                            city, now=now_ts, cache_dir=state_dir)
                    except Exception:  # noqa: BLE001 —— 天气失败自然省略
                        logger.exception("[routine] 天气实况获取失败（早安省略天气）")
                        weather_line = None
            if key == "morning":
                prompt = routine_mod.render_morning_prompt(now_dt, weather_line)
            else:
                prompt = routine_mod.render_night_prompt(now_dt)
            sent = await service.generate_and_send(
                now_ts=now_ts, now_dt=now_dt, template_override=prompt)
            if sent:
                logger.info("[routine] %s 问候已发出（长度 %d）。", key, len(sent))
            else:
                logger.info("[routine] %s 问候未送出（安静时段/生成失败等），当日不再补发。", key)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 —— 单次 tick 失败不终止心跳
        logger.exception("[routine] tick 失败")


async def _diary_tick(ctx: AppContext, now_ts: float) -> None:
    """日记触发点比对（SPEC §8.2）：到点后台写日记 → 存档 + 私聊发送。

    - 首个心跳排定下次触发：compute_next_diary_run + 0-20min 抖动；
    - 到点先重排明天再执行（写失败不重复触发）；当日已有存档则跳过
      （防重启后当天重写）；
    - 生成（LLM 长活）create_task 后台跑 + ctx._diary_running 复入门
      （PERF_SPEC §2-O4：tick 处理时长 p95 ≤1s）；
    - 服务未装配（模块缺失/enable 关）只静默跳过。
    """
    service = ctx.diary_service
    if service is None:
        return
    if ctx.diary_next_ts <= 0:
        target = diary_mod.compute_next_diary_run(
            datetime.fromtimestamp(now_ts), ctx.cfg.diary.diary_time)
        jitter = diary_mod.roll_jitter(ctx.diary_rng)
        ctx.diary_next_ts = target.timestamp() + jitter
        logger.info("[diary] 下次日记调度：%s（+抖动 %d 秒）",
                    target.isoformat(), jitter)
        return
    if now_ts < ctx.diary_next_ts:
        return
    ctx.diary_next_ts = 0.0  # 先重置，重排在下方（本函数下次心跳即重新排定）
    now_dt = datetime.fromtimestamp(now_ts)
    today = now_dt.date().isoformat()
    if getattr(ctx, "_diary_running", False):
        logger.warning("[diary] 上一次日记生成仍在途，跳过本次触发（复入门）")
        return
    ctx._diary_running = True
    task = asyncio.create_task(_diary_run(ctx, service, today, now_dt))
    task.add_done_callback(_log_task_exception)


def _log_task_exception(task: "asyncio.Task") -> None:
    """后台长活任务的兜底回调：异常只记日志，绝不冒泡打断心跳。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("后台调度任务异常：%s", exc)


async def _diary_run(ctx: AppContext, service: Any, today: str,
                     now_dt: datetime) -> None:
    """日记生成/发送执行体（后台任务，O4 长活后台化）。"""
    try:
        if await service.read_diary(today) is not None:
            logger.info("[diary] %s 已有日记存档，跳过重写", today)
            return
        diary_text = await service.write_today(today, now=now_dt)
        if not diary_text:
            return
        # 旧栈行为：日记存档后私聊发送（plugins/vl_diary main.py「存档 + 私聊发送」）；
        # O4：日记文字入 per-user 发送 FIFO 统一收编（管线缺席回退内联发送）
        user_id = _session_user_id(ctx.cfg)
        if user_id:
            pipeline = getattr(ctx, "message_pipeline", None)
            if pipeline is not None and hasattr(pipeline, "enqueue_text_send"):
                pipeline.enqueue_text_send(
                    user_id, [diary_text], ctx.cfg.reply.segmented)
            else:
                await _send_texts(ctx, user_id, [diary_text],
                                  ctx.cfg.reply.segmented)
        # 日记是她的自言自语：入历史（assistant 单条，不计记忆数）
        if ctx.history is not None:
            try:
                ctx.history.append(ctx.cfg.session_id, "assistant", diary_text)
            except Exception:  # noqa: BLE001
                logger.exception("[diary] 日记写入历史失败")
        logger.info("[diary] %s 的日记已生成并发送", today)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 —— 单日日记失败不影响主循环
        logger.exception("[diary] 日记生成/发送失败")
    finally:
        ctx._diary_running = False


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def _amain(ctx: AppContext, smoke: bool = False) -> int:
    """事件循环主体：启动桥监听 → 守护运行；返回进程退出码。

    - 端口占用（OSError）：中文报错退出 1（SPEC §13：禁静默）；
    - smoke：桥监听就绪即优雅退出（供 test_smoke 与部署就绪探测）。
    """
    ticker = asyncio.create_task(
        tick_forever(ctx.bus, interval_seconds=60.0, stop=ctx.stop))
    serve_task: "asyncio.Task | None" = None
    exit_code = 0
    bridge = ctx.bridge
    try:
        if bridge is not None:
            await bridge.start()  # 绑定失败（端口占用等）在此抛 OSError
            if smoke:
                print(f"[smoke] OneBot 桥已监听 ws://{bridge.host}:"
                      f"{bridge.bound_port}{bridge.path}")
            serve_task = asyncio.create_task(bridge.serve_forever())
        elif smoke:
            print("[smoke] OneBot 桥未装配（跳过监听检查）")
        if not smoke:
            # §1 生命周期后台任务（adopt-first + 30s 探测）在真实运行时启动；
            # --smoke 与测试绝不拉起 GSVI 子进程。
            voice_engine = getattr(ctx, "voice_engine", None)
            if voice_engine is not None:
                voice_engine.start_background()
        ready = [t for t in _SETUP_TARGETS if _module_loaded(t)]
        logger.info("装配完成：%d/%d 模块就绪（其余告警跳过）", len(ready), len(_SETUP_TARGETS))
        if smoke:
            logger.info("--smoke 自检通过：装配与监听就绪，优雅退出")
            ctx.stop.set()
        else:
            await ctx.stop.wait()
    except OSError as exc:
        logger.error("启动失败（端口占用或网络不可绑定）：%s", exc)
        exit_code = 1
    finally:
        ctx.stop.set()
        ticker.cancel()
        if serve_task is not None:
            serve_task.cancel()
        for task in (ticker, serve_task):
            if task is None:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 —— 收尾从宽
                pass
        if bridge is not None:
            # 主协程内统一收尾桥（serve_task 被取消时其 finally 里的 await 会被
            # 再次取消打断，close 可能半途而废；此处幂等补一次完整关闭）
            try:
                await bridge.stop()
            except Exception:  # noqa: BLE001 —— 收尾从宽
                pass
        voice_manager = getattr(ctx, "voice_manager", None)
        if voice_manager is not None:
            # 语音管线收尾（事件循环存活期内取消后台 worker，避免悬空任务）
            try:
                await voice_manager.aclose()
            except Exception:  # noqa: BLE001 —— 收尾从宽
                logger.exception("语音管线收尾失败")
        message_pipeline = getattr(ctx, "message_pipeline", None)
        if message_pipeline is not None:
            # O3/O4 管线收尾（在途轮/发送 worker/到达通道随循环取消，避免
            # "Task was destroyed but it is pending" 悬空告警）
            try:
                await message_pipeline.aclose()
            except Exception:  # noqa: BLE001 —— 收尾从宽
                logger.exception("对话管线收尾失败")
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    smoke = "--smoke" in args  # 装配自检：装配+监听就绪即优雅退出（就绪探测用）
    root = Path(os.environ.get("VL_ROOT") or REPO_ROOT)  # 冒烟/测试可指向 tmp
    print(f"Virtual-lover v{APP_VERSION} 启动（运行根 {root}）")
    # 全新安装初始化（O12）：不 load_config（全新目录尚无 vl.toml，load 会报错退出 2）
    if "--init-config" in args:
        from virtuallover.tools.init_config import run_init
        return run_init(root, force="--force-init" in args)
    # 配置先行：失败直接给中文报错退出（启动校验属启动路径，日志器未必要存在）
    try:
        cfg = load_config(root / "local" / "config" / "vl.toml")
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg, root)
    logger.info("vl.toml 加载成功（session_id 模板长度 %d 字符，schema v%d）",
                len(cfg.session_id), cfg.schema_version)
    token = ensure_bridge_token(cfg, root / "local" / "config" / "vl.toml")
    logger.info("桥 token 就绪（长度 %d）", len(token))

    ctx = AppContext(cfg=cfg, bus=EventBus(), repo_root=root)
    # 全局限流器（发送面共用：10 条/30s stall，参数从旧配置迁移）
    from virtuallover.brain.sender import RateLimiter
    ctx.rate_limiter = RateLimiter(
        max_count=cfg.reply.rate_limit.max_count,
        window_seconds=cfg.reply.rate_limit.window_seconds,
    )
    ctx.cleanups.extend(_import_setups(ctx))
    _wire_pipeline(ctx)
    _wire_scheduler(ctx)
    try:
        exit_code = asyncio.run(_amain(ctx, smoke=smoke))
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，优雅退出")
        exit_code = 0
    finally:
        for cleanup in reversed(ctx.cleanups):
            try:
                cleanup()
            except Exception:  # noqa: BLE001 —— 清理失败不阻断其余清理
                logger.exception("清理钩子失败：%r", cleanup)
    return exit_code


def _module_loaded(target: str) -> bool:
    """装配后统计用：模块是否成功 import（sys.modules 判定）。"""
    return target in sys.modules


if __name__ == "__main__":
    sys.exit(main())
