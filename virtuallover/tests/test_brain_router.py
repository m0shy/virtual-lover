# -*- coding: utf-8 -*-
"""brain/router.py 契约测试（五轮修缮第二轮智能降级）。

覆盖点（对照任务书七条）：
- 瞬态降级：5xx/网络失败 → per-request 本地兜底（不黏性，下轮仍先试云端）；
  主客户端 max_retries 降为 1；
- 黏性切换：401/403 连续 2 轮 → sticky_local 直发本地（不再碰云端）、
  spawn 被触发、显著告警、首次温柔提示只发一次；
- 半开恢复：sticky 30min 后探针（1-token）成功 → 只亮 cloud_recovered，
  不自动切回；
- 上下文截断：fallback 路径 4096 预算丢最旧 turn，system 与当前消息不碰；
- 本地不可用只告警：未配置 llama exe（不 spawn）→ 抛原 403 + 告警，
  黏性意图保留；
- 命令路由：/brain status|local|cloud 管理员门禁与行为。

全部用假客户端/假 manager/假钟，零网络零真实密钥（403 文案为复现 vl.log
「chat HTTP 403：error.Forbidden/AccountOverdueError」实证的占位串）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.brain.llm import LLMError, LLMReply  # noqa: E402
from virtuallover.brain.router import (  # noqa: E402
    AUTH_FAIL_STATUSES,
    HALFOPEN_PROBE_INTERVAL_SECONDS,
    MODE_CLOUD,
    MODE_STICKY_LOCAL,
    STICKY_AUTH_THRESHOLD,
    BrainRouter,
    LocalBrainManager,
    build_brain_router,
    truncate_for_fallback,
)
from virtuallover.commands import (  # noqa: E402
    BRAIN_USAGE_TEXT,
    CommandRouter,
)

# 占位账号（严禁真实 QQ 号）
ADMIN = "10001"
NON_ADMIN = "20002"     # 白名单内非管理员（/brain 拒绝文案路径）
STRANGER = "30003"      # 白名单外陌生人（§8.6 静默忽略路径）

_OK_403 = "chat HTTP 403：error.Forbidden/AccountOverdueError"


# ---------------------------------------------------------------------------
# 测试假件
# ---------------------------------------------------------------------------


class _FakeClient:
    """ChatClient 假件：按脚本队列回放（LLMError 抛 / LLMReply 返）。"""

    def __init__(self, provider_id: str = "chat",
                 default: "object | None" = None):
        self.provider_id = provider_id
        self.max_retries = 3
        self.calls: "list[tuple[list, dict]]" = []
        self.script: "list[object]" = []
        self.default = default if default is not None else _reply("ok")

    def chat(self, messages, **kwargs):
        self.calls.append((list(messages), dict(kwargs)))
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        return item

    async def chat_async(self, messages, **kwargs):
        return self.chat(messages, **kwargs)


class _FakeManager:
    """LocalBrainManager 假件：ensure 可编程，调用计数。"""

    def __init__(self, *, probe: bool = True, ensure_ok: bool = True):
        self._probe = probe
        self._ensure_ok = ensure_ok
        self.ensure_calls = 0
        self.last_error = ""
        self.host, self.port = "127.0.0.1", 8002

    def probe_ready(self) -> bool:
        return self._probe

    def ensure_running(self) -> bool:
        self.ensure_calls += 1
        if self._ensure_ok:
            self._probe = True
            return True
        self.last_error = "未配置 fallback_llama_exe（不 spawn，只告警）"
        return False

    def status(self) -> dict:
        return {"state": "ready" if self._probe else "not_started",
                "ownership": None, "pid": None,
                "host": self.host, "port": self.port, "last_error": self.last_error}


class _FakeClock:
    """可拨 monotonic 假钟（半开探针的 30min 推进用）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reply(text: str, provider_id: str = "chat") -> LLMReply:
    return LLMReply(content=text, provider_id=provider_id, attempts=1)


