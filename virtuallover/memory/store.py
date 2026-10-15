# -*- coding: utf-8 -*-
"""内嵌向量记忆库 —— SPEC §7.1 schema / §7.3 注入格式（记忆参谋章节）。

职责边界：
- 本模块只做「存与查」：SQLite 单文件库（memories 表）+ numpy 余弦检索 +
  /remember 手动写入 API（/eval 依赖）；写入触发策略在 service.py，
  历史回填在 backfill.py；不感知 AstrBot/pymilvus（clean-room）。

schema（SPEC §7.1 全字段，逐列对应）：
    memories(id PK, session_id TEXT, persona_id TEXT, content TEXT,
             metadata JSON, create_time INT, embedding BLOB float32[dim],
             embed_model TEXT, dim INT)
- embedding 以 float32 小端字节序存 BLOB；dim 由配置注入（现网 2048）。
- 另有两张内部表（对 SPEC 的最小实现补充）：
  * store_meta —— 库元数据（embedding provider/model/endpoint/dim + schema_version）。
    启动校验：与当前嵌入配置逐项比对，不一致则进入封锁态，之后一切写入与检索
    都抛 MemoryMetaMismatchError（fail-fast 拒写拒检索，绝不静默混用旧向量）；
  * service_state —— §7.2 写入触发的计数/待总结缓冲持久化。旧栈把计数落在独立
    sqlite（黑盒实测其 message_counts 表为 session_id/count 两列）；此处并入同库，
    配合 atomic() 事务块实现「记忆入库 + 计数清零」同生共死。

检索管道（SPEC §7.1）：session_id 精确过滤 → numpy 余弦 → 取候选 candidates=8 →
关键词重排（extract_query_keywords 简版）→ 返回 top_k=3（PERF_SPEC §3-O5 兜底补丁）。

规模预案（SPEC §7.1 注释要求）：命中会话的全部行以「检索矩阵缓存」驻留内存
做矩阵余弦（PERF_SPEC §3 O8）：1 万行 ×2048 维 float32 约 82MB、缓存命中
p50 ≤10ms（基准见 tests/test_memory_cache_perf.py，确定性合成语料）；
行数到 50 万量级时再切换 mmap / 分块检索路径，届时单独改造，v1 不预做。
量化/mmap/分块经十参谋实测为负优化，全盘否决（PERF_SPEC §3 O8）。

并发模型（PERF_SPEC §3 O8，对齐 brain/history.py 模式）：
- SQLite 连接 check_same_thread=False + 实例锁（threading.RLock）串行化全部
  连接操作；用 RLock 而非 Lock，因 atomic() 事务块跨多个方法调用，需可重入；
- WAL 日志 + busy_timeout(5000) + synchronous=NORMAL：多连接（含控制台等
  只读方）并发访问不互斥，WAL 下 NORMAL 只在检查点同步，提交不再逐次 fsync；
- 检索矩阵按会话缓存：失效点=remember 执行与 delete_session，失效与写同锁；
  缓存条目不可变（重建时整体替换、绝不原地改），读侧拿到的快照永远自洽；
- 嵌入调用走注入的 embedder 回调（协议见 EmbedFn），集成期由装配方接 brain.llm。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

import numpy as np

logger = logging.getLogger("virtuallover.memory.store")

# ---------------------------------------------------------------------------
# 常量（SPEC §4 / §7.1）
# ---------------------------------------------------------------------------

EMBED_DIM = 2048          # 嵌入维度：旧栈 embedding 模型 dim=2048（SPEC §4）
TOP_K = 3                 # 注入条数上限（PERF_SPEC §3-O5 兜底补丁：2→3）
CANDIDATES = 8            # 余弦粗排候选池大小（SPEC §7.1）
SCHEMA_VERSION = 1        # 库 schema 版本（升级迁移见 SPEC §13）

# 库元数据键（store_meta 表）；value 一律以字符串存储
META_PROVIDER = "embedding_provider"
META_MODEL = "embedding_model"
META_ENDPOINT = "embedding_endpoint"
META_DIM = "embedding_dim"
_META_KEYS = (META_PROVIDER, META_MODEL, META_ENDPOINT, META_DIM)

# <MNEMO_META> 元数据标签：旧栈记忆内容尾部附加的内部元数据，注入前剥离。
# 标签形态与 vl_eval/benchmark.py 一致（port-of-own，本项目自有代码）。
MNEMO_META_PREFIX = "<MNEMO_META>"
MNEMO_META_SUFFIX = "</MNEMO_META>"

# ---------------------------------------------------------------------------
# 类型别名与异常
# ---------------------------------------------------------------------------

# 嵌入回调协议：文本 → 向量（list[float] 或 np.ndarray，内部统一转 float32）。
# 集成期由装配方注入 brain.llm 的 embedding 客户端适配。
EmbedFn = Callable[[str], Awaitable["list[float] | np.ndarray"]]


class LLMNotWiredError(RuntimeError):
    """LLM/嵌入客户端未接线（brain.llm 并行开发中）。

    本包对 LLM 能力一律「可注入回调 + try-import 占位」：未注入且 brain.llm
    未就绪时抛出本异常，由上层决定降级或报错，绝不假装成功。
    """


class MemoryMetaMismatchError(RuntimeError):
    """库元数据与当前嵌入配置不一致（SPEC §7.1 fail-fast）：拒绝写入与检索。

    典型成因：更换 embedding provider/model/endpoint 或 dim 后未重建记忆库。
    处置：重建库（删除库文件后重跑 backfill）或把配置改回建库时的值。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"记忆库元数据校验失败，已拒绝写入与检索：{reason}")
        self.reason = reason


