# -*- coding: utf-8 -*-
"""python -m virtuallover 冒烟（--smoke）：最小配置（tmp 目录假 vl.toml）装配 +
桥监听 + 优雅退出（任务 5 固化）。

两条路径都覆盖：
- 进程内：monkeypatch REPO_ROOT 指向 tmp（与 test_config 的冒烟测试同一手法），
  直接断言退出码、监听横幅与 token 落盘；另覆盖「端口占用 → 中文报错退出 1」；
- 子进程：``python -m virtuallover --smoke`` + 环境变量 VL_ROOT=tmp，验证真实
  模块入口（含包导入、日志初始化）端到端可跑。

端口基线隔离（VOICE_SPEC §4 前置）：真实绑定走 bridge_port 夹具——默认 6199，
环境变量 VL_TEST_PORT 可覆盖；端口被在跑 vl 常驻进程占坑时 skip 并说明。
「端口占用 → 退出 1」用随机空闲端口自占坑，与真实端口解耦，永不互相干扰。

安全规约：配置全部占位值（admins/self_id 用 10000 占位号段），不落任何真实
QQ 号/密钥；console.enable=false 避免测试期间抢占本机 8787 控制台端口。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

CI_ENV = bool(os.environ.get("CI"))

from virtuallover.config import (
    BridgeConfig,
    ChatProviderConfig,
    ConsoleConfig,
    EmbeddingConfig,
    PersonaConfig,
    dump_config,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_smoke_config(root: Path, *, port: int) -> Path:
    """写一份最小可启动的 vl.toml（必填项齐全 + console 关闭 + 桥占位账号）。

    port 由调用方给：真实绑定用例传 bridge_port 夹具值（默认 6199，VL_TEST_PORT
    可覆盖，被占则 skip）；端口占用负向用例传自占的随机端口。
    """
    from virtuallover.config import VlConfig

    cfg = VlConfig(
        session_id="测试人格:FriendMessage:10000",
        admins=["10000"],
        whitelist=["10000"],
    )
    cfg.bridge = BridgeConfig(host="127.0.0.1", port=port, token="",
                              self_id="10000")  # 占位账号
    cfg.chat = ChatProviderConfig(api_base="https://chat.test/v1",
                                  api_key="sk-TEST-SMOKE", model="test-model")
    cfg.embedding = EmbeddingConfig(api_base="https://emb.test/v1",
                                    api_key="sk-TEST-EMB", model="emb-model")
    cfg.persona = PersonaConfig(name="测试人格", system_prompt="你是「她」（冒烟占位人格）。")
    cfg.console = ConsoleConfig(enable=False)  # 冒烟不启控制台（避免抢占 8787）
    config_dir = root / "local" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 进程内冒烟
# ---------------------------------------------------------------------------

def test_smoke_assembles_binds_bridge_port_and_exits(tmp_path, monkeypatch, capsys,
                                                     bridge_port):
    """--smoke：装配 → 桥监听配置端口（默认 127.0.0.1:6199）→ 优雅退出 0（token 已落盘）。

    bridge_port 夹具：默认 6199（SPEC §3），VL_TEST_PORT 可覆盖；端口被在跑
    vl 常驻进程占坑时整条 skip 并说明——不再连环 failed。
    """
    import virtuallover.__main__ as main_mod

    _write_smoke_config(tmp_path, port=bridge_port)
    monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
    exit_code = main_mod.main(["--smoke"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"[smoke] OneBot 桥已监听 ws://127.0.0.1:{bridge_port}/ws" in out
    # token 已自动生成并回写（SPEC §3.3）
    reloaded = load_config(tmp_path / "local" / "config" / "vl.toml")
    assert len(reloaded.bridge.token) >= 32
    # 冒烟产生的运行数据全部收敛在 tmp 下（不触碰真实 local/）
    assert (tmp_path / "local" / "vl_data" / "history.db").exists()


@pytest.mark.filterwarnings(
    "ignore:coroutine 'Server._close' was never awaited"
)  # websockets 在绑定失败路径的内部清理协程由 GC 兜底，良性
def test_smoke_port_in_use_reports_and_exits_1(tmp_path, monkeypatch):
    """配置端口被占用：中文报错日志 + 退出码 1（SPEC §13：端口占用禁静默）。

    自占坑用系统分配的随机空闲端口（bind 0 拿实际端口）——与 6199 真实端口
    解耦：vl 常驻进程在跑时本测试照常可跑，也不会反过来干扰别人。
    """
    import virtuallover.__main__ as main_mod

    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied_port = blocker.getsockname()[1]
        _write_smoke_config(tmp_path, port=occupied_port)
        monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
        exit_code = main_mod.main(["--smoke"])
        assert exit_code == 1
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# 子进程冒烟（真实模块入口）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(CI_ENV, reason="end-to-end subprocess test; requires local interactive environment")
def test_smoke_subprocess_end_to_end(tmp_path, bridge_port):
    """python -m virtuallover --smoke（VL_ROOT=tmp）：端到端装配监听后优雅退出。

    桥端口走 bridge_port 夹具（默认 6199，VL_TEST_PORT 可覆盖，占坑 skip）——
    端口写进子进程读的 vl.toml，无需向子进程传 VL_TEST_PORT。
    """
    _write_smoke_config(tmp_path, port=bridge_port)
    env = dict(os.environ)
    env["VL_ROOT"] = str(tmp_path)
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, "-m", "virtuallover", "--smoke"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert f"[smoke] OneBot 桥已监听 ws://127.0.0.1:{bridge_port}/ws" in proc.stdout
    assert "装配完成" in proc.stdout + proc.stderr
    # 日志轮转文件落在运行根下（SPEC §13）
    assert (tmp_path / "local" / "logs" / "vl.log").exists()


@pytest.mark.skipif(CI_ENV, reason="end-to-end subprocess test; requires local interactive environment")
def test_smoke_subprocess_missing_config_exit_2(tmp_path):
    """VL_ROOT 下无 vl.toml：中文报错退出 2（引导先跑迁移）。"""
    env = dict(os.environ)
    env["VL_ROOT"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "virtuallover", "--smoke"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "config_migrate" in proc.stderr


# ---------------------------------------------------------------------------
# 记忆降级冒烟（五轮修缮 R1）：[embedding] enabled=false → 装配整体跳过不报错
# ---------------------------------------------------------------------------

def test_smoke_assembles_with_embedding_disabled(tmp_path, monkeypatch, capsys,
                                                 bridge_port):
    """setup 跳过 embedding（enabled=false）的降级语义：vl 可正常装配启动。

    断言三层：--smoke 退出 0；日志明示记忆未启用（llm 与 memory 两处 INFO）；
    记忆槽位保持未装配（memory_store/memory_service 为 None，管线按各自
    None 守卫静默跳过）——聊天/日记不受影响，全程零报错。
    """
    import virtuallover.__main__ as main_mod
    from virtuallover.config import EmbeddingConfig

    _write_smoke_config(tmp_path, port=bridge_port)
    # 复刻 setup_writeback 跳过 embedding 后的落盘形态：enabled=false + 三字段清空
    path = tmp_path / "local" / "config" / "vl.toml"
    cfg = load_config(path)
    cfg.embedding = EmbeddingConfig(enabled=False)
    path.write_text(dump_config(cfg), encoding="utf-8")

    import virtuallover.memory.store as store_mod

    captured: "dict[str, object]" = {}
    real_store_setup = store_mod.setup

    def spy_store_setup(ctx):
        result = real_store_setup(ctx)
        # setup 之后读槽位：正常路径会挂上 MemoryStore 实例；降级路径两者保持 None
        captured["embedding_enabled"] = getattr(ctx.cfg.embedding, "enabled", True)
        captured["store"] = getattr(ctx, "memory_store", None)
        captured["service"] = getattr(ctx, "memory_service", None)
        return result

    monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(store_mod, "setup", spy_store_setup)
    exit_code = main_mod.main(["--smoke"])
    assert exit_code == 0
    assert captured["embedding_enabled"] is False
    assert captured["store"] is None  # 记忆库未装配（enabled=false → setup 直接跳过）
    assert captured["service"] is None
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "记忆功能未启用" in logs  # memory.store 的明示降级日志
    assert "记忆功能暂闭" in logs  # brain.llm 的同源明示日志
    # 历史库照常装配（聊天主链路不受记忆降级影响）
    assert (tmp_path / "local" / "vl_data" / "history.db").exists()
