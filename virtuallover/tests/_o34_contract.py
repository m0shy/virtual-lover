# -*- coding: utf-8 -*-
"""O3/O4 characterization 契约垫片 —— 先红测试的接口声明与探测（PERF_SPEC §7.3）。

定位：PERF_SPEC §7.3「characterization 先行清单（先红后绿，随实现同 PR）」。
本文件在实现落地前做两件事：

1. 把 O3（立即起跑 + 打断地平线）/ O4（per-user 双队列解耦）管线的**预期接口**
   写成可执行契约 —— 实现组照此落地；命名不一致时只改本文件的候选表，
   测试断言一律不动（断言才是 characterization 的本体）。
2. 探测落地状态：接口未落地时测试 skip（skip 理由点名缺失的符号），
   实现合入后全量断言生效 —— 先红（skip 时代）→ 后绿（实现同 PR）。

预期契约（duck typing，实现组的最小实现面，全部由本垫片的假件喂养）：

- 模块候选：``virtuallover.brain.pipeline`` / ``virtuallover.pipeline``；
- 入口形态二选一：
  a) 模块级管线类：名候选 ``MessagePipeline`` / ``ReplyPipeline`` /
     ``UserPipeline`` / ``Pipeline``，构造 ``Pipeline(ctx, *, now_fn=None)``；
  b) 模块级 ``setup(ctx)``（对齐 _SETUP_TARGETS 约定）把 per-user 管线接到
     ``ctx.bus`` 的 EVENT_MESSAGE，实例挂 ``ctx.message_pipeline``（候选属性名
     见 CTX_ATTR_CANDIDATES）；构造用时钟优先取 ``ctx.now_fn``。
- 管线最小面（测试只依赖这些成员）：
    await pipeline.on_message(event)   # 桥事件 dict（make_event 的形态）
    pipeline.superseded_count          # int：打断丢弃轮计数（O3②，§2「superseded 计数日志」）
    pipeline.state                     # "IDLE"|"RUNNING"|"DRIP"（§2 O3 状态机观测面）
    await pipeline.aclose()            # 收尾：取消后台任务/清空队列
- ctx 消费面与 __main__._handle_user_message 一致：cfg / bus / chat_client /
  bridge / history / router / mood_engine / proactive / rate_limiter
  （memory_store / memory_service / voice_manager / diary_service 允许为 None）。
- 时钟：管线的一切时刻判定（打断窗口 / 冷启动 >2min / 到达时刻落库）必须走
  可注入时钟（ctx.now_fn 或构造参 now_fn），否则 O3③ 无法确定性测试。

本文件不含任何真实敏感值；QQ 号一律 10000/10001 占位号段。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from virtuallover.brain.history import HistoryStore
from virtuallover.brain.llm import LLMReply
from virtuallover.brain.sender import RateLimiter
from virtuallover.commands import CommandRouter
from virtuallover.config import VlConfig
from virtuallover.events import EVENT_MESSAGE, EventBus
from virtuallover.soul.mood import MoodEngine
from virtuallover.soul.proactive import ProactiveParams, ProactiveService

# ---------------------------------------------------------------------------
# 契约候选表（实现落地后若命名不同，只改这里）
# ---------------------------------------------------------------------------

#: O3/O4 管线模块候选（按序探测，第一个可 import 的生效）
MODULE_CANDIDATES = ("virtuallover.brain.pipeline", "virtuallover.pipeline")

#: 模块级管线类名候选
CLASS_CANDIDATES = ("MessagePipeline", "ReplyPipeline", "UserPipeline", "Pipeline")

#: setup(ctx) 装配形态下，管线实例在 ctx 上的属性名候选
CTX_ATTR_CANDIDATES = ("message_pipeline", "pipeline", "reply_pipeline")

#: 定界符中和函数（O3⑥）名与落点候选
NEUTRALIZE_CANDIDATES = (
    ("virtuallover.brain.pipeline", "neutralize_delimiters"),
    ("virtuallover.brain.pipeline", "neutralize_merge_text"),
    ("virtuallover.bridge.textutil", "neutralize_delimiters"),
    ("virtuallover.brain.persona", "neutralize_delimiters"),
)

#: 未落地时的统一 skip 理由前缀（点名候选，实现组一眼看懂要建什么）
NOT_LANDED_HINT = (
    "O3/O4 管线接口未落地（PERF_SPEC §7.3 characterization 契约）："
    "在 " + " / ".join(MODULE_CANDIDATES) + " 之一提供管线类 "
    + " / ".join(CLASS_CANDIDATES)
    + "（或 setup(ctx) 装配形态）后本测试自动生效。"
)


def probe_pipeline_module() -> tuple[Any | None, str]:
    """探测 O3/O4 管线模块：返回 (module, 命中名) 或 (None, 未落地说明)。"""
    for name in MODULE_CANDIDATES:
        try:
            return import_module(name), name
        except ImportError:
            continue
    return None, NOT_LANDED_HINT


def probe_neutralize() -> tuple[Any | None, str]:
    """探测定界符中和函数（O3⑥）：返回 (callable, 落点) 或 (None, 说明)。"""
    for mod_name, attr in NEUTRALIZE_CANDIDATES:
        try:
            module = import_module(mod_name)
        except ImportError:
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn, f"{mod_name}.{attr}"
    return None, (
        "定界符中和函数未落地（O3⑥）：候选 "
        + " / ".join(f"{m}.{a}" for m, a in NEUTRALIZE_CANDIDATES)
    )


# ---------------------------------------------------------------------------
# 假件：可闸门控制的 chat 客户端 / 可控故障与闸门的桥 / 可注入假时钟
# ---------------------------------------------------------------------------


class FakeClock:
    """可手动推进的假时钟（秒）：打断窗口 / 冷启动判定 / 到达时刻 stamp 全走它。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