def _err(status: "int | None", retriable: bool, msg: str = "") -> LLMError:
    return LLMError(msg or f"chat HTTP {status}", status=status, retriable=retriable)


def _make_router(*, primary_script: "list | None" = None,
                 local: "_FakeClient | None" = None,
                 manager: "_FakeManager | None" = None,
                 notice_text: str = "我脑袋有点晕，说话可能慢一点",
                 clock: "_FakeClock | None" = None,
                 max_context_tokens: int = 4096) -> "tuple[BrainRouter, _FakeClient, _FakeClient, _FakeManager, list, _FakeClock]":
    """装配一套被测 Router；notice 投递记录进 notices 列表。"""
    primary = _FakeClient("chat")
    primary.script = list(primary_script or [])
    local = local or _FakeClient("chat_local", default=_reply("本地在呢", "chat_local"))
    manager = manager or _FakeManager()
    clock = clock or _FakeClock()
    notices: "list[str]" = []
    router = BrainRouter(
        primary,
        local_client=local,
        manager=manager,
        max_context_tokens=max_context_tokens,
        notice_enabled=True,
        notice_text=notice_text,
        notice_sink=notices.append,
        clock=clock,
        sleeper=lambda _s: None,
    )
    return router, primary, local, manager, notices, clock


# ---------------------------------------------------------------------------
# ① 瞬态降级（per-request fallback，不黏性）
# ---------------------------------------------------------------------------


def test_transient_failure_falls_back_per_request():
    """500 瞬态失败 → 本轮本地兜底成功；模式保持 cloud、计数 +1、下一轮先试云端。"""
    router, primary, local, manager, notices, clock = _make_router(
        primary_script=[_err(500, True)])
    reply = router.chat([{"role": "user", "content": "在吗"}])
    assert reply.provider_id == "chat_local"
    snap = router.status()
    assert snap["mode"] == MODE_CLOUD                  # 瞬态不粘
    assert snap["fallback_count"] == 1
    assert snap["consec_auth_fails"] == 0
    assert notices == []                               # 温柔提示只属于黏性切换
    # 下一轮：仍先试云端（per-request 语义），成功即照常返回
    reply2 = router.chat([{"role": "user", "content": "还在吗"}])
    assert reply2.provider_id == "chat"
    assert len(primary.calls) == 2


def test_router_caps_primary_max_retries_to_one():
    """fallback 场景主客户端 max_retries 降为 1（不再 4 连击晾用户）。"""
    router, primary, *_ = _make_router()
    assert primary.max_retries == 1


def test_network_error_is_transient_and_falls_back():
    """网络错误（status=None, retriable=True）同享 per-request 兜底。"""
    router, primary, local, *_ = _make_router(
        primary_script=[_err(None, True, "网络错误（URLError）")])
    reply = router.chat([{"role": "user", "content": "hi"}])
    assert reply.provider_id == "chat_local"
    assert len(local.calls) == 1


def test_nonretryable_nonauth_error_does_not_fall_back():
    """400 这类不可重试且非鉴权的错误不降级（本地换了也治不了坏请求）——照旧抛。"""
    router, primary, local, *_ = _make_router(
        primary_script=[_err(400, False)])
    with pytest.raises(LLMError) as excinfo:
        router.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status == 400
    assert local.calls == []


def test_primary_extra_body_prompt_cache_key_not_sent_to_local():
    """本地路径剥离 prompt_cache_key（云端 KV 缓存键，本地无此语义）。"""
    router, *_ = _make_router(primary_script=[_err(500, True)])
    router.chat([{"role": "user", "content": "hi"}], extra_body={"prompt_cache_key": "s1"})
    local = router._local
    _msgs, kwargs = local.calls[0]
    assert "prompt_cache_key" not in (kwargs.get("extra_body") or {})


# ---------------------------------------------------------------------------
# ② 黏性切换（401/403 连续 2 轮 → sticky_local）
# ---------------------------------------------------------------------------


