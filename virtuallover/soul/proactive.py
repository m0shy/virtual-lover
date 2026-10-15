# -*- coding: utf-8 -*-
"""主动消息（SPEC §8.3）—— 八条行为逐条实现（clean-room，行为规格全部出自 SPEC）。

五轮修缮第三轮（R3）行为变更（审计实证驱动，逐条见各实现点）：
- **1a 送达判定**：发送回调回传真实送达结果——桥无连接/no-connection 不算已
  送达（不写历史、unanswered 不 +1），失败按 5-10min 重排重试（原实现里
  pipeline 入队即算送达，NapCat 掉线时她沉默 7h 仍记「已送达」）；
- **1b 活跃避让**：on_user_message 清零时按回复时刻重排；_fire 前最近用户
  消息 <5min 则重排（对话进行中不插主动消息）；
- **1c 降频模式**：unanswered 满 3 不再永久沉默——按 6-12h 一条克制版消息
  （prompt_template_slowdown），回复清零即恢复常规节奏；
- **1d 近况注入**：生成 prompt 注入最近 8-12 条对话摘录（复用 diary 抽取
  机制 + 定界符 + 注入防护），并带 begin_dialogs 口吻锚点（setup 装配层）；
- **1e/1f 输出质量**：模板改第一人称情景描述（vl.toml 用户数据侧）+ 输出
  约束句；裸输出过质量门（含 ``(?:`` 碎片或 emoji → 丢弃重生成一次），
  清洗兜底走 soul/textclean.clean_proactive_text；
- **1g 日志降噪**：暂停（降频）状态日志只在状态变化时打一条；
- **1h 来源标记**：写历史带 meta.source=proactive（diary/memory 摘录侧可据此降权）。

主动陪伴升级（R10）行为变更：
- **沉默递进三档**：距上次用户消息 <worried_after_hours(24h) 维持现状；
  ≥24h 改用 prompt_template_worried（{{last_user_gap}} 人话渲染）且间隔
  放宽到 12-24h；≥give_up_after_hours(72h) 追告别语气发最后一条后置
  gave_up_date 停止排程，用户回来（on_user_message）才恢复；
- **真实计数**：unanswered 不再封顶 max_unanswered，{{unanswered_count}}
  展示真实值（暂停/降频判定阈值不变）；
- **深度素材**：_llm_chat 的 prompt 追加 (a) topics.pick_topic 当天一题
  （[proactive].daily_topic 开关）、(b) soul/schedule.render_line 当前状态
  行（模块缺席跳过）、(c) memory_store 检索 top-2 相关回忆（缺席失败跳过）；
- **表情包通道**：发送路径统一过 memes 后处理（&&tag&& 剥除 + 白名单取图，
  与主回复管线同一语义；&&morning&&/&&sleep&& 等标记不再漏发给用户）。

八条行为与本模块实现的对应关系（SPEC §8.3 行号级规格）：
- ①30-120min 随机间隔且 next_trigger_time 持久化重启恢复
    → ``schedule_next``（uniform[min,max]）+ ``save_state``/``_load_state``
      （local/vl_data/proactive_state.json，重启读回 next_trigger_ts / unanswered）；
- ②安静时段 23-7 跳过重排（跨午夜语义）
    → ``is_quiet``（quiet_start>quiet_end 跨午夜：hour>=23 or hour<7 视为安静）
      + tick 到点时安静→不发送、重新 schedule_next（"跳过重排"）；
- ③unanswered>=3 暂停、回复清零
    → ``tick`` 到点但 unanswered 满额时保持暂停（不触发不重排）；
      ``on_user_message`` 把 unanswered 清零并恢复调度；
- ④启动 30min 无消息触发一次
    → ``arm_boot_trigger``（启动武装 boot_deadline=now+30min）+ tick 到点且
      启动以来无任何用户消息 → 触发一次主动消息（仅一次，触发后解除武装）；
- ⑤prompt 模板 {{current_time}}/{{unanswered_count}} 替换
    → ``render_prompt``（模板文本=用户数据，从旧配置迁移，由 config.py 带入）；
- ⑥LLM 生成中来新消息丢弃本次
    → ``generate_and_send`` 生成前记 _msg_seq 快照，生成后若有新用户消息
      （on_user_message 递增序列号）→ 丢弃本次生成结果、只重排下次；
- ⑦发送后写回历史
    → 发送成功后调用注入的 ``append_history("assistant", text)``（集成期接
      brain/history.py，主动消息与普通回复同库同格式）；
- ⑧TTS 优先（成功只发语音，dual_output=false）
    → tts_enabled 时先调注入的 ``tts(text)``；成功且 not always_send_text →
      只发语音（send_voice）；TTS 失败/未启用 → 回退发文字（send_text）。
      VOICE_SPEC §2 起该双轨决策可整体迁移到 VoiceManager：注入 ``dual_send``
      （合成→成功只语音/失败回退文字，返回是否有任一发送成功）后整段委托；
      未注入时保留原内联路径（直连 tts/send_voice/send_text 回调，最小装配与
      测试用），两条路径的 dual_output 语义逐字一致。

依赖注入：llm_chat / tts / send_voice / send_text / append_history 全部由装配层
传入（集成期分别接 brain.llm、voice.gptsovits、bridge 发送面、brain.history），
本模块不感知网络与存储实现；时钟（now 参数）与 rng 可注入，宿主机 pytest 直测。
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
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .textclean import clean_proactive_text, has_llm_artifacts

logger = logging.getLogger("virtuallover.soul.proactive")

# 状态持久化文件（新栈自有数据；next_trigger_time 重启恢复 SPEC §8.3-①）
DEFAULT_STATE_PATH = "local/vl_data/proactive_state.json"

# 安静时段默认值（实测旧配置 quiet_hours="23-7"）
DEFAULT_QUIET_START = 23
DEFAULT_QUIET_END = 7

# 发送 FIFO 回执等待上限（秒）：覆盖「数段 × 3.5s 段间隔 + 限流 stall」的最坏
# 情况；超时按未送达处理（1a 的保守侧——宁重发不假记）
SEND_RESULT_TIMEOUT_SECONDS = 120.0


def parse_quiet_hours(raw: str | None) -> tuple[int, int]:
    """解析 "23-7" 形式的安静时段为 (start_hour, end_hour)；非法回退 (23, 7)。

    跨午夜语义：start > end（如 23-7）表示 hour>=start 或 hour<end 为安静。
    """
    if isinstance(raw, str) and raw.strip():
        parts = raw.strip().split("-")
        if len(parts) == 2:
            try:
                start, end = int(parts[0]), int(parts[1])
                if 0 <= start <= 23 and 0 <= end <= 23:
                    return start, end
            except ValueError:
                pass
    logger.warning("quiet_hours 配置非法（%r），回退默认 23-7。", raw)
    return DEFAULT_QUIET_START, DEFAULT_QUIET_END


def is_quiet(dt: datetime, quiet_start: int = DEFAULT_QUIET_START,
             quiet_end: int = DEFAULT_QUIET_END) -> bool:
    """判断时刻是否处于安静时段（跨午夜语义，SPEC §8.3-②）。

    - start > end（如 23-7）：hour >= start 或 hour < end → 安静；
    - start < end：start <= hour < end → 安静；
    - start == end：不设安静时段。
    """
    hour = dt.hour
    if quiet_start > quiet_end:
        return hour >= quiet_start or hour < quiet_end
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    return False


def humanize_gap(seconds: float) -> str:
    """把沉默时长换算成人话（R10 {{last_user_gap}} 的渲染口径）。

    分档：<1h → 「不到一小时」；1-23h → 「N 小时」（如「6 小时」）；
    1-3 天 → 「一天/两天/三天」；>3 天 → 「N 天」（如「7 天」）。
    """
    seconds = max(0.0, float(seconds))
    if seconds < 3600.0:
        return "不到一小时"
    hours = seconds / 3600.0
    if hours < 24.0:
        return f"{int(hours)} 小时"
    days = int(seconds // 86400.0)
    if days == 1:
        return "一天"
    if days == 2:
        return "两天"
    if days == 3:
        return "三天"
    return f"{days} 天"


def render_prompt(
    template: str,
    now: datetime,
    unanswered_count: int,
    *,
    recent_context: str = "",
    last_user_gap: str = "",
) -> str:
    """替换主动消息模板占位符（SPEC §8.3-⑤ + R3-1d 近况注入 + R10 沉默时长）。

    模板文本是用户数据（从旧配置迁移），本函数只负责占位符替换：
    - {{current_time}} → %Y-%m-%d %H:%M；
    - {{unanswered_count}} → 当前未回复计数（int）；
    - {{last_user_gap}} → 距上次用户消息的人话时长（humanize_gap 产物；
      R10 沉默递进的担心型模板用）；
    - {{recent_context}} → 近况素材块（1d；模板没写该占位符且素材非空时
      追加在末尾，老模板也受益）。
    """
    has_placeholder = "{{recent_context}}" in template
    out = (
        template
        .replace("{{current_time}}", now.strftime("%Y-%m-%d %H:%M"))
        .replace("{{unanswered_count}}", str(int(unanswered_count)))
        .replace("{{last_user_gap}}", str(last_user_gap or ""))
        .replace("{{recent_context}}", recent_context)
    )
    if recent_context and not has_placeholder:
        out = out.rstrip() + "\n\n" + recent_context
    return out


@dataclass
class ProactiveParams:
    """主动消息参数面（字段名与 config.ProactiveConfig 对齐，便于装配直传）。

    默认值全部取自现网实测（astrbot_plugin_proactive_chat_config.json 的
    friend_settings.schedule_settings / tts_settings / auto_trigger_settings）。
    """

    min_interval_minutes: int = 30      # ①随机间隔下界（实测 30）
    max_interval_minutes: int = 120     # ①随机间隔上界（实测 120）
    quiet_hours: str = "23-7"           # ②安静时段（实测 23-7）
    max_unanswered: int = 3             # ③unanswered>=3 暂停（实测 3）
    boot_trigger_minutes: int = 30      # ④启动 30min 无消息触发一次（实测 30）
    prompt_template: str = ""           # ⑤模板=用户数据，从旧配置迁入
    tts_enabled: bool = True            # ⑧TTS 优先（实测 enable_tts=true）
    always_send_text: bool = False      # ⑧成功只发语音（实测 always_send_text=false）
    # ---- 五轮修缮 R3 ----
    recent_user_window_minutes: float = 5.0  # 1b：_fire 前近期活跃窗口（分钟）
    retry_min_minutes: int = 5              # 1a：发送未送达的重排下界（分钟）
    retry_max_minutes: int = 10             # 1a：发送未送达的重排上界（分钟）
    slowdown_min_minutes: int = 360         # 1c：降频模式间隔下界（6h）
    slowdown_max_minutes: int = 720         # 1c：降频模式间隔上界（12h）
    prompt_template_slowdown: str = ""      # 1c：克制版模板（空=回退常规模板）
    # ---- R10 主动陪伴升级（沉默递进三档）----
    prompt_template_worried: str = ""       # ≥worried_after_hours 的担心型模板（含 {{last_user_gap}}）
    worried_after_hours: float = 24.0       # 沉默 ≥ 该小时数进入担心档（间隔放宽）
    give_up_after_hours: float = 72.0       # 沉默 ≥ 该小时数发最后一条告别后停止排程
    worried_min_minutes: int = 720          # 担心档间隔下界（12h）
    worried_max_minutes: int = 1440         # 担心档间隔上界（24h）


# R10：give_up 档的最后一条消息在担心模板上追的告别语气指令（prompt 侧
# 指令文本，非用户数据；「停止排程」语义由发送成功后置 gave_up_date 承载）
GIVE_UP_PROMPT_SUFFIX = (
    "\n\n另外：这已经是你很长一段时间里最后一条主动消息了——这次之后，接下来"
    "几天不要再主动找他，等他自己回来。好好道个别，语气温柔不怨怼，让他知道"
    "无论多久你都在。"
)


@dataclass
class ProactiveState:
    """可持久化的运行状态（SPEC §8.3-①：next_trigger_time 持久化重启恢复）。"""

    next_trigger_ts: float = 0.0        # 下次主动触发的时间戳（秒）
    unanswered: int = 0                 # 主动找过他但没回复的次数
    last_user_ts: float = 0.0           # R10：最近一次收到用户消息的时刻（秒；0=从未）
    gave_up_date: str = ""              # R10：放弃期开始日（YYYY-MM-DD；空=未放弃）

    def to_dict(self) -> dict:
        return {
            "next_trigger_ts": float(self.next_trigger_ts),
            "unanswered": int(self.unanswered),
            "last_user_ts": float(self.last_user_ts),
            "gave_up_date": str(self.gave_up_date),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ProactiveState":
        data = data or {}
        try:
            next_ts = float(data.get("next_trigger_ts", 0.0))
        except (TypeError, ValueError):
            next_ts = 0.0
        try:
            unanswered = int(data.get("unanswered", 0))
        except (TypeError, ValueError):
            unanswered = 0
        try:
            last_user = float(data.get("last_user_ts", 0.0))
        except (TypeError, ValueError):
            last_user = 0.0
        gave_up = str(data.get("gave_up_date", "") or "")
        return cls(
            next_trigger_ts=max(0.0, next_ts),
            unanswered=max(0, unanswered),
            last_user_ts=max(0.0, last_user),
            gave_up_date=gave_up,
        )


def _accepts_meta_arg(callback: "Callable[..., Any] | None") -> bool:
    """探测写历史回调是否接受第 3 个 meta 位置参数（R3-1h）。

    旧 2 参回调（早期测试桩/外部集成）自动降级直调；带 **kwargs 或 ≥3 个位置
    形参（bound method 已不含 self）按新契约传 meta。不可内省时按新契约调用。
    """
    if callback is None:
        return False
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        return True
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    positional = sum(
        1 for param in sig.parameters.values()
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    return positional >= 3


class ProactiveService:
    """主动消息服务：调度 + 暂停判定 + 生成 + TTS 优先发送 + 历史写回。

    回调注入约定（均为异步；集成期由 __main__ 装配）：
    - llm_chat(prompt) -> str | None：独立 LLM 请求（模板已含情景，不复用对话上下文）；
    - tts(text) -> bytes | None：语音合成（voice.gptsovits）；
    - send_voice(data) / send_text(text) -> bool：bridge 发送面，**返回真实送达
      结果**（R3-1a：桥无连接/发送失败必须返回 False，绝不把入队当送达）；
    - append_history(role, content, meta=None)：brain/history.py 写回
      （SPEC §8.3-⑦；meta 带 {"source": "proactive"}，R3-1h——兼容旧 2 参
      回调，按签名探测自动降级）；
    - recent_context_fn() -> str：近况素材块（R3-1d，setup 装配层接 history，
      复用 diary 抽取机制；缺省/异常=不带近况生成）。
    """

    def __init__(
        self,
        params: ProactiveParams | None = None,
        *,
        session_id: str = "",
        state_path: str | Path = DEFAULT_STATE_PATH,
        llm_chat: Callable[[str], Awaitable[str | None]] | None = None,
        tts: Callable[[str], Awaitable[bytes | None]] | None = None,
        send_voice: Callable[[bytes], Awaitable[bool]] | None = None,
        send_text: Callable[[str], Awaitable[bool]] | None = None,
        append_history: "Callable[..., Awaitable[None]] | None" = None,
        dual_send: "Callable[[str], Awaitable[bool]] | None" = None,
        recent_context_fn: "Callable[[], Awaitable[str]] | None" = None,
        process_memes: "Callable[[str], Any] | None" = None,
        send_image: "Callable[[Any], Awaitable[bool]] | None" = None,
        rng: random.Random | None = None,
    ) -> None:
        self.params = params or ProactiveParams()
        self.session_id = session_id
        self.state_path = Path(state_path)
        self.llm_chat = llm_chat
        self.tts = tts
        self.send_voice = send_voice
        self.send_text = send_text
        self.append_history = append_history
        self.dual_send = dual_send
        self.recent_context_fn = recent_context_fn
        # R10：&&tag&& 表情包后处理（返回 memes.MemePlan 形态：text/image_path）
        # 与独立图片消息发送回调；未注入时标记在 clean 阶段已剥不掉（提示词
        # 约定），装配层必须注入 strip 兜底防标记漏发
        self.process_memes = process_memes
        self.send_image = send_image
        self.rng = rng or random.Random()

        self.quiet_start, self.quiet_end = parse_quiet_hours(self.params.quiet_hours)
        self.state = ProactiveState()
        self._load_state()

        # ⑥生成期新消息检测：任何用户消息到达都递增序列号
        self._msg_seq: int = 0
        # ④启动触发武装：start() 时设置 boot_deadline，启动以来无消息到点触发一次
        self._boot_deadline_ts: float = 0.0
        self._boot_armed: bool = False
        self._boot_seen_message: bool = False
        self._boot_fired: bool = False
        # 复入保护：同一时刻只允许一个生成流程在跑
        self._generating: bool = False
        # R3-1b：最近一条用户消息时刻（_fire 前的活跃避让判定用）
        self._last_user_ts: float = 0.0
        # R3-1g：暂停（降频）状态只在变化时打日志
        self._pause_announced: bool = False
        # R3-1h：写历史回调是否接受 meta 形参（旧 2 参回调签名探测，一次定形）
        self._append_history_takes_meta = _accepts_meta_arg(append_history)

    @property
    def _recent_user_window_seconds(self) -> float:
        """1b 活跃避让窗口（秒）。"""
        return max(0.0, float(self.params.recent_user_window_minutes)) * 60.0

    # ------------------------------------------------------------------
    # 状态持久化（①next_trigger_time 重启恢复）
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """启动时读回持久化状态；缺失/损坏从零开始（不阻塞启动）。"""
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.state = ProactiveState.from_dict(data if isinstance(data, dict) else None)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("主动消息状态文件读取失败，从零开始: %s", exc)
            self.state = ProactiveState()

    def save_state(self) -> None:
        """把 next_trigger_ts / unanswered 原子落盘（OSError 只告警不抛）。"""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.state.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.error("主动消息状态写入失败: %s", exc)

    # ------------------------------------------------------------------
    # 调度（①②③④）
    # ------------------------------------------------------------------

    def _silence_tier(self, now_ts: float) -> str:
        """R10 沉默分档：距上次用户消息 <24h 常规 / ≥worried 担心 / ≥give_up 放弃。

        last_user_ts 从未记录（0，全新安装）按常规档——「没聊过天」不等于
        「失联」，不该一开机就担心或放弃。
        """
        if self.state.last_user_ts <= 0:
            return "normal"
        gap_hours = (float(now_ts) - self.state.last_user_ts) / 3600.0
        worried_h = max(0.0, float(self.params.worried_after_hours))
        give_up_h = max(0.0, float(self.params.give_up_after_hours))
        if gap_hours >= max(worried_h, give_up_h):
            return "giveup"
        if worried_h > 0 and gap_hours >= worried_h:
            return "worried"
        return "normal"

    def schedule_next(self, now_ts: float, *, immediate_check: bool = True) -> float:
        """按分档随机间隔排定下次触发时刻并持久化，返回该时间戳。

        now_ts 之后 uniform[min,max] 分钟；持久化供重启恢复。
        R10 分档：担心档（沉默 ≥worried_after_hours）放宽到 12-24h 一条；
        R3-1c：暂停态（unanswered 满 3）仍走降频区间 uniform[slowdown_min,
        slowdown_max]（6-12h）——担心档优先于降频（沉默更久节奏更慢）。
        """
        tier = self._silence_tier(now_ts)
        if tier == "worried":
            lo = max(1, int(self.params.worried_min_minutes))
            hi = max(lo, int(self.params.worried_max_minutes))
            mode = "担心档"
        elif self.paused:
            lo = max(1, int(self.params.slowdown_min_minutes))
            hi = max(lo, int(self.params.slowdown_max_minutes))
            mode = "降频"
        else:
            lo = max(0, int(self.params.min_interval_minutes))
            hi = max(lo, int(self.params.max_interval_minutes))
            mode = "常规"
        minutes = self.rng.uniform(lo, hi)
        next_ts = now_ts + minutes * 60.0
        self.state.next_trigger_ts = next_ts
        self.save_state()
        logger.info("下次主动消息调度（%s）：%.0f 分钟后（区间 [%d, %d] 分钟）。",
                    mode, minutes, lo, hi)
        return next_ts

    def schedule_retry(self, now_ts: float) -> float:
        """R3-1a 发送未送达的重试节奏：uniform[retry_min, retry_max]（默认 5-10min）。

        与常规间隔（30-120min）分开——发送失败是瞬时故障（NapCat 掉线/限流），
        等满常规间隔会让这条「想说的话」无声消失。
        """
        lo = max(1, int(self.params.retry_min_minutes))
        hi = max(lo, int(self.params.retry_max_minutes))
        minutes = self.rng.uniform(lo, hi)
        next_ts = now_ts + minutes * 60.0
        self.state.next_trigger_ts = next_ts
        self.save_state()
        logger.info("主动消息未送达：%.1f 分钟后重试（区间 [%d, %d] 分钟）。",
                    minutes, lo, hi)
        return next_ts

    @property
    def paused(self) -> bool:
        """③unanswered 达到上限即暂停（直到回复清零）。"""
        return self.state.unanswered >= max(0, int(self.params.max_unanswered))

    def arm_boot_trigger(self, now_ts: float) -> None:
        """④启动武装：boot_trigger_minutes 内无用户消息则到点触发一次。"""
        self._boot_deadline_ts = now_ts + max(0.0, float(self.params.boot_trigger_minutes)) * 60.0
        self._boot_armed = True
        self._boot_seen_message = False
        self._boot_fired = False

    def on_user_message(self, now_ts: float | None = None) -> None:
        """收到目标用户消息：③回复清零恢复调度；④解除启动武装；⑥递增消息序列。

        R3-1b：清零时按回复时刻重排下次主动消息（旧间隔作废——他刚回话，
        下次主动找他该从现在起算，而不是从上次发送起算）；同时记录
        _last_user_ts 供 _fire 前的活跃避让判定。
        R10：last_user_ts 持久化更新（沉默分档的基准）；gave_up_date 清除
        ——放弃期的「等他回来」由此恢复调度。
        """
        now_ts = time.time() if now_ts is None else float(now_ts)
        self._msg_seq += 1
        self._boot_seen_message = True
        self._last_user_ts = now_ts
        self.state.last_user_ts = now_ts
        resumed = False
        if self.state.unanswered:
            self.state.unanswered = 0
            resumed = True
            logger.info("用户已回复，主动消息 unanswered 计数清零，已按回复时刻重排调度。")
        if self.state.gave_up_date:
            self.state.gave_up_date = ""
            resumed = True
            logger.info("他回来了（收到新消息）：主动消息放弃期解除，恢复调度。")
        if resumed:
            self.save_state()
            self._announce_pause()
            self.schedule_next(now_ts)
        else:
            self.save_state()  # last_user_ts 持久化（重启后沉默分档不丢）

    def _announce_pause(self) -> None:
        """R3-1g：暂停（降频）状态只在状态变化时打一条日志。

        tick 每分钟驱动一次，逐分钟重复「已达上限已暂停」是纯噪声。
        """
        if self.paused and not self._pause_announced:
            self._pause_announced = True
            logger.info(
                "unanswered 已达 %d，主动消息进入降频模式（%g-%g 小时一条克制版消息，回复即恢复）。",
                self.state.unanswered,
                max(1, int(self.params.slowdown_min_minutes)) / 60.0,
                max(max(1, int(self.params.slowdown_min_minutes)),
                    int(self.params.slowdown_max_minutes)) / 60.0,
            )
        elif not self.paused and self._pause_announced:
            self._pause_announced = False
            logger.info("主动消息退出降频模式（用户已回复），恢复常规调度。")

    async def _fire(self, now_ts: float, now_dt: datetime) -> str | None:
        """到点触发一次主动消息（安静/活跃避让/暂停判定在本函数内完成）。"""
        if is_quiet(now_dt, self.quiet_start, self.quiet_end):
            # ②安静时段：跳过本次并重排（跨午夜语义）
            logger.info("当前处于安静时段 %02d-%02d，跳过本次并重排。", self.quiet_start, self.quiet_end)
            self.schedule_next(now_ts)
            return None
        window = self._recent_user_window_seconds
        if window > 0 and self._last_user_ts > 0 and now_ts - self._last_user_ts < window:
            # R3-1b：他刚刚还在说话——对话进行中不插主动消息，重排
            logger.info("最近用户消息在 %.0f 分钟内（活跃避让窗口），本次重排。",
                        window / 60.0)
            self.schedule_next(now_ts)
            return None
        if self.paused:
            # R3-1c：满 3 不再永久沉默——降频模式发一条克制版消息
            return await self.generate_and_send(now_ts=now_ts, now_dt=now_dt, subdued=True)
        return await self.generate_and_send(now_ts=now_ts, now_dt=now_dt)

    async def _recent_context(self) -> str:
        """R3-1d：近况素材块（注入 recent_context_fn 的产物，失败降级为空）。"""
        if self.recent_context_fn is None:
            return ""
        try:
            block = await self.recent_context_fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 素材故障不阻断主动消息
            logger.warning("近况素材读取失败，本次不带近况生成：%s", exc)
            return ""
        return str(block or "").strip()

    async def tick(self, now_ts: float, now_dt: datetime | None = None) -> str | None:
        """调度节拍：到点则尝试触发；返回本次发出的文本（未触发/丢弃为 None）。

        now_dt 为 now_ts 对应的本地时间（安静时段判定用）；None 时自动换算。
        """
        now_dt = now_dt or datetime.fromtimestamp(now_ts)

        # R10 放弃期：gave_up_date 在位（等他回来）一律安静——不触发、不排程
        if self.state.gave_up_date:
            return None

        # ④启动触发：武装到期且启动以来无任何用户消息 → 触发一次
        if (
            self._boot_armed
            and not self._boot_fired
            and not self._boot_seen_message
            and now_ts >= self._boot_deadline_ts
        ):
            self._boot_fired = True
            self._boot_armed = False
            logger.info("启动 %d 分钟无用户消息，触发一次主动消息。",
                        int(self.params.boot_trigger_minutes))
            sent = await self._fire(now_ts, now_dt)
            # ④触发后无论成败都进入正常随机间隔调度（未发送时 _fire 内已重排）
            if sent is not None and self.state.next_trigger_ts <= now_ts:
                self.schedule_next(now_ts)
            return sent

        next_ts = self.state.next_trigger_ts
        if next_ts <= 0:
            # 首次运行无持久化状态：排定下次，不立即触发
            self.schedule_next(now_ts)
            return None
        if now_ts < next_ts:
            return None
        return await self._fire(now_ts, now_dt)

    # ------------------------------------------------------------------
    # 生成与发送（⑤⑥⑦⑧）
    # ------------------------------------------------------------------

    async def generate_and_send(
        self,
        *,
        now_ts: float | None = None,
        now_dt: datetime | None = None,
        subdued: bool = False,
        template_override: "str | None" = None,
    ) -> str | None:
        """生成并发送一条主动消息；返回发出的文本（放弃/失败返回 None）。

        - ⑤模板占位符替换后作为独立 LLM 请求；R3-1d 注入近况素材块；
        - R3-1f 质量门：裸输出含 ``(?:`` 碎片或 emoji → 丢弃重生成一次；
          清洗兜底（soul/textclean.clean_proactive_text）后仍空则放弃；
        - ⑥生成期间到达任何新用户消息 → 丢弃本次结果；
        - ⑧TTS 优先：合成成功且 always_send_text=false 时只发语音；
        - R3-1a 送达判定：语音与文字均未送达 → 不写历史、unanswered 不 +1，
          按 5-10min 重排重试（schedule_retry）；
        - ⑦发送成功后写回历史（meta.source=proactive，R3-1h）并把
          unanswered+1（R10 起不再封顶，展示真实值）、排定下次。

        subdued=True（R3-1c）：降频模式的克制版消息——模板优先用
        prompt_template_slowdown（R10：沉默已进担心档时担心模板优先）。

        template_override（R9）：外部仪式感触发（soul/anniversary.py 里程碑、
        __main__._routine_tick 早晚安）指定完整 prompt 时直用之，优先级高于
        subdued/常规/担心模板选择；同时豁免暂停守卫（纪念日不该因降频惩罚
        而缺席——安静时段/活跃避让/质量门/dual_send/历史写回等其余语义全部
        照旧）。放弃期（gave_up_date）例外：整条通道静默，等他回来。

        R10 沉默递进：沉默 ≥worried_after_hours 用担心型模板（含
        {{last_user_gap}} 人话时长渲染）；≥give_up_after_hours 且尚未告别时，
        在担心模板上追 GIVE_UP_PROMPT_SUFFIX 告别语气发最后一条，发送成功后
        置 state.gave_up_date 并停排程（on_user_message 恢复）。

        R10 表情包：发送前统一过注入的 process_memes（&&tag&& 剥除 + 白名单
        取图，与主回复管线同一语义），文字按剥标记后的正文发送/写历史，
        命中图片经 send_image 追加一条独立图片消息（失败只告警）。
        """
        if self._generating:  # 复入保护
            return None
        self._generating = True
        try:
            now_ts = time.time() if now_ts is None else float(now_ts)
            now_dt = now_dt or datetime.fromtimestamp(now_ts)

            # R10 放弃期（等他回来）：整条主动通道静默（含 template_override
            # ——对失联的人早晚安/纪念日同样是打扰）；on_user_message 恢复
            if self.state.gave_up_date:
                logger.info("主动消息处于放弃期（自 %s，等他回来恢复），本次不发送。",
                            self.state.gave_up_date)
                return None
            if self.paused and not (subdued or template_override):
                # 直驱调用面（测试/外部）的暂停守卫；日志只在状态变化时打（1g）；
                # template_override（纪念日等外部仪式感）豁免——见 docstring
                self._announce_pause()
                self.schedule_next(now_ts)
                return None
            if is_quiet(now_dt, self.quiet_start, self.quiet_end):
                logger.info("安静时段内不生成主动消息。")
                self.schedule_next(now_ts)
                return None
            if self.llm_chat is None:
                logger.error("未注入 llm_chat，无法生成主动消息。")
                self.schedule_next(now_ts)
                return None

            # ⑤模板替换：模板文本为用户数据（旧配置迁移），这里只做占位符替换；
            # 降频模式优先用克制版模板（空则回退常规模板）；R3-1d 注入近况素材；
            # R9 template_override（纪念日/早晚安等外部仪式感）直用外部 prompt；
            # R10 沉默递进：担心档/放弃档用担心型模板，放弃档追告别语气指令
            give_up_now = (
                not template_override
                and self.state.last_user_ts > 0
                and self._silence_tier(now_ts) == "giveup"
            )
            if template_override and template_override.strip():
                template = template_override
            else:
                tier = self._silence_tier(now_ts)
                if tier in ("worried", "giveup") and self.params.prompt_template_worried.strip():
                    template = self.params.prompt_template_worried
                else:
                    template = self.params.prompt_template
                    if subdued and self.params.prompt_template_slowdown.strip():
                        template = self.params.prompt_template_slowdown
                if give_up_now:
                    template = template.rstrip() + GIVE_UP_PROMPT_SUFFIX
            gap_text = ""
            if self.state.last_user_ts > 0:
                gap_text = humanize_gap(now_ts - self.state.last_user_ts)
            prompt = render_prompt(
                template, now_dt, self.state.unanswered,
                recent_context=await self._recent_context(),
                last_user_gap=gap_text,
            )
            seq_before = self._msg_seq  # ⑥生成期新消息检测快照
            raw = str(await self.llm_chat(prompt) or "")
            # R3-1f 质量门：碎片 token/emoji 在场 → 丢弃本次裸输出重生成一次
            if raw.strip() and has_llm_artifacts(raw):
                logger.info("主动消息质量门触发（碎片 token/emoji），丢弃并重生成一次。")
                raw = str(await self.llm_chat(prompt) or "")
            text = clean_proactive_text(raw)
            if not text:
                logger.warning("主动消息清洗后无有效输出，本次放弃。")
                self.schedule_next(now_ts)
                return None
            if self._msg_seq != seq_before:
                # ⑥生成期间来了新消息：丢弃本次生成结果（避免答非所问）
                logger.info("生成期间收到新消息，丢弃本次主动消息。")
                self.schedule_next(now_ts)
                return None

            # R10 表情包后处理（与主回复管线同一 memes 语义）：&&tag&& 剥除 +
            # 白名单取图。文字按剥标记后的正文发送/合成/写历史——标记绝不漏
            # 给用户、绝不进 TTS；未注入 process_memes 时保持原正文（纯单测桩）。
            image_path = None
            if self.process_memes is not None:
                try:
                    plan = self.process_memes(text)
                except Exception:  # noqa: BLE001 —— 表情包故障不阻断消息
                    logger.exception("[proactive] 表情包处理失败，按纯文字发送")
                    plan = None
                if plan is not None:
                    image_path = getattr(plan, "image_path", None)
                    text = str(getattr(plan, "text", "") or "").strip()
            if not text:
                logger.warning("主动消息清洗后无有效输出，本次放弃。")
                self.schedule_next(now_ts)
                return None

            # ⑧TTS 优先：dual_output 双轨（VOICE_SPEC §2 起决策迁移到 VoiceManager）。
            # 注入 dual_send 时整段委托（合成→成功只发语音/失败回退文字，含
            # always_send_text 语义与共享限流，mood 快照由接缝闭包取有效状态）；
            # 未注入时保留原内联路径（直连 tts/send_voice/send_text 回调）——
            # 两条路径的「成功只发语音、失败回退文字」语义逐字一致。
            sent = False
            if self.params.tts_enabled and self.dual_send is not None:
                sent = bool(await self.dual_send(text))
            else:
                if self.params.tts_enabled and self.tts is not None:
                    try:
                        audio = await self.tts(text)
                    except Exception as exc:
                        logger.warning("TTS 合成异常，回退纯文字: %s", exc)
                        audio = None
                    if audio and self.send_voice is not None:
                        sent = bool(await self.send_voice(audio))
                need_text = (not sent) or self.params.always_send_text
                if need_text and self.send_text is not None:
                    sent = bool(await self.send_text(text)) or sent
            if not sent:
                # R3-1a 送达判定：她一个字都没说出去——不写历史、不计 unanswered，
                # 5-10 分钟后重排重试（而非等满常规间隔让这句话无声消失）
                self.schedule_retry(now_ts)
                return None

            # ⑦发送后写回历史（与普通回复同库同格式；R3-1h 带 meta.source=proactive）
            if self.append_history is not None:
                try:
                    if self._append_history_takes_meta:
                        await self.append_history("assistant", text, {"source": "proactive"})
                    else:
                        await self.append_history("assistant", text)
                except Exception as exc:
                    logger.error("主动消息写回历史失败: %s", exc)

            # ③主动找过他还没回复：计数 +1（R10：不再封顶——{{unanswered_count}}
            # 展示真实值；暂停判定只看阈值，累加不改变 paused 语义）；
            # ①排定下次并持久化（担心档/暂停态自动走对应区间）
            self.state.unanswered += 1
            self._announce_pause()

            # R10：命中白名单的 &&tag&& 追加一条独立图片消息（与主回复管线
            # 「表情包是独立消息」同一语义；失败只告警——文字已开口）
            if image_path is not None and self.send_image is not None:
                try:
                    if not await self.send_image(image_path):
                        logger.warning("[proactive] 表情包图片发送失败（文字已发出）。")
                except Exception:  # noqa: BLE001 —— 图片故障不影响已送出的文字
                    logger.exception("[proactive] 表情包图片发送异常（文字已发出）。")

            if give_up_now:
                # R10 放弃档：最后一条告别已发出——落账并停止排程，
                # on_user_message（他回来）才恢复
                self.state.gave_up_date = datetime.fromtimestamp(now_ts).date().isoformat()
                self.state.next_trigger_ts = 0.0
                self.save_state()
                logger.info(
                    "沉默已超 give_up_after_hours（%.0f 小时）：告别式关心已发出，"
                    "停止主动排程（gave_up_date=%s），等他回来恢复。",
                    float(self.params.give_up_after_hours), self.state.gave_up_date)
                return text
            self.schedule_next(now_ts)
            return text
        finally:
            self._generating = False

    # ------------------------------------------------------------------
    # asyncio 调度循环（events.py 总线并行开发中，集成期可改挂总线 tick）
    # ------------------------------------------------------------------

    async def run_forever(
        self,
        *,
        poll_interval: float = 20.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """独立调度循环：周期 tick（默认 20s），stop 置位后退出。

        启动时完成两件事：④武装启动触发；①若无持久化 next_trigger 则先排定。
        """
        clock = clock or (lambda: time.time())
        await_call = sleep or asyncio.sleep
        start_ts = clock()
        self.arm_boot_trigger(start_ts)  # ④
        if self.state.next_trigger_ts <= 0:
            self.schedule_next(start_ts)  # ①首次运行先排定
        logger.info(
            "主动消息服务启动：区间 [%d, %d] 分钟，安静 %02d-%02d，"
            "unanswered=%d，恢复的下次触发=%s。",
            self.params.min_interval_minutes, self.params.max_interval_minutes,
            self.quiet_start, self.quiet_end, self.state.unanswered,
            datetime.fromtimestamp(self.state.next_trigger_ts).isoformat()
            if self.state.next_trigger_ts else "（待排定）",
        )
        while stop is None or not stop.is_set():
            await self.tick(clock())
            try:
                await await_call(poll_interval)
            except asyncio.CancelledError:
                raise


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def setup(ctx: Any) -> None:
    """构造主动消息服务挂 ctx.proactive（回调全部接现成实例，SPEC §8.3）。

    - 调度不走 run_forever 轮询：__main__ 订阅 EVENT_SCHEDULED 心跳后逐分钟
      调 tick()（挂总线语义，SPEC §2）；启动武装（④）在此完成；
    - send_voice/send_text 运行时取 ctx.bridge（桥装配序在本模块之后，
      闭包引用 ctx 保证拿到最终实例）；send_text 过主动消息分段参数
      （阈值 80 字实测）与全局限流器；
    - ⑧双轨注入 dual_send（VOICE_SPEC §2 迁移落点）：经 ctx.voice_manager.
      deliver_proactive 完成「合成成功只发语音、失败回退文字」，mood 快照取
      生成时刻的有效状态（soul.mood.effective_mood 结构化派生，与主回复管线
      同源）；无语音门面（tts.enabled=false）时 dual_send 退化为纯文字；
    - O2b（PERF_SPEC §3）：llm_chat 请求补 system=persona+mood 快照——与主管线
      同源（cfg.persona.system_prompt + MoodEngine.render_fragment）；R3-1d 再补
      begin_dialogs 的 assistant 侧口吻锚点（她的说话风格示例）；
    - R3-1a：send_text 经 pipeline FIFO 的 on_done/on_fail 回传真实送达结果，
      桥无连接直接判未送达（不入队）；
    - R3-1d：recent_context_fn 接 history——最近 8-12 条对话摘录（复用 diary
      的 extract_chat_excerpt + 定界符 + 注入防护；meta.source=proactive 的
      自发言行不进素材区，她不引用自己说过的话）；
    - llm_chat/tts 异常吞掉返回 None（回退纯文字/放弃本次，行为⑧与 SPEC §9）。
    """
    import base64

    from ..brain import sender as sender_mod
    from ..memory import store as memory_store_mod
    from . import diary as diary_mod
    from . import memes as memes_mod
    from . import mood as mood_mod
    from . import topics as topics_mod

    try:  # R10 (b)：她此刻的状态行——soul/schedule 为可选模块，缺席即跳过
        from . import schedule as schedule_mod
    except ImportError:
        schedule_mod = None

    p_cfg = ctx.cfg.proactive
    if not p_cfg.enable:
        logger.info("主动消息未启用（proactive.enable=false），跳过装配")
        return
    # R10 新参数对旧装配桩（缺字段的 SimpleNamespace cfg）宽容：getattr 回退默认
    params = ProactiveParams(
        min_interval_minutes=p_cfg.min_interval_minutes,
        max_interval_minutes=p_cfg.max_interval_minutes,
        quiet_hours=p_cfg.quiet_hours,
        max_unanswered=p_cfg.max_unanswered,
        boot_trigger_minutes=p_cfg.boot_trigger_minutes,
        prompt_template=p_cfg.prompt_template,
        tts_enabled=p_cfg.tts_enabled,
        always_send_text=p_cfg.always_send_text,
        recent_user_window_minutes=float(
            getattr(p_cfg, "recent_user_window_minutes", 5.0)),
        retry_min_minutes=int(getattr(p_cfg, "retry_min_minutes", 5)),
        retry_max_minutes=int(getattr(p_cfg, "retry_max_minutes", 10)),
        slowdown_min_minutes=int(getattr(p_cfg, "slowdown_min_minutes", 360)),
        slowdown_max_minutes=int(getattr(p_cfg, "slowdown_max_minutes", 720)),
        prompt_template_slowdown=str(
            getattr(p_cfg, "prompt_template_slowdown", "") or ""),
        prompt_template_worried=str(
            getattr(p_cfg, "prompt_template_worried", "") or ""),
        worried_after_hours=float(getattr(p_cfg, "worried_after_hours", 24.0)),
        give_up_after_hours=float(getattr(p_cfg, "give_up_after_hours", 72.0)),
    )
    # R10 深度素材的公共落点：运行时状态目录与每日话题开关
    data_dir = Path(ctx.repo_root) / ctx.cfg.storage.data_dir
    daily_topic_enabled = bool(getattr(p_cfg, "daily_topic", True))

    def _target_user_id() -> str:
        """主动消息目标用户 = 会话模板尾段（`<人格名>:FriendMessage:<QQ号>`）。"""
        return str(p_cfg.session_id or ctx.cfg.session_id or "").rsplit(":", 1)[-1]

    chat_client = getattr(ctx, "chat_client", None)
    tts_client = getattr(ctx, "tts_client", None)

    # R10 (b)：soul/schedule.render_line（可选模块；setup 时缺席即整段跳过）。
    # 课表素材与开关取自 [schedule]（entries / enabled），缺席按空课表处理
    # （render_line 对空 entries 返回 None，自然不注入）。
    schedule_render = (getattr(schedule_mod, "render_line", None)
                       if schedule_mod is not None else None)
    schedule_cfg = getattr(ctx.cfg, "schedule", None)
    # R10 (c)：记忆检索源（memory.store 缺席/失败一律跳过，绝不阻断主动消息）
    memory_store = getattr(ctx, "memory_store", None)

    async def _memory_lines(query: str) -> "list[str]":
        """R10 (c)：按 prompt 检索 top-2 相关记忆并渲染注入行（失败返回空）。"""
        if memory_store is None:
            return []
        try:
            hits = await memory_store.search(
                session_key, str(query or "")[:4000], top_k=2)
        except Exception as exc:  # noqa: BLE001 —— 检索失败降级不注入
            logger.warning("[proactive] 记忆检索失败（跳过回忆注入）：%s", exc)
            return []
        lines: list[str] = []
        for hit in list(hits or [])[:2]:
            try:
                lines.append(memory_store_mod.format_memory_line(
                    getattr(hit, "create_time", None),
                    getattr(hit, "content", ""), now=time.time()))
            except Exception:  # noqa: BLE001 —— 单条渲染失败跳过
                continue
        return lines

    async def _llm_chat(prompt: str) -> "str | None":
        if chat_client is None:
            logger.error("chat 客户端未装配，无法生成主动消息。")
            return None
        # O2b（PERF_SPEC §3）：补 system=persona+mood 快照——模板里的「必须符合
        # 人格设定」不能没有人格在场。与主管线同源：persona 取 cfg.persona.
        # system_prompt 原文，mood 片段经 MoodEngine.render_fragment（内部尊重
        # inject_enabled 开关 + 同一 render_system_fragment 数学）。mood 读取
        # 失败降级为仅 persona，绝不阻断主动消息。
        system_text = (ctx.cfg.persona.system_prompt or "").strip()
        # R3-1d：begin_dialogs 的 assistant 侧台词是她的口吻锚点（persona.assemble
        # 同一奇偶语义：偶数位=user、奇数位=assistant）——主动消息生成时她在场
        voice_samples = [
            str(d).strip()
            for i, d in enumerate(getattr(ctx.cfg.persona, "begin_dialogs", None) or [])
            if i % 2 == 1 and str(d).strip()
        ]
        if voice_samples:
            system_text = (
                system_text + "\n\n她的说话风格示例（保持同样的口吻与长度）：\n"
                + "\n".join(voice_samples)
            ).strip()
        if mood_engine is not None:
            try:
                fragment = (mood_engine.render_fragment(_target_user_id()) or "").strip()
            except Exception:  # noqa: BLE001 —— 情绪源故障按无快照处理
                logger.exception("[proactive] mood 片段渲染失败（按无快照处理）")
                fragment = ""
            if fragment:
                system_text = (system_text + "\n\n" + fragment).strip()
        # ---- R10 主动消息深度素材（追加进 user 侧 prompt；任一素材故障
        # 只跳过自身，绝不阻断主动消息）----
        # (a) 每日一题：topics.pick_topic 当天同题、每天推进（[proactive].
        #     daily_topic 开关）；指示自然揉进，别像采访
        if daily_topic_enabled:
            try:
                topic = topics_mod.pick_topic(datetime.now(), data_dir)
            except Exception:  # noqa: BLE001
                logger.exception("[proactive] 每日话题读取失败（本次跳过）")
                topic = ""
            if topic:
                prompt = (
                    prompt.rstrip()
                    + f"\n\n今天想到一个想跟他聊的话题：{topic}\n"
                      "（自然揉进消息里，别像采访，别原样照抄提问。）"
                )
        # (b) 她此刻的状态行（soul/schedule.render_line(now, entries, enabled)；
        #     模块缺席=None 跳过；课表空/开关关由 render_line 自己返回 None）
        if schedule_render is not None:
            try:
                line = str(schedule_render(
                    datetime.now(),
                    getattr(schedule_cfg, "entries", None),
                    enabled=bool(getattr(schedule_cfg, "enabled", True)),
                ) or "").strip()
            except Exception:  # noqa: BLE001
                logger.exception("[proactive] 状态行渲染失败（本次跳过）")
                line = ""
            if line:
                prompt = prompt.rstrip() + "\n\n她此刻的状态：\n" + line
        # (c) 记忆检索 top-2 相关回忆（memory_store 缺席/失败跳过）
        memory_material = await _memory_lines(prompt)
        if memory_material:
            prompt = (
                prompt.rstrip()
                + "\n\n可能相关的共同回忆（自然引用其中相关的，别机械罗列）：\n"
                + "\n".join(memory_material)
            )
        messages: list[dict] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": prompt})
        try:
            reply = await chat_client.chat_async(messages)
        except Exception as exc:  # noqa: BLE001 —— LLM 故障按"无有效输出"放弃本次
            logger.error("主动消息 LLM 调用失败：%s", exc)
            return None
        return str(getattr(reply, "content", "") or "")

    async def _tts(text: str) -> "bytes | None":
        if tts_client is None:
            return None
        return await tts_client.synthesize(text)

    async def _send_voice(data: bytes) -> bool:
        bridge = getattr(ctx, "bridge", None)
        if bridge is None or not data:
            return False
        b64 = base64.b64encode(data).decode("ascii")
        result = await bridge.send_record_b64(_target_user_id(), b64)
        if not result.ok:
            logger.warning("主动语音发送失败（%s）", result.status)
        return bool(result.ok)

    async def _send_text(text: str) -> bool:
        # R3-1a 送达判定：返回值必须是「真实出帧到 NapCat」的结果。
        # - 桥缺位/无连接：不入队直接 False（不计送达、不写历史、不 +1）；
        # - FIFO 收编路径（PERF_SPEC §2-O4）：入队 ≠ 送达——经 on_done/on_fail
        #   回传真实出帧结果（零段送达=False，任一段出帧=True）；
        # - 管线缺席（旧装配/单测直驱）回退直发路径（返回值本就真实）。
        bridge = getattr(ctx, "bridge", None)
        if bridge is None:
            return False
        connected = getattr(bridge, "connected", True)
        if callable(connected):
            connected = connected()
        if not connected:
            logger.warning("[proactive] 桥无连接（NapCat 离线），本次主动消息不计送达。")
            return False
        pipeline = getattr(ctx, "message_pipeline", None)
        if pipeline is not None and hasattr(pipeline, "enqueue_text_send"):
            loop = asyncio.get_running_loop()
            done: "asyncio.Future[bool]" = loop.create_future()

            def _resolve(value: bool) -> None:
                if not done.done():
                    done.set_result(value)

            async def _on_done(_segs: list) -> None:
                _resolve(True)

            async def _on_fail(segs: list) -> None:
                _resolve(bool(segs))  # 部分送达也算送达（她已开口）

            pipeline.enqueue_text_send(
                _target_user_id(), [text], p_cfg.segmented,
                on_done=_on_done,
                on_fail=_on_fail,
            )
            try:
                return bool(await asyncio.wait_for(
                    done, timeout=SEND_RESULT_TIMEOUT_SECONDS))
            except (asyncio.TimeoutError, TimeoutError):
                # 超时按未送达处理（保守侧：宁重发不假记；wait_for 已取消 future，
                # 迟到的回执被 _resolve 的 done() 门挡住）
                logger.warning("[proactive] 发送 FIFO 回执超时，按未送达处理。")
                return False
        segments = await sender_mod.send_reply(
            text,
            lambda seg: bridge.send_private_text(_target_user_id(), seg),
            params=p_cfg.segmented,
            limiter=getattr(ctx, "rate_limiter", None),
        )
        return bool(segments)

    async def _append_history(role: str, content: str, meta: "dict | None" = None) -> None:
        history = getattr(ctx, "history", None)
        if history is not None:
            history.append(ctx.cfg.session_id, role, content, meta=meta)

    # R3-1d 近况素材：最近 8-12 条对话摘录（diary 抽取机制 + 定界符 + 注入防护）。
    # meta.source=proactive 的自发言行不进素材区（她不引用自己说过的话）；
    # 多读几条再过滤，保证过滤后仍有足量素材。
    history_store = getattr(ctx, "history", None)
    session_key = str(p_cfg.session_id or ctx.cfg.session_id or "")
    _recent_n = min(12, max(8, int(getattr(p_cfg, "history_context_messages", 10))))
    _recent_chars = max(400, int(getattr(p_cfg, "history_context_chars", 1200)))

    def _read_recent_records() -> list:
        records = history_store.get_records(session_key, limit=_recent_n + 8)
        msgs = [
            {"role": r.get("role", ""), "content": r.get("content", "")}
            for r in records
            if str((r.get("meta") or {}).get("source") or "") != "proactive"
        ]
        return msgs[-_recent_n:]

    async def _recent_context() -> str:
        if history_store is None:
            return ""
        try:
            msgs = await asyncio.to_thread(_read_recent_records)
        except Exception as exc:  # noqa: BLE001 —— 素材故障不阻断主动消息
            logger.warning("[proactive] 近况历史读取失败：%s", exc)
            return ""
        lines = diary_mod.extract_chat_excerpt(
            msgs, max_messages=0, max_chars=_recent_chars)
        if not lines:
            return ""
        excerpt = diary_mod.neutralize_template_placeholders("\n".join(lines))
        return (
            f"{diary_mod.EXCERPT_OPEN}\n{excerpt}\n{diary_mod.EXCERPT_CLOSE}\n"
            f"{diary_mod.INJECTION_GUARD}"
        )

    voice_manager = getattr(ctx, "voice_manager", None)
    mood_engine = getattr(ctx, "mood_engine", None)

    async def _dual_send(text: str) -> bool:
        """⑧双轨接缝（VOICE_SPEC §2）：「成功只发语音、失败回退文字」的决策在
        VoiceManager.deliver_proactive（合成走共享引擎栈）。mood 快照取生成
        时刻的有效状态——soul.mood.effective_mood 结构化派生（τ=4h 衰减 +
        <0.15 回落 calm），与主回复管线同一数学，绝不解析注入字符串。"""
        if voice_manager is None:
            # 无语音门面（tts.enabled=false 等）：纯文字（语义同 tts_enabled=false）
            return bool(await _send_text(text))
        snapshot = None
        if mood_engine is not None:
            try:
                snapshot = mood_mod.effective_mood(
                    mood_engine.snapshot_state(_target_user_id()), now=time.time())
            except Exception:  # noqa: BLE001 —— 情绪源故障按无快照处理（映射按 calm）
                logger.exception("[proactive] mood 有效状态读取失败（按无快照处理）")
                snapshot = None
        return await voice_manager.deliver_proactive(
            _target_user_id(), text, mood_snapshot=snapshot,
            always_send_text=params.always_send_text, send_text=_send_text)

    # R10 表情包通道（与主回复管线同一 memes 语义）；[memes].enable=false 或
    # 配置缺席时只剥标记不发图——&&tag&& 文本绝不能原样漏给用户
    memes_cfg = getattr(ctx.cfg, "memes", None)
    _MEMES_DIR_FALLBACK = "local/shared_plugin_data/meme_manager/memes"

    def _process_memes(text: str):
        try:
            if memes_cfg is None or not getattr(memes_cfg, "enable", False):
                return memes_mod.MemePlan(
                    text=memes_mod.strip_all_markers(text), image_path=None)
            return memes_mod.process_reply(
                text,
                memes_dir=Path(ctx.repo_root) / (
                    str(getattr(memes_cfg, "memes_dir", "") or "")
                    or _MEMES_DIR_FALLBACK),
                whitelist=memes_mod.build_whitelist(
                    list(getattr(memes_cfg, "extra_tags", None) or [])),
                probability=int(getattr(memes_cfg, "probability", 60)),
                max_images=int(getattr(memes_cfg, "max_images", 1)),
                rng=random.Random(),
            )
        except Exception:  # noqa: BLE001 —— 表情包故障按纯文字发送
            logger.exception("[proactive] 表情包处理失败（按纯文字发送）")
            return None

    async def _send_image(path) -> bool:
        """独立图片消息发送（读图 → bridge.send_image_b64；返回真实送达结果）。"""
        bridge = getattr(ctx, "bridge", None)
        if bridge is None or path is None:
            return False
        try:
            data = await asyncio.to_thread(Path(path).read_bytes)
        except OSError as exc:
            logger.warning("[proactive] 表情包图片读取失败：%s", exc)
            return False
        result = await bridge.send_image_b64(
            _target_user_id(), base64.b64encode(data).decode("ascii"))
        if not result.ok:
            logger.warning("[proactive] 表情包图片发送失败（%s）", result.status)
        return bool(result.ok)

    service = ProactiveService(
        params,
        session_id=str(p_cfg.session_id or ctx.cfg.session_id or ""),
        state_path=data_dir / "proactive_state.json",
        llm_chat=_llm_chat,
        tts=_tts,
        send_voice=_send_voice,
        send_text=_send_text,
        append_history=_append_history,
        dual_send=_dual_send,
        recent_context_fn=_recent_context,
        process_memes=_process_memes,
        send_image=_send_image,
    )
    service.arm_boot_trigger(time.time())  # ④启动武装：EVENT_SCHEDULED 驱动 tick 到点判定
    ctx.proactive = service
    logger.info("[proactive] 主动消息服务装配完成（区间 [%d, %d] 分钟，安静 %s，TTS=%s）",
                params.min_interval_minutes, params.max_interval_minutes,
                params.quiet_hours, params.tts_enabled)
