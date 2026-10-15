# -*- coding: utf-8 -*-
"""brain/pipeline.py —— O3「立即起跑 + 打断地平线」+ O4「per-user 双队列解耦」。

PERF_SPEC §2-O3/§2-O4 + §7.1-2/§7.2 的实现本体；characterization 契约见
virtuallover/tests/_o34_contract.py（接口形态：模块级管线类
``MessagePipeline(ctx, *, now_fn=None)`` 与 ``setup(ctx)`` 装配双形态，
实例挂 ``ctx.message_pipeline``）。

架构总览（一条消息的生命周期）::

    NapCat → 桥（帧路由，零业务 await）→ EVENT_MESSAGE
      → MessagePipeline.on_message（同步打到达戳 + 入队，绝不 await 业务）
      → per-session 串行通道（保序：history 写入 / memory on_message 到达序）
          ├ 命令路由逐条（旁路直答，不进 LLM 轮、不写 assistant 行）
          ├ mood.handle_message 逐条（危机兜底在引擎内）
          ├ proactive.on_user_message 逐条（_msg_seq 递增）
          ├ history user 行落库（到达时刻，不回滚，§7.1-2）
          └ memory user 计数（逐条）
      → per-user 打断地平线状态机（IDLE/RUNNING/DRIP）
          ├ IDLE   → 立即起跑第 gen 代 LLM 轮（零窗口税）
          ├ RUNNING（首段未发出）+ 窗口内新消息 → generation-id 丢弃当前轮
          │         （结果与 assistant 行不落库，仅计费）→ 合并上下文立即重跑
          ├ DRIP（首段已实际发出）→ 不可打断：已生成段必须发完（允许压缩段间隔
          │         收尾），随后新一轮回应最新
          └ 距上条 >2min（冷启动）→ 不启用打断期（发完再新一轮）
      → per-user 发送 FIFO（统一收编主回复段/meme 图/proactive 文字/diary 文字）
          ├ 共享全局 RateLimiter（ctx.rate_limiter 单例）
          ├ 发送失败短路本回复剩余段
          ├ 被弃轮（gen 过期）未发段一律清场，不再出帧
          └ 首段实际发出（出帧，入 FIFO 不算）= DRIP 起点（§7.2）
      → 首段文字实际出帧后语音即并行入队合成（R3-2c：不等全文发完；
        发送失败短路时排队中的语音作业由 voice_manager.drop_queued 收走）

EventBus.emit 语义不动（events.py 零改动）；O3+O4 无配置回退键
（interrupt_window_seconds 除外，§7.2），回退 = revert 整个提交。

七模块接线（本文件当前还承载主聊天管线的增值后处理，全部降级安全）::

    情绪自评   system 尾部一行标签约定 → 回复后 parse_mood_tag 解析 →
               EVENT_MOOD_LLM 推进心情 → 出站文本 strip_mood_tags 兜底
    故事模式   触发词 activate 开窗 → 窗口有效时 escape_block 随 system →
               回复完成 consume_turn（/story 命令侧管状态，管线只读+消费）
    自拍       memes 之后跑 selfies.process_reply（本轮已发 meme 图则跳过），
               照片走 job 的独立图片消息通道（send_image_b64）
    QQ 表情    system 工具箱给 faces_cheatsheet → 发送前剥 {{face:id}} 标记，
               有标记的段落改走 segments 数组（[text 段, face 段…]）
    打字速度   sample_gap / sample_first_gap 全部传下一段长度（typing_cps 节奏）
    引用回复   _SendJob.reply_to_message_id，克制策略：群聊 / 引用目标距最新
               消息 >3 条 / 随机 25%——三者其一才把 reply 段前置进首段
    天气       [tools].weather_city 已配且命中天气话题 → wttr.in 资料块
               （与 websearch 资料块同位同风格）
    纪念日     认识天数一行 + 她生日一行注入 system；消息里程碑（mood 计数
               恰过 100/500/1000/5000）经 proactive template_override 庆祝
               （vl_data/message_milestones.json 当日幂等）
    群聊路由   桥 _vl_group_id 标记 → _Arrival/_SendJob 透传 group_id，群消息
               回复走 send_group_msg 群发（首段前置 reply+@段），私聊零变化

时钟契约：``now_fn``（缺省取 ctx.now_fn，再缺省 time.time）是秒级时钟，
驱动打断窗口/冷启动判定与 user 行 created_at（量纲责任在调用方，
history 存储层默认毫秒仅适用于未显式传时刻的路径）；拟人抖动与
[perf] e2e 打点用真实 time.monotonic 基准。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional

from virtuallover.brain import persona, sender, stt as stt_mod, vision as vision_mod, websearch as websearch_mod
from virtuallover.brain.llm import content_to_text
from virtuallover.brain.sender import MIN_SEGMENT_GAP_SECONDS
from virtuallover.bridge import faces as faces_mod
from virtuallover.soul import anniversary as anniversary_mod
from virtuallover.soul import memes as memes_mod
from virtuallover.soul import mood as mood_mod
from virtuallover.soul import schedule as schedule_mod
from virtuallover.soul import selfies as selfies_mod
from virtuallover.soul import story as story_mod
from virtuallover.soul import textclean as textclean_mod
from virtuallover.soul import weather as weather_mod

logger = logging.getLogger("virtuallover.brain.pipeline")

# ---------------------------------------------------------------------------
# 常量（PERF_SPEC §2-O3 / §7.2）
# ---------------------------------------------------------------------------

#: 冷启动阈值：距上条消息超过该秒数视为冷启动，不启用打断期（§0 终裁行 1）
COLD_START_SECONDS = 120.0
#: reply 回查超时（≤10s，§2-O4；桥 get_msg 自身另有 action 超时，此处双保险）
REPLY_LOOKUP_TIMEOUT_SECONDS = 10.5
#: 打断窗口合法区间（§7.2：[chat] interrupt_window_seconds，0=关）
_INTERRUPT_WINDOW_MIN, _INTERRUPT_WINDOW_MAX = 0.0, 4.0
#: 打断窗口缺省值（配置缺字段时的兜底，对齐 config.ChatProviderConfig 默认）
_DEFAULT_INTERRUPT_WINDOW = 1.5

#: created_at 落库量纲：毫秒（§7.2「埋点改毫秒」——history 缺省 clock 即毫秒，
#: 管线时钟是秒，落库前统一换算，同表 user/assistant 行同一量纲可比）
_TIMESTAMP_SCALE_MS = 1000

# ---------------------------------------------------------------------------
# 边界常量（五轮修缮 R2：队列有界 / 命令去重 / 历史读取限幅）
# ---------------------------------------------------------------------------

#: per-user 发送 FIFO 上限（R2：满丢最旧+告警，防 proactive/diary 洪水撑爆内存）
SEND_QUEUE_MAX_ITEMS = 50
#: 命令去重窗口：同一用户同一命令 3 秒内重复到达只处理一次（协议端重发/手抖双击）
COMMAND_DEDUP_WINDOW_SECONDS = 3.0
#: 历史读取反推 limit 的口径：实测 158 条 ≈ 6201 token（约 39 token/条），
#: 取 32 保守多读 ~20% 余量；预算 26869 token → ~839 条，硬上限 2000 条
HISTORY_EST_TOKENS_PER_MESSAGE = 32
HISTORY_READ_LIMIT_MIN = 64
HISTORY_READ_LIMIT_MAX = 2000


def _ts_ms(seconds: float) -> int:
    """管线时钟秒值 → 毫秒整数（created_at 落库量纲，§7.2）。"""
    return int(round(float(seconds) * _TIMESTAMP_SCALE_MS))

_MEME_DIR_DEFAULT = "local/shared_plugin_data/meme_manager/memes"

# 记忆检索查询并入的最近历史条数（PERF_SPEC §3-O5 兜底补丁，与 __main__ 同口径）
_RECALL_HISTORY_MESSAGES = 2

# ---------------------------------------------------------------------------
# 七模块接线参数（情绪自评/故事/自拍/QQ 表情/引用回复/天气/纪念日里程碑）
# ---------------------------------------------------------------------------

#: 引用回复的随机克制档：无群聊、引用目标也不远时，仍以该概率引用最新一条
REPLY_QUOTE_PROBABILITY = 0.25
#: 引用回复的距离档：引用目标之后的消息超过该条数才值得前置 reply 段
QUOTE_DISTANCE_THRESHOLD = 3
#: 引用距离回看的历史行数上限（有界读取，防全表扫描）
QUOTE_LOOKBACK_ROWS = 200
#: 消息里程碑的当日幂等状态文件（<storage.data_dir> 下，proactive_state.json 同目录）
MILESTONE_STATE_FILENAME = "message_milestones.json"

# ---------------------------------------------------------------------------
# 定界符中和（O3⑥：合并文本拼接时防注入冒充，port-of-own 全角替换手法）
# ---------------------------------------------------------------------------

# (原样定界符, 中和形态)。四个受控定界符（§2-O3）逐一给出；成对闭合形态一并
# 中和。手法对齐 memory.service.neutralize_template_placeholders（半角→全角，
# 单遍替换，不可逆）：定界符不再成立，内容原样保留可辨认。
_DELIMITER_NEUTRALIZERS: "tuple[tuple[str, str], ...]" = (
    ("<资料引用开始>", "＜资料引用开始＞"),
    ("<资料引用结束>", "＜资料引用结束＞"),
    ("<Mnemosyne>", "＜Mnemosyne＞"),
    ("</Mnemosyne>", "＜/Mnemosyne＞"),
    ("<system_reminder>", "＜system_reminder＞"),
    ("</system_reminder>", "＜/system_reminder＞"),
    ("【当前内心状态", "〖当前内心状态"),
)


def neutralize_delimiters(text: str) -> str:
    """把消息原文里的四个注入定界符替换为全角形态（O3⑥）。

    - ``<资料引用开始/结束>``、``<Mnemosyne>``、``<system_reminder>``、
      ``【当前内心状态`` 的原样形态不再成立（不可冒充注入框架）；
    - 内容保留可辨认；无辜文本零改动（只做精确子串替换）。
    """
    out = str(text or "")
    for raw, safe in _DELIMITER_NEUTRALIZERS:
        if raw in out:
            out = out.replace(raw, safe)
    return out


def _load_json_file(path: "str | Path") -> dict:
    """读小状态 JSON；缺失/损坏返回空 dict（anniversary.load_state 同款容错）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_file(path: "str | Path", state: dict) -> None:
    """小状态 JSON 原子落盘（tmp + os.replace）；OSError 只告警不抛。"""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("管线状态文件写入失败（%s）：%s", Path(path).name, exc)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class _Arrival:
    """一条消息的到达态（on_message 同步打戳后进 per-session 串行通道）。

    arrived_ts 用管线时钟（可注入 FakeClock）——打断窗口/冷启动判定与
    user 行 created_at 都取它，保证「到达时刻」语义与测试确定性；
    arrived_mono 用真实 monotonic——拟人抖动与 [perf] e2e 打点基准；
    group_id 非空表示群聊上下文（桥 _handle_group_event 的 _vl_group_id
    标记），回复改走 send_group_msg 群发（空串=私聊，行为零变化）。
    """

    event: dict
    user_id: str
    text: str
    message_id: str
    arrived_ts: float
    arrived_mono: float
    group_id: str = ""