# ---------------------------------------------------------------------------
# 纯函数：元数据剥离 / 注入格式 / 查询关键词（SPEC §7.3 与重排简版）
# ---------------------------------------------------------------------------

def strip_memory_meta(text: str) -> str:
    """剥离记忆内容尾部的 <MNEMO_META>...</MNEMO_META> 元数据标签（§7.3）。

    - 无标签原样返回；标签截半（只有前标签）时丢弃前标签及其后残留；
    - 结果 strip 后返回，供注入格式与命中比对共用。
    """
    if not text:
        return ""
    start = text.rfind(MNEMO_META_PREFIX)
    if start == -1:
        return text
    end = text.find(MNEMO_META_SUFFIX, start)
    if end == -1:
        cleaned = text[:start]
    else:
        cleaned = text[:start] + text[end + len(MNEMO_META_SUFFIX):]
    return cleaned.strip()


# 相对时间分档的失真边界（R3 记忆质量）：
# - 未来超过 _TIME_FUTURE_SLACK 秒的时间视为失真（时钟漂移留 1 天余量）；
# - 早于 _TIME_FLOOR（2000-01-01）的时间视为失真（迁移残留的坏时间戳）；
#   两者与缺失时间统一落「较早」档，绝不把可疑时刻当真注入。
_TIME_FUTURE_SLACK = 86400   # 1 天
_TIME_FLOOR = 946684800      # 2000-01-01 00:00:00 本地时区 epoch 秒


def relative_time_label(create_time: int | None, now: float | int | None = None) -> str:
    """把记忆时间戳分档成相对时间标签（R3 记忆质量，SPEC §7.3 注入格式修订）。

    分档（按本地时区自然日差，today = now 所在日）：
        0 天        → 「今天」
        1 天        → 「昨天」
        2-6 天      → 「N天前」
        7-13 天     → 「上周」
        14-27 天    → 「N周前」（2周前/3周前——避免把三周前误称「上周」）
        ≥28 天      → 「N个月前」（按日历月差计，至少 1）
    NULL/失真（未来超 1 天、早于 2000 年）统一「较早」档，绝不伪造时刻。

    now 供测试注入固定时刻；缺省取当前时间。
    """
    if not create_time:
        return "较早"
    ts = int(create_time)
    now_ts = int(now) if now is not None else int(time.time())
    if ts > now_ts + _TIME_FUTURE_SLACK or ts < _TIME_FLOOR:
        return "较早"
    day = datetime.fromtimestamp(ts).date()
    today = datetime.fromtimestamp(now_ts).date()
    days = (today - day).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days <= 6:
        return f"{days}天前"
    if days <= 13:
        return "上周"
    if days <= 27:
        return f"{days // 7}周前"
    months = (today.year - day.year) * 12 + (today.month - day.month)
    if months < 1:
        # 同一自然月内跨 28+ 天的极端情形（如 1 日与 29/30 日）按 1 个月计
        months = 1
    return f"{months}个月前"