@dataclass
class ChatCall:
    """一次 chat_async 调用的记录与闸门（测试据此做确定性时序控制）。"""

    index: int
    messages: list[dict]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    reply: str = ""
    done: bool = False


class ScriptedChatClient:
    """假 chat 客户端（契约面 = brain.llm.ChatClient.chat_async 的用到的子集）。

    - 每次 chat_async 记录一条 ChatCall（messages 深拷贝留证）并置 started；
    - 默认 auto_release=True（自流）：置 started 后立即放行；
      auto_release=False 时由测试 await wait_calls(n) 后手工
      ``calls[i].release.set()`` 控制完成时刻（打断/DRIP 的确定性时序靠它）；
    - 回复按调用序取 replies，超出后用 default_reply。
    """

    def __init__(self, replies: list[str] | None = None,
                 default_reply: str = "嗯嗯 好呀。") -> None:
        self.calls: list[ChatCall] = []
        self.replies = list(replies or [])
        self.default_reply = default_reply
        self.auto_release = True
        #: 回声钩子：设置后每次回复 = reply_fn(call)（保序测试用它把回复钉到消息）
        self.reply_fn: Any = None

    @property
    def in_flight(self) -> int:
        return sum(1 for c in self.calls if not c.done)

    async def chat_async(self, messages: list[dict], **kwargs: Any) -> LLMReply:
        call = ChatCall(index=len(self.calls),
                        messages=[dict(m) for m in messages])
        self.calls.append(call)
        call.started.set()
        if self.auto_release:
            call.release.set()
        try:
            await call.release.wait()
            if self.reply_fn is not None:
                call.reply = str(self.reply_fn(call))
            else:
                call.reply = (
                    self.replies[call.index] if call.index < len(self.replies)
                    else self.default_reply
                )
            return LLMReply(content=call.reply)
        finally:
            call.done = True

    async def wait_calls(self, n: int, timeout: float = 3.0) -> list[ChatCall]:
        """等到第 n 次 chat 已发起（started 已置位），返回前 n 条调用。"""
        await wait_until(lambda: len(self.calls) >= n, timeout)
        return self.calls[:n]

    def set_replies(self, replies: list[str]) -> None:
        self.replies = list(replies)


@dataclass
class SendRecord:
    """一次发送尝试的记录（发起即记录，含失败与在途）。"""

    user_id: str
    text: str
    t: float
    ok: bool | None = None  # None=尚在途


