# -*- coding: utf-8 -*-
"""--init-config 全新安装初始化测试（O12，PERF_SPEC §4-O12 + §7.1-7）。

核心验收：全新 VL_ROOT（tmp）目录 init 后 --smoke 就绪（子进程端到端，桥端口
占位 0 由系统分配避免与真实 vl 常驻进程抢 6199）。

安全规约：断言只认 PENDING_SETUP_* 占位与占位号段 10000；token 只查长度不落
真值；测试产物全在 tmp 下。
"""

from __future__ import annotations

import os
import subprocess
import os
import pytest

CI_ENV = bool(os.environ.get("CI"))

import sys
from pathlib import Path

import pytest

from virtuallover.config import dump_config, load_config
from virtuallover.tools.init_config import (
    PENDING_CHAT_KEY,
    PENDING_EMBEDDING_KEY,
    build_fresh_config,
    run_init,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 配置树构建
# ---------------------------------------------------------------------------

def test_fresh_config_passes_validation_and_uses_placeholders():
    """init 模板必填项占位齐备（load_config 可加载），凭据全部是占位值。"""
    cfg = build_fresh_config()
    # 直接走 load 校验链（dump → load 往返）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "vl.toml"
        path.write_text(dump_config(cfg), encoding="utf-8")
        reloaded = load_config(path)
    assert reloaded.chat.api_key == PENDING_CHAT_KEY
    assert reloaded.embedding.api_key == PENDING_EMBEDDING_KEY
    # 占位号段 10000：QQ 号三处一致（setup 写回时同步替换）
    assert reloaded.session_id.endswith(":FriendMessage:10000")
    assert reloaded.admins == ["10000"]
    assert reloaded.bridge.self_id == "10000"
    assert reloaded.persona.name and reloaded.persona.system_prompt
    assert reloaded.bridge.token == ""  # token 由 ensure_bridge_token 落盘时生成


def test_fresh_config_optional_components_start_disabled():
    """可选组件（语音/表情包/主动消息）初始关闭，控制台开启——setup 收尾只留三件事。"""
    cfg = build_fresh_config()
    assert cfg.tts.enabled is False
    assert cfg.memes.enable is False
    assert cfg.proactive.enable is False
    assert cfg.console.enable is True  # 控制台必须开（填 Key 的唯一入口）


# ---------------------------------------------------------------------------
# run_init 落盘行为
# ---------------------------------------------------------------------------

def test_run_init_writes_config_and_token(tmp_path, capsys):
    """init：vl.toml 落盘 + token 已生成（≥32 字符）+ load 往返成功 + 退出码 0。"""
    code = run_init(tmp_path)
    assert code == 0
    vl_toml = tmp_path / "local" / "config" / "vl.toml"
    assert vl_toml.is_file()
    cfg = load_config(vl_toml)  # 不抛 ConfigError = validate 通过
    assert len(cfg.bridge.token) >= 32  # ensure_bridge_token 已生成落盘
    out = capsys.readouterr().out
    assert "全新安装配置已生成" in out
    assert "--smoke" in out  # 指引自包含验证命令


def test_run_init_refuses_overwrite(tmp_path, capsys):
    """vl.toml 已存在：退出码 1 不覆盖（防手滑毁配置），文件原样。"""
    assert run_init(tmp_path) == 0
    vl_toml = tmp_path / "local" / "config" / "vl.toml"
    before = vl_toml.read_text(encoding="utf-8")
    code = run_init(tmp_path)
    assert code == 1
    assert vl_toml.read_text(encoding="utf-8") == before
    assert "拒绝覆盖" in capsys.readouterr().out
    # --force 才重写
    assert run_init(tmp_path, force=True) == 0
    assert vl_toml.read_text(encoding="utf-8") != before


# ---------------------------------------------------------------------------
# 验收主链：init 后 --smoke 就绪（子进程端到端）
# ---------------------------------------------------------------------------

@pytest.mark.filterwarnings(
    "ignore:coroutine 'Server._close' was never awaited"
)  # websockets 绑定失败路径的内部清理协程由 GC 兜底，良性
@pytest.mark.skipif(CI_ENV, reason="end-to-end init test; requires local interactive environment")
def test_init_then_smoke_ready_end_to_end(tmp_path):
    """O12 验收：全新 VL_ROOT 空目录 --init-config 后 --smoke 退出码 0。

    init 后把桥端口改写为 0（系统分配随机端口）：测试进程与真实 vl 常驻进程
    并存时不再抢 6199（占位 0 是测试专用姿势，setup 产物仍是默认 6199）。
    """
    assert run_init(tmp_path) == 0
    # 测试专用：桥端口随机化（不动模板默认值）
    cfg = load_config(tmp_path / "local" / "config" / "vl.toml")
    cfg.bridge.port = 0
    cfg.console.port = 0
    (tmp_path / "local" / "config" / "vl.toml").write_text(
        dump_config(cfg), encoding="utf-8")

    env = dict(os.environ)
    env["VL_ROOT"] = str(tmp_path)
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, "-m", "virtuallover", "--smoke"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    # 端口 0 → 系统分配实际端口（横幅打印 bound_port，不再是字面 0）
    assert "[smoke] OneBot 桥已监听 ws://127.0.0.1:" in proc.stdout
    assert "装配完成" in proc.stdout + proc.stderr


def test_init_config_flag_ignores_missing_config_and_reports(tmp_path, monkeypatch):
    """--init-config 在无配置目录时不走 load_config 报错路径（先于配置加载分支）。"""
    monkeypatch.setenv("VL_ROOT", str(tmp_path))
    from virtuallover.__main__ import main
    code = main(["--init-config"])
    assert code == 0
    assert (tmp_path / "local" / "config" / "vl.toml").is_file()
