# -*- coding: utf-8 -*-
"""asyncio 事件总线 —— 框架内部的唯一消息通道（SPEC §2）。

四类事件（事件名为字符串常量，便于日志聚合与跨模块解耦）：
- ``EVENT_MESSAGE``  ：收到用户消息（bridge 解析后投递，白名单门禁之后的净文本）
- ``EVENT_SEND``     ：即将发送给用户的消息（发送侧行为：分段/TTS/meme 消费它）
- ``EVENT_SCHEDULED``：定时调度心跳（__main__ 的 ticker 周期投递，soul 各定时行为
  自行比对时刻：proactive 间隔、diary 22:30 等）
- ``EVENT_ERROR``    ：任何处理器抛出的异常都会转投到这里（不再二次递归）

处理器约定：
- 同步与异步混合：``handler(event)`` 返回 awaitable 则被 await，否则视为同步完成；
- 单个处理器异常不影响后续处理器，异常转投 EVENT_ERROR 并记日志；
- emit 时对处理器列表做快照，处理器内部再订阅/退订不会破坏本次迭代。

事件本体是轻量 dataclass（Event），payload 由发布方自定义。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List

__all__ = [
    "EVENT_MESSAGE", "EVENT_SEND", "EVENT_SCHEDULED", "EVENT_ERROR",
    "Event", "EventBus", "Unsubscribe",
]

logger = logging.getLogger("virtuallover.events")

EVENT_MESSAGE = "message"
EVENT_SEND = "send"
EVENT_SCHEDULED = "scheduled"
EVENT_ERROR = "error"

Handler = Callable[["Event"], Any]
Unsubscribe = Callable[[], None]


@dataclass
class Event:
    """总线事件。name 为四类常量之一；ts 为发布时刻（unix 秒）。"""
    name: str
    payload: Any = None
    ts: float = field(default_factory=time.time)
    # 处理器失败后写回的异常清单（便于诊断；正常路径为空）
    errors: List[BaseException] = field(default_factory=list)


class EventBus:
    """进程内异步事件总线。一个框架实例一个总线（AppContext 持有）。"""

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)

    # -- 订阅 ----------------------------------------------------------------

    def on(self, name: str, handler: Handler) -> Unsubscribe:
        """注册处理器；返回退订函数（幂等）。"""
        self._handlers[name].append(handler)
        logger.debug("事件 %s 注册处理器 %s（当前 %d 个）",
                     name, getattr(handler, "__qualname__", repr(handler)),
                     len(self._handlers[name]))

        def _unsubscribe() -> None:
            try:
                self._handlers[name].remove(handler)
            except ValueError:  # 已被移除，幂等返回
                pass

        return _unsubscribe

    # -- 发布 ----------------------------------------------------------------

    async def emit(self, name: str, payload: Any = None) -> Event:
        """发布事件并同步顺序执行全部处理器。

        - 处理器异常：记日志、收集到 event.errors，并转投 EVENT_ERROR（携带
          source 上下文）；EVENT_ERROR 自身的处理器异常只记日志，不再递归；
        - 返回该事件对象（errors 非空时调用方可感知部分失败）。
        """
        event = Event(name=name, payload=payload)
        for handler in list(self._handlers.get(name, ())):  # 快照：迭代中注册安全
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 —— 总线是异常屏障，绝不上抛
                event.errors.append(exc)
                logger.exception("事件 %s 的处理器 %s 抛出异常",
                                 name, getattr(handler, "__qualname__", repr(handler)))
                if name != EVENT_ERROR:
                    await self.emit(EVENT_ERROR, {
                        "source_event": name,
                        "exception": exc,
                        "handler": getattr(handler, "__qualname__", repr(handler)),
                    })
        return event

    # -- 观测 ----------------------------------------------------------------

    def handler_count(self, name: str) -> int:
        """当前注册的处理器数（状态自检/测试用）。"""
        return len(self._handlers.get(name, ()))


async def tick_forever(bus: EventBus, interval_seconds: float = 60.0,
                       stop: asyncio.Event | None = None) -> None:
    """骨架级调度器：周期投递 EVENT_SCHEDULED（payload 带 now 时间戳）。

    只负责"心跳"，不负责"该不该做"——proactive/diary 等模块订阅后自行比对
    时刻与状态（SPEC §8.3 的 next_trigger_time 持久化归 soul 组实现）。
    被 cancel 或 stop 置位后安静退出。
    """
    logger.info("调度心跳启动：每 %.0fs 投递一次 %s", interval_seconds, EVENT_SCHEDULED)
    while stop is None or not stop.is_set():
        try:
            await asyncio.wait_for(_wait_stop(stop), timeout=interval_seconds)
            break  # stop 被置位
        except asyncio.TimeoutError:
            await bus.emit(EVENT_SCHEDULED, {"now": time.time()})


async def _wait_stop(stop: asyncio.Event | None) -> None:
    if stop is not None:
        await stop.wait()
    else:
        await asyncio.Event().wait()  # 永不完成：仅由超时打断
