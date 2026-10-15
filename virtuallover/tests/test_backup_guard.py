# -*- coding: utf-8 -*-
"""备份护栏测试（PERF_SPEC §3 O8「备份链路同批修」）。

覆盖 scripts/backup_guard.py 两步语义：
- pre：wal_checkpoint(TRUNCATE) 把 WAL 合并回主库（-wal 截断/清空）+
  行数快照落盘；未登记映射的库只检查点不计数；WAL 忙碌 → 退出码 3 且快照照常；
- verify：备份副本行数 vs 快照——一致通过、漂移判败、缺件判败、坏库判败、
  未登记库跳过。

全程临时目录合成库（history.db/messages 与 memory.db/memories 同构），
不触碰本机 local/vl_data 与在跑的 vl 进程。stdout 一律 ASCII
（cmd GBK 控制台安全），CLI 形态用子进程冒烟。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backup_guard  # noqa: E402 —— scripts/ 无包结构，按目录注入导入


def _make_wal_db(path: Path, table: str, rows: int) -> None:
    """建一个 WAL 库并写入 rows 行业务数据（故意不检查点，留待 pre 处理）。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(f"INSERT INTO {table}(v) VALUES (?)",
                     [(f"行{i}",) for i in range(rows)])
    conn.commit()
    conn.execute("INSERT INTO %s(v) VALUES ('未检查点行')" % table)  # 留在 WAL 里
    conn.commit()
    conn.close()


def test_pre_checkpoints_wal_and_snapshots_counts(tmp_path):
    """pre：WAL 页合并回主库（-wal 清空）、行数快照、退出码 0。"""
    data = tmp_path / "vl_data"
    data.mkdir()
    _make_wal_db(data / "history.db", "messages", rows=7)
    _make_wal_db(data / "memory.db", "memories", rows=3)
    snapshot = tmp_path / "bk" / "vl_data" / "_row_counts.json"

    rc = backup_guard.main(["pre", "--data", str(data), "--snapshot", str(snapshot)])
    assert rc == backup_guard.EXIT_OK

    counts = json.loads(snapshot.read_text(encoding="ascii"))
    assert counts == {"history.db": 8, "memory.db": 4}  # 含 WAL 中那行
    # 检查点后 -wal 清空（TRUNCATE 语义：文件可为 0 字节或不存在）
    for name in ("history.db", "memory.db"):
        wal = data / f"{name}-wal"
        assert not wal.exists() or wal.stat().st_size == 0
    # 快照纯 ASCII（cmd GBK 控制台安全）
    snapshot.read_text(encoding="ascii")


def test_pre_ignores_temp_files_and_unknown_dbs(tmp_path):
    """pre：-wal/-shm 不当库处理；未登记映射的库只检查点、行数记 null。"""
    data = tmp_path / "vl_data"
    data.mkdir()
    _make_wal_db(data / "history.db", "messages", rows=1)
    _make_wal_db(data / "custom.db", "anything", rows=5)  # 未登记映射

    snapshot = tmp_path / "snap.json"
    assert backup_guard.main(["pre", "--data", str(data), "--snapshot", str(snapshot)]) == 0
    counts = json.loads(snapshot.read_text(encoding="ascii"))
    assert counts["custom.db"] is None
    assert counts["history.db"] == 2


def test_pre_busy_returns_3_but_snapshot_written(tmp_path):
    """pre：并发写连接挡住检查点 → 退出码 3（警告），快照照常落盘。"""
    data = tmp_path / "vl_data"
    data.mkdir()
    _make_wal_db(data / "memory.db", "memories", rows=2)
    # 制造忙碌：另一连接持未提交写事务（WAL 检查点 TRUNCATE 被挡）
    holder = sqlite3.connect(str(data / "memory.db"))
    holder.execute("INSERT INTO memories(v) VALUES ('在途未提交')")
    try:
        snapshot = tmp_path / "snap.json"
        rc = backup_guard.main(
            ["pre", "--data", str(data), "--snapshot", str(snapshot)])
        assert rc == backup_guard.EXIT_BUSY
        counts = json.loads(snapshot.read_text(encoding="ascii"))
        assert counts["memory.db"] == 3  # 已提交的部分照常计数（在途行不可见）
    finally:
        holder.rollback()
        holder.close()


