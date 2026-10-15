# -*- coding: utf-8 -*-
"""一次性历史迁移：data_v4.db → history.db + 归一化（SPEC §11.2，离线执行）。

执行前提：astrbot 容器已 stop（或用 --copy-first 走 sqlite backup API 复制后读取）。

产出：
- local/vl_data/history.db：messages(id, session_id, role, content, created_at, meta)；
- 逐表对账单 + 源库 sha256 → backup/history_migrate_report_<时间戳>.json。

归一化规则（十参谋终裁：清理注入脚手架，对话内容本体原样保留）：
- think 剥离：字符串形态的 <think>…</think> 段删除；列表形态（content parts）里
  type='think' 的分片跳过（实测 data_v4.db 的 think 以分片形态存在）；
- <system_reminder>…</system_reminder> 清除（实测 120 条中 52 条残留时间注入）；
- 双形态展开：字符串与分段列表都接受，扁平化为纯文本（非 text 分片降级占位）；
- 对话本体保留：&&tag&&（表情包标记）、CQ 残文等一律不动；
- created_at 一律留空——旧数据无消息级时间戳，不可伪造（SPEC §6）。

守恒断言：迁移条数 == 源库 conversations.content 消息总数，不符则报错退出（exit 1），
不产出 history.db。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

__all__ = ["normalize_content", "strip_think", "clear_system_reminder", "migrate", "main"]

# 成对/孤立的注入脚手架（清理目标是"残留=0"，故孤立半截标签也一并清掉）
_RE_THINK_PAIRED = re.compile(r"<think>.*?</think>", re.DOTALL)
_RE_THINK_OPEN = re.compile(r"<think>.*\Z", re.DOTALL)
_RE_REMINDER_PAIRED = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)
_RE_REMINDER_ORPHAN = re.compile(r"</?system_reminder>")


def strip_think(text: str) -> str:
    """剥离字符串形态的 think 段（成对段 + 未闭合到结尾的孤立段）。"""
    text = _RE_THINK_PAIRED.sub("", text)
    text = _RE_THINK_OPEN.sub("", text)
    return text


def clear_system_reminder(text: str) -> str:
    """清除 <system_reminder>…</system_reminder>（成对 + 孤立标签）。"""
    text = _RE_REMINDER_PAIRED.sub("", text)
    text = _RE_REMINDER_ORPHAN.sub("", text)
    return text


def normalize_content(raw: Any) -> Tuple[str, dict]:
    """content 双形态 → (纯文本, 归一化统计)。

    - str：原样（think/reminder 的文本级清理由调用方统一执行）；
    - list（OpenAI content parts）：text 分片拼接；think 分片剥离；其余分片降级占位；
    - 其他类型：整条降级占位（实测 120 条只出现 str/list 两种形态）。
    """
    stats = {"think_part_stripped": False, "unknown_part": 0, "list_expanded": False}
    if isinstance(raw, str):
        return raw, stats
    if isinstance(raw, list):
        stats["list_expanded"] = True
        parts: List[str] = []
        for piece in raw:
            if not isinstance(piece, dict):
                parts.append(str(piece))
                continue
            ptype = piece.get("type")
            if ptype == "text":
                parts.append(str(piece.get("text") or ""))
            elif ptype == "think":
                stats["think_part_stripped"] = True  # 分片级 think：直接不入正文
            else:
                stats["unknown_part"] += 1
                parts.append(f"[不支持的内容:{ptype}]")
        return "\n".join(p for p in parts if p != ""), stats
    stats["unknown_part"] += 1
    return f"[不支持的内容:{type(raw).__name__}]", stats


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def _open_source(db_path: Path, copy_first: bool, tmp_dir: Path,
                 dry_run: bool = False) -> Tuple[sqlite3.Connection, Path]:
    """打开源库。copy_first=True 用 sqlite backup API 复制到临时文件再读（热库安全）。

    dry-run 时快照落系统临时目录，不碰目标数据目录（保持"未写任何文件"语义）。
    """
    if copy_first:
        snap_dir = Path(tempfile.mkdtemp(prefix="vl_hist_")) if dry_run else tmp_dir
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap = snap_dir / ("data_v4_snapshot_" + time.strftime("%Y%m%d%H%M%S") + ".db")
        src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(str(snap))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        return sqlite3.connect(str(snap)), snap
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True), db_path


def migrate(source_db: str | Path = "local/astrbot_data/data_v4.db",
            out_db: str | Path = "local/vl_data/history.db",
            report_dir: str | Path = "backup",
            copy_first: bool = False, dry_run: bool = False) -> dict:
    """执行迁移。返回对账单 dict；守恒断言失败抛 SystemExit(exit 1)。"""
    source_db = Path(source_db)
    out_db = Path(out_db)
    if not source_db.is_file():
        raise SystemExit(f"迁移失败：找不到源库 {source_db}")

    con, snap = _open_source(source_db, copy_first, out_db.parent, dry_run=dry_run)
    try:
        # 逐表对账：行数清点（对账单进报告，敏感内容不落盘）
        table_names = [row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        tables = {name: con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                  for name in table_names}
        rows = con.execute(
            "SELECT conversation_id, user_id, content FROM conversations "
            "ORDER BY inner_conversation_id"  # 真库主键列（实测 DDL）
        ).fetchall()
    finally:
        con.close()

    stats = {"think_text_stripped": 0, "reminder_cleared": 0,
             "think_part_stripped": 0, "list_expanded": 0, "unknown_part": 0}
    staged: List[Tuple[str, str, str, Optional[int], str]] = []
    for conv_id, user_id, content_json in rows:
        try:
            entries = json.loads(content_json) if content_json else []
        except (TypeError, ValueError):
            raise SystemExit(f"迁移中止：会话 {conv_id} 的 content 不是合法 JSON")
        for seq, entry in enumerate(entries):
            role = str((entry or {}).get("role") or "user")
            flat, sub = normalize_content((entry or {}).get("content"))
            for k in ("think_part_stripped", "unknown_part", "list_expanded"):
                if sub.get(k):
                    stats[k] += 1
            orig_len = len(flat)
            if "<think" in flat:
                flat = strip_think(flat)
                stats["think_text_stripped"] += 1
            if "system_reminder" in flat:
                flat = clear_system_reminder(flat)
                stats["reminder_cleared"] += 1
            meta = json.dumps({"migrated": True, "conv_id": conv_id, "seq": seq,
                               "orig_len": orig_len, **sub}, ensure_ascii=False)
            # created_at 留空：旧数据无消息级时间戳，不可伪造（SPEC §6）
            staged.append((str(user_id or ""), role, flat, None, meta))

    src_count = len(staged)
    # 守恒断言：独立重开一次只读连接重新展开计数，避免"自己证明自己"
    # （copy_first 时对同一快照计数，保证分母与迁出数据同源）
    con2 = sqlite3.connect(f"file:{snap.as_posix()}?mode=ro", uri=True)
    try:
        expanded_total = 0
        for (content_json,) in con2.execute(
                "SELECT content FROM conversations ORDER BY inner_conversation_id"):
            expanded_total += len(json.loads(content_json) if content_json else [])
    finally:
        con2.close()

    if src_count != expanded_total:
        raise SystemExit(
            f"守恒断言失败：迁出 {src_count} 条 != 源库展开 {expanded_total} 条，"
            "已中止（未产出 history.db）。")

    if not dry_run:
        out_db.parent.mkdir(parents=True, exist_ok=True)
        if out_db.exists():
            raise SystemExit(f"迁移中止：目标库 {out_db} 已存在（防覆盖；确认后请手动删除）")
        dst = sqlite3.connect(str(out_db))
        try:
            with dst:
                dst.executescript(_SCHEMA)
                dst.executemany(
                    "INSERT INTO messages(session_id, role, content, created_at, meta) "
                    "VALUES (?,?,?,?,?)", staged)
            count = dst.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            dst.close()
        if count != src_count:
            raise SystemExit(f"守恒断言失败：落库 {count} 条 != 迁出 {src_count} 条")

    # 对账单 + 源库 sha256 落 backup/（SPEC §11.2）
    sha = hashlib.sha256(source_db.read_bytes()).hexdigest()
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_db": str(source_db),
        "source_sha256": sha,
        "source_tables": tables,
        "conversations_rows": len(rows),
        "messages_in": expanded_total,
        "messages_migrated": src_count,
        "conservation_ok": True,
        "normalization": stats,
        "created_at_policy": "NULL（旧数据无消息级时间戳，不可伪造）",
    }
    if not dry_run:
        rdir = Path(report_dir)
        rdir.mkdir(parents=True, exist_ok=True)
        rpath = rdir / f"history_migrate_report_{time.strftime('%Y%m%d%H%M%S')}.json"
        rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"历史迁移完成：{src_count} 条 → {out_db}（守恒 {src_count}/{expanded_total}）")
        print(f"对账单：{rpath}")
    else:
        print(f"[dry-run] 守恒预检通过：{src_count}/{expanded_total}；未写任何文件")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="一次性历史迁移：data_v4.db → history.db（SPEC §11.2）")
    parser.add_argument("--source", default="local/astrbot_data/data_v4.db")
    parser.add_argument("--out", default="local/vl_data/history.db")
    parser.add_argument("--report-dir", default="backup")
    parser.add_argument("--copy-first", action="store_true",
                        help="先经 sqlite backup API 快照再读（源库可能仍在写入时用）")
    parser.add_argument("--dry-run", action="store_true", help="只做守恒预检不落库")
    args = parser.parse_args(argv)
    migrate(source_db=args.source, out_db=args.out, report_dir=args.report_dir,
            copy_first=args.copy_first, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
