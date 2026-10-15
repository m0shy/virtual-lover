# -*- coding: utf-8 -*-
"""管理控制台 —— 最近事件环形缓冲（recent）。五轮修缮 R2「最近事件面板」。

背景实证：ark 欠费 403 时她哑 8 小时（local/logs/vl.log:4420
``LLMError: chat HTTP 403：error.Forbidden/AccountOverdueError``），控制台
总览页却全绿——旧 /api/status 只探「端口/客户端对象是否存在」，403 这类
「可达但调用失败」完全不可见。本模块把进程内日志流搬进控制台：

- :class:`RecentEventsHandler`：进程内 ``logging.Handler``，ERROR/WARNING
  与「关键 INFO」（装配完成/日记已生成/主动消息已发出等里程碑行）进
  ``deque(maxlen=50)`` 环形缓冲；多行异常栈拍平为单行并截断（状态卡单行
  展示契约）；线程安全（logging.Handler 自带锁）。
- :func:`read_log_tail`：跨重启兜底——进程内缓冲为空（刚重启/接管前）时，
  从 vl.log 尾部按同款格式 ``%(asctime)s %(levelname)s %(name)s: %(message)s``
  解析出同构事件列表；解析不动的行（traceback 续行等）跳过，绝不抛异常。
- :func:`recent_events_payload`：GET /api/recent-errors 的响应体组装——
  优先进程内缓冲（source="memory"），空则退回日志尾部（source="log"）。

日志安全：事件正文来自已落盘日志（链路各处已按规约打码），本模块不再
二次加工敏感值；单行截断上限 240 字符防止异常栈刷爆状态卡。
"""

from __future__ import annotations

import logging
import re
from collections import deque
from pathlib import Path
from typing import Any

__all__ = [
    "RecentEventsHandler", "get_global_handler", "attach_global_handler",
    "read_log_tail", "recent_events_payload",
    "MAX_EVENTS", "MAX_LINE_CHARS",
]

logger = logging.getLogger("virtuallover.console.recent")

# 环形缓冲容量与单行截断上限（任务契约：deque 50；单行截断防刷屏）
MAX_EVENTS = 50
MAX_LINE_CHARS = 240

# 「关键 INFO」白名单：消息含这些子串的 INFO 行才进缓冲（里程碑/接线事件）。
# 全量 INFO 会把环形缓冲灌满限流/HTTP 请求行，真正值得看的是这些低频里程碑。
KEY_INFO_MARKS: "tuple[str, ...]" = (
    "装配完成",            # 各模块 setup 完成 / 客户端装配完成
    "vl.toml 加载成功",     # 配置加载（启动里程碑）
    "桥 token 就绪",        # 启动里程碑
    "日记已生成",           # 日记写完（后台任务里程碑）
    "主动消息已发出",       # proactive 触发成功
    "记忆总结入库",         # 记忆沉淀成功
    "备份完成",             # 控制台备份动作完成
    "smoke 自检通过",       # 就绪探测里程碑
    "熔断",                 # 熔断进 degraded 的 error 行虽已覆盖，INFO 兜底
)

# 日志行解析（与 __main__._setup_logging 的 fmt 逐字对应）：
#   "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+"
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"([\w.]+):\s(.*)$"
)


def to_single_line(text: str, max_chars: int = MAX_LINE_CHARS) -> str:
    """把日志正文拍平为单行并截断（异常栈/多行消息 → 状态卡单行契约）。

    - 换行替换为「 ⏎ 」可视化标记（保留“这里还有内容”的线索）；
    - 超长截断加「…」后缀；返回值绝不含换行。
    """
    flat = " ⏎ ".join(part.strip() for part in str(text or "").splitlines()
                      if part.strip())
    if len(flat) > max_chars:
        return flat[: max(0, max_chars - 1)] + "…"
    return flat


def is_key_event(level_no: int, message: str) -> bool:
    """判定一条日志是否进缓冲：≥WARNING 全收；INFO 仅白名单里程碑。"""
    if level_no >= logging.WARNING:
        return True
    if level_no >= logging.INFO:
        return any(mark in message for mark in KEY_INFO_MARKS)
    return False


class RecentEventsHandler(logging.Handler):
    """进程内最近事件环形缓冲 Handler（GET /api/recent-errors 的数据源）。

    - 只收 WARNING+ 与关键 INFO（DEBUG 永远不进——轮询类噪音必须留在文件里）；
    - 每条事件：``{"ts": epoch 秒, "level": "ERROR", "line": 单行截断正文}``；
    - emit 内任何异常都吞掉（record 与 emit 本身可能带坑，观测面故障绝不
      反噬业务日志管线——logging 框架对 handler 异常只打 stderr，这里直接
      自兜底）。
    """

    def __init__(self, capacity: int = MAX_EVENTS) -> None:
        super().__init__()
        self._events: "deque[dict]" = deque(maxlen=max(1, int(capacity)))
        self.dropped_non_key = 0  # 观测：被白名单挡掉的 INFO 行数

    # logging.Handler 线程锁（self.lock）内做缓冲读写
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            message = record.getMessage()
            if not is_key_event(record.levelno, message):
                self.dropped_non_key += 1
                return
            ts = getattr(record, "created", None)
            event = {
                "ts": float(ts) if ts is not None else 0.0,
                "level": record.levelname,
                "line": to_single_line(message),
            }
            with self.lock:
                self._events.append(event)
        except Exception:  # noqa: BLE001 —— 观测面故障绝不反噬日志管线
            self.handleError(record)

    def snapshot(self, limit: int = MAX_EVENTS) -> "list[dict]":
        """按「最新在前」返回缓冲副本（控制台状态卡倒序展示）。"""
        limit = max(0, int(limit))
        with self.lock:
            events = list(self._events)
        return list(reversed(events))[:limit]

    def __len__(self) -> int:
        with self.lock:
            return len(self._events)