def test_auth_failure_once_stays_cloud():
    """403 只有 1 轮：不黏性、不 spawn（她可能只是单次抖动+欠费未确认）。"""
    router, primary, local, manager, notices, clock = _make_router(
        primary_script=[_err(403, False, _OK_403)])
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "hi"}])
    assert router.status()["mode"] == MODE_CLOUD
    assert manager.ensure_calls == 0
    assert notices == []


@pytest.mark.parametrize("status", sorted(AUTH_FAIL_STATUSES))
def test_auth_failures_twice_trip_sticky(status):
    """连续 2 轮 401/403 → sticky_local：本轮即本地保命、spawn 触发、告警 + 首次提示。"""
    router, primary, local, manager, notices, clock = _make_router(
        primary_script=[_err(status, False), _err(status, False)])
    # 第 1 轮：403（尚不黏性，抛出）
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "第一条"}])
    # 第 2 轮：403 → 凑满 2 → 黏性 + 本轮直接本地兜底（保命，不再哑）
    reply = router.chat([{"role": "user", "content": "第二条"}])
    assert reply.provider_id == "chat_local"
    snap = router.status()
    assert snap["mode"] == MODE_STICKY_LOCAL
    assert snap["sticky_switches"] == 1
    assert snap["consec_auth_fails"] == STICKY_AUTH_THRESHOLD
    assert manager.ensure_calls == 1                   # 按需 spawn/收编恰一次
    assert notices == ["我脑袋有点晕，说话可能慢一点"]   # 温柔提示恰一次
    # 第 3 轮：直发本地，primary 不再被碰（第 1 轮 403 未兜底 → 本地共 2 次）
    primary.calls.clear()
    router.chat([{"role": "user", "content": "第三条"}])
    assert primary.calls == []
    assert len(local.calls) == 2


def test_sticky_notice_sent_only_once():
    """黏性长期持续：提示绝不重复刷屏（首次语义）。"""
    router, primary, local, manager, notices, clock = _make_router()
    primary.script = [_err(403, False, _OK_403)]
    primary.default = _err(403, False, _OK_403)
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "a"}])      # 第 1 轮抛
    router.chat([{"role": "user", "content": "b"}])          # 第 2 轮黏性 + 提示
    for i in range(3):
        router.chat([{"role": "user", "content": f"n{i}"}])  # 后续全走本地
    assert notices == ["我脑袋有点晕，说话可能慢一点"]


# ---------------------------------------------------------------------------
# ③ 半开恢复（30min 探针，成功只亮提示不自动切回）
# ---------------------------------------------------------------------------


def test_halfopen_probe_success_sets_flag_without_switchback():
    """sticky 30min 后探针成功 → cloud_recovered=True；模式不变、流量仍走本地。"""
    clock = _FakeClock()
    router, primary, local, manager, notices, clock = _make_router(clock=clock)
    primary.default = _err(403, False, _OK_403)
    primary.script = [_err(403, False, _OK_403)]
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "a"}])
    router.chat([{"role": "user", "content": "b"}])     # 进入黏性
    assert router.status()["mode"] == MODE_STICKY_LOCAL
    # 推进 30min：下一轮 sticky 请求先打探针（1-token chat）
    clock.advance(HALFOPEN_PROBE_INTERVAL_SECONDS + 1)
    primary.script = [_reply("好")]                     # 探针成功
    router.chat([{"role": "user", "content": "c"}])
    snap = router.status()
    assert snap["cloud_recovered"] is True              # 只亮提示
    assert snap["mode"] == MODE_STICKY_LOCAL            # 不自动切回
    probe_msgs, probe_kwargs = primary.calls[-1]
    assert probe_msgs == [{"role": "user", "content": "ping"}]
    assert probe_kwargs.get("max_tokens") == 1          # 1-token，成本可忽略
    # 后续流量仍直发本地（30min 内不再探）
    n_primary = len(primary.calls)
    router.chat([{"role": "user", "content": "d"}])
    assert len(primary.calls) == n_primary
    assert local.calls[-1][0][-1]["content"] == "d"


