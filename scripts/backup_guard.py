# -*- coding: utf-8 -*-
"""备份护栏：WAL 检查点 + 行数校验（PERF_SPEC §3 O8「备份链路同批修」）。

背景：memory.db WAL 化后（O8），主库文件可能落后于 -wal 日志——只复制 .db
（robocopy /XF "*-wal" "*-shm" 排除临时文件，与控制台备份口径一致）会丢掉
WAL 里未检查点的页。本脚本两步封堵：

- pre   （复制前）：对数据目录每个 *.db 执行 wal_checkpoint(TRUNCATE) 把
  WAL 全量合并回主库并截断日志，同时快照各库业务表行数到 JSON；
- verify（复制后）：逐库比对「备份副本行数 vs 复制前快照」，任何缺失/
  不一致/打不开都判失败——不停机备份期间的写入漂移会被如实拦下。

GBK 处理注意（PERF_SPEC §3 O8）：本脚本被 cmd 批处理（GBK 控制台）调用，
stdout 一律输出 ASCII（[OK]/[WARN]/[FAIL] + 库名/数字），中文字面只存在于
本文件注释（源码 UTF-8）；快照 JSON 用 ensure_ascii=True 落盘，全 ASCII。

用法（scripts/backup.bat 调用；也可手工执行）：
  python scripts/backup_guard.py pre --data local/vl_data \
      --snapshot backup/<TS>/vl_data/_row_counts.json
  python scripts/backup_guard.py verify --backup backup/<TS>/vl_data \
      --snapshot backup/<TS>/vl_data/_row_counts.json

退出码：pre  0=全部检查点完成  3=部分库忙碌（打印 WARN，备份继续，
复制+校验兜底）  1=致命错误；verify  0=校验通过  1=校验失败。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# 业务表映射：行数校验的对象（新增业务库时在此登记；未登记的库只做检查点，
# 行数记 None 且不参与校验——避免误把无业务表的元数据库判失败）
KNOWN_TABLES = {
    "history.db": "messages",   # SPEC §6 会话库
    "memory.db": "memories",    # SPEC §7.1 内嵌向量记忆库
}

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BUSY = 3  # pre 专用：部分库 checkpoint 忙碌（有连接在写），备份继续由校验兜底


def count_rows(conn: sqlite3.Connection, db_name: str) -> int | None:
    """统计该库的业务表行数；未登记映射的库返回 None。"""
    table = KNOWN_TABLES.get(db_name)
    if table is None:
        return None
    # 表不存在按 0 计（新装空库），打不开/语法错交由上层统一处理
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def cmd_pre(data_dir: Path, snapshot_path: Path) -> int:
    """复制前：逐库 wal_checkpoint(TRUNCATE) + 行数快照。"""
    if not data_dir.is_dir():
        print(f"[FAIL] data dir not found: {data_dir}")
        return EXIT_FAIL
    counts: dict[str, int | None] = {}
    busy: list[str] = []
    for db_path in sorted(data_dir.glob("*.db")):
        try:
            # 独立短连接：检查点后立即关闭，不留长持句（不干扰在跑的 vl）
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                # 返回 (busy, WAL页数, 已检查点页数)：busy=1 表示有并发连接挡住
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row[0]:
                    busy.append(db_path.name)
                    print(f"[WARN] checkpoint busy (concurrent writer?): {db_path.name}")
                else:
                    print(f"[OK] checkpoint(TRUNCATE): {db_path.name}")
                counts[db_path.name] = count_rows(conn, db_path.name)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(f"[FAIL] checkpoint failed: {db_path.name}: {exc}")
            return EXIT_FAIL
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=True：快照文件全 ASCII，规避任何编码链路问题
    snapshot_path.write_text(
        json.dumps(counts, ensure_ascii=True, indent=1, sort_keys=True),
        encoding="ascii",
    )
    print(f"[OK] row-count snapshot: {snapshot_path.name}"
          f" ({sum(1 for v in counts.values() if v is not None)} dbs)")
    return EXIT_BUSY if busy else EXIT_OK


def cmd_verify(backup_dir: Path, snapshot_path: Path) -> int:
    """复制后：备份副本行数 vs 复制前快照，逐库校验。"""
    if not snapshot_path.is_file():
        print(f"[FAIL] snapshot not found: {snapshot_path}")
        return EXIT_FAIL
    if not backup_dir.is_dir():
        print(f"[FAIL] backup dir not found: {backup_dir}")
        return EXIT_FAIL
    try:
        snapshot: dict[str, int | None] = json.loads(snapshot_path.read_text(encoding="ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"[FAIL] snapshot unreadable: {exc}")
        return EXIT_FAIL

    failed = False
    for name in sorted(snapshot):
        expected = snapshot[name]
        if expected is None:
            print(f"[SKIP] no countable table registered: {name}")
            continue
        copy_path = backup_dir / name
        if not copy_path.is_file():
            print(f"[FAIL] missing in backup: {name}")
            failed = True
            continue
        try:
            # 只读打开副本（mode=ro：绝不改动备份件；WAL 化的副本单文件自足，
            # 因为 pre 阶段已 TRUNCATE 检查点）
            conn = sqlite3.connect(
                f"file:{copy_path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                actual = count_rows(conn, name)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(f"[FAIL] unreadable backup db: {name}: {exc}")
            failed = True
            continue
        if actual != expected:
            print(f"[FAIL] row mismatch: {name}: backup={actual} expected={expected}"
                  " (writes drifted or WAL not checkpointed)")
            failed = True
        else:
            print(f"[OK] rows match: {name} ({actual})")

    # 备份目录里多出的未登记库：提醒但不判失败（快照生成后才出现的库）
    for copy_path in sorted(backup_dir.glob("*.db")):
        if copy_path.name not in snapshot:
            print(f"[WARN] db in backup but not in snapshot: {copy_path.name}")
    return EXIT_FAIL if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="backup guard: WAL checkpoint + row verify")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pre = sub.add_parser("pre", help="checkpoint WAL and snapshot row counts")
    p_pre.add_argument("--data", required=True, help="源数据目录（如 local/vl_data）")
    p_pre.add_argument("--snapshot", required=True, help="行数快照 JSON 输出路径")

    p_verify = sub.add_parser("verify", help="verify backup copies against snapshot")
    p_verify.add_argument("--backup", required=True, help="备份目录（如 backup/<TS>/vl_data）")
    p_verify.add_argument("--snapshot", required=True, help="行数快照 JSON 路径")

    args = parser.parse_args(argv)
    if args.cmd == "pre":
        return cmd_pre(Path(args.data), Path(args.snapshot))
    return cmd_verify(Path(args.backup), Path(args.snapshot))


if __name__ == "__main__":
    sys.exit(main())
