# -*- coding: utf-8 -*-
"""brain/history.py —— 会话存储 + 双写镜像（SPEC §6）。

存储面（SPEC §6 定稿 schema）::

    messages(id, session_id, role, content TEXT, created_at INT, meta JSON)

- content 列存规整后的纯文本（content_to_text 扁平化），上下文装配与
  "system_reminder 残留=0" 类检查都基于该列；
- meta 列存调用方附加的 JSON（如来源标记），本层不自动塞业务字段；
- 新消息必带时间戳（缺省取注入的 clock）；旧迁移数据时间戳留空不可伪造，
  由 tools/history_migrate.py 负责（SPEC §11.2）。

双写镜像（SPEC §6）：
- 切换窗口内每轮 append 同步追加一行 JSONL 到 mirror_v4.json，
  行结构 ``{"role", "content": 原格式, "ts"}``（content 保留双形态原格式，
  回退脚本据此合并回 data_v4.db）；
- append 语义 = 追加写，不重写文件；镜像失败只告警不阻塞主库写入
  （主库是唯一事实源，镜像服务于 14 天回退窗口）。

时间戳精度（PERF_SPEC §7.2 埋点改毫秒）：
- 新写入行 created_at 取毫秒精度（clock() 秒浮点 × 1000 取整）——秒级取整
  会把 0.5s 连发间隔坍缩成同秒，测不出真实节奏；
- 量纲判别惯例：毫秒值 ≥ 1e12，旧秒级值 ~1e9，迁移旧行为 NULL（迁移工具
  「旧数据无消息级时间戳不可伪造」，原样保留不回填）；
- 显式传入 created_at 的调用方自带量纲责任，本层原样入库不改写。

并发模型：SQLite 连接 check_same_thread=False + 内部线程锁，写串行读共享；
事件循环侧经 asyncio.to_thread 调用（与 workbench/插件既有模式一致）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .llm import content_to_text

logger = logging.getLogger("virtuallover.brain.history")

# 新写入行的时间戳精度：毫秒（PERF_SPEC §7.2——秒级坍缩测不出 0.5s 连发间隔）
_TIMESTAMP_SCALE_MS = 1000

_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        meta TEXT NOT NULL DEFAULT '{}'
    )
    """
)
_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
# R2 日记日期过滤的支撑索引：(session_id, created_at) 让 get_messages_since
# 的「本会话 + 当日起始时刻之后」走索引范围扫描，不触发全表扫描
_INDEX_SINCE_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_messages_session_created "
    "ON messages(session_id, created_at)"
)

Clock = Callable[[], float]