def format_memory_line(
    create_time: int | None,
    content: str,
    *,
    now: float | int | None = None,
) -> str:
    """渲染单条记忆注入行（SPEC §7.3 + R3 相对时间分档）：``- [今天] 内容``。

    - 时间不再给绝对时刻（R3：审计实证她会把绝对时间锚错档——「上周聊的」
      实为当天），改用 relative_time_label 的相对分档；NULL/失真统一「较早」；
    - 内容先剥 <MNEMO_META> 元数据，再折叠换行/连续空白为单空格，
      保证单行格式不被多行内容破坏；
    - now 供测试注入固定时刻（缺省当前时间，与生产一致）。
    """
    text = " ".join(strip_memory_meta(str(content or "")).split())
    stamp = relative_time_label(create_time, now)
    return f"- [{stamp}] {text}"


# 查询关键词抽取（重排简版，SPEC §7.1「extract_query_keywords 简版」）：
# ASCII 词取 >=2 连续字母数字下划线（大小写折叠）；中文连续段切二元组。
# 不做停用词表——重排只在 8 条候选内生效，宁滥勿缺是更安全的简版取向。
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def extract_query_keywords(text: str) -> list[str]:
    """从查询文本抽取用于重排的关键词（保序去重，简版）。

    - ASCII 词：连续 [A-Za-z0-9_]{2,}，折叠小写；
    - 中文段：连续汉字 >=2 的滑窗二元组（如「最喜欢的食物」→ 最喜/喜欢/欢的/…）；
    - 空文本返回空列表，此时检索退化为纯余弦排序。
    """
    if not text:
        return []
    keywords: list[str] = []
    seen: set[str] = set()

    def _push(word: str) -> None:
        if word and word not in seen:
            seen.add(word)
            keywords.append(word)

    for token in _ASCII_TOKEN_RE.findall(text):
        _push(token.lower())
    for run in _CJK_RUN_RE.findall(text):
        for i in range(len(run) - 1):
            _push(run[i:i + 2])
    return keywords


# ---------------------------------------------------------------------------
# 检索结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryHit:
    """单条记忆检索命中。cosine 为余弦相似度，keyword_hits 为查询关键词命中数。"""

    id: int
    session_id: str
    content: str
    metadata: dict
    create_time: int
    cosine: float
    keyword_hits: int


@dataclass(frozen=True)
class _SessionMatrix:
    """某会话的检索矩阵缓存条目（PERF_SPEC §3 O8）。

    「重建不原地改」纪律：本对象一经构造视为不可变——失效后重建时构造
    **新对象**整体替换字典槽位，绝不就地修改旧对象。这样检索方在锁外使用
    快照引用期间，即使另一连接写入并重建缓存，旧快照仍然自洽。
    """

    unit: np.ndarray        # (n, dim) float32：行向量已做 L2 归一化（零向量保持 0）
    rows: list[tuple]       # (id, content, metadata原文, create_time)——重排与命中元数据


# ---------------------------------------------------------------------------
# 库本体
# ---------------------------------------------------------------------------

