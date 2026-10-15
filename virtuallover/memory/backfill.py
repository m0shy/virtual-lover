# -*- coding: utf-8 -*-
"""历史记忆回填 —— SPEC §7.4（M2 主任务）。

流程：只读打开旧库 data_v4.db 的 conversations 表 → 展开消息（120 条，role/content
JSON 数组，PRAGMA 实测）→ 清洗（剥 think 段与 <system_reminder>，对话本体保留）→
按 num_pairs*2 条分窗 → LLM 第一人称总结（prompt 与 service 共用）→ embedding →
入库。幂等可重跑：每窗以「会话+消息区间+内容摘要」生成 window_key 写入 metadata，
重跑时已存在的窗直接跳过——部分失败后再跑只补缺失的窗。

clean-room 说明：只依据 SPEC 行为规格实现；旧库仅以 SQLite 只读模式（mode=ro）
访问表结构与数据，绝不读取任何容器内源码。

LLM 接线：chat 回调可注入；未注入时经 service.resolve_chat_fn() try-import
brain.llm（并行开发中）占位适配；嵌入走 store 注入的 embedder。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .service import (
    SUMMARY_MAX_CHARS,
    ChatFn,
    build_summary_prompts,
    clean_message_text,
    format_window_lines,
    message_content_text,
    resolve_chat_fn,
)
from .store import LLMNotWiredError, MemoryStore

logger = logging.getLogger("virtuallover.memory.backfill")

BACKFILL_SOURCE = "backfill"   # 回填入库记忆的 metadata.source 标记
DEFAULT_NUM_PAIRS = 2          # 与 service 一致：每窗 num_pairs*2 条消息

# 参与总结的消息角色：只取对话双方，系统/工具类消息不入记忆窗
_VALID_ROLES = frozenset({"user", "assistant"})

# 旧库 updated_at 的候选解析格式（SQLite DATETIME 常见形态，PRAGMA 实测为字符串列）
_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)


# ---------------------------------------------------------------------------
# 数据结构与报告
# ---------------------------------------------------------------------------

@dataclass
class BackfillReport:
    """回填执行报告（验收与对账用，SPEC §7.4）。

    - total_messages：清洗过滤后参与分窗的消息数（预期 120）；
    - skipped：已存在（幂等跳过）或清洗后无有效内容的窗数；
    - errors：失败窗的 window_key 与原因，重跑只补这些窗。
    """

    session_id: str
    total_messages: int = 0
    window_size: int = 0
    windows: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 旧库读取与窗口构造
# ---------------------------------------------------------------------------

def parse_history_datetime(raw: Any) -> int | None:
    """解析旧库 DATETIME 字符串为 epoch 秒（本地时区口径）；失败返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    logger.debug("无法解析历史时间戳 %r（忽略，create_time 回退回填时刻）", raw)
    return None