def test_halfopen_probe_failure_keeps_sticky_quietly():
    """探针失败（云端仍 403）：保持黏性，不刷错误横幅。"""
    clock = _FakeClock()
    router, primary, local, manager, notices, clock = _make_router(clock=clock)
    primary.default = _err(403, False, _OK_403)
    primary.script = [_err(403, False, _OK_403)]
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "a"}])
    router.chat([{"role": "user", "content": "b"}])
    clock.advance(HALFOPEN_PROBE_INTERVAL_SECONDS + 1)
    primary.script = [_err(403, False, _OK_403)]        # 探针失败
    router.chat([{"role": "user", "content": "c"}])
    snap = router.status()
    assert snap["mode"] == MODE_STICKY_LOCAL
    assert snap["cloud_recovered"] is False


def test_force_cloud_clears_sticky_and_recovers_traffic():
    """/brain cloud：清黏性与恢复标记；后续流量回云端。"""
    router, primary, local, manager, notices, clock = _make_router()
    primary.default = _err(403, False, _OK_403)
    primary.script = [_err(403, False, _OK_403)]
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "a"}])
    router.chat([{"role": "user", "content": "b"}])
    assert router.status()["mode"] == MODE_STICKY_LOCAL
    router.force_cloud()
    snap = router.status()
    assert snap["mode"] == MODE_CLOUD
    assert snap["cloud_recovered"] is False
    primary.script = [_reply("云端回来了")]
    reply = router.chat([{"role": "user", "content": "c"}])
    assert reply.provider_id == "chat"
    assert reply.content == "云端回来了"


def test_force_local_requires_local_ready():
    """/brain local：本地拉不起（不 spawn 配置）→ 返回失败与原因，不进黏性。"""
    manager = _FakeManager(probe=False, ensure_ok=False)
    router, primary, local, *_ = _make_router(manager=manager)
    ok, message = router.force_local()
    assert ok is False
    assert "本地大脑不可用" in message
    assert router.status()["mode"] == MODE_CLOUD


# ---------------------------------------------------------------------------
# ④ 上下文截断（fallback 路径 4096 预算）
# ---------------------------------------------------------------------------


def _long_messages() -> list[dict]:
    """system(100 tok) + 10 turn×2 条（每条约 104 tok）+ 当前 user(50 tok)。"""
    msgs = [{"role": "system", "content": "系" * 100}]
    for i in range(10):
        msgs.append({"role": "user", "content": f"问{i}" + "字" * 97})
        msgs.append({"role": "assistant", "content": f"答{i}" + "字" * 97})
    msgs.append({"role": "user", "content": "最" * 50})
    return msgs


def test_truncate_for_fallback_within_budget_is_noop():
    """预算内原样返回（长度不变）。"""
    msgs = [{"role": "system", "content": "短"},
            {"role": "user", "content": "你好"}]
    out = truncate_for_fallback(msgs, 4096)
    assert len(out) == len(msgs)
    assert out[0]["content"] == "短"


def test_truncate_for_fallback_drops_oldest_turns_keeps_system_and_last():
    """超预算：丢最旧 turn；system 前缀与最后一条（当前消息）不碰。"""
    msgs = _long_messages()
    budget = 600
    out = truncate_for_fallback(msgs, budget)
    assert out[0]["role"] == "system" and out[0]["content"] == "系" * 100
    assert out[-1]["content"] == "最" * 50
    from virtuallover.brain.persona import estimate_token_list
    assert sum(estimate_token_list(out)) <= budget
    assert len(out) < len(msgs)
    # 丢的是最旧（问0/答0 先消失）
    texts = [m["content"] for m in out]
    assert "问0" + "字" * 97 not in texts