def test_verify_passes_on_fresh_copy(tmp_path):
    """verify：复制前 pre + 只复制 .db（模拟 robocopy /XF -wal）→ 校验通过。"""
    data = tmp_path / "vl_data"
    data.mkdir()
    _make_wal_db(data / "history.db", "messages", rows=5)
    _make_wal_db(data / "memory.db", "memories", rows=2)
    snapshot = tmp_path / "bk" / "vl_data" / "_row_counts.json"
    assert backup_guard.main(["pre", "--data", str(data), "--snapshot", str(snapshot)]) == 0

    backup = tmp_path / "bk" / "vl_data"
    for name in ("history.db", "memory.db"):
        shutil.copyfile(data / name, backup / name)  # 只复制主库文件
    assert backup_guard.main(
        ["verify", "--backup", str(backup), "--snapshot", str(snapshot)]) == 0


def test_verify_fails_on_write_drift(tmp_path):
    """verify：快照后源库又有写入并被复制 → 行数漂移判败（退出码 1）。"""
    data = tmp_path / "vl_data"
    data.mkdir()
    _make_wal_db(data / "memory.db", "memories", rows=2)
    snapshot = tmp_path / "snap.json"
    assert backup_guard.main(["pre", "--data", str(data), "--snapshot", str(snapshot)]) == 0

    # 快照之后源库又提交了两行，并被复制进备份
    conn = sqlite3.connect(str(data / "memory.db"))
    conn.executemany("INSERT INTO memories(v) VALUES (?)", [("新1",), ("新2",)])
    conn.commit()
    conn.close()
    backup = tmp_path / "bk" / "vl_data"
    backup.mkdir(parents=True)
    shutil.copyfile(data / "memory.db", backup / "memory.db")

    assert backup_guard.main(
        ["verify", "--backup", str(backup), "--snapshot", str(snapshot)]) == 1


def test_verify_fails_on_missing_or_broken_backup(tmp_path):
    """verify：备份缺件/坏件判败；未登记库 SKIP；快照缺失判败。"""
    data = tmp_path / "vl_data"
    data.mkdir()
    _make_wal_db(data / "history.db", "messages", rows=4)
    _make_wal_db(data / "custom.db", "anything", rows=1)
    snapshot = tmp_path / "snap.json"
    assert backup_guard.main(["pre", "--data", str(data), "--snapshot", str(snapshot)]) == 0

    backup = tmp_path / "bk"
    backup.mkdir()
    # custom.db 未登记 → SKIP 不影响结论；history.db 缺失 → FAIL
    shutil.copyfile(data / "custom.db", backup / "custom.db")
    rc = backup_guard.main(["verify", "--backup", str(backup), "--snapshot", str(snapshot)])
    assert rc == 1

    # 坏件：截断的假库文件
    shutil.copyfile(data / "history.db", backup / "history.db")
    (backup / "history.db").write_bytes(b"not a sqlite file")
    rc = backup_guard.main(["verify", "--backup", str(backup), "--snapshot", str(snapshot)])
    assert rc == 1

    # 快照缺失
    rc = backup_guard.main(
        ["verify", "--backup", str(backup), "--snapshot", str(tmp_path / "nope.json")])
    assert rc == 1


def test_cli_smoke_ascii_output(tmp_path):
    """CLI 冒烟（子进程，等价 backup.bat 调用形态）：退出码正确、输出纯 ASCII。"""
    data = tmp_path / "vl_data"
    data.mkdir()
    _make_wal_db(data / "memory.db", "memories", rows=1)
    snapshot = tmp_path / "snap.json"

    pre = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "backup_guard.py"),
         "pre", "--data", str(data), "--snapshot", str(snapshot)],
        capture_output=True, text=True, timeout=60)
    assert pre.returncode == 0
    pre.stdout.encode("ascii")  # GBK 控制台安全：stdout 必须 ASCII

    backup = tmp_path / "bk"
    backup.mkdir()
    shutil.copyfile(data / "memory.db", backup / "memory.db")
    verify = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "backup_guard.py"),
         "verify", "--backup", str(backup), "--snapshot", str(snapshot)],
        capture_output=True, text=True, timeout=60)
    assert verify.returncode == 0
    verify.stdout.encode("ascii")
