# -*- coding: utf-8 -*-
"""memory_clean.py —— 记忆存量性别漂移修正（五轮修缮 R3，幂等、默认演练）。

背景（R3 审计实证）：R3 之前的总结 prompt 无人设锚，个别总结把男性恋人写成了
「她」（存量库中确有此类行，整行多处「她」均指代用户）。本脚本对该类
存量行做保守修正：她 → 他。

保守规则（宁漏勿误，逐行 diff 供人核后才可落库）：
- 只处理「上下文明确」的行，须同时满足：
  1) 内容含「她」；
  2) 内容含恋人指代词之一（老公/亲爱的/宝宝/对方）——「她」最可能的先行词；
  3) 内容不含第三方女性语境词（室友/闺蜜/妈妈/姐姐/妹妹/女生/阿姨/外婆/奶奶/
     女同学/女老师/护士）——存在第三方女性时绝不盲替；
  4) metadata.source != "eval"（/eval 合成事实永不触碰）；
  5) --session 给定时只处理该会话。
- 命中行内所有「她」→「他」（覆盖主语「她…」与「她养…」等动词搭配，同字替换）。

幂等：落库后行内已无「她」，重跑不命中、不写库。
演练优先：默认只打印 old→new diff，不写任何数据；--apply 才写库，
且写库前先经 SQLite backup API 备份整库（WAL 安全）到
<db>.bak-memclean-<时间戳>。

用法（仓库根执行，包入口与根 tools/ 启动器等价）：
    python -m virtuallover.tools.memory_clean                 # 演练，只看 diff
    python -m virtuallover.tools.memory_clean --apply         # 备份并写库
    python tools/memory_clean.py --db 路径 --session 会话ID --apply

注意：
- 直接改库绕过运行中的 vl 进程：--apply 后请重启 vl（检索矩阵缓存在进程内，
  重启后重建才会看到修正后的正文）；
- 行内 embedding 是按修正前正文算的——「她→他」仅换 1-2 个字，向量漂移可忽略；
  如需向量与正文严格一致，可删库重跑 backfill（重量级，非必需）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("local/vl_data/memory.db")

# 规则 2：恋人指代词（她最可能的先行词是这些人称）
COUNTERPART_MARKERS = ("老公", "亲爱的", "宝宝", "对方")
# 规则 3：第三方女性语境词——出现即整行放弃（绝不盲替）
THIRD_PARTY_FEMALE_MARKERS = (
    "室友", "闺蜜", "妈妈", "姐姐", "妹妹", "女生", "阿姨",
    "外婆", "奶奶", "女同学", "女老师", "护士",
)


def _is_candidate(content: str, metadata: dict) -> bool:
    """规则 1-4 的整行准入判定（不含 --session 过滤）。"""
    if "她" not in content:
        return False  # 规则 1：没有「她」就没有可修的
    if not any(m in content for m in COUNTERPART_MARKERS):
        return False  # 规则 2：无恋人指代词，先行词不明，不碰
    if any(m in content for m in THIRD_PARTY_FEMALE_MARKERS):
        return False  # 规则 3：有第三方女性语境，绝不盲替
    if str(metadata.get("source", "")) == "eval":
        return False  # 规则 4：评测合成事实永不触碰
    return True


def _parse_metadata(raw: Any) -> dict:
    """metadata 列 JSON 容错解析：坏 JSON 按空对象（与 store 同口径）。"""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _scan(conn: sqlite3.Connection, session_id: str | None) -> list[dict]:
    """扫描候选行：返回 [{id, session_id, content, new_content, source}]。"""
    sql = "SELECT id, session_id, content, metadata FROM memories"
    args: tuple = ()
    if session_id:
        sql += " WHERE session_id = ?"
        args = (session_id,)
    rows = conn.execute(sql, args).fetchall()
    hits: list[dict] = []
    for row_id, sid, content, meta_raw in rows:
        text = str(content or "")
        metadata = _parse_metadata(meta_raw)
        if not _is_candidate(text, metadata):
            continue
        hits.append({
            "id": int(row_id),
            "session_id": str(sid),
            "content": text,
            "new_content": text.replace("她", "他"),
            "source": str(metadata.get("source", "")),
        })
    return hits


def _print_diff(hits: list[dict]) -> None:
    """逐行打印 old→new diff（修正必须逐条过目后才允许 --apply）。"""
    for hit in hits:
        print(f"—— 记忆 id={hit['id']}（session={hit['session_id']}，"
              f"source={hit['source'] or '无'}）——")
        print(f"  旧: {hit['content']}")
        print(f"  新: {hit['new_content']}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="记忆存量性别漂移修正（她→他，保守规则；默认演练只打印 diff）",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="记忆库路径（默认 local/vl_data/memory.db）")
    parser.add_argument("--session", default="", help="只处理指定会话（缺省全库扫描）")
    parser.add_argument("--apply", action="store_true", help="实际写库（写前自动整库备份）；缺省只演练")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"记忆库不存在：{db_path}")
        return 2
    session_id = args.session.strip() or None

    conn = sqlite3.connect(str(db_path))
    try:
        hits = _scan(conn, session_id)
        if not hits:
            print(f"扫描完成：无命中行（规则已收敛，库干净或无上下文明确的漂移行）。"
                  f"db={db_path} session={session_id or '全部'}")
            return 0
        print(f"扫描完成：命中 {len(hits)} 行候选（她→他，逐行 diff 如下）：\n")
        _print_diff(hits)
        if not args.apply:
            print("演练模式：未写库。确认 diff 无误后加 --apply 落库。")
            return 0

        # 写前整库备份（SQLite backup API 对 WAL 库安全，含未检查点页）
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.bak-memclean-{stamp}")
        with sqlite3.connect(str(backup_path)) as bconn:
            conn.backup(bconn)
        print(f"已备份整库 → {backup_path}")

        changed = 0
        for hit in hits:
            cur = conn.execute(
                "UPDATE memories SET content = ? WHERE id = ? AND content = ?",
                (hit["new_content"], hit["id"], hit["content"]),
            )
            changed += cur.rowcount
        conn.commit()
        print(f"已修正 {changed}/{len(hits)} 行（幂等：重跑本脚本将无命中）。")
        print("提示：--apply 直接改库，请重启 vl 让检索矩阵缓存重建；"
              "行内 embedding 按修正前正文计算，逐字换名级差异可忽略。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