def test_truncate_for_fallback_trims_last_when_only_system_plus_suffix():
    """只有 system+suffix 且回合为零的极端：原样返回并告警（人设不能砍）。"""
    msgs = [{"role": "system", "content": "系" * 100},
            {"role": "user", "content": "最" * 50}]
    out = truncate_for_fallback(msgs, 60)
    assert out == msgs


def test_router_local_path_truncates_messages():
    """端到端：主 500 → 本地收到的 messages 已按预算裁剪（此处预算 600）。"""
    router, *_ = _make_router(primary_script=[_err(500, True)], max_context_tokens=600)
    long_msgs = _long_messages()
    router.chat(long_msgs)
    local = router._local
    sent = local.calls[0][0]
    assert len(sent) < len(long_msgs)
    assert sent[-1]["content"] == "最" * 50


# ---------------------------------------------------------------------------
# ⑤ 本地不可用只告警不 spawn
# ---------------------------------------------------------------------------


def test_sticky_with_local_unavailable_warns_and_reraises(caplog):
    """黏性但本地拉不起（exe 未配置→不 spawn）：抛原 403 + 显著告警 + 黏性意图保留。"""
    manager = LocalBrainManager(
        host="127.0.0.1", port=8002,
        spawn_exe="",                      # 未配置 → 只告警不 spawn
        probe=lambda: False,
        clock=_FakeClock(), sleeper=lambda _s: None,
    )
    router, primary, local, fm, notices, clock = _make_router(
        primary_script=[_err(403, False, _OK_403), _err(403, False, _OK_403)],
        manager=manager)
    with caplog.at_level(logging.WARNING, logger="virtuallover.brain.router"):
        with pytest.raises(LLMError) as first:
            router.chat([{"role": "user", "content": "a"}])
        assert first.value.status == 403
        with pytest.raises(LLMError) as second:
            router.chat([{"role": "user", "content": "b"}])
        # 第 2 轮凑满 2 → 黏性意图置位，但无备可降 → 抛原 403（轮静默同旧行为）
        assert second.value.status == 403
    assert router.status()["mode"] == MODE_STICKY_LOCAL
    text = caplog.text
    assert "本地大脑不可用" in text
    assert "仅告警不阻塞" in text
    assert notices == []                   # 本地都没起来，不发「切本地」提示


def test_router_without_local_client_only_warns():
    """local_client=None（fallback 未装配本地）→ 黏性置位 + 每次告警 + 照旧抛。"""
    primary = _FakeClient("chat", default=_err(403, False, _OK_403))
    notices: "list[str]" = []
    router = BrainRouter(
        primary, local_client=None, manager=None,
        notice_text="x", notice_sink=notices.append,
        clock=_FakeClock(), sleeper=lambda _s: None,
    )
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "a"}])
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "b"}])
    assert router.status()["mode"] == MODE_STICKY_LOCAL
    assert notices == []
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "c"}])   # 黏性后仍走 primary 并如实抛


def test_sticky_cloud_success_auto_exits_when_local_down():
    """黏性 + 本地不可用 + 云端意外成功（如欠费已充值）→ 自动解除黏性。"""
    primary = _FakeClient("chat")
    primary.script = [_err(403, False, _OK_403), _err(403, False, _OK_403)]
    primary.default = _reply("云端好了")
    manager = _FakeManager(probe=False, ensure_ok=False)
    router = BrainRouter(primary, local_client=None, manager=manager,
                         clock=_FakeClock(), sleeper=lambda _s: None)
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "a"}])
    with pytest.raises(LLMError):
        router.chat([{"role": "user", "content": "b"}])   # 进黏性（无备，抛）
    reply = router.chat([{"role": "user", "content": "c"}])  # 本地仍不可用 → 回试云端
    assert reply.content == "云端好了"
    assert router.status()["mode"] == MODE_CLOUD


# ---------------------------------------------------------------------------
# ⑥ /brain 命令路由（管理员门禁）
# ---------------------------------------------------------------------------