@dataclass
class _PendingMsg:
    """进入 LLM 轮的一条待答消息（round_msgs / pending 的元素）。"""

    text: str
    reply_id: Any = None
    reply_text: "str | None" = None
    message_id: str = "-"
    arrived_ts: float = 0.0
    arrived_mono: float = 0.0
    row_id: "int | None" = None   # 到达时刻已落库的 user 行 id（装配时按 id 排除，防重复注入上下文）
    group_id: str = ""            # 群聊上下文标记（非空=群消息，回复走群发）

    @property
    def display_text(self) -> str:
        """进 LLM 轮的用户文本：引用回查文本并入（对齐 __main__ 现行为）。"""
        if self.reply_text:
            return f"[引用消息] {self.reply_text}\n{self.text}"
        return self.text


@dataclass
class _SendJob:
    """per-user 发送 FIFO 的一个作业（一段回复 = 文字段序列 [+ meme 图]）。

    gen：所属 LLM 轮的 generation 代号；-1 = 非轮内发送（命令直答/proactive/
    diary 文字），永不因打断丢弃、不参与状态机迁移、不写 assistant 行。
    on_done / on_fail：发送收尾回调（成功收尾 / 失败短路收尾）——assistant
    行落库、记忆计数、语音入队（仅成功且完整）、状态机收尾都在这里。
    """

    user_id: str
    segments: List[str]
    params: Any                       # segmented 参数（段间隔采样）
    image_b64: "str | None" = None    # meme 图（base64，跟在文字段后）
    first_send_at: "float | None" = None  # 首段最早可发时刻（monotonic 基准；None=不等待）
    gen: int = -1
    message_id: str = "-"             # §7.1-6 首段日志关联
    arrived_mono: "float | None" = None   # e2e 计算（None 则 e2e 打 "-"）
    on_done: "Callable[[List[str]], Awaitable[None]] | None" = None
    on_fail: "Callable[[List[str]], Awaitable[None]] | None" = None
    # R3-2c 语音并行：首段文字实际出帧后调用一次（入参=完整计划段列表）——
    # 语音合成从此刻起跑，不等全文发完
    on_first_sent: "Callable[[List[str]], Awaitable[None]] | None" = None
    discarded: bool = False          # 被弃代标记（sender 见之即跳过）
    # ---- 七模块接线（群聊路由 / 引用回复 / QQ 表情）----
    group_id: str = ""               # 非空=群聊回复（首段前置 reply+@段，走 send_group_msg）
    reply_to_message_id: str = ""    # 首段前置 reply 段的目标消息 id（空=不引用）
    face_ids: "List[List[int]] | None" = None  # 与 segments 对齐的 QQ 表情 id（None=无）


