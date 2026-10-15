# -*- coding: utf-8 -*-
"""备份保留上限契约测试（五轮修缮 R2 任务 10）。

控制台备份动作（control.create_backup）默认只保留 newest 10 份
vl-backup-*.zip，更旧的删除——防止备份日积月累塞满磁盘。
文件名含秒级时间戳（vl-backup-%Y%m%d-%H%M%S.zip），字典序即时间序。

全程临时目录，不触碰真实 backup/。
运行：python -m pytest virtuallover/tests/test_backup_retention.py -q
"""

from __future__ import annotations

from pathlib import Path

from virtuallover.console.control import (
    BACKUP_KEEP_COUNT,
    create_backup,
    prune_backups,
)


def _seed_backups(backup_dir: Path, count: int) -> list[Path]:
    """造 count 份带时间戳序的假备份包（文件名递增=时间递增）。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i in range(count):
        # 秒级步进保证字典序与时间序一致（与 BACKUP_STAMP_FORMAT 同构）
        name = f"vl-backup-20260101-{i // 3600:02d}{(i % 3600) // 60:02d}{i % 60:02d}.zip"
        p = backup_dir / name
        p.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # 空 zip 尾块，仅占位
        paths.append(p)
    return paths


def test_keep_count_default_is_ten():
    assert BACKUP_KEEP_COUNT == 10


def test_prune_keeps_newest_ten_deletes_older(tmp_path):
    """15 份 → 删最旧 5 份、保留 newest 10 份；返回被删名单。"""
    backup_dir = tmp_path / "backup"
    paths = _seed_backups(backup_dir, 15)
    removed = prune_backups(backup_dir, keep=10)
    assert len(removed) == 5
    assert removed == [p.name for p in paths[:5]], "应按时间序删最旧"
    survivors = sorted(backup_dir.glob("vl-backup-*.zip"))
    assert len(survivors) == 10
    assert survivors == paths[5:], "保留的必须是 newest 10 份"


def test_prune_noop_under_limit(tmp_path):
    """不足上限：零删除（绝不误删）。"""
    backup_dir = tmp_path / "backup"
    _seed_backups(backup_dir, 10)
    assert prune_backups(backup_dir, keep=10) == []
    assert len(list(backup_dir.glob("*.zip"))) == 10


def test_prune_deletion_failure_never_raises(tmp_path, monkeypatch):
    """单个删除失败（如被占用）：记日志后继续，绝不中断备份动作。"""

    def _boom(self):
        raise PermissionError(13, "文件被占用")

    monkeypatch.setattr(Path, "unlink", _boom)
    backup_dir = tmp_path / "backup"
    _seed_backups(backup_dir, 12)
    removed = prune_backups(backup_dir, keep=10)   # 2 份待删，全部失败
    assert removed == []                            # 都删失败 → 名单如实为空
    assert len(list(backup_dir.glob("*.zip"))) == 12  # 文件都还在（宁留着不炸）


def test_create_backup_prunes_within_action(tmp_path):
    """备份动作自带保留上限：打包成功后超限旧包被清理；响应契约三键不变。"""
    local_dir = tmp_path / "local"
    (local_dir / "vl_data").mkdir(parents=True)
    (local_dir / "vl_data" / "note.txt").write_text("占位数据", encoding="utf-8")
    backup_dir = tmp_path / "backup"
    _seed_backups(backup_dir, 12)   # 预置 12 份旧包

    result = create_backup(local_dir, backup_dir, clock=1770000000.0, keep=10)
    assert result["ok"] is True
    assert set(result) == {"ok", "path", "size"}, (
        f"响应契约必须保持三键（test_console_workbench 钉死）：{set(result)!r}")
    survivors = sorted(backup_dir.glob("vl-backup-*.zip"))
    assert len(survivors) == 10, "12 份旧包 + 1 份新包 → 应只保留 newest 10"
    assert (backup_dir / Path(result["path"]).name).is_file(), "新包必须在保留名单内"
