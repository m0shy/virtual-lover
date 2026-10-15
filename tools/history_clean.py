# -*- coding: utf-8 -*-
"""history_clean.py —— 历史库 bug 产物清理（五轮修缮 R3，幂等可复跑）。

只清 bug 产物，绝不动用户资产（role=user 的行一律不碰）：

1. ``(?:`` 碎片行（assistant 行内容含半角/全角 ``(?:``）——旧栈主动消息用
   裸 prompt 生成该碎片且回写历史，后续生成读历史继续模仿成毒循环；作为
   同源毒产物的永久特征规则，此后再出现照清；
2. 旧栈 function-call 泄漏行——assistant 行含 ``FunctionCall`` 标记、
   ``{"name": ...}`` 工具调用 JSON 或以 ``assistant:`` 裸前缀开头的行
   （按泄漏特征识别，不点名具体行号）。

幂等性：删除行不存在时计数为 0、退出码不变；特征规则天然幂等。
安全性：仅 role='assistant' 参与匹配；删除前先整库备份；写盘前支持
--dry-run 预览。默认不写盘（防误跑），加 --yes 才真正删除。

用法（仓库根执行）::

    python tools/history_clean.py            # 预览（dry-run 语义）
    python tools/history_clean.py --yes      # 备份后实际删除
    python tools/history_clean.py --db PATH  # 指定历史库路径

本脚本只依赖标准库。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 清理规则（只作用于 role='assistant' 的行）
# ---------------------------------------------------------------------------

# (?: 正则碎片（含全角形态）；旧栈主动消息裸 prompt 的产出特征。
# 毒循环产物统一按特征识别（三连裸 prompt 退化产物同样命中碎片规则），
# 不点名具体行号；删除时仍要求 role=assistant 双保险。
FRAG_RE = re.compile(r"[（(]\?:")

# 旧栈 function-call 泄漏：FunctionCall 标记 / 工具调用 JSON / 裸 assistant 前缀
FUNC_RE = re.compile(r"FunctionCall|\{\s*\"name\"\s*:|<\|")
BARE_ASSISTANT_RE = re.compile(r"^\s*assistant\s*[:：]", re.IGNORECASE)


def row_is_poison(role: str, content: str) -> str | None:
    """返回该行的命中原因标签；不是 bug 产物返回 None。

    role='user' 恒为 None（用户资产绝不清理）。
    """
    if role != "assistant":
        return None
    if FRAG_RE.search(content):
        return "regex-fragment"
    if FUNC_RE.search(content) or BARE_ASSISTANT_RE.match(content):
        return "function-call-leak"
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def clean(db_path: Path, *, apply: bool) -> int:
    """扫描并（apply=True 时）删除 bug 产物行；返回命中行数。"""
    if not db_path.is_file():
        print(f"[history-clean] 历史库不存在：{db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM messages ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    hits = []
    for row in rows:
        reason = row_is_poison(str(row["role"]), str(row["content"] or ""))
        if reason:
            hits.append((row, reason))

    print(f"[history-clean] 库：{db_path}（共 {len(rows)} 行）")
    if not hits:
        print("[history-clean] 未发现 bug 产物，无事可做（幂等空跑）。")
        return 0
    for row, reason in hits:
        head = " ".join(str(row["content"] or "").split())[:60]
        print(f"  - id={row['id']} role={row['role']} 原因={reason}：{head}")

    if not apply:
        print(f"[history-clean] 预览模式：命中 {len(hits)} 行未删除"
              f"（加 --yes 备份后实际删除）。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(db_path.name + f".bak-clean-{stamp}")
    shutil.copy2(db_path, backup)
    print(f"[history-clean] 已整库备份 -> {backup}")

    conn = sqlite3.connect(db_path)
    try:
        with conn:
            deleted = 0
            for row, _reason in hits:
                cur = conn.execute(
                    "DELETE FROM messages WHERE id = ? AND role = 'assistant'",
                    (int(row["id"]),),
                )
                deleted += cur.rowcount if cur.rowcount > 0 else 0
        print(f"[history-clean] 已删除 {deleted} 行（剩余 "
              f"{conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]} 行）。")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="历史库 bug 产物清理（幂等）")
    parser.add_argument(
        "--db", default="local/vl_data/history.db",
        help="历史库路径（默认 local/vl_data/history.db，相对仓库根）")
    parser.add_argument(
        "--yes", action="store_true", help="实际删除（默认仅预览）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_absolute():
        repo_root = Path(__file__).resolve().parents[1]
        db_path = repo_root / db_path
    return clean(db_path, apply=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