# ---------------------------------------------------------------------------
# 进程级单例（__main__ 装日志时挂到 root logger；控制台端点读同一实例）
# ---------------------------------------------------------------------------

_GLOBAL_HANDLER: "RecentEventsHandler | None" = None


def get_global_handler() -> RecentEventsHandler:
    """取进程级环形缓冲单例（懒创建——独立运行控制台也有缓冲可挂）。"""
    global _GLOBAL_HANDLER
    if _GLOBAL_HANDLER is None:
        _GLOBAL_HANDLER = RecentEventsHandler()
    return _GLOBAL_HANDLER


def attach_global_handler(root_logger: "logging.Logger | None" = None) -> RecentEventsHandler:
    """把进程级缓冲挂到 root logger（幂等；__main__._setup_logging 调用）。

    幂等性：重复装配（重复调用/重启内嵌控制台）绝不重复挂同一个实例，
    否则一条日志写两份缓冲、事件重复刷屏。
    """
    handler = get_global_handler()
    target = root_logger or logging.getLogger()
    for existing in target.handlers:
        if existing is handler:
            return handler
    handler.setLevel(logging.INFO)  # DEBUG 噪音不进缓冲（白名单双保险之一）
    target.addHandler(handler)
    return handler


# ---------------------------------------------------------------------------
# 跨重启兜底：解析 vl.log 尾部
# ---------------------------------------------------------------------------

def read_log_tail(
    log_path: "Path | str",
    *,
    lines: int = 400,
    max_events: int = MAX_EVENTS,
) -> "list[dict]":
    """读 vl.log 尾部并解析出与缓冲同构的事件列表（最新在前）。

    - 先按行数取尾部（默认 400 行——异常栈多行占位后仍足够覆盖 50 条事件），
      再逐行匹配 ``asctime LEVEL name: message`` 格式；
    - 只保留 WARNING+ 与关键 INFO（与进程内缓冲同一 is_key_event 口径，
      避免重启前后两个来源的过滤标准不一致）；
    - traceback 续行并入其前最近一条关键事件的末尾（只取**最后一条**续行
      ——异常链的终点行如 ``LLMError: chat HTTP 403：...`` 才是因果所在；
      实证 vl.log:4420，头行只有「LLM 轮处理异常」，403 在末行）；
    - 文件缺失/读不动返回 []，绝不抛异常。
    """
    path = Path(log_path)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-max(1, int(lines)):]
    except OSError:
        return []
    events: "list[dict]" = []
    last_ref: "dict | None" = None  # 最近一条关键事件（续行并入对象）
    for raw in tail:
        line = raw.rstrip("\n")
        matched = _LOG_LINE_RE.match(line)
        if matched is None:
            # traceback 续行：并入前一条关键事件（只留最后一条，后续覆盖）
            stripped = line.strip()
            if last_ref is not None and stripped:
                last_ref["_cont"] = stripped
            continue
        stamp, level, _name, message = matched.groups()
        if not is_key_event(getattr(logging, level, 0), message):
            last_ref = None  # 非关键事件打头的关键事件归属终止
            continue
        event = {
            "ts": _parse_stamp(stamp),
            "level": level,
            "line": to_single_line(message),
        }
        events.append(event)
        last_ref = event
    for event in events:
        cont = event.pop("_cont", None)
        if cont:
            event["line"] = to_single_line(event["line"] + " ⏎ " + cont)
    return list(reversed(events))[: max(0, int(max_events))]


def _parse_stamp(stamp: str) -> float:
    """把 ``YYYY-MM-DD HH:MM:SS[,sss]`` 解析为 epoch 秒；解析不动返回 0。"""
    from datetime import datetime

    text = stamp.replace(",", ".")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def recent_events_payload(
    log_path: "Path | str | None" = None,
    *,
    handler: "RecentEventsHandler | None" = None,
) -> dict:
    """GET /api/recent-errors 的响应体：``{source, events, count}``。

    - source="memory"：进程内环形缓冲（本进程启动以来的事件）；
    - source="log"：缓冲为空（刚重启/独立运行无装配）时退回 vl.log 尾部
      ——跨重启兜底，重启前的 403 哑火也能在控制台看到；
    - events 最新在前；两来源都拿不到时返回空列表（如实，绝不编造）。
    """
    # 显式判 None（本类实现了 __len__：空缓冲的 handler 是假值，
    # `handler or ...` 会把「新而空」的注入实例错误地替换成单例）
    active = handler if handler is not None else get_global_handler()
    events = active.snapshot()
    source = "memory"
    if not events and log_path is not None:
        events = read_log_tail(log_path)
        source = "log"
    return {"source": source, "events": events, "count": len(events)}