class _StubBrainRouter:
    """BrainRouter 假件：status 快照固定，force 调用记录。"""

    def __init__(self):
        self.forced: "list[str]" = []
        self._status = {
            "enabled": True, "mode": MODE_STICKY_LOCAL,
            "fallback_count": 3, "sticky_switches": 1, "consec_auth_fails": 2,
            "cloud_recovered": True, "local_ready": True,
            "last_error": {"status": 403, "kind": "auth",
                           "message": _OK_403, "at": 1728690000.0},
            "last_local_error": None,
            "local": {"host": "127.0.0.1", "port": 8002, "state": "ready",
                      "ownership": "owned", "pid": 4321, "last_error": ""},
        }

    def status(self) -> dict:
        return dict(self._status)

    def force_local(self):
        self.forced.append("local")
        return True, "已切到本地大脑（恢复云端用 /brain cloud）"

    def force_cloud(self) -> dict:
        self.forced.append("cloud")
        return self.status()


def test_brain_command_rejects_non_admin():
    """/brain 管理员门禁：门禁内（白名单）非管理员给拒绝文案；门外静默忽略（§8.6）。"""
    stub = _StubBrainRouter()
    router = CommandRouter(brain_router=stub)
    out = asyncio.run(router.handle("/brain status", NON_ADMIN, admins=[ADMIN],
                                    whitelist=[NON_ADMIN]))
    assert out == ["只有管理员才能查看和切换大脑哦。"]
    assert stub.forced == []
    # 白名单/管理员之外的陌生人：整个命令面静默忽略（不暴露命令存在）
    out = asyncio.run(router.handle("/brain status", STRANGER, admins=[ADMIN],
                                    whitelist=[NON_ADMIN]))
    assert out is None


def test_brain_status_renders_mode_counters_and_http_code():
    """/brain status：黏性模式、兜底计数、最近错误含 HTTP 403。"""
    stub = _StubBrainRouter()
    router = CommandRouter(brain_router=stub)
    out = asyncio.run(router.handle("/brain", ADMIN, admins=[ADMIN], whitelist=[]))
    assert len(out) == 1
    text = out[0]
    assert "黏性本地" in text
    assert "HTTP 403" in text
    assert "AccountOverdueError" in text
    assert "3 次" in text
    assert "127.0.0.1:8002" in text
    assert "已恢复" in text            # cloud_recovered=True → 亮可切回提示


def test_brain_local_and_cloud_dispatch():
    """/brain local|cloud 转发 force_local/force_cloud；未知子命令给用法。"""
    stub = _StubBrainRouter()
    router = CommandRouter(brain_router=stub)
    out = asyncio.run(router.handle("/brain local", ADMIN, admins=[ADMIN], whitelist=[]))
    assert "已切到本地大脑" in out[0]
    out = asyncio.run(router.handle("/brain cloud", ADMIN, admins=[ADMIN], whitelist=[]))
    assert "已切回云端大脑" in out[0]
    assert stub.forced == ["local", "cloud"]
    out = asyncio.run(router.handle("/brain 什么", ADMIN, admins=[ADMIN], whitelist=[]))
    assert out == [BRAIN_USAGE_TEXT]


def test_brain_command_without_router_reports_disabled():
    """fallback_enabled=false（router 未装配）是正常态：如实说未启用，不假装。"""
    router = CommandRouter(brain_router=None)
    out = asyncio.run(router.handle("/brain status", ADMIN, admins=[ADMIN], whitelist=[]))
    assert "fallback_enabled=false" in out[0]
    out = asyncio.run(router.handle("/brain local", ADMIN, admins=[ADMIN], whitelist=[]))
    assert "fallback_enabled=false" in out[0]


# ---------------------------------------------------------------------------
# ⑦ 装配工厂与 LocalBrainManager 基础语义
# ---------------------------------------------------------------------------


def test_build_router_disabled_returns_none():
    """fallback_enabled=false（默认）→ None：云端用户不装本地也安全，零行为变化。"""
    cfg = SimpleNamespace(fallback_enabled=False)
    assert build_brain_router(_FakeClient(), cfg) is None