class FakeBridge:
    """假桥（契约面 = __main__ 消费的 send_private_text / send_image_b64 子集）。

    - ``sent``：全部发送尝试（发起即记录）；``completed``：成功完成的文本序；
    - ``hold_after``：第 hold_after 条发送成功完成后，后续发送挂起在
      ``hold_event`` 上（DRIP 段间 / 首段在途 的确定性构造）；
    - ``fail_on``：第 n 条（1 起）尝试直接返回失败（发送短路测试）。
    """

    def __init__(self, *, now_fn: Any = time.time) -> None:
        self.sent: list[SendRecord] = []
        self.now = now_fn
        self._hold_after: int | None = None
        self.hold_event = asyncio.Event()
        self.hold_event.set()  # 默认不挂起
        self.fail_on: set[int] = set()
        #: get_msg 回查应答（O4「回查随事件进管线」的契约面；None=模拟回查失败）
        self.lookup_reply: str | None = None

    @property
    def hold_after(self) -> "int | None":
        """第 hold_after 条发送成功完成后，后续发送挂起（见类 docstring）。"""
        return self._hold_after

    @hold_after.setter
    def hold_after(self, value: "int | None") -> None:
        """赋值即进入挂起现场：清 hold_event，后续发送真实挂起直到测试放行。

        （夹具语义修正：docstring 承诺「后续发送挂起在 hold_event 上」——
        事件默认置位=不挂起，测试只赋 hold_after 不清事件时挂不起来，
        「首段在途 / DRIP 段间」的确定性现场构造不出。赋值即 clear。）
        """
        self._hold_after = value
        if value is not None:
            self.hold_event.clear()

    # -- 观测面 ---------------------------------------------------------------

    @property
    def in_flight(self) -> int:
        return sum(1 for r in self.sent if r.ok is None)

    @property
    def completed(self) -> list[str]:
        return [r.text for r in self.sent if r.ok]

    def sent_texts(self) -> list[str]:
        return [r.text for r in self.sent]

    # -- 发送面 ---------------------------------------------------------------

    async def send_private_text(self, user_id: Any, text: str):
        from virtuallover.bridge.onebot_server import ActionResult

        index = len(self.sent) + 1
        record = SendRecord(user_id=str(user_id), text=text, t=self.now())
        self.sent.append(record)
        if self.hold_after is not None and index > self.hold_after:
            await self.hold_event.wait()
        if index in self.fail_on:
            record.ok = False
            return ActionResult(ok=False, status="test-fail")
        record.ok = True
        return ActionResult(ok=True)

    async def send_image_b64(self, user_id: Any, b64: str):
        from virtuallover.bridge.onebot_server import ActionResult

        self.sent.append(SendRecord(user_id=str(user_id), text="<image>", t=self.now()))
        return ActionResult(ok=True)

    async def get_msg_text(self, message_id: Any) -> str | None:
        """契约面（O4）：reply 回查走 bridge.get_msg_text，文本可由测试预置。"""
        return self.lookup_reply


# ---------------------------------------------------------------------------
# 透明调用记录代理（O3⑤：观测 router/mood 是否逐条执行）
# ---------------------------------------------------------------------------


def record_calls(obj: Any, *method_names: str) -> tuple[Any, dict[str, list[tuple]]]:
    """把 obj 的指定方法包一层记录代理（async/sync 通吃），其余成员透传。

    返回 (proxy, calls)；calls[method] = [(args, kwargs), ...]。
    """
    calls: dict[str, list[tuple]] = {name: [] for name in method_names}

    class _Proxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(obj, name)

        def _wrap(self, name: str):
            original = getattr(obj, name)

            async def _async(*args, **kwargs):
                calls[name].append((args, kwargs))
                return await original(*args, **kwargs)

            def _sync(*args, **kwargs):
                calls[name].append((args, kwargs))
                return original(*args, **kwargs)

            import inspect

            return _async if inspect.iscoroutinefunction(original) else _sync

    proxy = _Proxy()
    for name in method_names:
        setattr(proxy, name, proxy._wrap(name))  # type: ignore[attr-defined]
    return proxy, calls


# ---------------------------------------------------------------------------
# ctx 装配（全部假件 + tmp 落盘，绝不触碰真实 local/）
# ---------------------------------------------------------------------------

FAKE_SESSION = "测试人格:FriendMessage:10001"


