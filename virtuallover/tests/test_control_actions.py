# -*- coding: utf-8 -*-
"""控制动作扩展测试（五轮修缮 R1）：brain 切换动作 + /api/memory/list。

覆盖面（任务契约逐项）：
1. brain_local / brain_cloud / brain_stop：固定 argv 子进程
   ``<sys.executable> scripts/brain_switch.py local|cloud|stop``——argv 逐元素
   钉死（用户输入绝不进 argv 之外的任何拼接）、cwd=运行根、timeout=900s
   （首次 GGUF 转换 15 分钟预算）、输出截尾、失败/超时明确降级、
   响应注明 brain_switch 自带 vl 重启语义；
2. /api/memory/list?q=：q 必填非空（空白/缺失/超长 400）、记忆库未装配 503、
   库封锁 503、正常检索返回 [{time, content}] 且 ≤20 条、<MNEMO_META> 剥离；
3. HTTP 全链路：/api/memory/list 登录门（401）与方法白名单（POST 404）。

安全规约：session_id 用 conftest 占位值，不落任何真实账号/密钥。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from conftest import FAKE_SESSION, TEST_DIM, deterministic_vector, make_fake_embedder
from virtuallover.console import app as console_app
from virtuallover.console import auth as console_auth
from virtuallover.console import control as console_control
from virtuallover.console import core as console_core
from virtuallover.memory.store import MemoryStore

BRAIN_SCRIPT = str(Path("scripts") / "brain_switch.py")


# ---------------------------------------------------------------------------
# 1) brain 切换动作：argv 固定性
# ---------------------------------------------------------------------------

class _RecordingRunner:
    """subprocess.run 替身：记录 (argv, kwargs)，返回可配置结果。"""

    def __init__(self, result: "types.SimpleNamespace | None" = None,
                 raise_exc: "Exception | None" = None):
        self.calls: "list[tuple[list[str], dict]]" = []
        self.result = result or types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        self.raise_exc = raise_exc

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def _control(tmp_path: Path, runner: _RecordingRunner) -> console_control.ControlService:
    return console_control.ControlService(
        tmp_path / "local", tmp_path / "backup", tmp_path, runner=runner)


@pytest.mark.parametrize("action,mode", [
    ("brain_local", "local"),
    ("brain_cloud", "cloud"),
    ("brain_stop", "stop"),
])
def test_brain_action_argv_is_fixed(tmp_path, action, mode):
    """brain 动作：argv 逐元素钉死 = [sys.executable, scripts/brain_switch.py, mode]，
    cwd=运行根，timeout=900s（首次 GGUF 转换 15 分钟）。"""
    runner = _RecordingRunner()
    service = _control(tmp_path, runner)

    status, payload = service.dispatch({"action": action})
    assert status == 200 and payload["ok"] is True
    assert len(runner.calls) == 1
    argv, kwargs = runner.calls[0]
    assert argv == [sys.executable, BRAIN_SCRIPT, mode]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["timeout"] == console_control.BRAIN_SWITCH_TIMEOUT_SECONDS == 900
    assert kwargs["capture_output"] is True and kwargs["text"] is True


def test_brain_action_note_declares_restart_semantics(tmp_path):
    """响应注明 brain_switch 自带 vl 重启语义（连接可能中断）。"""
    runner = _RecordingRunner()
    service = _control(tmp_path, runner)
    _, payload = service.dispatch({"action": "brain_cloud"})
    assert "note" in payload
    assert "重启" in payload["note"]


def test_brain_action_failure_reports_output(tmp_path):
    """子进程非零退出：ok=False 且 stdout/stderr 截尾返回（不吞输出）。"""
    runner = _RecordingRunner(result=types.SimpleNamespace(
        returncode=1, stdout="转换失败", stderr="traceback-line"))
    service = _control(tmp_path, runner)
    _, payload = service.dispatch({"action": "brain_local"})
    assert payload["ok"] is False
    assert "转换失败" in payload["output"]
    assert "traceback-line" in payload["output"]


def test_brain_action_timeout_degrades(tmp_path):
    """超 900s：ok=False + 明确超时文案（绝不挂死控制台线程）。"""
    runner = _RecordingRunner(raise_exc=subprocess.TimeoutExpired(cmd="x", timeout=900))
    service = _control(tmp_path, runner)
    _, payload = service.dispatch({"action": "brain_local"})
    assert payload["ok"] is False
    assert "超时" in payload["output"]


def test_run_brain_switch_rejects_illegal_mode(tmp_path):
    """白名单外 mode 直接 400（不产生任何子进程调用）。"""
    runner = _RecordingRunner()
    with pytest.raises(console_core.ApiError) as exc:
        console_control.run_brain_switch("cloud; rm -rf /", cwd=tmp_path, runner=runner)
    assert exc.value.status == 400
    assert runner.calls == []  # 非法 mode 不执行任何子进程


def test_dispatch_rejects_unknown_action_with_brain_in_message(tmp_path):
    service = console_control.ControlService(".", ".", ".")
    with pytest.raises(console_core.ApiError) as exc:
        service.dispatch({"action": "brain_panic"})
    assert exc.value.status == 400
    assert "brain_local" in exc.value.message
    assert "brain_cloud" in exc.value.message
    assert "brain_stop" in exc.value.message


# ---------------------------------------------------------------------------
# 2) /api/memory/list：校验与检索
# ---------------------------------------------------------------------------

def _memory_runtime(store: "object | None", session_id: str = FAKE_SESSION):
    return types.SimpleNamespace(
        memory_store=store, cfg=types.SimpleNamespace(session_id=session_id))


def _make_store(tmp_path: Path, name: str = "mem_list.db", dim: int = TEST_DIM,
                vectors: "dict[str, list[float]] | None" = None) -> MemoryStore:
    return MemoryStore(
        tmp_path / name,
        embedder=make_fake_embedder(vectors, dim),
        provider_id="占位供应商", model="test-embedding-model",
        endpoint="http://127.0.0.1:9", dim=dim,
    )


def test_memory_list_blank_q_400(tmp_path):
    for bad_q in ("", "   ", "\u3000"):
        with pytest.raises(console_core.ApiError) as exc:
            console_control.memory_search_payload(_memory_runtime(None), bad_q)
        assert exc.value.status == 400
        assert "q" in exc.value.message


def test_memory_list_overlong_q_400(tmp_path):
    with pytest.raises(console_core.ApiError) as exc:
        console_control.memory_search_payload(
            _memory_runtime(None), "记" * (console_control.MEMORY_QUERY_MAX_CHARS + 1))
    assert exc.value.status == 400


def test_memory_list_503_when_store_missing(tmp_path):
    with pytest.raises(console_core.ApiError) as exc:
        console_control.memory_search_payload(_memory_runtime(None), "查询词")
    assert exc.value.status == 503
    # runtime=None（独立运行控制台）同样 503 降级，绝不 500
    with pytest.raises(console_core.ApiError) as exc:
        console_control.memory_search_payload(None, "查询词")
    assert exc.value.status == 503


def test_memory_list_503_when_session_id_missing(tmp_path):
    store = _make_store(tmp_path)
    runtime = types.SimpleNamespace(memory_store=store,
                                    cfg=types.SimpleNamespace(session_id=""))
    with pytest.raises(console_core.ApiError) as exc:
        console_control.memory_search_payload(runtime, "查询词")
    assert exc.value.status == 503


def test_memory_list_search_returns_time_content(tmp_path):
    """正常检索：[{time, content}]，<MNEMO_META> 剥离，时间取 create_time。"""
    store = _make_store(tmp_path)
    asyncio.run(store.remember(
        "她最喜欢的食物是草莓蛋糕<MNEMO_META>source:summary</MNEMO_META>",
        FAKE_SESSION, create_time=1700000100))
    asyncio.run(store.remember(
        "她养了一只叫团子的猫", FAKE_SESSION, create_time=1700000200))

    payload = console_control.memory_search_payload(
        _memory_runtime(store), "草莓蛋糕")
    assert payload["ok"] is True
    assert payload["count"] == len(payload["items"])
    contents = [item["content"] for item in payload["items"]]
    assert any("草莓蛋糕" in c for c in contents)
    assert all("MNEMO_META" not in c for c in contents)  # 元数据绝不外泄
    hit = next(item for item in payload["items"] if "草莓蛋糕" in item["content"])
    assert hit["time"] == 1700000100
    assert set(hit) == {"time", "content"}


def test_memory_list_caps_at_20_items(tmp_path):
    """条数上限 ≤20：库里有 25 条同向量记忆，查询最多返回 20 条。"""
    shared = deterministic_vector("共享记忆向量", TEST_DIM)
    store = _make_store(tmp_path, vectors={"查询词": shared.tolist()})
    for i in range(25):
        asyncio.run(store.remember(
            f"共享记忆第 {i} 条", FAKE_SESSION, embedding=shared, create_time=i))
    payload = console_control.memory_search_payload(
        _memory_runtime(store), "查询词")
    assert payload["ok"] is True
    assert payload["count"] == 20
    assert len(payload["items"]) == 20
    # limit=50 请求同样被 20 封顶
    payload = console_control.memory_search_payload(
        _memory_runtime(store), "查询词", limit=50)
    assert payload["count"] == 20


def test_memory_list_503_when_store_blocked(tmp_path):
    """库封锁（元数据不匹配，如换模型后未重建）：503 明确降级。"""
    db = tmp_path / "blocked.db"
    MemoryStore(db, embedder=make_fake_embedder(dim=TEST_DIM),
                provider_id="占位供应商", model="m", endpoint="http://x", dim=TEST_DIM)
    blocked = MemoryStore(db, embedder=make_fake_embedder(dim=4),
                          provider_id="占位供应商", model="m", endpoint="http://x", dim=4)
    assert blocked.blocked_reason is not None
    runtime = types.SimpleNamespace(
        memory_store=blocked, cfg=types.SimpleNamespace(session_id=FAKE_SESSION))
    with pytest.raises(console_core.ApiError) as exc:
        console_control.memory_search_payload(runtime, "查询词")
    assert exc.value.status == 503


# ---------------------------------------------------------------------------
# 2.5) /api/status brain 行：本地/云端判定与探活（五轮修缮 R1）
# ---------------------------------------------------------------------------

def test_brain_row_local_when_api_base_hits_loopback():
    """api_base 命中 127.0.0.1 → 本地大脑，探 <base>/health，走严格探针。

    R7（FUSION_SPEC §3.3）：--api-key 武装后 /v1/models 未带 Bearer 恒 401
    （真机红灯复现：合成/对话成功而状态卡标红），探活端点改 /health 免鉴权
    （brain_switch._server_ready 同款裁定），探活与密钥解耦。"""
    seen = []

    def fetch_fn(url, timeout):
        seen.append((url, timeout))
        return True, 12

    runtime = types.SimpleNamespace(cfg=types.SimpleNamespace(
        chat=types.SimpleNamespace(api_base="http://127.0.0.1:8002/v1")))
    row = console_core.brain_service_row(runtime, fetch_fn=fetch_fn)
    assert row["key"] == "brain"
    assert "大脑·本地" in row["name"]
    assert row["ok"] is True and row["ms"] == 12
    assert seen == [("http://127.0.0.1:8002/health",
                     console_core.BRAIN_PROBE_TIMEOUT)]


def test_brain_row_local_probe_failure_is_red():
    """本地探活失败 → ok=False 且 optional=False（前端渲染红点/失败，即标红）。"""
    runtime = types.SimpleNamespace(cfg=types.SimpleNamespace(
        chat=types.SimpleNamespace(api_base="http://127.0.0.1:8002/v1")))
    row = console_core.brain_service_row(runtime, fetch_fn=lambda url, t: (False, 0))
    assert row["ok"] is False
    assert row["optional"] is False


def test_brain_row_cloud_uses_reachability_probe():
    """api_base 非本机 → 云端 ark，探 api_base 本身，走可达性探针（宽容语义）。"""
    seen = []

    def brain_fetch_fn(url, timeout):
        seen.append((url, timeout))
        return True, 88

    runtime = types.SimpleNamespace(cfg=types.SimpleNamespace(
        chat=types.SimpleNamespace(api_base="https://ark.test/api/v3")))
    row = console_core.brain_service_row(
        runtime, fetch_fn=lambda url, t: (False, 0), brain_fetch_fn=brain_fetch_fn)
    assert "大脑·云端" in row["name"]
    assert row["ok"] is True and row["ms"] == 88
    assert seen == [("https://ark.test/api/v3", console_core.BRAIN_PROBE_TIMEOUT)]


def test_brain_row_empty_api_base_reports_cloud_down():
    """api_base 未配置：按云端命名、ok=False（绝不假装在线）。"""
    runtime = types.SimpleNamespace(cfg=types.SimpleNamespace(
        chat=types.SimpleNamespace(api_base="")))
    row = console_core.brain_service_row(
        runtime, fetch_fn=lambda url, t: (True, 1))
    assert row["name"] == "大脑·云端"
    assert row["ok"] is False and row["ms"] == 0


def test_brain_row_no_cfg_degrades():
    runtime = types.SimpleNamespace()  # cfg 缺位（独立运行/未装配）
    row = console_core.brain_service_row(runtime)
    assert row["name"] == "大脑·云端"
    assert row["ok"] is False


def test_vl_status_includes_brain_row():
    """vl_status 聚合里 brain 行就位（键集与 optional 语义）。"""
    runtime = types.SimpleNamespace(
        cfg=types.SimpleNamespace(tts=types.SimpleNamespace(enabled=False, base_url=""),
                                  chat=types.SimpleNamespace(api_base="")),
        bridge=None, chat_client=None, embedding_client=None, history=None,
        memory_store=None, mood_engine=None, diary_service=None, proactive=None)
    payload = console_core.vl_status(runtime, fetch_fn=None, now=1700000000)
    by_key = {s["key"]: s for s in payload["services"]}
    assert "brain" in by_key
    assert by_key["brain"]["ok"] is False
    assert by_key["brain"]["optional"] is False


def test_local_models_probe_url_variants():
    """api_base 带/不带 /v1 都归一到 /health（R7 免鉴权探活端点）；无 netloc 返回空。"""
    assert console_core.local_models_probe_url(
        "http://127.0.0.1:8002/v1") == "http://127.0.0.1:8002/health"
    assert console_core.local_models_probe_url(
        "http://127.0.0.1:8002") == "http://127.0.0.1:8002/health"
    assert console_core.local_models_probe_url("") == ""
    assert console_core.is_local_api_base("http://LOCALHOST:8002/v1") is True
    assert console_core.is_local_api_base("https://ark.test/api/v3") is False
    assert console_core.is_local_api_base("") is False


def test_voice_row_probe_sends_basic_auth_when_armed():
    """R7 真机回归（R6-R8 验证链 d 实测复现）：引擎 Basic 武装后 /character_list
    未带认证恒 401，严格 2xx 探针把活着的引擎画成红灯（合成成功而状态卡
    语音行标红）。配了 [tts].tts_auth_password 时探针必须带 Basic 头——
    用户名固定 TTS_AUTH_USER，口令经 fetch_fn 第三参（headers）下发。
    安全规约：口令用 sk-TEST 占位，不落真实凭据。"""
    seen = {}

    def fetch_fn(url, timeout, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        return True, 7

    runtime = types.SimpleNamespace(cfg=types.SimpleNamespace(
        tts=types.SimpleNamespace(enabled=True, base_url="http://127.0.0.1:5000",
                                  tts_auth_password="sk-TEST-TTS-PW")))
    payload = console_core.vl_status(runtime, fetch_fn=fetch_fn, now=1700000000)
    voice = {s["key"]: s for s in payload["services"]}["voice"]
    assert voice["ok"] is True and voice["ms"] == 7
    assert seen["url"] == "http://127.0.0.1:5000/character_list"
    import base64 as _b64
    want = "Basic " + _b64.b64encode(
        f"{console_core.TTS_AUTH_USER}:sk-TEST-TTS-PW".encode()).decode("ascii")
    assert seen["headers"] == {"Authorization": want}


def test_voice_row_probe_without_password_keeps_bare_call():
    """未配 [tts].tts_auth_password → 探针不带认证头（引擎未武装的兜底形态）。"""
    seen = {}

    def fetch_fn(url, timeout, headers=None):
        seen["headers"] = headers
        return True, 3

    runtime = types.SimpleNamespace(cfg=types.SimpleNamespace(
        tts=types.SimpleNamespace(enabled=True, base_url="http://127.0.0.1:5000",
                                  tts_auth_password="")))
    payload = console_core.vl_status(runtime, fetch_fn=fetch_fn, now=1700000000)
    voice = {s["key"]: s for s in payload["services"]}["voice"]
    assert voice["ok"] is True
    assert seen["headers"] is None


def test_http_reachability_probe_counts_any_http_response():
    """可达性探针：404/405 应答算可达；连接拒绝算不可达（loopback 实测）。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Always404(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Always404)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ok, ms = console_core.http_reachability_probe(
            f"http://127.0.0.1:{port}/anything", timeout=3.0)
        assert ok is True and ms >= 0  # 404 也是「有应答」= 可达
    finally:
        server.shutdown()
        server.server_close()
    # 未监听端口：连接拒绝 → 不可达
    ok, ms = console_core.http_reachability_probe(
        "http://127.0.0.1:9/nothing", timeout=1.0)
    assert ok is False and ms == 0