class _UserRuntime:
    """单个 user 的双队列运行态（管线队列 + 发送 FIFO + 状态机字段）。

    懒创建（对齐 voice/manager.py 的 per-user dict 模式）：首个消息到达时
    建 runtime 并启动发送 worker；状态迁移全部持 ``lock``（临界区内无
    业务 await，只做字段读写与 create_task）。
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.lock = asyncio.Lock()
        # R2 队列有界：满 50 丢最旧（_enqueue_send_job 收口），防消息洪水撑爆内存
        self.send_queue: "asyncio.Queue[_SendJob]" = asyncio.Queue(
            maxsize=SEND_QUEUE_MAX_ITEMS)
        self.sender_task: "asyncio.Task | None" = None
        # ---- 打断地平线状态机（O3）----
        self.state: str = "IDLE"          # IDLE | RUNNING | DRIP
        self.gen: int = 0                 # 当前代（generation-id 守卫锚点）
        self.round_task: "asyncio.Task | None" = None
        self.round_msgs: List[_PendingMsg] = []   # 当前代轮的消息集（打断时追加合并）
        self.pending: List[_PendingMsg] = []      # 超窗/冷启动/DRIP 期间到达：发完再新一轮
        self.last_arrival_ts: "float | None" = None  # 上一条消息到达时刻（管线时钟）


# ---------------------------------------------------------------------------
# 管线本体
# ---------------------------------------------------------------------------


class MessagePipeline:
    """对话主管线（O3+O4）：到达语义串行通道 + per-user 打断地平线 + 发送 FIFO。"""

    def __init__(self, ctx: Any, *, now_fn: "Callable[[], float] | None" = None,
                 stt_client: Any = None) -> None:
        self.ctx = ctx
        clock = now_fn or getattr(ctx, "now_fn", None)
        self._now: Callable[[], float] = clock or time.time
        self._rng = random.Random()
        #: STT 客户端（多模态语音转文字）：缺省按 [chat] stt_* 配置构造，
        #: 未配置 provider 时 enabled=False、语音走 [语音消息] 占位降级，
        #: 零行为变化；测试可注入替身（构造参 stt_client）。
        self._stt = stt_client if stt_client is not None \
            else stt_mod.build_stt_client(getattr(ctx.cfg, "chat", None))
        #: 打断丢弃轮计数（§2-O3「superseded 计数日志」的观测面；R2 控制台 chips 消费）
        self.superseded_count: int = 0
        #: LLM 调用最终失败计数（R2 控制台 chips 消费：重试与 fallback 全耗尽才 +1）
        self.llm_fail_count: int = 0
        #: 发送队列满丢弃作业计数（R2 队列有界的观测面）
        self.send_dropped_count: int = 0
        self._users: "Dict[str, _UserRuntime]" = {}
        self._channels: "Dict[str, _ArrivalChannel]" = {}
        #: 命令去重表（(user_id, text) → 上次到达时刻；3s 窗口内重复只处理一次）
        self._command_seen: "Dict[tuple, float]" = {}
        self._closed = False
        #: 主事件循环（惰性绑定：首个在 loop 线程的调用面绑住）。Router 降级
        #: 提示（brain.llm._brain_notice_sink）从 asyncio.to_thread 工作线程
        #: 调 enqueue_text_send——工作线程没有 running loop，worker 的
        #: ensure_future 必须投回主 loop（实测 RuntimeError 复现后修）。
        self._loop: "asyncio.AbstractEventLoop | None" = None

    # ------------------------------------------------------------------
    # 观测面（tests/_o34_contract.py 契约）
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """聚合状态机观测面：任一 user RUNNING → RUNNING；任一 DRIP → DRIP。"""
        states = [u.state for u in self._users.values()]
        if "RUNNING" in states:
            return "RUNNING"
        if "DRIP" in states:
            return "DRIP"
        return "IDLE"

    @property
    def interrupt_window(self) -> float:
        """打断期窗口秒数（[chat] interrupt_window_seconds，clamp [0,4]，0=关）。"""
        chat_cfg = getattr(self.ctx.cfg, "chat", None)
        raw = getattr(chat_cfg, "interrupt_window_seconds", _DEFAULT_INTERRUPT_WINDOW)
        try:
            window = float(raw)
        except (TypeError, ValueError):
            window = _DEFAULT_INTERRUPT_WINDOW
        return min(max(window, _INTERRUPT_WINDOW_MIN), _INTERRUPT_WINDOW_MAX)

    # ------------------------------------------------------------------
    # 入口：桥事件 → per-session 串行通道（绝不 await 业务，O4 帧路由只投递）
    # ------------------------------------------------------------------

    async def on_message(self, event: Any) -> None:
        """EVENT_MESSAGE 处理器：校验 + 同步打到达戳 + 入队，立即返回。

        双形态兼容：bus 订阅形态收 ``events.Event``（取 .payload），direct
        直驱形态收裸桥事件 dict。到达时刻在本方法内同步取样（而非通道
        worker 出队时）——「user 行在到达时刻落库」「打断窗口按到达间隔
        判定」的时点都是消息真实到达时刻，连发场景下出队前的时间推进不得
        污染判定。
        """
        if self._closed:
            return
        payload = getattr(event, "payload", event)   # events.Event → 桥事件 dict
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return
        user_id = str(payload.get("user_id") or "")
        text = str(payload.get("text") or "").strip()
        if not user_id or not text:
            logger.debug("空文本/缺 user_id 的消息，跳过管线")
            return
        session = str(self.ctx.cfg.session_id or "")
        item = _Arrival(
            event=payload,
            user_id=user_id,
            text=text,
            message_id=str(payload.get("message_id") or "-"),
            group_id=self._extract_group_id(payload),
            arrived_ts=self._now(),            # 管线时钟（秒）
            arrived_mono=time.monotonic(),     # 抖动/e2e 基准
        )
        channel = self._channels.get(session)
        if channel is None:
            channel = _ArrivalChannel(self, session)
            self._channels[session] = channel
        channel.submit(item)

    def _binding_loop(self) -> "asyncio.AbstractEventLoop | None":
        """惰性绑定并返回当前 running loop；非 loop 线程返回 None（不炸）。"""
        if self._loop is not None and self._loop.is_closed():
            self._loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._loop is None:
            self._loop = loop
        return loop

    def _spawn_task(self, coro: "Coroutine") -> None:
        """把协程投给主 loop：loop 线程直接 create_task；工作线程经
        run_coroutine_threadsafe；主 loop 尚未见过面/已闭合则放弃（协程
        显式关闭防「never awaited」告警，调用面自带降级语义）。"""
        loop = self._binding_loop()
        if loop is not None:
            loop.create_task(coro)
            return
        bound = self._loop
        if bound is not None:
            try:
                asyncio.run_coroutine_threadsafe(coro, bound)
                return
            except RuntimeError:
                pass  # loop 已闭合（关机竞态）
        coro.close()
        logger.warning("[pipeline] 主事件循环不可用，协程投递放弃（%s）",
                       getattr(coro, "__qualname__", "?"))

    def _user(self, user_id: str) -> _UserRuntime:
        """取 per-user 运行态，懒创建并启动发送 worker（voice/manager 手法）。

        线程语义（R2 集成修复）：Router 降级提示从 to_thread 工作线程进来，
        此时没有 running loop——worker 的启动经 call_soon_threadsafe 投回主
        loop（绑定 loop 缺失时 st 照建：作业先排队，worker 由首个 loop 线程
        调用补启）。`_users` 的懒创建竞态最坏双建一个 st（dict 原子覆盖），
        与修前一致不恶化。
        """
        st = self._users.get(user_id)
        if st is None:
            st = _UserRuntime(user_id)
            self._users[user_id] = st
            loop = self._binding_loop()
            if loop is not None:
                st.sender_task = asyncio.ensure_future(self._sender_worker(st))
            else:
                self._start_sender_threadsafe(st)
        return st

    def _start_sender_threadsafe(self, st: "_UserRuntime") -> None:
        """跨线程启动 sender worker（幂等）：call_soon_threadsafe 投回主 loop。"""
        def _start() -> None:
            if st.sender_task is None or st.sender_task.done():
                st.sender_task = asyncio.ensure_future(self._sender_worker(st))
        bound = self._loop
        if bound is None:
            # 主 loop 还没见过面（纯后台进程？）：worker 欠着，作业先排队，
            # 首个 loop 线程调用（任一 _user/chat 链）会经 _binding_loop 补启
            logger.warning("[pipeline] 主事件循环未绑定，sender worker 稍后补启"
                           "（user=%s）", st.user_id[:2] + "***")
            return
        try:
            bound.call_soon_threadsafe(_start)
        except RuntimeError:
            logger.warning("[pipeline] 主事件循环已闭合，sender worker 未启动"
                           "（user=%s）", st.user_id[:2] + "***")

    # ------------------------------------------------------------------
    # 命令去重 / 发送队列有界（R2）
    # ------------------------------------------------------------------

    def _is_dup_command(self, user_id: str, text: str, arrived_ts: float) -> bool:
        """3s 窗口内同一 (user, 命令) 的第二次到达返回 True（首次登记时刻）。

        剪枝：每次判定顺手清掉窗口外的旧键，防长尾命令把表撑大。
        """
        key = (user_id, text)
        horizon = arrived_ts - COMMAND_DEDUP_WINDOW_SECONDS
        if len(self._command_seen) > 256:
            self._command_seen = {k: t for k, t in self._command_seen.items() if t > horizon}
        last = self._command_seen.get(key)
        if last is not None and last > horizon:
            return True
        self._command_seen[key] = arrived_ts
        return False

    def _enqueue_send_job(self, st: _UserRuntime, job: _SendJob) -> None:
        """发送作业入队（有界）：队列满时丢最旧 + 告警 + 计数（R2）。

        - 被丢作业若带 on_fail（gen>=0 的轮内作业），按「零段送达失败」收尾
          ——on_fail([]) 会 _finish_round 复位状态机（防状态卡死 RUNNING），
          且 R4 幽灵回复契约保证空段不写 assistant 行；
        - 丢最旧而非拒新：新消息永远优先被处理（旧 proactive 提醒过期无妨）。
        """
        queue = st.send_queue
        if queue.full():
            try:
                dropped = queue.get_nowait()
            except asyncio.QueueEmpty:  # 理论不可达（full 与 get 间无让出点），防御
                dropped = None
            if dropped is not None:
                self.send_dropped_count += 1
                logger.warning(
                    "[pipeline] 发送队列已满（%d/%d），丢弃最旧作业（user=%s，%d 段，"
                    "gen=%d，累计丢弃 %d）",
                    queue.qsize(), SEND_QUEUE_MAX_ITEMS, st.user_id[:2] + "***",
                    len(dropped.segments), dropped.gen, self.send_dropped_count)
                if dropped.on_fail is not None:
                    # 收尾回调投给主 loop（本方法可能从工作线程被调——Router
                    # 降级提示同款线程语义；_spawn_task 内 loop 线程走 create_task）
                    self._spawn_task(self._fail_dropped_job(dropped))
        queue.put_nowait(job)

    async def _fail_dropped_job(self, job: _SendJob) -> None:
        """被丢作业的失败收尾：on_fail([])（零段送达语义），异常只记日志。"""
        try:
            if job.on_fail is not None:
                await job.on_fail([])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 被丢作业的收尾失败不影响队列
            logger.exception("[pipeline] 被丢发送作业的收尾回调失败")

    # ------------------------------------------------------------------
    # 到达语义（per-session 串行通道内逐条执行，O3⑤「合并前逐条执行」）
    # ------------------------------------------------------------------

    async def _process_arrival(self, item: _Arrival) -> None:
        """一条消息的逐条语义（顺序固化）：语音转写/图片识别/命令/mood/
        proactive/user 行/计数，然后按命令旁路或打断地平线状态机分派。"""
        cfg = self.ctx.cfg
        session = str(cfg.session_id or "")
        user_id, text = item.user_id, item.text
        st = self._user(user_id)

        # ---- 0) 图片识别（多模态①，brain/vision.py）：带图消息把「[图片]」
        #          占位升级为视觉描述，后续命令/mood/history/LLM 全部消费
        #          enrich 后的文本。supports_vision=false（默认）时占位替换
        #          是恒等操作、零网络调用，行为与旧版完全一致。
        image_urls = self._extract_image_urls(item.event)
        if image_urls:
            text = await self._vision_enrich(text, image_urls)
            item.text = text   # 同步回写到达态（本轮内所有消费者同源）

        # ---- 0b) 语音转写（多模态②，brain/stt.py）：语音消息把「[语音]」
        #          占位升级为转写文字（get_record 取音频 → OpenAI 兼容
        #          /audio/transcriptions 转写）；stt_provider 未配置/失败时
        #          降级「[语音消息]」占位，零第三方依赖、绝不阻断消息循环。
        if stt_mod.PLACEHOLDER_RECORD in text:
            text = await self._resolve_voice_text(item)
            item.text = text   # 同步回写到达态（本轮内所有消费者同源）

        # ---- 1) 命令去重（R2）：同一用户同一命令 3s 窗内重复到达只处理一次
        #          （协议端重发/手抖双击会触发两次 /diary、两次 LLM 直答计费）。
        #          只对「/」前缀文本生效；普通消息绝不去重（连发同句是正常语义）。
        if text.startswith("/") and self._is_dup_command(user_id, text, item.arrived_ts):
            logger.info("[pipeline] 命令去重：user=%s 在 %.0fs 窗内重复命令 %r，本次忽略",
                        user_id[:2] + "***", COMMAND_DEDUP_WINDOW_SECONDS, text[:16])
            return

        # ---- 1) 命令路由逐条（旁路直答判定；未知 / 命令返回 None 静默忽略）----
        replies: "List[str] | None" = None
        router = getattr(self.ctx, "router", None)
        if router is not None:
            try:
                replies = await router.handle(
                    text, user_id, admins=cfg.admins, whitelist=cfg.whitelist)
            except Exception:  # noqa: BLE001 —— 路由故障按非命令继续，不吞消息
                logger.exception("命令路由处理失败（按非命令继续）")
                replies = None

        # ---- 2) mood 更新逐条（危机兜底/命令跳过在引擎内部）----
        mood_engine = getattr(self.ctx, "mood_engine", None)
        new_mood_state = None
        if mood_engine is not None:
            try:
                new_mood_state = await mood_engine.handle_message(user_id, text)
            except Exception:  # noqa: BLE001 —— 情绪推进失败不阻塞对话
                logger.exception("情绪引擎处理失败（对话继续）")

        # ---- 2b) 消息里程碑（接线：total 恰为 100/500/1000/5000 → 主动庆祝，
        #          vl_data 小状态文件当日幂等；独立 task 不阻塞到达通道）----
        if new_mood_state is not None:
            self._maybe_fire_message_milestone(new_mood_state, item.arrived_ts)

        # ---- 3) proactive 感知逐条（unanswered 清零 + ⑥_msg_seq 递增）----
        proactive = getattr(self.ctx, "proactive", None)
        if proactive is not None:
            try:
                proactive.on_user_message(item.arrived_ts)
            except Exception:  # noqa: BLE001
                logger.exception("proactive.on_user_message 失败")

        # ---- 4) history user 行（到达时刻落库，不回滚，§7.1-2）----
        # created_at 传管线时钟秒值×1000 的毫秒精度（§7.2「埋点改毫秒」：秒级
        # 取整会把 0.5s 连发间隔坍缩成同秒；与 history 缺省 clock 的毫秒量纲
        # 同一单位，user/assistant 行同表可比）；行序真相在自增 id（单调），
        # created_at 只用于对账与观测。
        row_id: "int | None" = None
        history = getattr(self.ctx, "history", None)
        if history is not None:
            try:
                # user 行 meta 带 message_id：引用回复的距离判定（接线 6）靠它
                # 把 reply_id 定位到历史行（meta 本就是调用方附加列，R3-1h 同款）
                row_id = history.append(
                    session, "user", text, created_at=_ts_ms(item.arrived_ts),
                    meta={"message_id": item.message_id}
                    if item.message_id != "-" else None)
            except Exception:  # noqa: BLE001 —— 历史库故障不阻塞对话
                logger.exception("历史写入失败（对话继续）")

        # ---- 5) 命令旁路直答：发送 FIFO（无首段抖动）→ 不进 LLM 轮、
        #         不写 assistant 历史行（O3⑤ characterization 钉死语义）----
        if replies is not None:
            if replies:
                self._enqueue_text_job(
                    st, replies, cfg.reply.segmented,
                    message_id=item.message_id, arrived_mono=item.arrived_mono)
            return

        # ---- 6) memory user 计数逐条（命令不计数=现行为；user 侧在到达时
        #         计数，被打断轮也不丢——§7.1-2 只弃 assistant 侧）----
        memory_service = getattr(self.ctx, "memory_service", None)
        if memory_service is not None:
            try:
                await memory_service.on_message(session, "user", text)
            except Exception:  # noqa: BLE001 —— 计数失败不阻塞消息循环
                logger.exception("记忆计数处理失败")

        # ---- 7) 打断地平线状态机（O3：到达即起跑，零窗口税）----
        msg = _PendingMsg(
            text=text,
            reply_id=item.event.get("reply_id"),
            reply_text=item.event.get("reply_text"),
            message_id=item.message_id,
            arrived_ts=item.arrived_ts,
            arrived_mono=item.arrived_mono,
            row_id=row_id,
            group_id=item.group_id,
        )
        await self._feed_round(st, msg)

    # ------------------------------------------------------------------
    # 图片识别（多模态①：image 段 → 视觉描述，brain/vision.py）
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_image_urls(event: Any) -> List[str]:
        """从桥事件段列表提取 image 段的 URL（NapCat 媒体 URL）。

        桥（onebot_server._handle_message_event）已把段列表原样随事件上行；
        无段/无 url（如字符串形态 CQ 码降级路径）返回空列表=零改动。
        """
        segments = event.get("segments") if isinstance(event, dict) else None
        if not segments:
            return []
        urls: List[str] = []
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "image":
                url = (seg.get("data") or {}).get("url")
                if isinstance(url, str) and url:
                    urls.append(url)
        return urls

    async def _vision_enrich(self, text: str, image_urls: List[str]) -> str:
        """把文本里的「[图片]」占位按出现顺序替换为视觉描述。

        - 开关关闭/客户端不可用/识别失败：process_image 返回占位本身，
          替换是恒等操作（现行为零变化，多张图也各回占位）；
        - 文本里没有占位（段列表有图但 text 降级丢失）：描述以「（图片：…）」
          追加，绝不静默丢图；
        - caption 传剥除占位后的文本（占位字样对视觉模型无意义）；
        - vision.process_image 任何异常都内部降级为占位，这里的 try 是
          双保险（识别故障绝不阻断消息循环）。
        """
        cfg = self.ctx.cfg
        chat_client = getattr(self.ctx, "chat_client", None)
        vision_on = vision_mod.supports_vision(cfg)
        caption = text.replace(vision_mod.PLACEHOLDER_IMAGE, " ").strip()
        for url in image_urls:
            try:
                desc = await vision_mod.process_image(
                    url, caption,
                    chat_client=chat_client,
                    supports_vision_flag=vision_on)
            except Exception:  # noqa: BLE001 —— 双保险：占位继续，不吞消息
                logger.exception("[vision] 图片识别异常（按占位继续）")
                desc = vision_mod.PLACEHOLDER_IMAGE
            if vision_mod.PLACEHOLDER_IMAGE in text:
                text = text.replace(vision_mod.PLACEHOLDER_IMAGE, desc, 1)
            elif desc and desc != vision_mod.PLACEHOLDER_IMAGE:
                text = f"{text}（图片：{desc}）" if text else f"（图片：{desc}）"
        return text

    # ------------------------------------------------------------------
    # 语音转写（多模态②：record 段 → get_record → 云端转写 → 文字替换占位）
    # ------------------------------------------------------------------

    async def _resolve_voice_text(self, item: _Arrival) -> str:
        """语音消息文本解析：文本含 [语音] 占位且有 record 段时转写替换。

        - 云端 STT（[chat].stt_provider 已配）：逐段 ``bridge.get_record_base64``
          取音频 → ``stt.transcribe_async`` 转写 → 文字替换占位（LLM 看到的
          是「对方说的话」，后续人格/记忆/语音回复全走正常文本管线）；
        - 本地降级：未配 STT / 桥缺 get_record / 取音频或转写失败 / 转写为空
          → 占位替换为 ``[语音消息]``（stt.replace_voice_placeholder 统一收口，
          与 [图片] 同款语义）——任何一步失败都不抛出，绝不让一条听不懂的
          语音拖垮对话；
        - 无 record 段（用户纯文本里恰好写了「[语音]」字样）：原样返回不动。
        """
        text = item.text
        files = stt_mod.iter_record_files(item.event.get("segments"))
        if not files:
            logger.debug("文本含 [语音] 字样但无 record 段，按普通文本处理")
            return text
        bridge = getattr(self.ctx, "bridge", None)
        get_record = getattr(bridge, "get_record_base64", None)
        if self._stt.enabled and get_record is None:
            logger.warning(
                "[pipeline] STT 已配置（provider=%s）但桥无 get_record_base64，"
                "语音消息降级 [语音消息] 占位", getattr(self._stt, "provider", "?"))
        for file_id in files:
            heard = ""
            if self._stt.enabled and get_record is not None:
                try:
                    audio_b64 = await get_record(file=file_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 —— 取音频失败降级占位
                    logger.warning("get_record 取音频失败（file=%s…）：%s",
                                   file_id[:16], exc)
                else:
                    if audio_b64:
                        try:
                            heard = await self._stt.transcribe_async(audio_b64)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 —— 转写失败降级占位
                            logger.warning("语音转写失败（provider=%s）：%s",
                                           getattr(self._stt, "provider", "?"), exc)
                    else:
                        logger.warning("get_record 未返回音频 base64（file=%s…），降级占位",
                                       file_id[:16])
            text = stt_mod.replace_voice_placeholder(text, heard)
        if self._stt.enabled and stt_mod.PLACEHOLDER_RECORD not in text:
            # 占位全部替换成了转写文字（无 [语音] 残留）才算听懂；
            # [语音消息] 残留（降级占位）不算，免得误导排障
            logger.info("[pipeline] 语音消息已转写（user=%s***，%d 段）",
                        item.user_id[:2], len(files))
        return text

    # ------------------------------------------------------------------
    # 打断地平线状态机（O3：IDLE / RUNNING / DRIP）
    # ------------------------------------------------------------------

    async def _feed_round(self, st: _UserRuntime, msg: _PendingMsg) -> None:
        """把一条非命令消息喂给状态机：起跑 / 打断重跑 / 排队（发完再新一轮）。

        判定（§2-O3 + §7.2）：
        - IDLE → 立即起跑（零窗口税）；
        - RUNNING（首段未发出）且 距上条 ≤ interrupt_window（窗口>0）
          → generation 前进：旧轮由守卫丢弃（结果与 assistant 行不落库，
          仅计费），合并全部上下文立即重跑；
        - RUNNING 但超窗 / 窗口关（0=现行为）/ 冷启动（距上条 >2min）
          → 排队，当前轮发完再新一轮；
        - DRIP → 一律排队（分段发送中不可打断）。
        """
        window = self.interrupt_window
        async with st.lock:
            gap = (None if st.last_arrival_ts is None
                   else msg.arrived_ts - st.last_arrival_ts)
            st.last_arrival_ts = msg.arrived_ts
            if st.state == "IDLE":
                self._start_round_locked(st, [msg])
                return
            if st.state == "RUNNING" and window > 0 and gap is not None \
                    and gap <= window:
                st.gen += 1
                self.superseded_count += 1
                logger.info(
                    "[pipeline] 打断丢弃：user=%s 第 %d 代被第 %d 代取代"
                    "（距上条 %.2fs ≤ 窗口 %.2fs，superseded 累计 %d）——"
                    "旧轮结果与 assistant 行不落库（§7.1-2）",
                    st.user_id[:2] + "***", st.gen - 1, st.gen, gap, window,
                    self.superseded_count)
                st.round_msgs.append(msg)
                st.round_task = asyncio.ensure_future(
                    self._run_round(st.user_id, st.gen))
                return
            # 超窗 / 窗口关 / 冷启动 / DRIP：发完再新一轮（§7.2「超窗到达一律
            # 发完再新一轮」；冷启动只多一条明示日志，行为一致）
            if gap is not None and gap > COLD_START_SECONDS:
                logger.info(
                    "[pipeline] 冷启动：user=%s 距上条 %.0fs > 2min，"
                    "不启用打断期（当前轮发完再回应最新）",
                    st.user_id[:2] + "***", gap)
            st.pending.append(msg)

    def _start_round_locked(self, st: _UserRuntime, msgs: List[_PendingMsg]) -> None:
        """起跑一代 LLM 轮（须持 st.lock；临界区内只做字段读写 + create_task）。"""
        st.gen += 1
        st.state = "RUNNING"
        st.round_msgs = list(msgs)
        st.round_task = asyncio.ensure_future(self._run_round(st.user_id, st.gen))

    def _pickup_pending_locked(self, st: _UserRuntime) -> None:
        """状态归 IDLE 后接续排队消息（合并成一轮回应最新；须持 st.lock）。"""
        if st.state != "IDLE" or not st.pending:
            return
        msgs = list(st.pending)
        st.pending.clear()
        self._start_round_locked(st, msgs)

    async def _finish_round(self, st: _UserRuntime, gen: int) -> None:
        """一代轮的发送阶段收尾：状态归 IDLE + 接续排队消息。

        gen 已过期（被新代取代）时不动状态——状态机归属当前代。
        """
        async with st.lock:
            if st.gen != gen:
                return
            st.state = "IDLE"
            self._pickup_pending_locked(st)

    # ------------------------------------------------------------------
    # LLM 轮（检索 → 装配 → chat → 守卫 → 分段入发送 FIFO）
    # ------------------------------------------------------------------

    async def _run_round(self, user_id: str, gen: int) -> None:
        """一代 LLM 轮的完整执行体（作为独立 task 跑，便于打断后让位）。

        generation-id 守卫点：chat 返回后、发送入队前后、收尾回调处——
        任何一点发现 gen 过期即静默放弃（不做传输层 cancel，被弃线程由
        chat timeout_main=30s 压住，§0 终裁 / §7.1-4）。
        """
        st = self._users.get(user_id)
        if st is None:
            return
        cfg = self.ctx.cfg
        session = str(cfg.session_id or "")
        msgs = list(st.round_msgs)
        # 起跑守卫（首个切片前已被更新代取代）：静默让位，不发起任何动作
        if st.gen != gen:
            return
        try:
            # ---- ① reply 回查（随事件进管线，per-user 隔离，≤10s，O4②）----
            bridge = getattr(self.ctx, "bridge", None)
            for m in msgs:
                if m.reply_id and not m.reply_text and bridge is not None:
                    try:
                        m.reply_text = await asyncio.wait_for(
                            bridge.get_msg_text(m.reply_id),
                            timeout=REPLY_LOOKUP_TIMEOUT_SECONDS)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 —— 回查失败置空不阻塞主流程
                        logger.warning("reply 回查失败（message_id=%s），引用文本置空",
                                       m.reply_id)
                        m.reply_text = None

            # ---- ② history 读取（排除本轮 user 行：它们已在到达时刻落库，
            #         合并文本走 user_text 注入，防上下文重复）----
            history_msgs = self._read_history(
                session, exclude_ids={m.row_id for m in msgs if m.row_id is not None})

            # ---- ③ 记忆检索（注入开关；失败降级为不注入）----
            memories = await self._recall(session, msgs[-1].text, history_msgs)

            # ---- ③b 联网搜索（多模态③，brain/websearch.py）：最新一条消息
            #          命中搜索意图关键词 → 检索 → 资料引用块（prepend 到
            #          user_text 前）。开关关/无意图/检索失败/零结果一律返回
            #          空串=零行为变化；网络请求在 to_thread 工作线程执行。
            search_block = await self._search_block(msgs)

            # ---- ③b' 天气感知（soul/weather.py）：[tools].weather_city 已配
            #          且消息命中天气话题 → wttr.in 资料行（资料引用块形态，
            #          与 websearch 资料块同位同风格；未配置/无意图/失败=零变化）
            weather_block = await self._weather_block(msgs)

            # ---- ③c 故事模式（soul/story.py）：触发词开窗 → 本轮豁免块 ----
            story_escape = self._prepare_story_mode(msgs)

            # ---- ④ mood 快照 + persona 装配（合并文本先做定界符中和，O3⑥）----
            # 搜索/天气资料块是本模块自产的受控文本（非用户输入），不做定界符
            # 中和——它们的 <资料引用开始/结束> 定界符必须原样成立才能隔离资料。
            merged_user_text = "\n".join(
                neutralize_delimiters(m.display_text) for m in msgs)
            material_blocks = [b for b in (weather_block, search_block) if b]
            user_text = ("\n\n".join(material_blocks + [merged_user_text])
                         if material_blocks else merged_user_text)
            mood_fragment, mood_snapshot = self._mood_view(user_id)
            schedule_line = self._schedule_line()
            assembled = persona.assemble(
                persona_name=getattr(cfg.persona, 'name', '') or '',
                persona_prompt=cfg.persona.system_prompt,
                user_text=user_text,
                history=history_msgs,
                begin_dialogs=cfg.persona.begin_dialogs,
                memories=memories,
                mood_fragment=mood_fragment,
                schedule_line=schedule_line,
                inject_mood=cfg.mood.inject_enabled,
                inject_time=True,
                now=self._now(),
                max_context_tokens=cfg.context.max_context_tokens,
                truncate_ratio=cfg.context.truncate_ratio,
                max_memory_items=cfg.mnemosyne.top_k,
                tz_label="CST",
                mood_position=cfg.mood.fragment_position,
                message_id=msgs[0].message_id,
            )
            # ---- ④b system 尾部增补（工具箱紧凑块 + 数据行 + 故事豁免块）----
            self._augment_system(assembled, session, story_escape)

            # ---- ⑤ LLM 对话（O6 隐式缓存：prompt_cache_key=会话 ID）----
            # 起跑守卫（pre-chat）：检索/装配期间已被更新代取代的轮，在此让位
            # ——不发起 LLM 调用（零计费）、不落任何行；「已发起 chat 才被取代」
            # 的轮走 ⑥ 的 post-chat 守卫丢弃（§2-O3：generation-id 丢弃守卫）。
            if st.gen != gen:
                logger.info("[pipeline] 第 %d 代轮起跑前让位（已被第 %d 代取代）",
                            gen, st.gen)
                return
            chat_client = getattr(self.ctx, "chat_client", None)
            if chat_client is None:
                logger.error("chat 客户端未装配，无法回复（消息已入情绪/历史管道）")
                await self._finish_round(st, gen)
                return
            try:
                reply = await chat_client.chat_async(
                    assembled.messages,
                    extra_body={"prompt_cache_key": session},
                    # R5：被弃轮止损——代被取代后，在途/退避中的 chat 不再重试
                    # 不再切 fallback（修前僵尸重试占满本地大脑多 slot，新代
                    # 挤不进导致 burst 全静默，实测见 vl.log 00:57:55 起）
                    should_retry=lambda: st.gen == gen,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if st.gen != gen:
                    # 失败（或止损放弃）时轮已被 newer 代取代：这是正常让位，
                    # 不是用户可感知的失败——不计 llm_fail_count、不打 ERROR
                    logger.info("[pipeline] 第 %d 代失败时已被第 %d 代取代，静默让位：%s",
                                gen, st.gen, exc)
                    return
                # R2 计数面：LLM 调用最终失败（重试/fallback 全耗尽）单独计数，
                # 控制台「LLM 失败 N 次」chips 数据源；本轮仍按失败收尾（静默）
                self.llm_fail_count += 1
                logger.error("LLM 调用最终失败（第 %d 代，累计 %d 次），本轮静默：%s",
                             gen, self.llm_fail_count, exc)
                await self._finish_round(st, gen)
                return

            # ---- ⑥ generation-id 守卫：被弃轮只丢结果（仅计费）----
            if st.gen != gen:
                logger.info(
                    "[pipeline] 第 %d 代轮完成即弃（已被第 %d 代取代）——"
                    "不发送、不写 assistant 行", gen, st.gen)
                return
            reply_text_out = reply.content
            if not reply_text_out.strip():
                logger.warning("LLM 返回空回复，本轮静默")
                await self._finish_round(st, gen)
                return

            # ---- ⑦ 出站后处理（七模块接线）：情绪自评标签 → meme &&tag&&
            #          → 自拍 → 分段 → QQ 表情标记剥出 → 引用回复目标 ----
            # 标签先行剥净：解析出心情后，正文/历史/语音任何出口都不再见标签
            await self._apply_llm_mood(user_id, reply_text_out)
            reply_text_out = textclean_mod.strip_mood_tags(reply_text_out)
            plan = self._meme_plan(reply_text_out)
            plan_text, image_path = plan.text, plan.image_path
            selfie_dir = self._data_dir()
            if image_path is None and selfie_dir is not None:
                # 自拍（soul/selfies.py，对齐 memes 调用方式）：本轮已发 meme
                # 图则跳过；命中时照片走 job 的独立图片消息通道
                try:
                    plan_text, selfie_img = selfies_mod.process_reply(
                        plan.text,
                        now=self._now(),
                        state_dir=selfie_dir,
                        rng=self._rng,
                    )
                    if selfie_img is not None:
                        image_path = selfie_img
                except Exception:  # noqa: BLE001 —— 自拍故障不阻断回复
                    logger.exception("[selfies] 自拍后处理失败（按纯文字继续）")
                    plan_text = plan.text
            # 分段前再兜底剥一次标签（parse_mood_tag 只剥严格合法形态，
            # strip_mood_tags 连残缺标签也剥）；剥空段丢弃
            segments = [
                seg for seg in (textclean_mod.strip_mood_tags(raw)
                                for raw in sender.segment_reply(
                                    plan_text, cfg.reply.segmented))
                if seg
            ]
            # QQ 表情（bridge/faces.py）：{{face:id}} 标记剥出，发送层按段
            # 构造 face 数组段；无标记段原样保留
            segments, face_ids = self._split_face_markers(segments)
            # 历史行/命令侧与出站同源：落库文本按剥净标记后的段拼接
            # （face/selfie/mood 标记任何情况不进历史、不进语音）
            reply_text_out = "".join(segments)

            # ---- 桥缺失：现行为=仍写历史（发送面告警）----
            if bridge is None:
                logger.warning("桥未装配，LLM 回复无法送出（长度 %d）", len(plan_text))
                self._archive_assistant(session, reply_text_out)
                await self._count_assistant(session, reply_text_out)
                await self._finish_round(st, gen)
                return
            if not segments:
                await self._finish_round(st, gen)
                return

            # ---- ⑧ 分段入发送 FIFO（首段拟人抖动=并行下限，§7.1-1）----
            image_b64: "str | None" = None
            if image_path is not None:
                try:
                    data = await asyncio.to_thread(Path(image_path).read_bytes)
                    image_b64 = base64.b64encode(data).decode("ascii")
                except OSError as exc:
                    logger.warning("表情包/自拍图片读取失败（%s）：%s", image_path, exc)
            group_id = getattr(msgs[0], "group_id", "")
            reply_to_message_id = self._reply_target(session, msgs, group_id)
            first_gap = sender.sample_first_gap(
                cfg.reply.segmented,
                next_len=len(segments[0]) if segments else None)
            first_send_at = (msgs[0].arrived_mono + first_gap) if first_gap > 0 else None

            async def _archive_and_count() -> None:
                self._archive_assistant(session, reply_text_out)
                await self._count_assistant(session, reply_text_out)

            voice_enqueued = {"done": False}

            async def on_first_sent(_sent: List[str]) -> None:
                """R3-2c 语音并行：首段文字实际出帧后立即入队整条语音作业。

                旧 §7.2 语义（完整发完才入队）让语音总在最后一个文字段之后才
                开始合成——段落越多，语音落后文字越远。改为首段出帧即起跑：
                合成第 1 段与文字第 2 段的段间隔并行，语音全程追着文字走。
                只入队一次（首段回调恰好触发一次，守卫是防御性的）。
                """
                if voice_enqueued["done"]:
                    return
                voice_enqueued["done"] = True
                voice_manager = getattr(self.ctx, "voice_manager", None)
                if voice_manager is None or not segments:
                    return
                try:
                    voice_manager.enqueue_reply(
                        user_id, segments, mood_snapshot=mood_snapshot)
                except Exception:  # noqa: BLE001 —— 语音轨故障不影响文字回复
                    logger.exception("语音队列入队失败（文字回复不受影响）")

            async def on_done(sent_segments: List[str]) -> None:
                """回复完整发完后收尾：assistant 行 + 记忆计数 + 状态收尾。

                R3-2c：语音已在首段出帧时并行入队（on_first_sent），此处不再
                重复入队；被弃轮的清场路径到不了这里（语音守卫同旧 §7.2）。
                故事模式（接线 2）：每轮回复完成后消耗一轮（无窗口时幂等空转）。
                """
                await _archive_and_count()
                story_dir = self._data_dir()
                if story_dir is not None:
                    try:
                        story_mod.consume_turn(story_dir)
                    except Exception:  # noqa: BLE001 —— 窗口记账失败不影响收尾
                        logger.exception("[story] 故事轮消耗失败（不影响本轮回复）")
                await self._finish_round(st, gen)

            async def on_fail(sent_segments: List[str]) -> None:
                """发送失败短路收尾（R4 幽灵回复修复）：只写实际送达的段。

                旧行为整条 reply_text_out 落库——她实际一个字都没送出去时，
                历史里却多了一条她「说过」的 assistant 行（幽灵回复：下一轮
                她接着没说出口的话往下聊）。修复：
                - sent_segments 空 → 不写 assistant 行、不计记忆（本轮她沉默）；
                - 部分送达 → 只落送达段的拼接（sender.segment_reply 各段自带
                  标点，"".join 可忠实还原已说出的话），绝不写未送达的尾巴。
                R3-2c：排队中尚未处理的语音作业一并收走（文字没说完的段，
                语音不再出帧；正在合成/发送中的段自然完成）。
                """
                delivered = "".join(sent_segments).strip()
                if delivered:
                    self._archive_assistant(session, delivered)
                    await self._count_assistant(session, delivered)
                else:
                    logger.info("[pipeline] 发送失败且零段送达（gen=%d）——"
                                "本轮不写 assistant 行（幽灵回复修复）", gen)
                voice_manager = getattr(self.ctx, "voice_manager", None)
                if voice_manager is not None and voice_enqueued["done"]:
                    try:
                        voice_manager.drop_queued(user_id)
                    except Exception:  # noqa: BLE001 —— 语音清场失败不影响文字收尾
                        logger.exception("语音队列清场失败（文字收尾不受影响）")
                await self._finish_round(st, gen)

            job = _SendJob(
                user_id=user_id,
                segments=segments,
                params=cfg.reply.segmented,
                image_b64=image_b64,
                first_send_at=first_send_at,
                gen=gen,
                message_id=msgs[0].message_id,
                arrived_mono=msgs[0].arrived_mono,
                on_done=on_done,
                on_fail=on_fail,
                on_first_sent=on_first_sent,
                group_id=group_id,
                reply_to_message_id=reply_to_message_id,
                face_ids=face_ids,
            )
            if st.gen != gen:   # 入队前再守卫一次（装配/回查期间可能已被打断）
                logger.info("[pipeline] 第 %d 代轮入队前弃（已被第 %d 代取代）",
                            gen, st.gen)
                return
            self._enqueue_send_job(st, job)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 单轮失败不拖垮管线
            logger.exception("LLM 轮处理异常（第 %d 代）", gen)
            await self._finish_round(st, gen)

    # ------------------------------------------------------------------
    # 发送 FIFO（O4：统一收编全部文字发送方）
    # ------------------------------------------------------------------

    def _enqueue_text_job(self, st: _UserRuntime, texts: List[str], params: Any,
                          *, message_id: str = "-",
                          arrived_mono: "float | None" = None,
                          on_done: "Callable[[List[str]], Awaitable[None]] | None" = None,
                          on_fail: "Callable[[List[str]], Awaitable[None]] | None" = None) -> None:
        """非轮内文字发送入口（命令直答/proactive/diary 共用）。

        gen=-1：永不因打断丢弃、不参与状态机迁移、不写 assistant 行
        （proactive/diary 各自写历史，命令旁路直答不写）。
        R3-1a：on_done/on_fail 可选回传真实出帧结果（proactive 送达判定消费；
        回调按作业逐个触发，多文本入队时每个作业各回调一次）。
        """
        for text in texts:
            segments = sender.segment_reply(str(text or ""), params)
            if not segments:
                continue
            self._enqueue_send_job(st, _SendJob(
                user_id=st.user_id,
                segments=segments,
                params=params,
                message_id=message_id,
                arrived_mono=arrived_mono,
                on_done=on_done,
                on_fail=on_fail,
            ))

    def enqueue_text_send(self, user_id: str, texts: List[str], params: Any, *,
                          on_done: "Callable[[List[str]], Awaitable[None]] | None" = None,
                          on_fail: "Callable[[List[str]], Awaitable[None]] | None" = None) -> None:
        """公开入口：proactive 文字 / diary 文字入 per-user 发送 FIFO（O4 收编）。

        R3-1a：on_done(实际出帧段) / on_fail(短路前已出帧段) 可选回传——
        proactive 由此区分「已送达 / 未送达」（桥无连接时根本不入队）。
        """
        st = self._user(str(user_id))
        self._enqueue_text_job(st, list(texts or []), params,
                               on_done=on_done, on_fail=on_fail)

    async def _sender_worker(self, st: _UserRuntime) -> None:
        """per-user 发送 FIFO worker：逐作业顺序发送（FIFO 保序）。

        单作业异常只记日志，worker 常驻；被 cancel（aclose）时安静退出。
        """
        while True:
            job = await st.send_queue.get()
            if job.discarded:
                continue
            try:
                await self._send_job(st, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 单作业失败不拖垮发送队列
                logger.exception("发送作业异常（user=%s）", st.user_id[:2] + "***")

    async def _send_job(self, st: _UserRuntime, job: _SendJob) -> None:
        """发送一个作业：限流 → 首段抖动等待 → 逐段出帧 → 段间隔 → meme 图 → 收尾。

        - 每段发送前检查 generation：被弃代的未发段一律不再出帧（O4④ 清场）；
        - 发送失败短路本回复剩余段（O4③，修掉只告警不抛的现状）；
        - 段间隔：有排队消息（pending）时压缩到拟人硬下限（§2-O3「允许压缩
          段间隔收尾」），否则按分段参数采样（统一 ≥1.2s 下限）；
        - 首段实际出帧 = RUNNING→DRIP（§7.2：入 FIFO 不算）。
        """
        bridge = getattr(self.ctx, "bridge", None)
        limiter = getattr(self.ctx, "rate_limiter", None)
        if bridge is None:
            logger.warning("桥未装配，发送作业丢弃（%d 段）", len(job.segments))
            return
        sent_segments: List[str] = []
        aborted = False        # 发送失败短路
        interrupted = False    # 被弃代清场
        total = len(job.segments)
        for index, segment in enumerate(job.segments):
            if job.gen >= 0 and job.gen != st.gen:
                interrupted = True
                break
            if index == 0 and job.first_send_at is not None:
                delay = job.first_send_at - time.monotonic()
                if delay > 0:
                    logger.debug("[pipeline] 首段拟人抖动：%.2fs 后出帧", delay)
                    await asyncio.sleep(delay)
            if limiter is not None:
                await limiter.acquire()
            result = await self._deliver_text_segment(bridge, job, index)
            if result is not None and not getattr(result, "ok", True):
                logger.warning(
                    "发送失败（%s）：user=%s 段长度 %d —— 短路剩余段（O4③）",
                    getattr(result, "status", None),
                    job.user_id[:2] + "***", len(segment))
                aborted = True
                break
            sent_segments.append(segment)
            if index == 0:
                self._log_first_segment(job, segment)
                if job.on_first_sent is not None:
                    # R3-2c 语音并行锚点：首段真实出帧后触发（失败收尾不触发）
                    try:
                        await job.on_first_sent(list(job.segments))
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 —— 并行轨故障不影响文字发送
                        logger.exception("on_first_sent 回调失败（文字发送继续）")
                if job.gen >= 0:
                    async with st.lock:
                        if st.gen == job.gen and st.state == "RUNNING":
                            st.state = "DRIP"   # 首段实际发出（出帧才算，§7.2）
            if index < total - 1:
                if st.pending:
                    gap = MIN_SEGMENT_GAP_SECONDS   # 有排队消息：压缩收尾
                else:
                    # 打字节奏（接线 5）：下一段长度参与间隔采样——段越长等越久
                    gap = sender.sample_gap(
                        job.params, self._rng,
                        next_len=len(job.segments[index + 1]))
                await asyncio.sleep(gap)
        if not interrupted and job.image_b64 and not aborted:
            if job.gen >= 0 and job.gen != st.gen:
                interrupted = True
            else:
                try:
                    if limiter is not None:
                        await limiter.acquire()
                    if not await self._send_image_b64(bridge, job):
                        logger.warning("表情包/自拍图片发送失败（%s）",
                                       "group" if job.group_id else "private")
                except OSError as exc:
                    logger.warning("表情包/自拍图片发送异常：%s", exc)
        if interrupted:
            # 被弃轮：未发段已清场，收尾回调不跑（结果与 assistant 行均不落库）
            logger.info("[pipeline] 第 %s 代作业清场：未发段不再出帧", job.gen)
            return
        if job.gen >= 0 and job.gen != st.gen:
            return
        try:
            if aborted:
                if job.on_fail is not None:
                    await job.on_fail(sent_segments)
            elif job.on_done is not None:
                await job.on_done(sent_segments)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 收尾失败不影响已发出的段
            logger.exception("回复收尾回调失败")

    def _log_first_segment(self, job: _SendJob, segment: str) -> None:
        """首个 send_private_msg 出帧打点（PERF_SPEC §7.1-5/§7.1-6 判定器口径）。"""
        e2e = ("-" if job.arrived_mono is None
               else f"{(time.monotonic() - job.arrived_mono) * 1000:.0f}")
        logger.info("[perf] 首段已发送 message_id=%s e2e_ms=%s len=%d",
                    job.message_id, e2e, len(segment))

    # ------------------------------------------------------------------
    # 七模块接线 helpers（情绪自评/故事/自拍/QQ 表情/引用/天气/纪念日/群聊）
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_group_id(event: Any) -> str:
        """桥事件里的群聊标记（onebot_server._handle_group_event 写入）。

        桥把 ``_vl_group_id`` 写在原始 payload 上，随事件的 raw 字段原样上行；
        这里兼容顶层与 raw 两处读取。缺失=私聊（空串，行为零变化）。
        """
        if not isinstance(event, dict):
            return ""
        group_id = event.get("_vl_group_id")
        if not group_id:
            raw = event.get("raw")
            if isinstance(raw, dict):
                group_id = raw.get("_vl_group_id")
        return str(group_id or "")

    def _data_dir(self) -> "Path | None":
        """运行时状态目录（storage.data_dir，默认 local/vl_data；解析同
        soul/proactive.setup 与 soul/story 的落点约定）。

        cfg 缺 storage 段（最小测试桩，如语音并行测试的手搭 ctx）时返回
        None——七模块接线的状态类增值（故事/自拍/纪念日/里程碑）整体跳过，
        绝不因缺配置面崩管线，更不误写真实运行目录。
        """
        storage_cfg = getattr(self.ctx.cfg, "storage", None)
        if storage_cfg is None:
            return None
        return Path(self.ctx.repo_root) / storage_cfg.data_dir

    async def _weather_block(self, msgs: List[_PendingMsg]) -> str:
        """天气感知（soul/weather.py）：城市已配 + 消息命中天气话题 → 资料块。

        - 与 websearch 资料块同位同风格：<资料引用开始/结束> 包裹，prepend
          到 user_text 前（自产受控文本，不做定界符中和）；
        - 未配置城市 / 无意图 / 拉取失败（maybe_weather_block 内部静默）
          一律空串=零行为变化；阻塞网络请求在 to_thread 工作线程执行；
        - urlopen 经 ctx.weather_urlopen 注入（测试替身；缺省 None=真实网络）。
        """
        try:
            tools_cfg = getattr(self.ctx.cfg, "tools", None)
            city = str(getattr(tools_cfg, "weather_city", "") or "").strip()
            if not city:
                return ""
            for msg in reversed(msgs):
                if weather_mod.has_weather_intent(msg.text):
                    line = await asyncio.to_thread(
                        weather_mod.maybe_weather_block,
                        msg.text, city,
                        now=self._now(), cache_dir=self._data_dir(),
                        urlopen=getattr(self.ctx, "weather_urlopen", None))
                    if not line:
                        return ""
                    return (
                        f"{persona.MATERIAL_OPEN}\n{persona.INJECTION_GUARD}\n"
                        f"{line}\n{persona.MATERIAL_CLOSE}"
                    )
            return ""
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 天气故障绝不拖垮本轮对话
            logger.exception("[weather] 天气资料块构造异常（本轮不注入）")
            return ""

    def _prepare_story_mode(self, msgs: List[_PendingMsg]) -> str:
        """故事模式开窗与本轮豁免块（soul/story.py）。

        - 触发词命中任一条消息 → activate 开窗（/story 命令侧已处理状态，
          管线只读+消费）；窗口有效（turns_left>0 且未过 2h 兜底）→ 返回
          escape_block 文本随 system 注入；
        - 任何故障降级为不注入（空串），绝不阻断本轮。
        """
        try:
            state_dir = self._data_dir()
            if state_dir is None:
                return ""
            now_ts = self._now()
            if any(story_mod.triggered(m.text) for m in msgs):
                story_mod.activate(state_dir, now=now_ts)
            if story_mod.is_active(state_dir, now=now_ts):
                return story_mod.escape_block()
        except Exception:  # noqa: BLE001
            logger.exception("[story] 故事模式状态处理失败（本轮不注入豁免块）")
        return ""

    def _augment_system(self, assembled: Any, session: str,
                        story_escape: str) -> None:
        """system 尾部增补（接线 1/2/4/8/9）——控制每轮总增量的收口。

        - 工具箱紧凑块（对用户不可见的输出约定）：心情自评标签一行 +
          QQ 表情速查一行，合并成单个块，防 system 逐轮膨胀；
        - 数据行：认识天数（ensure_anchor 同源锚点）、她的生日；
        - 故事模式豁免块（窗口有效时原样追加）。
        无增补时 messages 零改动。
        """
        parts: List[str] = []
        toolbox: List[str] = []
        if getattr(self.ctx, "mood_engine", None) is not None:
            mood_names = "、".join(mood_mod.MOOD_ZH[name] for name in mood_mod.MOOD_ZH)
            toolbox.append(
                "每次回复的最末尾另起一行，输出心情自评标签 "
                f"<<mood:心情名:强度>>（强度 0.0-1.0；心情名从这 "
                f"{len(mood_mod.MOOD_ZH)} 个里选：{mood_names}）。"
                "该行由系统记录用于校准你的情绪，对方看不到，不要在正文提起。")
        cheatsheet = faces_mod.faces_cheatsheet()
        if cheatsheet:
            toolbox.append(
                "可以在一条消息的末尾放一个 QQ 经典小黄脸（每条最多 1 个），"
                f"写法照抄：{cheatsheet}")
        if toolbox:
            parts.append("【输出约定】（对方看不到约定本身）\n"
                         + "\n".join(f"- {line}" for line in toolbox))
        days_line = self._relation_days_line(session)
        if days_line:
            parts.append(f"（{days_line}）")
        try:
            if anniversary_mod.her_birthday_today(self.ctx.cfg, self._now()):
                parts.append("（今天是我的生日）")
        except Exception:  # noqa: BLE001
            logger.exception("[anniversary] 生日判定失败（本轮不注入）")
        if story_escape:
            parts.append(story_escape)
        extra = "\n".join(part for part in parts if part)
        if not extra:
            return
        for msg in assembled.messages:
            if str(msg.get("role")) == "system":
                msg["content"] = f"{str(msg.get('content') or '').rstrip()}\n\n{extra}"
                return
        assembled.messages.insert(0, {"role": "system", "content": extra})

    def _relation_days_line(self, session: str) -> str:
        """认识天数一行（soul/anniversary.relation_days_line；锚点走
        ensure_anchor 同源数据——首次从会话库最早消息落盘，此后只读）。"""
        data_dir = self._data_dir()
        if data_dir is None:
            return ""
        try:
            anchor_state = anniversary_mod.ensure_anchor(
                getattr(self.ctx, "history", None), session,
                self._data_dir() / "anniversary.json", clock=self._now)
            anchor = anchor_state.get("anchor_ts")
            line = anniversary_mod.relation_days_line(
                anchor if isinstance(anchor, (int, float)) else 0, self._now())
            return str(line or "")
        except Exception:  # noqa: BLE001
            logger.exception("[anniversary] 认识天数渲染失败（本轮不注入）")
            return ""

    async def _apply_llm_mood(self, user_id: str, reply_text: str) -> None:
        """情绪自评（soul/mood.parse_mood_tag + EVENT_MOOD_LLM）：回复末尾的
        ``<<mood:名:强度>>`` 解析出心情后推进引擎状态（只改心情与强度）。

        - 无合法标签 / 引擎缺席 / 任何故障：静默跳过（自评是增益项，
          绝不阻断回复）；剥标签的出站兜底在调用方（strip_mood_tags）；
        - apply_event 是纯函数：引擎锁内读改写、锁外 to_thread 落盘，
          与 MoodEngine.handle_message 自己的序完全一致。
        """
        parsed = mood_mod.parse_mood_tag(reply_text)
        if parsed is None:
            return
        _cleaned, name, intensity = parsed
        engine = getattr(self.ctx, "mood_engine", None)
        if engine is None:
            return
        try:
            event = mood_mod.Event(
                mood_mod.EVENT_MOOD_LLM, value=float(intensity), mood=name)

            def _apply() -> None:
                state = engine.storage.get(user_id)
                engine.storage.set(
                    user_id, mood_mod.apply_event(state, event, now=self._now()))

            lock = getattr(engine, "_lock", None)
            if lock is not None:
                async with lock:
                    _apply()
            else:
                _apply()
            await asyncio.to_thread(engine.storage.flush)
            logger.debug("[pipeline] 情绪自评已应用：mood=%s intensity=%.2f",
                         name, intensity)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[pipeline] 情绪自评应用失败（不影响本条回复）")

    @staticmethod
    def _split_face_markers(segments: List[str]) -> "tuple[List[str], List[List[int]]]":
        """QQ 表情标记剥出（bridge/faces.extract_face_markers）。

        返回与输入等长的 (清洗后段列表, 各段 face id 列表)；不含 ``{{`` 的
        段原样保留（无标记零改动）；标记剥空且无 id 的段丢弃（剥空但有
        id 的段保留——纯表情消息）。白名单外 id 在 extract 内已丢弃。
        """
        cleaned: List[str] = []
        faces: List[List[int]] = []
        for seg in segments:
            if "{{" not in seg:
                cleaned.append(seg)
                faces.append([])
                continue
            text, ids = faces_mod.extract_face_markers(seg)
            if not text and not ids:
                continue
            cleaned.append(text)
            faces.append(list(ids))
        return cleaned, faces

    def _reply_target(self, session: str, msgs: List[_PendingMsg],
                      group_id: str) -> str:
        """引用回复目标（克制策略，三者其一才引用）：

        - (a) 群聊消息：始终引用他最新一条（群聊不引用会不知在回谁）；
        - (b) 私聊且他引用了较远的目标（之后的消息 >3 条）：引用被引用那条；
        - (c) 其余情况 25% 随机：引用他最新一条（克制的点缀）。
        空串 = 不前置 reply 段（原路径）。
        """
        latest = msgs[-1]
        if group_id:
            return str(getattr(latest, "message_id", "") or "")
        latest_reply_id = getattr(latest, "reply_id", None)
        if latest_reply_id:
            try:
                if self._quote_distance(session, latest_reply_id) \
                        > QUOTE_DISTANCE_THRESHOLD:
                    return str(latest_reply_id)
            except Exception:  # noqa: BLE001 —— 距离判定失败按「不引用」处理
                logger.exception("[pipeline] 引用距离判定失败")
        if self._rng.random() < REPLY_QUOTE_PROBABILITY:
            return str(getattr(latest, "message_id", "") or "")
        return ""

    def _quote_distance(self, session: str, reply_id: Any) -> int:
        """引用目标距最新消息的条数：user 行 meta.message_id（到达时刻写入）
        在历史里的定位，取其后行数（0=引用的就是最新一条）。

        - 查无此行（超出回看窗口/重启前的旧消息）→ 视为久远（窗口+1）；
        - 历史库缺席 → 0（克制侧：不引用）。
        """
        history = getattr(self.ctx, "history", None)
        if history is None:
            return 0
        records = history.get_records(session, limit=QUOTE_LOOKBACK_ROWS)
        target = str(reply_id)
        for index, record in enumerate(records):
            meta = record.get("meta") or {}
            if str(meta.get("message_id") or "") == target:
                return len(records) - 1 - index
        return QUOTE_LOOKBACK_ROWS + 1

    def _maybe_fire_message_milestone(self, mood_state: Any,
                                      arrived_ts: float) -> None:
        """消息里程碑庆祝：mood 计数恰过 100/500/1000/5000 → 主动消息庆祝。

        - 幂等：<data_dir>/message_milestones.json 记当日已发档
          （{"date","fired"}，跨日重置）；判定与落账同步完成，同日绝不重发；
        - 发送走 proactive.generate_and_send(template_override=...)（完整
          主动消息链路），spawn 独立 task，不阻塞到达串行通道。
        """
        try:
            total = int(getattr(mood_state, "total_messages", 0) or 0)
        except (TypeError, ValueError):
            return
        if total not in anniversary_mod.MESSAGE_COUNT_MILESTONES:
            return
        data_dir = self._data_dir()
        if data_dir is None:
            return
        path = data_dir / MILESTONE_STATE_FILENAME
        today = datetime.fromtimestamp(arrived_ts).strftime("%Y-%m-%d")
        state = _load_json_file(path)
        if state.get("date") != today:
            state = {"date": today, "fired": []}
        fired = state.setdefault("fired", [])
        if not isinstance(fired, list):
            fired = []
            state["fired"] = fired
        if str(total) in fired:
            return
        fired.append(str(total))
        _save_json_file(path, state)
        prompt = anniversary_mod.message_milestone_prompt(total)
        proactive = getattr(self.ctx, "proactive", None)
        if prompt is None or proactive is None:
            return
        self._spawn_task(self._fire_milestone(proactive, prompt, total))

    async def _fire_milestone(self, proactive: Any, prompt: str,
                              total: int) -> None:
        """里程碑主动消息的异步触发（异常只记日志，不影响消息循环）。"""
        try:
            text = await proactive.generate_and_send(template_override=prompt)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[pipeline] 消息里程碑（%d 条）主动消息触发失败", total)
            return
        if text:
            logger.info("[pipeline] 消息里程碑：累计 %d 条消息，主动庆祝已发送。", total)

    async def _deliver_text_segment(self, bridge: Any, job: _SendJob,
                                    index: int) -> Any:
        """一段文字的实际出帧：群聊走群发，私聊保持原路径。

        - 群聊（job.group_id 非空）：send_group_msg 一条消息出帧，首段前置
          reply 段（回应对最新消息）与 @发送者 段，face 段随段尾附加；
          桥过旧缺 send_group_msg 时降级私发（不致命）；
        - 私聊：无 face 标记且无 reply 前置 → send_private_text 原路径
          （零变化）；有则改走 send_private_segments 数组段
          （[reply 段] + text 段 + face 段），旧桥缺该方法时降级纯文本。
        """
        segment = job.segments[index]
        face_ids = (list(job.face_ids[index])
                    if job.face_ids and index < len(job.face_ids) else [])
        first = index == 0
        want_reply = first and str(job.reply_to_message_id or "") not in ("", "-")
        if job.group_id:
            segs: List[dict] = []
            if first:
                if want_reply:
                    segs.append({"type": "reply",
                                 "data": {"id": str(job.reply_to_message_id)}})
                segs.append({"type": "at", "data": {"qq": str(job.user_id)}})
            if segment:
                segs.append({"type": "text", "data": {"text": segment}})
            segs.extend({"type": "face", "data": {"id": str(fid)}}
                        for fid in face_ids)
            send_group = getattr(bridge, "send_group_msg", None)
            if callable(send_group):
                return await send_group(job.group_id, segs)
            logger.warning("[pipeline] 桥无 send_group_msg，群聊文本降级私发")
            return await bridge.send_private_text(job.user_id, segment)
        if not face_ids and not want_reply:
            return await bridge.send_private_text(job.user_id, segment)
        segs = []
        if want_reply:
            segs.append({"type": "reply",
                         "data": {"id": str(job.reply_to_message_id)}})
        if segment:
            segs.append({"type": "text", "data": {"text": segment}})
        segs.extend({"type": "face", "data": {"id": str(fid)}}
                    for fid in face_ids)
        send_segs = getattr(bridge, "send_private_segments", None)
        if callable(send_segs):
            return await send_segs(job.user_id, segs)
        return await bridge.send_private_text(job.user_id, segment)

    async def _send_image_b64(self, bridge: Any, job: _SendJob) -> bool:
        """图片通道（meme/自拍共用）：群聊走 send_group_msg 群发，私聊走
        现有 send_image_b64；群聊桥过旧降级私发。返回真实送达结果。"""
        if job.group_id:
            send_group = getattr(bridge, "send_group_msg", None)
            if callable(send_group):
                result = await send_group(job.group_id, [
                    {"type": "image",
                     "data": {"file": f"base64://{job.image_b64}"}}])
                return bool(getattr(result, "ok", True))
            logger.warning("[pipeline] 桥无 send_group_msg，群聊图片降级私发")
        result = await bridge.send_image_b64(job.user_id, job.image_b64)
        return bool(getattr(result, "ok", True))

    # ------------------------------------------------------------------
    # 轮内取数 helpers（与 __main__ 主管线同口径）
    # ------------------------------------------------------------------

    def _history_read_limit(self) -> int:
        """按 max_context_tokens 反推历史读取条数上限（R2，防全量读）。

        实证：vl.log 里 158 条历史 ≈ 6201 token（约 39 token/条）——每条取
        32 token 的保守低估值，多读 ~20% 余量交给 persona 截断兜底：
        limit = clamp(预算 token // 32, 64, 2000)。32768 预算 → ~680 条；
        超大预算（256k）也被硬上限 2000 条摁住——旧代码 get_records 不带
        limit 时全表读（实测 5 万行 105ms，且随历史无限增长）。
        """
        try:
            budget = (int(self.ctx.cfg.context.max_context_tokens)
                      * float(self.ctx.cfg.context.truncate_ratio))
        except (AttributeError, TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            return HISTORY_READ_LIMIT_MIN
        limit = int(budget // HISTORY_EST_TOKENS_PER_MESSAGE)
        return max(HISTORY_READ_LIMIT_MIN, min(limit, HISTORY_READ_LIMIT_MAX))

    def _read_history(self, session: str,
                      exclude_ids: "set[int] | None" = None) -> List[dict]:
        """读会话历史并排除指定行 id（本轮消息的 user 行走 user_text 注入）。

        R2：带 limit 读取（_history_read_limit 反推）——「最近 N 条、升序」
        语义由 HistoryStore 保证，装配管道与截断口径零改动。
        """
        history = getattr(self.ctx, "history", None)
        if history is None:
            return []
        try:
            records = history.get_records(session, limit=self._history_read_limit())
        except Exception:  # noqa: BLE001 —— 历史库故障按空历史继续
            logger.exception("历史读取失败（本轮按空历史装配）")
            return []
        if exclude_ids:
            records = [r for r in records if r.get("id") not in exclude_ids]
        return [{"role": r["role"], "content": r["content"]} for r in records]

    @staticmethod
    def _build_recall_query(text: str, history_msgs: List[dict],
                            max_chars: int) -> str:
        """构造记忆检索查询：最近 1-2 条历史 + 本轮原文（PERF_SPEC §3-O5）。"""
        parts: List[str] = []
        for msg in history_msgs[-_RECALL_HISTORY_MESSAGES:]:
            line = content_to_text(msg.get("content")).strip()
            if line:
                parts.append(line)
        parts.append((text or "").strip())
        query = "\n".join(part for part in parts if part)
        return query[:max(0, int(max_chars))]

    async def _recall(self, session: str, text: str,
                      history_msgs: List[dict]) -> List[Any]:
        """记忆检索（注入开关；失败降级为不注入，对话继续）。"""
        cfg = self.ctx.cfg
        store = getattr(self.ctx, "memory_store", None)
        if store is None or not cfg.mnemosyne.inject_enabled:
            return []
        try:
            recall_query = self._build_recall_query(
                text, history_msgs, cfg.mnemosyne.max_prompt_chars)
            return await store.search(
                session,
                recall_query,
                top_k=cfg.mnemosyne.top_k,
                candidates=cfg.mnemosyne.candidate_k,
                min_cosine=cfg.mnemosyne.min_similarity,
            )
        except Exception as exc:  # noqa: BLE001 —— 封锁态/未接线都降级
            logger.warning("记忆检索失败，本轮不注入（%s）", exc)
            return []

    async def _search_block(self, msgs: List[_PendingMsg]) -> str:
        """联网搜索（多模态③，brain/websearch.py）：意图命中 → 检索 → 资料引用块。

        - 触发：本轮（自最新向前）任一条消息含搜索意图关键词（搜|查|搜索|
          最新|新闻|今天.*什么|now|latest|news）——查询词取自该条消息
          （打断合并场景下她回应的是最新一条，检索本体也应以它为准）；
        - 开关：[tools].web_search_enabled=false 时整条短路（零行为变化）；
        - 降级：检索失败/零结果一律空串不注入（maybe_search_block 内部兜底），
          这里的 try 是双保险——搜索故障绝不拖垮 LLM 轮。
        """
        try:
            tools_cfg = getattr(self.ctx.cfg, "tools", None)
            if tools_cfg is None or not getattr(tools_cfg, "web_search_enabled", False):
                return ""
            for msg in reversed(msgs):
                if websearch_mod.has_search_intent(msg.text):
                    return await websearch_mod.maybe_search_block(
                        msg.text, tools_cfg)
            return ""
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 双保险：搜索故障不影响本轮对话
            logger.exception("[websearch] 搜索资料块构造异常（本轮不注入）")
            return ""

    def _mood_view(self, user_id: str) -> "tuple[str, Any]":
        """mood 注入片段 + 语音映射快照（与 __main__ 主管线同一数学）。"""
        mood_engine = getattr(self.ctx, "mood_engine", None)
        mood_fragment = ""
        mood_snapshot = None
        if mood_engine is not None:
            try:
                now_ts_mood = self._now()
                mood_state = mood_engine.snapshot_state(user_id)
                if mood_engine.inject_enabled:
                    mood_fragment = mood_mod.render_system_fragment(
                        mood_state, now=now_ts_mood)
                mood_snapshot = mood_mod.effective_mood(mood_state, now=now_ts_mood)
            except Exception:  # noqa: BLE001
                logger.exception("mood 快照渲染失败（本轮不注入）")
        return mood_fragment, mood_snapshot

    def _schedule_line(self) -> str:
        """「她此刻」作息行（soul/schedule.render_line；开关关/无课表/异常=空串）。"""
        sched = getattr(self.ctx.cfg, "schedule", None)
        if sched is None:
            return ""
        try:
            return schedule_mod.render_line(
                self._now(), sched.entries, enabled=sched.enabled) or ""
        except Exception:  # noqa: BLE001 —— 课表渲染失败不注入，聊天继续
            logger.exception("[schedule] 作息行渲染异常（本轮不注入）")
            return ""

    def _meme_plan(self, reply_text: str):
        """meme &&tag&& 后处理（白名单 + 命中率 + 残缺清理；开关关=原样）。"""
        cfg = self.ctx.cfg
        memes_cfg = cfg.memes
        if memes_cfg.enable:
            return memes_mod.process_reply(
                reply_text,
                memes_dir=Path(self.ctx.repo_root)
                / (memes_cfg.memes_dir or _MEME_DIR_DEFAULT),
                whitelist=memes_mod.build_whitelist(memes_cfg.extra_tags),
                probability=memes_cfg.probability,
                max_images=memes_cfg.max_images,
                rng=random.Random(),
            )
        return memes_mod.MemePlan(text=reply_text, image_path=None)

    def _archive_assistant(self, session: str, assistant_text: str) -> None:
        """assistant 行落库（user 行已在到达时刻落库；写失败只告警）。"""
        history = getattr(self.ctx, "history", None)
        if history is None or not assistant_text:
            return
        try:
            history.append(session, "assistant", assistant_text)
        except Exception:  # noqa: BLE001
            logger.exception("历史写入失败（对话继续）")

    async def _count_assistant(self, session: str, assistant_text: str) -> None:
        """assistant 侧记忆计数（user 侧已在到达时计数）。"""
        memory_service = getattr(self.ctx, "memory_service", None)
        if memory_service is None or not assistant_text:
            return
        try:
            await memory_service.on_message(session, "assistant", assistant_text)
        except Exception:  # noqa: BLE001 —— 计数失败不阻塞消息循环
            logger.exception("记忆计数处理失败")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """收尾：取消通道/发送/在途轮任务并清空队列（幂等）。"""
        self._closed = True
        tasks: "List[asyncio.Task]" = []
        for channel in list(self._channels.values()):
            tasks.append(channel.task)
        for st in list(self._users.values()):
            if st.sender_task is not None:
                tasks.append(st.sender_task)
            if st.round_task is not None and not st.round_task.done():
                tasks.append(st.round_task)
            st.pending.clear()
            st.round_msgs.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 —— 收尾从宽
                pass
        self._channels.clear()
        self._users.clear()


class _ArrivalChannel:
    """per-session 串行通道（O4 保序条款：同会话 history 写入与 memory
    on_message 走串行通道——到达语义逐条按到达序执行）。

    懒创建（voice/manager.py 手法）：首个消息到达时由 on_message 建立；
    worker 单任务常驻，逐条处理，单条异常不杀死通道。
    """

    def __init__(self, pipeline: MessagePipeline, session_id: str) -> None:
        self.pipeline = pipeline
        self.session_id = session_id
        self._queue: "asyncio.Queue[_Arrival]" = asyncio.Queue()
        self.task = asyncio.ensure_future(self._run())
        logger.debug("[pipeline] 会话 %s… 的到达串行通道已启动", session_id[:6])

    def submit(self, item: _Arrival) -> None:
        self._queue.put_nowait(item)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self.pipeline._process_arrival(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 单条失败不影响后续
                logger.exception("到达语义处理异常（消息被跳过，连接保持）")


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def setup(ctx: Any) -> Callable[[], None]:
    """构造 O3/O4 管线并接线 EVENT_MESSAGE（实例挂 ctx.message_pipeline）。

    - 时钟：优先取 ctx.now_fn（测试注入），缺省 time.time；
    - 订阅：ctx.bus.on(EVENT_MESSAGE, pipeline.on_message)——EventBus.emit
      语义不动；on_message 只打戳入队立即返回，桥帧路由零阻塞（O4②）；
    - 桥的 per-user 队列分发语义由本管线承接（桥 _event_worker 只做帧路由）；
    - cleanup：同步上下文（事件循环已关闭），任务随循环生灭，这里只清引用。
    """
    from virtuallover.events import EVENT_MESSAGE

    pipeline = MessagePipeline(ctx, now_fn=getattr(ctx, "now_fn", None))
    ctx.bus.on(EVENT_MESSAGE, pipeline.on_message)
    ctx.message_pipeline = pipeline
    logger.info(
        "[pipeline] O3/O4 对话管线已接线：EVENT_MESSAGE → 到达语义串行通道 → "
        "per-user 打断地平线（IDLE/RUNNING/DRIP，窗口 %.2fs）+ 发送 FIFO",
        pipeline.interrupt_window)

    def cleanup() -> None:
        """同步收尾：清引用（任务已随事件循环关闭而终结）。"""
        pipeline._channels.clear()
        pipeline._users.clear()

    return cleanup