def make_cfg(now_fn: Any) -> VlConfig:
    """构造管线测试用的 VlConfig：占位账号 + 测试调参（分段/间隔/限流全加速）。"""
    cfg = VlConfig(session_id=FAKE_SESSION, admins=["10000"], whitelist=["10001"])
    # 分段：阈值压到 10，用句号切段 —— 一句三段的多段回复可稳定构造（O3④/O4④）
    seg = cfg.reply.segmented
    seg.enable = True
    seg.words_count_threshold = 10
    seg.split_mode = "regex"
    seg.regex = r"[^\n]+?[。！？…]"
    seg.first_gap = ""            # 关首段抖动（O2a 回退键已存在），测试时序要确定
    seg.interval_method = "random"
    seg.interval = "0.02,0.05"
    seg.min_gap = 0.02
    seg.max_gap = 0.05
    cfg.reply.rate_limit.max_count = 10_000   # 限流等效关闭，不干扰发送计数
    cfg.reply.rate_limit.window_seconds = 1
    cfg.mnemosyne.inject_enabled = False      # 不依赖记忆检索（harness 无 memory_store）
    cfg.memes.enable = False
    # 作息行默认关：开启时「她此刻」防护块（<资料引用>包裹）会进 user 消息，
    # 污染 search/stt/o4 等用例的「普通消息不出现资料块」断言；注入行为由
    # test_pipeline_integration 与 test_persona/test_schedule 专项覆盖。
    cfg.schedule.enabled = False
    # O3 打断窗口回退键（§7.2：[chat] interrupt_window_seconds，默认 1.5）。
    # 键尚未进 config 时以 setattr 预置 —— 实现落地读取同名键即可，测试无需改动。
    if not hasattr(cfg.chat, "interrupt_window_seconds"):
        try:
            object.__setattr__(cfg.chat, "interrupt_window_seconds", 1.5)
        except Exception:
            setattr(cfg.chat, "interrupt_window_seconds", 1.5)
    else:
        cfg.chat.interrupt_window_seconds = 1.5
    cfg.chat.timeout = 30           # §7.1-4：主客户端 30s（bg 保持 120 由 ⑦ 单测）
    return cfg


def make_ctx(tmp_path, *, now_fn: Any, cfg: VlConfig | None = None) -> Any:
    """组装管线消费的最小 ctx：全部假件 + tmp 落盘（对齐 __main__ 消费面）。"""
    from types import SimpleNamespace

    cfg = cfg or make_cfg(now_fn)
    history = HistoryStore(tmp_path / "history.db", mirror_path=None, clock=now_fn)
    mood_engine = MoodEngine(
        tmp_path / "mood",
        config={"keyword_groups": {"sweet": "喜欢|想你", "praise": "厉害", "rude": "滚"},
                "daily_bonus_enabled": False, "inject_enabled": False},
        config_path=tmp_path / "mood_config_not_used.json",
    )
    proactive = ProactiveService(
        ProactiveParams(min_interval_minutes=10 ** 6, max_interval_minutes=10 ** 6,
                        boot_trigger_minutes=10 ** 6, tts_enabled=False,
                        prompt_template="占位模板"),
        session_id=cfg.session_id,
        state_path=tmp_path / "proactive_state.json",
    )
    ctx = SimpleNamespace(
        cfg=cfg,
        bus=EventBus(),
        repo_root=tmp_path,
        now_fn=now_fn,
        chat_client=ScriptedChatClient(),
        bridge=FakeBridge(now_fn=now_fn),
        history=history,
        mood_engine=mood_engine,
        proactive=proactive,
        router=CommandRouter(mood_engine=mood_engine, diary_service=None, eval_runner=None),
        rate_limiter=RateLimiter(max_count=10_000, window_seconds=1.0),
        memory_store=None,
        memory_service=None,
        voice_manager=None,
        diary_service=None,
    )
    return ctx


# ---------------------------------------------------------------------------
# 管线装配探测（两种入口形态统一成一个 handle）
# ---------------------------------------------------------------------------


class PipelineHandle:
    """统一驱动面：屏蔽「直接构造类」与「setup(ctx)+bus」两种装配形态差异。"""

    def __init__(self, pipeline: Any, ctx: Any, *, driver: str,
                 module_name: str, class_name: str, clock_ok: bool,
                 cleanup: Any = None) -> None:
        self.pipeline = pipeline
        self.ctx = ctx
        self.driver = driver          # "direct" | "bus"
        self.module_name = module_name
        self.class_name = class_name
        self.clock_ok = clock_ok      # 构造时 now_fn 是否被接受
        self._cleanup = cleanup

    @property
    def superseded_count(self) -> int:
        return int(getattr(self.pipeline, "superseded_count", 0))

    @property
    def state(self) -> str:
        return str(getattr(self.pipeline, "state", "IDLE"))

    @property
    def chat(self) -> ScriptedChatClient:
        return self.ctx.chat_client

    @property
    def bridge(self) -> FakeBridge:
        return self.ctx.bridge

    async def emit(self, event: dict) -> None:
        """投递一条桥事件：direct 形态直调 on_message；bus 形态走 EVENT_MESSAGE。"""
        if self.driver == "direct":
            await self.pipeline.on_message(event)
        else:
            await self.ctx.bus.emit(EVENT_MESSAGE, event)

    async def aclose(self) -> None:
        closer = getattr(self.pipeline, "aclose", None)
        if callable(closer):
            await closer()
        if self._cleanup is not None:
            try:
                self._cleanup()
            except Exception:
                pass