# ---------------------------------------------------------------------------
# 3) HTTP 全链路：/api/memory/list 登录门与方法白名单
# ---------------------------------------------------------------------------

class _StubControl:
    def dispatch(self, payload):
        return 200, {"ok": True, "echo": payload}


@pytest.fixture
def memory_server(tmp_path):
    """动态端口控制台实例：memory_store 缺位（检索一律 503，门行为仍可断言）。"""
    cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(cfg.console_path)
    auth_mgr.load()
    runtime = types.SimpleNamespace(repo_root=tmp_path, memory_store=None, cfg=None)
    httpd = console_app.make_server(
        "127.0.0.1", 0, cfg, auth=auth_mgr, control=_StubControl(),
        status_provider=lambda: {"services": [], "generated_at": 1},
        runtime=runtime,
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, port, auth_mgr
    httpd.shutdown()
    httpd.server_close()


def _request(port, method, path, *, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return resp.status, payload, dict(resp.getheaders())
    finally:
        conn.close()


def test_memory_list_http_gates(memory_server):
    _httpd, port, auth_mgr = memory_server

    # 未登录 → 401（登录门内）
    status, _, _ = _request(port, "GET", "/api/memory/list?q=%E6%9F%A5%E8%AF%A2")
    assert status == 401

    # 登录后：缺 q → 400
    _, _, headers = _request(
        port, "POST", "/api/auth/login",
        body=json.dumps({"password": auth_mgr.load()["password"]}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    cookie = {"Cookie": headers["Set-Cookie"].split(";")[0]}
    status, payload, _ = _request(port, "GET", "/api/memory/list", headers=cookie)
    assert status == 400

    # 方法白名单照旧：POST /api/memory/list → 404
    status, _, _ = _request(
        port, "POST", "/api/memory/list",
        body=b"{}", headers={"Content-Type": "application/json",
                             "X-Requested-With": "XMLHttpRequest", **cookie})
    assert status == 404

    # 带 q 但 memory_store 未装配 → 503（如实降级，不假装检索过）
    status, payload, _ = _request(
        port, "GET", "/api/memory/list?q=%E6%9F%A5%E8%AF%A2", headers=cookie)
    assert status == 503
    assert "memory_store" in payload["error"]