_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        persona_id TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        create_time INTEGER NOT NULL,
        embedding BLOB NOT NULL,
        embed_model TEXT NOT NULL DEFAULT '',
        dim INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id)",
    """
    CREATE TABLE IF NOT EXISTS store_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS service_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
)


class MemoryStore:
    """内嵌向量记忆库（SPEC §7.1）。

    用法：构造时注入嵌入配置（provider/model/endpoint/dim）与 embedder 回调；
    库元数据校验通过后 remember()/search() 可用，校验失败则进入封锁态
    （两个方法都抛 MemoryMetaMismatchError），count()/kv_* 等只读操作不受限，
    便于控制台展示现状与触发原因。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedder: EmbedFn | None = None,
        provider_id: str = "",
        model: str = "",
        endpoint: str = "",
        dim: int = EMBED_DIM,
    ) -> None:
        if int(dim) <= 0:
            raise ValueError(f"嵌入维度非法：dim={dim!r}，必须为正整数")
        self.db_path = Path(db_path)
        self._embedder = embedder
        self._dim = int(dim)
        self._expected: dict[str, str] = {
            META_PROVIDER: str(provider_id),
            META_MODEL: str(model),
            META_ENDPOINT: str(endpoint),
            META_DIM: str(int(dim)),
        }
        # 实例锁（PERF_SPEC §3 O8，对齐 brain/history.py 模式）：串行化本实例
        # 全部连接操作。RLock 而非 Lock——atomic() 事务块跨多个方法调用
        # （remember/kv_set 以 commit=False 复入），需要可重入语义。
        self._lock = threading.RLock()
        # 检索矩阵会话级缓存（见 _SessionMatrix）；dirty=atomic 事务内有
        # 未提交写入（remember commit=False），提交时需整体失效
        self._matrix_cache: dict[str, _SessionMatrix] = {}
        self._matrix_dirty = False
        self._cache_hits = 0
        self._cache_misses = 0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # WAL + busy_timeout + synchronous=NORMAL（PERF_SPEC §3 O8）：
        # 读写不互斥；写写冲突等 5s；WAL 下 NORMAL 免逐次 fsync（持久性由
        # WAL 兜底，掉电最多丢最近事务、库不损坏）。备份链路依赖 WAL 化——
        # scripts/backup_guard.py 复制前 wal_checkpoint(TRUNCATE)。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for stmt in _SCHEMA_SQL:
            self._conn.execute(stmt)
        self._conn.commit()
        self._blocked_reason: str | None = self._validate_meta()

    # ------------------------------------------------------------------
    # 元数据 fail-fast 校验（SPEC §7.1）
    # ------------------------------------------------------------------

    def _validate_meta(self) -> str | None:
        """比对库元数据与当前嵌入配置；新库写入元数据。返回封锁原因或 None。"""
        with self._lock:
            return self._validate_meta_locked()

    def _validate_meta_locked(self) -> str | None:
        """_validate_meta 的持锁实现（调用方须持实例锁）。"""
        rows = self._conn.execute("SELECT key, value FROM store_meta").fetchall()
        stored = {str(k): str(v) for k, v in rows}
        if not stored:
            # 新库（或元数据丢失且无记忆行的空壳）：写入当前配置作为基线
            count = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if count:
                return "store_meta 表为空但 memories 已有数据，库元数据缺失"
            now = int(time.time())
            for key, value in self._expected.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO store_meta(key, value) VALUES(?, ?)",
                    (key, value),
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO store_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            self._conn.commit()
            logger.info("记忆库初始化完成：%s（dim=%s）", self.db_path, self._dim)
            return None

        for key in (*_META_KEYS, "schema_version"):
            if key not in stored:
                return f"库元数据缺少键 {key}"
        for key in _META_KEYS:
            if stored[key] != self._expected[key]:
                return (
                    f"{key} 不一致：库内={stored[key]!r}，当前配置={self._expected[key]!r}"
                )
        if stored["schema_version"] != str(SCHEMA_VERSION):
            return (
                f"schema_version 不一致：库内={stored['schema_version']!r}，"
                f"程序={SCHEMA_VERSION}（需走升级迁移，见 SPEC §13）"
            )
        # 数据级复核：已有行的 dim 列也必须一致，防历史脏数据混入
        bad = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE dim != ?", (self._dim,)
        ).fetchone()[0]
        if bad:
            return f"memories 表有 {bad} 行 dim 列与当前配置 {self._dim} 不一致"
        return None

    def ensure_usable(self) -> None:
        """写入/检索前的封锁检查（fail-fast 语义：抛错，绝不静默降级）。

        公开供 backfill 等批量流程在开工前预检，避免中途逐窗失败。
        """
        if self._blocked_reason is not None:
            raise MemoryMetaMismatchError(self._blocked_reason)

    @property
    def blocked_reason(self) -> str | None:
        """元数据校验失败原因；None 表示库可用。供控制台/日志展示。"""
        return self._blocked_reason

    def get_meta(self) -> dict[str, str]:
        """返回库元数据快照（含 schema_version）。"""
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM store_meta").fetchall()
            return {str(k): str(v) for k, v in rows}

    # ------------------------------------------------------------------
    # 嵌入与写入
    # ------------------------------------------------------------------

    async def embed_text(self, text: str) -> np.ndarray:
        """经注入的 embedder 取向量并规整为 float32 一维数组（校验维度）。

        公开供 persona（查询向量化）、service/backfill（总结向量化）复用；
        未接线时抛 LLMNotWiredError。
        """
        if self._embedder is None:
            raise LLMNotWiredError(
                "嵌入客户端未接线：请向 MemoryStore 注入 embedder 回调"
                "（集成期接 brain.llm embedding 客户端）。"
            )
        raw = await self._embedder(text)
        try:
            vec = np.asarray(raw, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"嵌入回调返回非法向量：{exc}") from exc
        if vec.size != self._dim:
            raise ValueError(
                f"嵌入维度不一致：回调返回 {vec.size} 维，库配置 {self._dim} 维"
            )
        return vec

    @contextmanager
    def atomic(self):
        """事务上下文：块内以 commit=False 累积的多步写，退出时一次提交。

        用于「记忆入库 + 服务状态清零」这类必须同生共死的组合写；
        块内异常整体回滚并原样抛出。

        实例锁语义（PERF_SPEC §3 O8）：RLock 全程持有（块内 commit=False
        的 remember/kv_set 可重入复入），事务期间其他线程的连接操作被挡在
        锁外——杜绝「半程事务被他人 commit 夹带提交」。因此块内禁止真实
        让出点（await 到其他协程/网络）：唯一调用面 _summarize 已把嵌入
        预算在块外，块内无让出。
        """
        with self._lock:
            try:
                yield self
            except BaseException:
                self._conn.rollback()
                self._matrix_dirty = False
                raise
            else:
                self._conn.commit()
                if self._matrix_dirty:
                    # 事务内有 memories 写入（remember commit=False）：整体失效。
                    # 未提交的行同连接可见，但事务可能回滚，故不按会话增量失效
                    self._matrix_cache.clear()
                    self._matrix_dirty = False

    async def remember(
        self,
        content: str,
        session_id: str,
        *,
        persona_id: str = "",
        metadata: dict | None = None,
        create_time: int | None = None,
        embedding: Sequence[float] | np.ndarray | None = None,
        commit: bool = True,
    ) -> int:
        """/remember 手动写入 API（SPEC §7.2 保留项，/eval 依赖）：总结落库共用。

        - 内容为空、session_id 为空、嵌入维度不符 → ValueError；
        - metadata 存 JSON 文本，写入方自定义（source、window_key 等）；
        - create_time 缺省取当前时刻；embedding 未给时内部向量化；
        - commit=False 时只在事务内累积，配合 atomic() 一次提交；
        - 返回新行 id。
        """
        self.ensure_usable()
        text = str(content or "").strip()
        if not text:
            raise ValueError("记忆内容为空，拒绝写入")
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id 为空，拒绝写入")
        if embedding is None:
            vec = await self.embed_text(text)
        else:
            vec = np.asarray(embedding, dtype=np.float32).reshape(-1)
            if vec.size != self._dim:
                raise ValueError(
                    f"嵌入维度不一致：传入 {vec.size} 维，库配置 {self._dim} 维"
                )
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        created = int(create_time) if create_time else int(time.time())
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO memories(session_id, persona_id, content, metadata,"
                    " create_time, embedding, embed_model, dim)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        str(persona_id or ""),
                        text,
                        meta_json,
                        created,
                        vec.tobytes(),
                        self._expected[META_MODEL],
                        self._dim,
                    ),
                )
                if commit:
                    self._conn.commit()
                    # 缓存失效点=remember 执行，失效与写同锁（PERF_SPEC §3 O8）
                    self._invalidate_matrix(session_id)
                else:
                    self._matrix_dirty = True
        except sqlite3.Error as exc:
            with self._lock:
                self._conn.rollback()
            raise RuntimeError(f"记忆写入失败：{exc}") from exc
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # 检索（SPEC §7.1：过滤 → 余弦 → 候选 → 重排）
    # ------------------------------------------------------------------

    async def search(
        self,
        session_id: str,
        query_text: str,
        *,
        top_k: int = TOP_K,
        candidates: int = CANDIDATES,
        query_vector: Sequence[float] | np.ndarray | None = None,
        min_cosine: float | None = None,
    ) -> list[MemoryHit]:
        """按会话检索记忆：余弦粗排取候选池，关键词重排后返回 top_k。

        - session_id 精确等值过滤（格式逐字符复刻旧栈 unified_msg_origin，
          值由迁移写入配置，本模块不做格式校验）；
        - query_vector 未给时经 embedder 向量化 query_text（调用方若已在
          persona 层截断查询文本，传截断后的文本即可）；
        - 重排规则（简版）：关键词命中数多者优先，命中数相同按余弦降序；
          query_text 无关键词时退化为纯余弦排序；
        - min_cosine：检索相似度下限（PERF_SPEC §3-O5 兜底补丁）——余弦低于
          该值的命中不返回（「低于阈值不注入」防噪声）；None/0=不过滤（旧行为，
          /eval 评测自管阈值不受此限）；
        - 会话无记忆或候选全空返回空列表；
        - 检索矩阵按会话缓存（PERF_SPEC §3 O8）：命中路径免 SQL 与建矩阵，
          只做「归一化行矩阵 × 查询单位向量 + 粗排 + 重排」，万条 p50 ≤10ms。
        """
        self.ensure_usable()
        top_k = max(int(top_k), 1)
        candidates = max(int(candidates), top_k)
        if query_vector is None:
            query_vector = await self.embed_text(str(query_text or ""))
        qvec = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        if qvec.size != self._dim:
            raise ValueError(
                f"查询向量维度不一致：{qvec.size}，库配置 {self._dim}"
            )

        entry = self._get_matrix(str(session_id))
        if entry is None:
            return []
        qnorm = float(np.linalg.norm(qvec))
        if qnorm == 0.0:
            qnorm = 1.0
        # 余弦相似度：行已 L2 归一化（零向量行保持 0 → 相似度 0，防除零语义不变）
        sims = entry.unit @ (qvec / qnorm)

        # 粗排：取余弦最高的 candidates 条进入重排池（stable 保证同分按入库序）
        order = np.argsort(-sims, kind="stable")[:candidates]

        keywords = extract_query_keywords(str(query_text or ""))
        cand: list[MemoryHit] = []
        for idx in order:
            row_id, content, meta_json, create_time = entry.rows[int(idx)]
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except (TypeError, ValueError):
                logger.warning("记忆 id=%s metadata 解析失败，按空对象处理", row_id)
                meta = {}
            lowered = str(content).lower()
            kw_hits = sum(1 for kw in keywords if kw in lowered)
            cand.append(MemoryHit(
                id=int(row_id),
                session_id=str(session_id),
                content=str(content),
                metadata=meta if isinstance(meta, dict) else {},
                create_time=int(create_time),
                cosine=float(sims[idx]),
                keyword_hits=kw_hits,
            ))

        # 重排：关键词命中数优先，其余按余弦（sorted 稳定，保住粗排顺序）
        cand.sort(key=lambda h: (-h.keyword_hits, -h.cosine))
        if min_cosine is not None and min_cosine > 0:
            # O5 兜底补丁：余弦低于下限的命中不注入（宁缺勿滥防噪声）
            filtered = [h for h in cand if h.cosine >= float(min_cosine)]
            if len(filtered) != len(cand):
                logger.debug(
                    "会话 %s 检索相似度下限 %.2f 过滤：%d/%d 条命中被剔除",
                    session_id, float(min_cosine), len(cand) - len(filtered), len(cand),
                )
            cand = filtered
        return cand[:top_k]

    # ------------------------------------------------------------------
    # 检索矩阵缓存（PERF_SPEC §3 O8）：失效点=remember 执行与 delete_session
    # ------------------------------------------------------------------

    def _get_matrix(self, session_id: str) -> "_SessionMatrix | None":
        """取会话检索矩阵：命中返回缓存条目，未命中计数并在锁内重建。

        命中/未命中计数与缓存读写同锁；返回的条目按不可变纪律使用
        （重建不原地改），锁外使用快照永远自洽。
        """
        with self._lock:
            entry = self._matrix_cache.get(session_id)
            if entry is not None:
                self._cache_hits += 1
                return entry
            self._cache_misses += 1
            return self._build_matrix_locked(session_id)

    def _build_matrix_locked(self, session_id: str) -> "_SessionMatrix":
        """重建会话检索矩阵（调用方须持实例锁）：SELECT → 归一化矩阵 → 整体入缓存。

        构造新 _SessionMatrix 整体替换字典槽位（绝不原地改旧条目）。
        """
        rows = self._conn.execute(
            "SELECT id, session_id, persona_id, content, metadata, create_time,"
            " embedding, dim FROM memories WHERE session_id = ?",
            (session_id,),
        ).fetchall()

        matrix_rows: list[np.ndarray] = []
        slim_rows: list[tuple] = []
        skipped_bad = 0
        for row in rows:
            blob = row[6]
            if not isinstance(blob, (bytes, bytearray)) or len(blob) != self._dim * 4:
                skipped_bad += 1
                continue
            matrix_rows.append(np.frombuffer(blob, dtype=np.float32))
            # 瘦身留存：命中元数据只要 id/content/metadata/create_time，
            # 丢弃 8KB 级 blob 引用（矩阵才是检索用的向量副本）
            slim_rows.append((row[0], row[3], row[4], row[5]))
        if skipped_bad:
            logger.warning("会话 %s 有 %d 行 embedding 字段损坏，检索时跳过",
                           session_id, skipped_bad)

        mat = (np.vstack(matrix_rows) if matrix_rows
               else np.zeros((0, self._dim), dtype=np.float32))
        norms = np.linalg.norm(mat, axis=1) if len(matrix_rows) else np.zeros(0, dtype=np.float32)
        safe = np.where(norms == 0.0, np.float32(1.0), norms)
        entry = _SessionMatrix(unit=mat / safe[:, None], rows=slim_rows)
        self._matrix_cache[session_id] = entry
        return entry

    def _invalidate_matrix(self, session_id: str | None = None) -> None:
        """失效检索矩阵缓存（PERF_SPEC §3 O8：失效与写同锁；RLock 可重入）。"""
        with self._lock:
            if session_id is None:
                self._matrix_cache.clear()
            else:
                self._matrix_cache.pop(str(session_id), None)

    def cache_stats(self) -> dict[str, int]:
        """检索矩阵缓存命中计数（PERF_SPEC §3 O8：缓存命中计数暴露）。"""
        with self._lock:
            return {
                "hits": self._cache_hits,
                "misses": self._cache_misses,
                "cached_sessions": len(self._matrix_cache),
            }

    # ------------------------------------------------------------------
    # 管理面：计数 / 删除 / 元数据扫描 / 服务状态 kv
    # ------------------------------------------------------------------

    def count(self, session_id: str | None = None) -> int:
        """记忆行数；给定 session_id 时只统计该会话（封锁态仍可查）。"""
        if session_id is None:
            sql = "SELECT COUNT(*) FROM memories"
            args: tuple = ()
        else:
            sql = "SELECT COUNT(*) FROM memories WHERE session_id = ?"
            args = (str(session_id),)
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()[0])

    def delete_session(self, session_id: str) -> int:
        """删除某会话全部记忆（/eval 等评测数据清理用）。返回删除行数。

        检索矩阵缓存失效点之一（PERF_SPEC §3 O8）：失效与写同锁。
        """
        self.ensure_usable()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memories WHERE session_id = ?", (str(session_id),)
            )
            self._conn.commit()
            self._invalidate_matrix(session_id)
            return int(cur.rowcount)

    def session_metadata_map(self, session_id: str) -> dict[int, dict]:
        """返回 {记忆id: metadata字典}（metadata 解析失败的行跳过）。

        供回填幂等检查与控制台巡检；行内 Python 侧解析 JSON，
        不依赖 SQLite JSON1 扩展。行数大时可再改列存优化。
        """
        result: dict[int, dict] = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, metadata FROM memories WHERE session_id = ?",
                (str(session_id),),
            ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row[1]) if row[1] else {}
            except (TypeError, ValueError):
                logger.warning("记忆 id=%s metadata 解析失败，扫描时跳过", row[0])
                continue
            if isinstance(meta, dict):
                result[int(row[0])] = meta
        return result

    # -- service_state kv（§7.2 计数/缓冲持久化的存储面）------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        """读取服务状态值（JSON）；缺失或损坏返回 default 并告警。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM service_state WHERE key = ?", (str(key),)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            logger.error("service_state 键 %s 内容损坏，按默认值处理", key)
            return default

    def kv_set(self, key: str, value: Any, *, commit: bool = True) -> None:
        """写入服务状态值（JSON 序列化）；commit=False 时配合 atomic() 提交。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO service_state(key, value, updated_at)"
                " VALUES(?, ?, ?)",
                (str(key), json.dumps(value, ensure_ascii=False, sort_keys=True),
                 int(time.time())),
            )
            if commit:
                self._conn.commit()

    def kv_delete(self, key: str) -> bool:
        """删除服务状态键。返回是否确实存在并删除。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM service_state WHERE key = ?", (str(key),)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def kv_keys(self, prefix: str = "") -> list[str]:
        """列出服务状态键（可按前缀过滤，升序返回）。"""
        if prefix:
            # LIKE 通配符转义后按前缀匹配（键名可能含 %/_ 以外的任意字符，
            # 但服务状态键由本包生成，转义仅为防御调用方拼接奇异前缀）
            pattern = (prefix.replace("\\", "\\\\")
                       .replace("%", r"\%").replace("_", r"\_") + "%")
            with self._lock:
                rows = self._conn.execute(
                    "SELECT key FROM service_state"
                    " WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                    (pattern,),
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT key FROM service_state ORDER BY key"
                ).fetchall()
        return [str(r[0]) for r in rows]

    def close(self) -> None:
        """关闭数据库连接（进程退出/测试收尾用）。"""
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def setup(ctx: Any) -> "Callable[[], None]":
    """打开向量库并构造写入触发服务，实例挂 ctx.memory_store / ctx.memory_service。

    - [embedding] enabled=false（setup 跳过 embedding 的降级语义）→ 记忆功能
      整体不装配：ctx.memory_store/memory_service 保持 None，消息管线的检索/
      计数按各自的 None 守卫静默跳过（零报错），聊天/历史/日记不受影响；
      补配 embedding 后重启即自动恢复；
    - 嵌入回调：经 service.embedder_from_client 接 brain.llm 的 EmbeddingClient
      （装配序：brain.llm 先于本模块）；缺失时 embedder=None，写入/检索在用时
      抛 LLMNotWiredError 由上层降级，绝不假装成功；
    - 库元数据（provider/model/endpoint/dim）取自 cfg.embedding——与建库时不一致
      会进入封锁态（blocked_reason 非空，拒写拒检索，控制台可见）；
    - 总结 chat 回调：经 service.chat_fn_from_client 注入 ChatClient 适配器
      （SPEC §7.2 后台总结走 brain.llm）。
    """
    if not getattr(ctx.cfg.embedding, "enabled", True):
        logger.info("[memory] 记忆功能未启用（[embedding] enabled=false）：检索与写入"
                    "整体关闭，聊天/历史/日记不受影响；补配 embedding 后重启即恢复")
        return None
    from .service import MemoryService, chat_fn_from_client, embedder_from_client

    emb = ctx.cfg.embedding
    mne = ctx.cfg.mnemosyne
    embedding_client = getattr(ctx, "embedding_client", None)
    # 总结 chat 回调走 bg 客户端（PERF_SPEC §2 O1 + §7.1-4：记忆总结是后台任务，
    # thinking=enabled + timeout=120s；bg 未装配时回退主客户端保链路可用）
    chat_client = getattr(ctx, "chat_client_bg", None) or getattr(ctx, "chat_client", None)
    store = MemoryStore(
        Path(ctx.repo_root) / ctx.cfg.storage.memory_db,
        embedder=embedder_from_client(embedding_client) if embedding_client is not None else None,
        provider_id="embedding",          # 与 brain.llm EmbeddingClient 的 provider_id 一致
        model=emb.model,
        endpoint=emb.api_base,
        dim=emb.dim,
    )
    if store.blocked_reason:
        logger.error("记忆库元数据校验失败（已封锁写入与检索）：%s", store.blocked_reason)
    ctx.memory_store = store
    ctx.memory_service = MemoryService(
        store,
        chat_fn=chat_fn_from_client(chat_client) if chat_client is not None else None,
        num_pairs=mne.num_pairs,
        # 时间阈值兜底默认禁用（SPEC §7.2：对齐旧栈），如需启用在 vl.toml 扩展
        time_threshold_seconds=None,
        persona_id=ctx.cfg.persona.name,
    )
    logger.info("[memory] 向量库就绪：%s（dim=%d，已有 %d 条；触发阈值 %d 条消息）",
                ctx.cfg.storage.memory_db, emb.dim, store.count(),
                ctx.memory_service.threshold)

    def cleanup() -> None:
        try:
            store.close()
        except Exception:  # noqa: BLE001 —— 清理失败不阻断其余清理
            logger.exception("[memory] 关闭向量库失败（忽略）")

    return cleanup