def test_build_router_wires_local_client_and_manager():
    """fallback_enabled=true → Router 包住 primary：本地客户端/manager/预算/提示接线。"""
    primary = _FakeClient("chat")
    cfg = SimpleNamespace(
        fallback_enabled=True,
        fallback_api_base="http://127.0.0.1:8002/v1",
        fallback_model="qwen2.5-7b-instruct",
        fallback_api_key="local",
        fallback_timeout=120,
        fallback_max_context_tokens=4096,
        fallback_llama_exe="",
        fallback_gguf="",
        fallback_llama_args="",
        fallback_notice_enabled=True,
        fallback_notice_text="我脑袋有点晕",
    )
    notices: "list[str]" = []
    router = build_brain_router(primary, cfg, notice_sink=notices.append)
    assert router is not None
    assert primary.max_retries == 1
    assert router._local is not None
    assert router._local.provider_id == "chat_local"
    assert router._local.api_base == "http://127.0.0.1:8002/v1"
    assert router._local.model == "qwen2.5-7b-instruct"
    assert router._local.timeout == 120.0
    assert router._manager.port == 8002
    assert router.status()["max_context_tokens"] == 4096
    # shared 形态（后台路由）：复用同一 state/manager/local，且不重复发提示
    bg = build_brain_router(_FakeClient("chat_bg"), cfg, shared=router,
                            provider_id="chat_bg")
    assert bg._state is router._state
    assert bg._manager is router._manager
    assert bg._local is router._local


def test_local_manager_adopts_external_instance_without_spawn(tmp_path):
    """adopt-first：端口已有 llama-server（probe=True）→ 收编 external、绝不 spawn。"""
    spawned: "list[list]" = []
    mgr = LocalBrainManager(
        host="127.0.0.1", port=8002,
        spawn_exe="whatever.exe", spawn_args=["-m", "m.gguf"],
        pid_path=tmp_path / "pid", log_path=tmp_path / "log",
        probe=lambda: True,
        spawner=lambda argv, cwd: (spawned.append(argv), 1)[1],
        clock=_FakeClock(), sleeper=lambda _s: None,
    )
    assert mgr.ensure_running() is True
    assert mgr.ownership == "external"
    assert spawned == []          # 有应答即收编，一个进程都没拉


def test_local_manager_spawns_and_waits_ready(tmp_path):
    """无应答 → spawn（argv 带 -m gguf/--port/--alias）→ 预算内 probe 转 True → owned。"""
    calls: "dict" = {}

    def fake_probe() -> bool:
        return bool(calls.get("ready"))

    def fake_spawner(argv, cwd):
        calls["argv"] = argv
        calls["cwd"] = cwd
        calls["ready"] = True     # 拉起即就绪（模型已在加载的乐观路径）
        return 4321

    pid_path = tmp_path / "brain-local.pid"
    mgr = LocalBrainManager(
        host="127.0.0.1", port=8002,
        spawn_exe="llama-server.exe",
        spawn_args=["-m", "m.gguf", "--host", "127.0.0.1", "--port", "8002",
                    "--alias", "qwen2.5-7b-instruct"],
        spawn_cwd=str(tmp_path),
        pid_path=pid_path, log_path=tmp_path / "logs" / "llama.log",
        probe=fake_probe, spawner=fake_spawner,
        clock=_FakeClock(), sleeper=lambda _s: None,
    )
    assert mgr.ensure_running() is True
    assert mgr.ownership == "owned"
    assert mgr.pid == 4321
    assert pid_path.read_text(encoding="ascii") == "4321"
    argv = calls["argv"]
    assert argv[0] == "-m" and "m.gguf" in argv
    assert "--port" in argv and "8002" in argv
    assert "--alias" in argv and "qwen2.5-7b-instruct" in argv