async def attach_pipeline(ctx: Any) -> tuple[PipelineHandle | None, str]:
    """按契约探测并装配管线到 ctx；未落地返回 (None, skip 理由)。"""
    module, mod_name = probe_pipeline_module()
    if module is None:
        return None, mod_name

    now_fn = getattr(ctx, "now_fn", None)

    # 形态 a：模块级管线类，构造 Pipeline(ctx, *, now_fn=...)
    for class_name in CLASS_CANDIDATES:
        cls = getattr(module, class_name, None)
        if cls is None:
            continue
        clock_ok = True
        try:
            pipeline = cls(ctx, now_fn=now_fn)
        except TypeError:
            try:
                pipeline = cls(ctx)
                clock_ok = False
            except Exception as exc:  # noqa: BLE001 —— 构造失败=契约违约，如实上报
                return None, f"管线类 {mod_name}.{class_name} 构造失败（契约违约）：{exc!r}"
        except Exception as exc:  # noqa: BLE001
            return None, f"管线类 {mod_name}.{class_name} 构造失败（契约违约）：{exc!r}"
        return PipelineHandle(pipeline, ctx, driver="direct", module_name=mod_name,
                              class_name=class_name, clock_ok=clock_ok), ""

    # 形态 b：模块级 setup(ctx)，实例挂 ctx 属性（管线从 ctx.now_fn 取时钟）
    setup = getattr(module, "setup", None)
    if callable(setup):
        try:
            cleanup = setup(ctx)
        except Exception as exc:  # noqa: BLE001
            return None, f"{mod_name}.setup(ctx) 抛异常（契约违约）：{exc!r}"
        for attr in CTX_ATTR_CANDIDATES:
            pipeline = getattr(ctx, attr, None)
            if pipeline is not None:
                return PipelineHandle(pipeline, ctx, driver="bus", module_name=mod_name,
                                      class_name=f"setup->{attr}", clock_ok=True,
                                      cleanup=cleanup), ""
        return None, (
            f"{mod_name}.setup(ctx) 已执行但 ctx 上找不到管线实例"
            f"（候选属性：{' / '.join(CTX_ATTR_CANDIDATES)}）——契约违约"
        )

    return None, (
        f"{mod_name} 已存在但既无管线类（{' / '.join(CLASS_CANDIDATES)}）"
        f"也无 setup(ctx) —— 契约未落地"
    )


# ---------------------------------------------------------------------------
# 桥事件构造与等待工具（与 bridge._handle_message_event 的 event 形态一致）
# ---------------------------------------------------------------------------


def make_event(user_id: str, text: str, message_id: int, *, ts: float | None = None,
               reply_id: Any = None, reply_text: str | None = None) -> dict:
    """构造与 OneBotBridge._handle_message_event 同形的桥事件 dict。"""
    return {
        "type": "message",
        "time": int(ts if ts is not None else time.time()),
        "self_id": "10000",
        "user_id": str(user_id),
        "message_id": str(message_id),
        "text": text,
        "segments": [{"type": "text", "data": {"text": text}}],
        "reply_id": reply_id,
        "reply_text": reply_text,
        "raw": {},
    }


async def wait_until(pred, timeout: float = 3.0, interval: float = 0.02) -> None:
    """轮询等待谓词成立（事件循环友好，不引入 pytest-asyncio）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError(f"条件等待超时（{timeout}s）")
        await asyncio.sleep(interval)


async def settle(handle: PipelineHandle, timeout: float = 5.0) -> None:
    """等管线收尾：状态 IDLE（若暴露）+ 无在途 chat + 无在途发送。"""
    def _quiet() -> bool:
        if getattr(handle.pipeline, "state", "IDLE") != "IDLE":
            return False
        return handle.chat.in_flight == 0 and handle.bridge.in_flight == 0

    await wait_until(_quiet, timeout)
    await asyncio.sleep(0.05)  # 收尾宽限：让最后一跳的 history/计数落定
