# -*- coding: utf-8 -*-
"""日记日期过滤契约测试（五轮修缮 R2 任务 5：lonely 路径复活）。

背景：旧实现 ``diary.setup → get_messages(最近 N 条)`` 不看日期——他今天
没来，昨天的对话照样灌进今天日记的素材区，且 LONELY 模板（无对话日 60-120
字短篇）永远走不到。本文件钉死修复后的契约：

1. HistoryStore.get_messages_since：created_at >= since 过滤 + (session_id,
   created_at) 索引存在（EXPLAIN QUERY PLAN 不全表扫描）+ limit 最近 N 条
   升序 + 量纲按毫秒（与库缺省写入一致）。
2. diary 换用：diary.setup 的 _get_history 只取「今天」——
   - 昨天有对话、今天没有 → write_today 走 LONELY 路径（有心情素材）；
   - 今天有对话 → 正常路径（素材含今天的话）。

安全规约：占位 session/QQ 号；时间用 HistoryStore 的可注入 clock 控制。
午夜竞态免疫（门禁在 00:00 边界踩过）：「今天起点」取一次存 start 常量，
写入时间戳锚定 start（今天零点 ± 偏移），查询门限复用同一 start——三者
同源后跨午夜翻转不再把「今天」误滤成空；测产品闭包的用例 monkeypatch
diary.today_start_ms 固定同一锚；write_today 显式传 today 与存档断言同源。
运行：python -m pytest virtuallover/tests/test_diary_since.py -q
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from virtuallover.brain.history import HistoryStore
from virtuallover.config import DiaryConfig
from virtuallover.soul import diary as diary_mod


def _today_start_ms() -> int:
    """今天本地零点的毫秒时刻（与 diary.setup 的 _get_history 同一口径）。"""
    return int(datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


# ---------------------------------------------------------------------------
# 1) HistoryStore.get_messages_since
# ---------------------------------------------------------------------------

def test_get_messages_since_filters_by_created_at(tmp_path):
    """只取 since 之后的行，升序；limit 取最近 N 条再回排升序。"""
    now_ms = int(time.time() * 1000)
    store = HistoryStore(tmp_path / "history.db", mirror_path=None)
    try:
        yesterday = now_ms - 86_400_000
        store.append("会话:FriendMessage:10001", "user", "昨天的话", created_at=yesterday)
        store.append("会话:FriendMessage:10001", "assistant", "昨天的回复",
                     created_at=yesterday + 1)
        today_id = store.append("会话:FriendMessage:10001", "user", "今天早上的话",
                                created_at=now_ms - 1000)
        store.append("会话:FriendMessage:10001", "assistant", "今天的回复",
                     created_at=now_ms - 500)
        store.append("会话:FriendMessage:10001", "user", "刚刚的话", created_at=now_ms)

        since = now_ms - 2000
        msgs = store.get_messages_since("会话:FriendMessage:10001", since)
        assert [m["content"] for m in msgs] == ["今天早上的话", "今天的回复", "刚刚的话"], (
            f"日期过滤违约（含了昨天）：{msgs!r}")

        limited = store.get_messages_since("会话:FriendMessage:10001", since, limit=2)
        assert [m["content"] for m in limited] == ["今天的回复", "刚刚的话"], (
            "limit 应取最近 N 条且保持升序")
        assert today_id > 0
    finally:
        store.close()


def test_get_messages_since_empty_today_returns_empty(tmp_path):
    """今天零条（他没来）→ 空列表——diary lonely 路径的判定基础。"""
    now_ms = int(time.time() * 1000)
    store = HistoryStore(tmp_path / "history.db", mirror_path=None)
    try:
        store.append("会话:FriendMessage:10001", "user", "只有昨天的话",
                     created_at=now_ms - 86_400_000)
        assert store.get_messages_since("会话:FriendMessage:10001",
                                        _today_start_ms()) == []
    finally:
        store.close()


def test_since_index_exists_and_query_uses_it(tmp_path):
    """/api 契约的支撑索引存在，且 since 查询走索引（5 万行不全量读）。"""
    store = HistoryStore(tmp_path / "history.db", mirror_path=None)
    try:
        indexes = {
            row[1] for row in store._conn.execute(
                "PRAGMA index_list(messages)").fetchall()
        }
        assert "idx_messages_session_created" in indexes, (
            f"缺 (session_id, created_at) 索引：{indexes!r}")
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM messages "
            "WHERE session_id = ? AND created_at >= ?",
            ("会话:FriendMessage:10001", 0),
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan).lower()
        assert "scan" not in detail or "index" in detail, (
            f"since 查询应走索引而非全表扫描：{plan!r}")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2) diary 换用：当日空 → lonely 路径复活
# ---------------------------------------------------------------------------

class _ScriptedChat:
    """假 llm_chat（DiaryService 契约：async (system, user) -> str）：记录调用。"""

    def __init__(self) -> None:
        self.calls: "list[tuple[str, str]]" = []

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return "今天我心里有些空落落的，等了一整天。"


def _make_service(tmp_path: Path, get_history, chat: _ScriptedChat) -> diary_mod.DiaryService:
    """构造走完 write_today 全流程的最小 DiaryService（对齐 setup 的接线面）。"""
    mood_file = tmp_path / "mood_states.json"
    mood_file.write_text(
        '{"10001": {"mood": "lonely", "mood_intensity": 0.4, "affection": 42,'
        ' "daily_streak": 1, "total_messages": 9, "last_interaction": 1,'
        ' "updated_at": 1}}',
        encoding="utf-8")
    return diary_mod.DiaryService(
        tmp_path / "diary",
        persona_prompt="占位人格。",
        mood_states_path=mood_file,
        mood_user_id="人格:FriendMessage:10001",
        llm_chat=chat,
        get_history=get_history,
    )


def test_diary_setup_history_reader_filters_to_today(tmp_path, monkeypatch):
    """diary.setup 的 _get_history 只取今天（日期过滤接线契约）。

    diary.today_start_ms 固定为 start：门限与写入时间戳同源，跨午夜翻转
    不再把「今天的话」误滤成空（产品闭包内联重算时的毫秒级竞态窗口）。
    """
    from virtuallover.config import VlConfig

    start = _today_start_ms()
    monkeypatch.setattr(diary_mod, "today_start_ms", lambda: start)
    cfg = VlConfig(session_id="人格:FriendMessage:10001", admins=["10000"],
                   whitelist=["10001"])
    cfg.diary = DiaryConfig()
    history = HistoryStore(tmp_path / "history.db", mirror_path=None)
    try:
        history.append(cfg.session_id, "user", "昨天的话",
                       created_at=start - 3_600_000)
        history.append(cfg.session_id, "user", "今天的话", created_at=start + 60_000)

        ctx = SimpleNamespace(cfg=cfg, repo_root=tmp_path, history=history,
                              chat_client_bg=None, chat_client=None)
        diary_mod.setup(ctx)
        service = ctx.diary_service
        got = asyncio.run(service.get_history())  # setup 注入的 async 闭包
        contents = [m["content"] for m in got]
        assert contents == ["今天的话"], f"setup 的历史读取应只含今天：{contents!r}"
    finally:
        history.close()


def test_write_today_goes_lonely_when_only_yesterday_had_chats(tmp_path):
    """他今天没来：昨天的对话不得救活素材区——LONELY 短篇路径必须复活。"""
    start = _today_start_ms()   # 门限与时间戳同源锚（午夜竞态免疫，见文件头）
    history = HistoryStore(tmp_path / "history.db", mirror_path=None)
    try:
        history.append("人格:FriendMessage:10001", "user", "昨天聊得很开心",
                       created_at=start - 3_600_000)
        history.append("人格:FriendMessage:10001", "assistant", "是呢昨天很开心",
                       created_at=start - 3_590_000)

        async def _history():  # 与 diary.setup 同款日期过滤（复用同一 start：
            # 重算会在跨午夜时翻到新零点，把「今天」误滤成空）
            return history.get_messages_since("人格:FriendMessage:10001",
                                              start, limit=60)

        chat = _ScriptedChat()
        service = _make_service(tmp_path, _history, chat)
        today = datetime.now().date().isoformat()
        text = asyncio.run(service.write_today(today=today))
        assert text, "lonely 路径应产出日记（旧实现此处因昨天素材而走普通路径）"
        assert len(chat.calls) == 1
        _system, user_prompt = chat.calls[0]
        assert "他今天没有来" in user_prompt, f"应使用 LONELY 模板：{user_prompt[:120]!r}"
        assert "昨天聊得很开心" not in user_prompt, "昨天的对话不得混入素材区"
        # 存档落盘（当日存档判重消费的文件确实存在；today 显式传入与断言同源）
        archive = tmp_path / "diary" / (today + ".md")
        assert archive.is_file()
    finally:
        history.close()


def test_write_today_normal_path_with_today_chats(tmp_path):
    """今天有对话：普通模板 + 素材只含今天的话。"""
    start = _today_start_ms()   # 门限与时间戳同源锚（午夜竞态免疫，见文件头）
    history = HistoryStore(tmp_path / "history.db", mirror_path=None)
    try:
        history.append("人格:FriendMessage:10001", "user", "昨天的话题",
                       created_at=start - 3_600_000)     # 今天零点前 1h，恒被滤
        history.append("人格:FriendMessage:10001", "user", "今天我们一起看了电影",
                       created_at=start + 60_000)        # 今天零点后 1min，恒命中

        async def _history():
            return history.get_messages_since("人格:FriendMessage:10001",
                                              start, limit=60)

        chat = _ScriptedChat()
        service = _make_service(tmp_path, _history, chat)
        text = asyncio.run(service.write_today())
        assert text
        _system, user_prompt = chat.calls[0]
        assert "他今天没有来" not in user_prompt, "有对话日不得走 LONELY 模板"
        assert "今天我们一起看了电影" in user_prompt
        assert "昨天的话题" not in user_prompt, "昨天的对话不得混入素材区"
    finally:
        history.close()


def test_write_today_skips_when_today_empty_and_no_mood(tmp_path):
    """今天零对话且无心情素材 → 如实跳过（返回 None，不硬编日记）。"""
    start = _today_start_ms()   # 门限与时间戳同源锚（午夜竞态免疫，见文件头）
    history = HistoryStore(tmp_path / "history.db", mirror_path=None)
    try:
        history.append("人格:FriendMessage:10001", "user", "昨天的话",
                       created_at=start - 3_600_000)

        async def _history():
            return history.get_messages_since("人格:FriendMessage:10001",
                                              start, limit=60)

        chat = _ScriptedChat()
        service = diary_mod.DiaryService(
            tmp_path / "diary", persona_prompt="占位。",
            llm_chat=chat, get_history=_history)  # 无 mood_states_path
        text = asyncio.run(service.write_today())
        assert text is None and chat.calls == []
    finally:
        history.close()


def test_history_db_schema_includes_since_index(tmp_path):
    """迁移面：老库文件（无该索引）打开后自动补建（CREATE INDEX IF NOT EXISTS）。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,"
        " created_at INTEGER NOT NULL, meta TEXT NOT NULL DEFAULT '{}')")
    conn.commit()
    conn.close()
    store = HistoryStore(db, mirror_path=None)
    try:
        names = {row[1] for row in store._conn.execute(
            "PRAGMA index_list(messages)").fetchall()}
        assert "idx_messages_session_created" in names
    finally:
        store.close()