def test_local_manager_budget_exhausted_reports_failure(tmp_path):
    """就绪预算耗尽：返回 False + last_error，绝不抛异常打断 chat 主链。"""
    clock = _FakeClock()
    mgr = LocalBrainManager(
        host="127.0.0.1", port=8002,
        spawn_exe="llama-server.exe", spawn_args=["-m", "m.gguf"],
        pid_path=tmp_path / "pid", log_path=tmp_path / "log",
        probe=lambda: False, spawner=lambda argv, cwd: 1,
        ready_budget=2.0, probe_interval=1.0,
        clock=clock, sleeper=lambda _s: clock.advance(1.0),
    )
    assert mgr.ensure_running() is False
    assert mgr.state == "failed"
    assert "未就绪" in mgr.last_error


def test_local_manager_allow_spawn_false_never_spawns(tmp_path):
    """R5 接线回归：fallback_auto_spawn=false → 无应答只告警不拉起（配了 exe 也不）。

    修前该键零消费——ensure_running 只看 spawn_exe，llm.py 装配日志打「spawn=关」
    时配了 exe 照样拉起（日志撒谎）。"""
    spawned: "list[list]" = []
    mgr = LocalBrainManager(
        host="127.0.0.1", port=8002,
        spawn_exe="llama-server.exe", spawn_args=["-m", "m.gguf"],
        pid_path=tmp_path / "pid", log_path=tmp_path / "log",
        probe=lambda: False,
        spawner=lambda argv, cwd: (spawned.append(argv), 1)[1],
        allow_spawn=False,
        clock=_FakeClock(), sleeper=lambda _s: None,
    )
    assert mgr.ensure_running() is False
    assert spawned == []                        # 绝不拉起
    assert "fallback_auto_spawn=false" in mgr.last_error
    assert mgr.state == "not_started" and mgr.ownership is None


def test_local_manager_from_chat_cfg_reads_auto_spawn():
    """from_chat_cfg 工厂把 [chat].fallback_auto_spawn 带进 allow_spawn。"""
    from types import SimpleNamespace

    cfg_on = SimpleNamespace(
        fallback_api_base="http://127.0.0.1:8002/v1", fallback_model="m",
        fallback_llama_exe="llama-server.exe", fallback_gguf="m.gguf",
        fallback_llama_args="", fallback_auto_spawn=True)
    cfg_off = SimpleNamespace(
        fallback_api_base="http://127.0.0.1:8002/v1", fallback_model="m",
        fallback_llama_exe="llama-server.exe", fallback_gguf="m.gguf",
        fallback_llama_args="", fallback_auto_spawn=False)
    assert LocalBrainManager.from_chat_cfg(cfg_on).allow_spawn is True
    assert LocalBrainManager.from_chat_cfg(cfg_off).allow_spawn is False
    # 缺字段（旧桩配置）回退 True：与 dataclass 默认一致
    cfg_legacy = SimpleNamespace(fallback_api_base="", fallback_model="")
    assert LocalBrainManager.from_chat_cfg(cfg_legacy).allow_spawn is True


# ---------------------------------------------------------------------------
# ⑧ chat_async 包装与观测面
# ---------------------------------------------------------------------------


@pytest.mark.flaky(reruns=3, reruns_delay=1)
def test_chat_async_wrapper_and_recent_failure_surface():
    """chat_async 同语义；recent_failure 在黏性期带「已切本地保命」前缀。"""
    router, primary, local, manager, notices, clock = _make_router()
    primary.script = [_err(500, True)]
    reply = asyncio.run(router.chat_async([{"role": "user", "content": "hi"}]))
    assert reply.provider_id == "chat_local"
    # 云端错误进入观测面（console brain 行黄灯消费）
    router._state.record_primary_error(_err(403, False, _OK_403))
    router._state.enter_sticky()
    surface = router.recent_failure(window_seconds=600.0)
    assert "403" in surface and "已切本地" in surface
    assert router.recent_failure(window_seconds=0.0) == ""   # 窗口外如实为空