def load_history_messages(db_path: str | Path) -> list[dict]:
    """只读加载 data_v4.db conversations 里的全部对话消息（加载即清洗）。

    - 表结构经 PRAGMA 实测：content 列为 JSON 数组，元素形如 {role, content}，
      content 兼容字符串与分段列表双形态；
    - 只保留 user/assistant 双方角色、且清洗（剥 think 段与
      <system_reminder>）后文本非空的消息——「有效消息」口径在此统一，
      后续分窗不会出现空窗（顺序 = 会话内原文顺序，会话间按
      inner_conversation_id 升序）；
    - 每条附带来源信息：conversation_id / persona_id / period_ts（会话
      updated_at 解析值，作该窗记忆的 create_time 依据）；
    - 以 mode=ro 打开，绝不写旧库。
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"旧历史库不存在：{path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    messages: list[dict] = []
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT conversation_id, persona_id, content, updated_at"
            " FROM conversations ORDER BY inner_conversation_id"
        ).fetchall()
    finally:
        conn.close()

    for conv_id, persona_id, content_raw, updated_at in rows:
        try:
            items = json.loads(content_raw) if content_raw else []
        except (TypeError, ValueError):
            logger.warning("会话 %s 的 content 不是合法 JSON，整段跳过", conv_id)
            continue
        if isinstance(items, dict):  # 容错：个别历史形态可能为单对象
            items = [items]
        if not isinstance(items, list):
            logger.warning("会话 %s 的 content 结构异常（%s），整段跳过",
                           conv_id, type(items).__name__)
            continue
        period_ts = parse_history_datetime(updated_at)
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "") or "")
            if role not in _VALID_ROLES:
                continue
            text = clean_message_text(message_content_text(item.get("content")))
            if not text:
                continue
            messages.append({
                "role": role,
                "content": text,
                "conversation_id": str(conv_id or ""),
                "persona_id": str(persona_id or ""),
                "period_ts": period_ts,
            })
    logger.info("旧库 %s 加载对话消息 %d 条（conversations=%d）",
                path, len(messages), len(rows))
    return messages


def build_windows(messages: list[dict], window_size: int) -> list[list[dict]]:
    """按固定窗长顺序切片（窗长 = num_pairs*2）；窗边界由消息序号唯一确定。"""
    if int(window_size) < 1:
        raise ValueError(f"window_size 非法：{window_size!r}")
    size = int(window_size)
    return [messages[i:i + size] for i in range(0, len(messages), size)]


def _window_digest(window: list[dict]) -> str:
    """窗口内容摘要（sha256）：清洗后文本拼接，用于 window_key 幂等判定。"""
    digest = hashlib.sha256()
    for item in window:
        digest.update(str(item.get("role", "")).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(item.get("content", "")).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def make_window_key(session_id: str, start: int, end: int, digest: str) -> str:
    """生成窗幂等键：会话 + 消息区间 + 内容摘要前缀（写入 metadata.window_key）。"""
    return f"{session_id}#{start}-{end}#{digest[:16]}"


# ---------------------------------------------------------------------------
# 回填主流程
# ---------------------------------------------------------------------------

async def run_backfill(
    store: MemoryStore,
    *,
    session_id: str,
    db_path: str | Path,
    persona_id: str = "",
    num_pairs: int = DEFAULT_NUM_PAIRS,
    chat_fn: ChatFn | None = None,
    max_summary_chars: int = SUMMARY_MAX_CHARS,
    progress: Callable[[int, int], None] | None = None,
) -> BackfillReport:
    """执行历史回填（幂等，可重跑；SPEC §7.4）。

    - session_id：记忆归属会话（旧栈 unified_msg_origin 格式，从配置带出）；
    - persona_id：记忆归属人格，会话行自带的 persona_id 优先于该缺省值；
    - 窗口径：每 num_pairs*2 条消息一窗，逐窗总结入库；窗 create_time 取该窗
      最后一条消息所在会话的 updated_at（近似对话发生时刻，缺省回退当前时刻）；
    - 单窗失败只记入 report.errors 继续后续窗；重跑时已入库的窗按 window_key
      跳过，只补失败与缺失的窗。

    返回 BackfillReport；库元数据校验未通过时抛 MemoryMetaMismatchError。
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("session_id 为空（应从配置迁移的会话模板带出）")
    store.ensure_usable()
    window_size = int(num_pairs) * 2

    messages = load_history_messages(db_path)
    windows = build_windows(messages, window_size)
    report = BackfillReport(
        session_id=session_id,
        total_messages=len(messages),
        window_size=window_size,
        windows=len(windows),
    )
    if not windows:
        logger.warning("旧库无可回填消息（%s）", db_path)
        return report

    # 幂等基线：本会话已有记忆的 window_key 集合
    existing_keys: set[str] = {
        str(meta.get("window_key"))
        for meta in store.session_metadata_map(session_id).values()
        if isinstance(meta, dict) and meta.get("window_key")
    }

    chat = chat_fn or resolve_chat_fn()
    for index, window in enumerate(windows):
        if progress is not None:
            progress(index + 1, len(windows))
        start = index * window_size
        end = start + len(window)

        # 消息已在加载阶段清洗，这里只防御性确认摘录行非空
        lines = format_window_lines(window)
        if not lines:
            report.skipped += 1
            continue

        digest = _window_digest(window)
        key = make_window_key(session_id, start, end, digest)
        if key in existing_keys:
            report.skipped += 1
            continue

        try:
            if chat is None:
                raise LLMNotWiredError(
                    "chat 回调未注入且 brain.llm 未就绪，回填无法总结"
                )
            system, user = build_summary_prompts(lines, max_summary_chars)
            summary = clean_message_text(str(await chat(system, user) or ""))
            summary = summary.strip()
            if not summary:
                raise RuntimeError("LLM 返回空总结")
            period_ts = window[-1].get("period_ts")
            meta = {
                "source": BACKFILL_SOURCE,
                "window_key": key,
                "window_start": start,
                "window_end": end,
                "msg_count": len(lines),
                "conversation_id": window[0].get("conversation_id", ""),
                "history_period_end": period_ts,
            }
            await store.remember(
                summary,
                session_id,
                persona_id=persona_id or str(window[0].get("persona_id") or ""),
                metadata=meta,
                # 窗内无可靠逐条时间戳（旧库 content 不含时刻），取会话级时间
                create_time=period_ts if period_ts else int(time.time()),
            )
            report.written += 1
            logger.info("回填窗口 %d/%d 入库（%s）", index + 1, len(windows), key)
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"{key}: {exc}")
            logger.warning("回填窗口 %d/%d 失败（%s）：%s",
                           index + 1, len(windows), key, exc)

    logger.info(
        "回填结束：消息 %d 条 / %d 窗，入库 %d、跳过 %d、失败 %d",
        report.total_messages, report.windows,
        report.written, report.skipped, report.failed,
    )
    return report