class HistoryStore:
    """messages 表存储 + mirror_v4.json 双写镜像。"""

    def __init__(
        self,
        db_path: "str | Path",
        *,
        mirror_path: "str | Path | None" = None,
        clock: Clock = time.time,
    ) -> None:
        self.db_path = Path(db_path)
        self.mirror_path = Path(mirror_path) if mirror_path else None
        self._clock = clock
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # synchronous=NORMAL（PERF_SPEC §3 O8，与 memory/store.py 同批对齐）：
        # WAL 模式下 NORMAL 只在检查点时同步，持久性由 WAL 兜底（掉电最多丢
        # 最近事务、库不损坏），换掉每个提交的 fsync 磁盘等待
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA_SQL)
        self._conn.execute(_INDEX_SQL)
        self._conn.execute(_INDEX_SINCE_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(
        self,
        session_id: str,
        role: str,
        content: Any,
        *,
        created_at: "int | None" = None,
        meta: "dict | None" = None,
        mirror: bool = True,
    ) -> int:
        """写入一条消息并镜像，返回新行 id。

        - content 接受双形态（字符串 / 分段列表）：纯文本入 content 列，
          原格式进镜像行（SPEC §6「content 原格式」）；
        - created_at 缺省取 clock()（毫秒精度 int，PERF_SPEC §7.2），新消息
          绝不留空时间戳；显式传入的值原样入库（量纲由调用方负责）；
        - mirror=False 跳过镜像（迁移工具批量导入迁移数据时使用）。
        """
        if not session_id:
            raise ValueError("session_id 不能为空")
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"非法 role：{role!r}")
        text = content_to_text(content)
        if created_at is None:
            created_at = int(self._clock() * _TIMESTAMP_SCALE_MS)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at, meta) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(session_id), role, text, int(created_at), meta_json),
            )
            self._conn.commit()
            row_id = int(cursor.lastrowid)
        if mirror:
            self._append_mirror(role, content, int(created_at))
        return row_id

    def append_pair(
        self,
        session_id: str,
        user_content: Any,
        assistant_content: Any,
        *,
        created_at: "int | None" = None,
        meta: "dict | None" = None,
        mirror: bool = True,
    ) -> "tuple[int, int]":
        """写入一轮 user+assistant（SPEC §6「每轮 user+assistant」）。

        两行共用同一时间基准（同一次 clock 取样的毫秒值相同），镜像同样成对追加。
        """
        if created_at is None:
            created_at = int(self._clock() * _TIMESTAMP_SCALE_MS)
        user_id = self.append(
            session_id, "user", user_content,
            created_at=created_at, meta=meta, mirror=mirror,
        )
        assistant_id = self.append(
            session_id, "assistant", assistant_content,
            created_at=created_at, meta=meta, mirror=mirror,
        )
        return user_id, assistant_id

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_messages(
        self, session_id: str, *, limit: "int | None" = None
    ) -> list[dict]:
        """按时间升序取某会话的 [{"role","content"}]（纯文本，装配用）。

        limit 取最近 N 条（升序返回，语义不变）。
        """
        rows = self._select(session_id, limit)
        return [{"role": r[1], "content": r[2]} for r in rows]

    def get_messages_since(
        self, session_id: str, since: int, *, limit: "int | None" = None
    ) -> list[dict]:
        """取 created_at >= since 的消息（升序 [{"role","content"}]，R2 日记用）。

        - since 与表内 created_at 同量纲（本库缺省写入是毫秒；显式传秒的调用方
          自带量纲责任——量纲不一致会过滤出空集或全集，调用方自证）；
        - limit 取过滤集中最近 N 条再回排升序（与 get_messages 同语义）；
        - 走 idx_messages_session_created 索引，五万行库也只是范围扫描。
        """
        query = (
            "SELECT id, role, content, created_at, meta FROM messages "
            "WHERE session_id = ? AND created_at >= ? ORDER BY id ASC"
        )
        params: "list[Any]" = [str(session_id), int(since)]
        if limit is not None and limit > 0:
            query = (
                "SELECT id, role, content, created_at, meta FROM messages "
                "WHERE session_id = ? AND created_at >= ? "
                "ORDER BY id DESC LIMIT ?"
            )
            params.append(int(limit))
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        if limit is not None and limit > 0:
            rows.reverse()
        return [{"role": r[1], "content": r[2]} for r in rows]

    def get_records(
        self, session_id: str, *, limit: "int | None" = None
    ) -> list[dict]:
        """按时间升序取完整行（含 id/created_at/meta，对账与调试用）。"""
        rows = self._select(session_id, limit)
        records = []
        for row_id, role, content, created_at, meta_json in rows:
            try:
                meta = json.loads(meta_json)
            except (json.JSONDecodeError, TypeError):
                meta = {}
            records.append({
                "id": row_id,
                "role": role,
                "content": content,
                "created_at": created_at,
                "meta": meta,
            })
        return records

    def _select(self, session_id: str, limit: "int | None") -> list[tuple]:
        query = (
            "SELECT id, role, content, created_at, meta FROM messages "
            "WHERE session_id = ? ORDER BY id ASC"
        )
        params: "list[Any]" = [str(session_id)]
        if limit is not None and limit > 0:
            # 取最近 N 条再按 id 升序回排，保持旧→新语义
            query = (
                "SELECT id, role, content, created_at, meta FROM messages "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?"
            )
            params.append(int(limit))
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        if limit is not None and limit > 0:
            rows.reverse()
        return rows

    def first_created_at(self, session_id: str) -> "int | None":
        """取某会话最早一条消息的 created_at（毫秒；R9 纪念日锚点用，只读）。

        会话无任何消息（新安装/迁移未跑）返回 None——锚点不可伪造，宁缺勿猜。
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT MIN(created_at) FROM messages WHERE session_id = ?",
                (str(session_id),),
            )
            row = cursor.fetchone()
        value = row[0] if row else None
        return int(value) if value is not None else None

    def list_day(
        self,
        session_id: str,
        *,
        start_ms: int,
        end_ms: int,
        offset: int = 0,
        limit: int = 200,
    ) -> "tuple[list[dict], int]":
        """取 [start_ms, end_ms) 时间窗内某会话的消息（升序）与窗口内总数。

        R9 对话回放器的存储面（只读）：与 created_at 同量纲（本库缺省毫秒）；
        走 idx_messages_session_created 索引，单日窗口是范围扫描。返回
        (records, total)：records 为含 id/role/content/created_at 的完整行
        （不含 meta——回放面不展示调用方附加元数据），total 为该窗口总条数
        （供分页计算，与 limit/offset 无关）。
        """
        if end_ms <= start_ms:
            return [], 0
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE session_id = ? AND created_at >= ? AND created_at < ?",
                (str(session_id), int(start_ms), int(end_ms)),
            )
            total = int(cursor.fetchone()[0])
            cursor = self._conn.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE session_id = ? AND created_at >= ? AND created_at < ? "
                "ORDER BY id ASC LIMIT ? OFFSET ?",
                (str(session_id), int(start_ms), int(end_ms), limit, offset),
            )
            rows = cursor.fetchall()
        records = [
            {"id": int(r[0]), "role": r[1], "content": r[2], "created_at": int(r[3])}
            for r in rows
        ]
        return records, total

    def count(self, session_id: "str | None" = None) -> int:
        """统计消息数；session_id 为 None 时统计全表。"""
        with self._lock:
            if session_id is None:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            else:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (str(session_id),)
                )
            return int(cursor.fetchone()[0])

    def clear(self, session_id: str) -> int:
        """删除某会话全部消息（/eval 清理评测会话用），返回删除行数。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (str(session_id),)
            )
            self._conn.commit()
            return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # 双写镜像
    # ------------------------------------------------------------------

    def _append_mirror(self, role: str, content: Any, ts: int) -> None:
        """追加一行 JSONL 到镜像文件（content 保留原格式，SPEC §6）。

        镜像失败只告警：主库已提交，回退窗口的完整性靠告警暴露与补迁。
        """
        if self.mirror_path is None:
            return
        line = json.dumps(
            {"role": role, "content": content, "ts": ts}, ensure_ascii=False
        )
        try:
            self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self.mirror_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            logger.error("[history] 镜像写入失败（主库不受影响）：%s", exc)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭连接（进程退出或测试夹具清理时调用）。"""
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def setup(ctx: Any) -> "Callable[[], None]":
    """按 cfg.storage 打开 history.db 与 mirror_v4.json，实例挂 ctx.history。

    路径均相对运行根（ctx.repo_root，默认仓库根；冒烟/测试可指向 tmp）。
    返回同步 cleanup（关闭 SQLite 连接）。
    """
    storage = ctx.cfg.storage
    mirror_path = (Path(ctx.repo_root) / storage.mirror_file) if storage.mirror_file else None
    ctx.history = HistoryStore(
        Path(ctx.repo_root) / storage.history_db,
        mirror_path=mirror_path,
    )
    logger.info("[history] 会话库就绪：%s（镜像 %s）",
                storage.history_db, storage.mirror_file or "<禁用>")

    def cleanup() -> None:
        try:
            ctx.history.close()
        except Exception:  # noqa: BLE001 —— 清理失败不阻断其余清理
            logger.exception("[history] 关闭会话库失败（忽略）")

    return cleanup
