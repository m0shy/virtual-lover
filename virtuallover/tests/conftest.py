# -*- coding: utf-8 -*-
"""pytest 共享夹具：把仓库根与本测试目录加入 sys.path。

- 仓库根入 path：便于 import virtuallover.memory.*（无 __init__.py 时按
  namespace package 导入）；
- 本目录入 path：pytest ≥8.4 对 namespace 包内的 conftest 以全限定名导入，
  测试模块里 `from conftest import …` 需要本目录在 path 上才成立
  （与 workbench/tests/_bootstrap.py 同一手法的推广）。

另提供记忆测试共用的确定性假嵌入与假 chat 回调（本组三个测试文件共用，
不注入任何真实敏感值；session_id 一律用占位值）。
"""

from __future__ import annotations

import hashlib
import os
import socket
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from virtuallover.memory.store import MemoryStore  # noqa: E402

# 测试用低维嵌入（dim 可配，校验链路与 2048 维完全一致，跑得快）
TEST_DIM = 8

# 占位会话标识：复刻旧栈 unified_msg_origin 形态，号码段为明显占位值
FAKE_SESSION = "测试人格:FriendMessage:12345678"


# ---------------------------------------------------------------------------
# 桥端口基线隔离 + xdist per-worker 化（VOICE_SPEC §4 → PERF_SPEC §4-O13）
# ---------------------------------------------------------------------------
#
# 桥默认监听 127.0.0.1:6199（SPEC §3），真实机器上 vl 常驻进程也占着 6199，
# 直接绑端口的测试（契约 1 / 冒烟）会 winerror 10048 连环失败。隔离手段演进：
# 1) 端口参数化：环境变量 VL_TEST_PORT 指定测试桥端口（串行/单机人工跑用）；
# 2) 占坑检测：钉死端口被占（在跑 vl 进程/其它服务）时相关测试 skip 并说明；
# 3) per-worker 化（PERF_SPEC §4-O13 / §7.2：xdist 进默认门禁，bridge_port
#    按 worker_id 派生端口）恢复 3 条绑定测试真执行：
#    - 设 VL_TEST_PORT=<base>：worker gwN 用 base+N（确定性偏移，互不抢口；
#      串行跑 worker 序号恒为 0，行为与旧语义完全一致）；
#    - 未设 VL_TEST_PORT：每个 worker 进程向 OS 要一个当前空闲端口
#      （bind 0 探测后缓存于本进程）——不再默认 6199（被 vl 常驻进程占坑），
#      绑定测试在任何机器上都真实执行而不是 skip。
#    两种取法都保证同一次 pytest -n auto 内各 worker 端口互不相同。


def _xdist_worker_index() -> int:
    """pytest-xdist worker 序号：gw3 → 3；串行运行/主进程返回 0。"""
    raw = (os.environ.get("PYTEST_XDIST_WORKER") or "").strip()
    if raw.startswith("gw") and raw[2:].isdigit():
        return int(raw[2:])
    return 0


def _acquire_free_port(host: str = "127.0.0.1") -> int:
    """向 OS 要一个当前空闲端口（bind 0 拿系统分配值，随即释放）。"""
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


_FREE_PORT_CACHE: "dict[str, int]" = {}


def vl_test_port() -> int:
    """测试桥端口：VL_TEST_PORT=<base> 时取 base+worker 序号；
    未设时每 worker 进程缓存一个空闲端口（TOCTOU 双探防抖）。"""
    raw = (os.environ.get("VL_TEST_PORT") or "").strip()
    if raw:
        return int(raw) + _xdist_worker_index()
    cached = _FREE_PORT_CACHE.get("port")
    if cached is None or port_in_use(cached):
        for _ in range(5):
            cached = _acquire_free_port()
            if not port_in_use(cached):
                break
        _FREE_PORT_CACHE["port"] = cached
    return cached


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """占坑检测：connect 探测端口是否已有监听者（vl 常驻进程即命中）。"""
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((host, int(port))) == 0


@pytest.fixture(scope="session")
def bridge_port():
    """需要真实绑定桥端口的测试用（session 级 = xdist 下每 worker 各一份）。

    钉死端口（VL_TEST_PORT）被占则 skip 并说明——不误报 failed；
    空闲端口取法（默认）总是新鲜探测，正常情况下不会 skip。
    """
    port = vl_test_port()
    pinned = bool((os.environ.get("VL_TEST_PORT") or "").strip())
    if pinned and port_in_use(port):
        pytest.skip(
            f"测试桥端口 {port} 已被监听（很可能是在跑的 vl 常驻进程）。"
            f"相关绑定测试已跳过；可设环境变量 VL_TEST_PORT=<空闲端口> 后重跑。"
        )
    return port


@pytest.fixture()
def fake_gsvi():
    """假 GSVI 契约服务器（tests/fake_gsvi.py）：默认 OK 模式随机端口，用完即关。

    语音组共用（VOICE_SPEC §4）：gsvi.base_url 接 GptSoVitsClient/VoiceManager，
    gsvi.set_mode() 切故障模式，gsvi.tts_requests 读请求起止时间。
    """
    from fake_gsvi import FakeGsvi

    server = FakeGsvi().start()
    try:
        yield server
    finally:
        server.stop()


def deterministic_vector(text: str, dim: int = TEST_DIM) -> np.ndarray:
    """按文本 md5 生成确定性伪随机单位向量（未知文本的稳定嵌入）。"""
    seed = int.from_bytes(hashlib.md5(text.encode("utf-8")).digest()[:4], "little")
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


def make_fake_embedder(vectors: dict[str, list[float]] | None = None,
                       dim: int = TEST_DIM):
    """确定性假嵌入：命中 vectors 表用给定向量，否则按文本哈希生成。"""
    table = {k: list(v) for k, v in (vectors or {}).items()}

    async def embed(text: str) -> list[float]:
        if text in table:
            return table[text]
        return deterministic_vector(text, dim).tolist()

    return embed


@pytest.fixture
def fake_store(tmp_path):
    """构造临时 MemoryStore 工厂：默认占位嵌入配置 + 8 维假嵌入。"""
    counter = {"n": 0}

    def _make(vectors: dict[str, list[float]] | None = None,
              dim: int = TEST_DIM, name: str | None = None, **kwargs) -> MemoryStore:
        counter["n"] += 1
        db_name = name or f"memory_{counter['n']}.db"
        return MemoryStore(
            tmp_path / db_name,
            embedder=make_fake_embedder(vectors, dim),
            provider_id="占位供应商",
            model="test-embedding-model",
            endpoint="http://127.0.0.1:9",
            dim=dim,
            **kwargs,
        )

    return _make


class FakeChat:
    """假 chat 回调：记录 (system, user) 调用；可配置回复序列或恒定失败。"""

    def __init__(self, replies: list[str] | None = None, fail: bool = False,
                 default_reply: str = "我记得了。") -> None:
        self.calls: list[tuple[str, str]] = []
        self.replies = list(replies or [])
        self.fail = fail
        self.default_reply = default_reply

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError("模拟 LLM 故障")
        if len(self.calls) <= len(self.replies):
            return self.replies[len(self.calls) - 1]
        return self.default_reply


@pytest.fixture
def fake_chat():
    """假 chat 工厂：FakeChat(replies=[...], fail=False)。"""
    return FakeChat
