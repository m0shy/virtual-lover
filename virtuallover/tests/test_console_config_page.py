# -*- coding: utf-8 -*-
"""五轮修缮 R1 前端消费契约的钉子测试（test_console_config_page）。

覆盖前端重构工程师按契约消费的三条链路（实现属后端组 console/config_page.py
与 console/control.py，集成期以此处钉住的签名为准）：
1. GET/PUT /api/config/{chat,embedding,persona}：密钥打码 {"len": N}、留空=
   保持现值、金样板 .bak 备份、begin_dialogs 偶数校验、未知字段拒绝；
2. GET /api/memory/list?q=：q 必填非空、返回 [{time, content}]、
   memory_store 缺失 503 如实降级；
3. POST /api/control/action 的 brain_local|brain_cloud：固定 argv 子进程
   调 scripts/brain_switch.py，响应带重启提示 note。

安全规约：QQ 号用 10000 占位号段，密钥全部测试内生成的占位值，不落真实值。
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from virtuallover.config import (
    BridgeConfig,
    VlConfig,
    dump_config,
    load_config,
)
from virtuallover.console import app as console_app
from virtuallover.console import auth as console_auth
from virtuallover.console import config_page as console_config_page
from virtuallover.console import control as console_control
from virtuallover.console import core as console_core

REPO_ROOT = Path(__file__).resolve().parents[2]

PLACEHOLDER_UIN = "10000"
PLACEHOLDER_KEY = "sk-PLACEHOLDER-KEY-0123456789abcdef"


# ---------------------------------------------------------------------------
# 夹具：最小合法 vl.toml（与 test_console_port._prepare_vl_toml 同款口径）
# ---------------------------------------------------------------------------

def _write_vl_toml(tmp_path: Path) -> Path:
    cfg = VlConfig(session_id="测试人格:FriendMessage:10000", admins=["10000"],
                   whitelist=["10000"])
    cfg.bridge = BridgeConfig(host="127.0.0.1", port=6199, token="a" * 43,
                              self_id=PLACEHOLDER_UIN)
    cfg.chat.api_base = "http://127.0.0.1:8002/v1"
    cfg.chat.model = "qwen2.5-7b-instruct"
    cfg.chat.api_key = PLACEHOLDER_KEY
    cfg.embedding.api_base = "https://emb.test/v1"
    cfg.embedding.api_key = PLACEHOLDER_KEY
    cfg.embedding.model = "emb-model"
    cfg.embedding.dim = 2048
    cfg.persona.name = "测试人格"
    cfg.persona.system_prompt = "占位人格。"
    cfg.persona.begin_dialogs = ["在吗？", "在的呀。"]
    config_dir = tmp_path / "local" / "config"
    config_dir.mkdir(parents=True)
    path = config_dir / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    return path


def _service(tmp_path: Path) -> console_config_page.ConfigPageService:
    """runtime 只带 repo_root（GET 走文件层，无需内存快照）。"""
    return console_config_page.ConfigPageService(
        types.SimpleNamespace(repo_root=tmp_path))


# ---------------------------------------------------------------------------
# 1) 配置页 GET/PUT（密钥打码 / 留空保持 / 备份 / 校验）
# ---------------------------------------------------------------------------

def test_config_get_masks_api_key(tmp_path):
    """GET 三节：密钥只回 {"len": N}，响应不含密钥原文。"""
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    for section in ("chat", "embedding"):
        payload = service.get_section(section)
        assert payload["ok"] is True and payload["section"] == section
        assert payload["source"] == "file"
        assert payload["config"]["api_key"] == {"len": len(PLACEHOLDER_KEY)}
        assert payload["config"]["api_base"]
        assert PLACEHOLDER_KEY not in json.dumps(payload, ensure_ascii=False)
    persona = service.get_section("persona")
    assert persona["config"]["name"] == "测试人格"
    assert persona["config"]["begin_dialogs"] == ["在吗？", "在的呀。"]


def test_config_put_chat_keeps_key_and_backups(tmp_path):
    """PUT chat：api_key 省略 = 保持现值；写盘产生 .bak-<时间戳> 备份。"""
    path = _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    payload = service.put_section("chat", {
        "api_base": "https://ark.test/api/v3",
        "model": "doubao-lite",
    })
    assert payload["ok"] is True
    assert payload["need_restart"] is True
    assert payload["applied_keys"] == ["api_base", "model"]
    cfg = load_config(path)
    assert cfg.chat.api_base == "https://ark.test/api/v3"
    assert cfg.chat.model == "doubao-lite"
    assert cfg.chat.api_key == PLACEHOLDER_KEY          # 省略 → 保持
    assert cfg.chat.thinking == "disabled"              # 未触碰字段不动
    backups = list(path.parent.glob("vl.toml.bak-*"))
    assert len(backups) == 1


def test_config_put_chat_updates_key_when_given(tmp_path):
    path = _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    new_key = "sk-BRAND-NEW-KEY-fedcba9876543210"
    payload = service.put_section("chat", {"api_key": new_key})
    assert payload["applied_keys"] == ["api_key"]
    assert load_config(path).chat.api_key == new_key


def test_config_put_rejects_unknown_field_and_empty(tmp_path):
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    with pytest.raises(console_core.ApiError) as exc_unknown:
        service.put_section("chat", {"nope": "x"})
    assert exc_unknown.value.status == 400
    with pytest.raises(console_core.ApiError) as exc_empty:
        service.put_section("chat", {})
    assert exc_empty.value.status == 400
    with pytest.raises(console_core.ApiError) as exc_blank:
        service.put_section("chat", {"api_base": ""})
    assert exc_blank.value.status == 400


def test_config_put_persona_begin_dialogs_must_be_even(tmp_path):
    """契约：开场白必须偶数条（一问一答成对）；奇数 400，合法偶数落盘。"""
    path = _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    with pytest.raises(console_core.ApiError) as exc_odd:
        service.put_section("persona", {"begin_dialogs": ["只有一句"]})
    assert exc_odd.value.status == 400
    payload = service.put_section("persona", {
        "name": "测试人格",
        "system_prompt": "新人格全文。",
        "begin_dialogs": ["早上好呀", "早安主人"],
    })
    assert payload["ok"] is True
    cfg = load_config(path)
    assert cfg.persona.system_prompt == "新人格全文。"
    assert cfg.persona.begin_dialogs == ["早上好呀", "早安主人"]


def test_config_put_embedding_dim_bounds(tmp_path):
    path = _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    payload = service.put_section("embedding", {"dim": 1024, "style": "openai"})
    assert load_config(path).embedding.dim == 1024
    with pytest.raises(console_core.ApiError) as exc_dim:
        service.put_section("embedding", {"dim": 0})
    assert exc_dim.value.status == 400


# ---------------------------------------------------------------------------
# 2) /api/memory/list?q=（control.memory_search_payload）
# ---------------------------------------------------------------------------

class _StubHit:
    def __init__(self, content: str, create_time: int):
        self.content = content
        self.create_time = create_time
        self.metadata = "{}"


class _StubStore:
    def __init__(self):
        self.calls: "list[tuple[str, str, int]]" = []

    async def search(self, session_id: str, query: str, top_k: int = 8):
        self.calls.append((session_id, query, top_k))
        return [_StubHit("她最喜欢菊花茶。<MNEMO_META>src:test</MNEMO_META>", 1700000000),
                _StubHit("周二一起看了流星。", 1700000100)]


def _memory_runtime(store):
    return types.SimpleNamespace(
        memory_store=store,
        cfg=types.SimpleNamespace(session_id="测试人格:FriendMessage:10000"))


def test_memory_search_payload_strips_meta_and_shapes_items():
    store = _StubStore()
    payload = console_control.memory_search_payload(_memory_runtime(store), "菊花茶", limit=5)
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["items"][0]["time"] == 1700000000
    assert payload["items"][0]["content"] == "她最喜欢菊花茶。"
    assert "MNEMO_META" not in payload["items"][0]["content"]
    assert store.calls[0][2] == 5   # top_k 透传


def test_memory_search_requires_nonempty_query():
    with pytest.raises(console_core.ApiError) as exc:
        console_control.memory_search_payload(_memory_runtime(_StubStore()), "  ")
    assert exc.value.status == 400


def test_memory_search_store_missing_is_503():
    with pytest.raises(console_core.ApiError) as exc:
        console_control.memory_search_payload(
            types.SimpleNamespace(memory_store=None, cfg=types.SimpleNamespace(session_id="x")), "q")
    assert exc.value.status == 503


# ---------------------------------------------------------------------------
# 3) brain_local / brain_cloud（固定 argv 子进程调 brain_switch）
# ---------------------------------------------------------------------------

class _StubRunner:
    def __init__(self, returncode: int = 0):
        self.calls: "list[list[str]]" = []
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=self.returncode, stdout="ok", stderr="")


def test_brain_switch_dispatch_uses_fixed_argv():
    runner = _StubRunner()
    service = console_control.ControlService(".", ".", ".", runner=runner)
    status, payload = service.dispatch({"action": "brain_local"})
    assert status == 200 and payload["ok"] is True
    argv = runner.calls[0]
    assert argv[0] == sys.executable
    assert "brain_switch" in argv[1]
    assert argv[2] == "local"
    assert "重启" in payload["note"]   # 契约：note 明示 brain_switch 自带 vl 重启
    status, _payload = service.dispatch({"action": "brain_cloud"})
    assert status == 200
    assert runner.calls[1][2] == "cloud"


def test_brain_switch_propagates_failure():
    service = console_control.ControlService(".", ".", ".", runner=_StubRunner(returncode=3))
    status, payload = service.dispatch({"action": "brain_cloud"})
    assert status == 200 and payload["ok"] is False


def test_brain_switch_rejects_unknown_mode():
    with pytest.raises(console_core.ApiError) as exc:
        console_control.run_brain_switch("fmt_disk", cwd=".", runner=_StubRunner())
    assert exc.value.status == 400


# ---------------------------------------------------------------------------
# 4) HTTP 层三重门接线（config/memory 端点随 /api/* 继承认证 + CSRF）
# ---------------------------------------------------------------------------

@pytest.fixture
def config_server(tmp_path):
    """动态端口控制台：config_service 指向 tmp vl.toml，runtime 不带 memory_store。"""
    vl_toml = _write_vl_toml(tmp_path)
    cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(cfg.console_path)
    auth_mgr.load()
    runtime = types.SimpleNamespace(repo_root=tmp_path, cfg=None, memory_store=None)
    httpd = console_app.make_server(
        "127.0.0.1", 0, cfg, auth=auth_mgr,
        config_service=console_config_page.ConfigPageService(runtime),
        runtime=runtime,
        status_provider=lambda: {"services": [], "generated_at": 1},
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, port, auth_mgr, vl_toml
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, method: str, path: str, *, body: "bytes | None" = None,
             headers: "dict | None" = None) -> "tuple[int, dict]":
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def _login(port: int, password: str) -> dict:
    status, payload = _request(
        port, "POST", "/api/auth/login",
        body=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    assert status == 200 and payload["ok"] is True
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/api/auth/login",
                     body=json.dumps({"password": password}).encode("utf-8"),
                     headers={"Content-Type": "application/json",
                              "X-Requested-With": "XMLHttpRequest"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie", "").split(";")[0]
    finally:
        conn.close()
    return {"Cookie": cookie, "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json"}


def test_http_config_gates_and_roundtrip(config_server):
    _httpd, port, auth_mgr, vl_toml = config_server
    headers = _login(port, auth_mgr.load()["password"])

    # 门 1：未登录 → 401
    status, _ = _request(port, "GET", "/api/config/chat")
    assert status == 401

    # GET 打码：密钥原文不出现在响应体
    status, payload = _request(port, "GET", "/api/config/chat", headers=headers)
    assert status == 200 and payload["config"]["api_key"] == {"len": len(PLACEHOLDER_KEY)}
    status, raw_body = _request(port, "GET", "/api/config/embedding", headers=headers)
    assert status == 200
    assert PLACEHOLDER_KEY not in json.dumps(raw_body, ensure_ascii=False)

    # 门 2：PUT 缺 CSRF → 403
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    cookie_header = {"Cookie": headers["Cookie"], "Content-Type": "application/json"}
    try:
        conn.request("PUT", "/api/config/persona",
                     body=json.dumps({"name": "x"}).encode("utf-8"),
                     headers=cookie_header)
        assert conn.getresponse().status == 403
    finally:
        conn.close()

    # 三关齐过：PUT persona 落盘且 need_restart=true
    status, payload = _request(
        port, "PUT", "/api/config/persona", headers=headers,
        body=json.dumps({"system_prompt": "HTTP 层改写。", "begin_dialogs": ["嗯?", "嗯呀"]}).encode("utf-8"))
    assert status == 200 and payload["need_restart"] is True
    assert load_config(vl_toml).persona.system_prompt == "HTTP 层改写。"


def test_http_memory_list_gates(config_server):
    _httpd, port, auth_mgr, _vl_toml = config_server
    headers = _login(port, auth_mgr.load()["password"])
    # q 必填：缺 q → 400
    status, _payload = _request(port, "GET", "/api/memory/list", headers=headers)
    assert status == 400
    # runtime 无 memory_store（本夹具如实未装配）→ 503 降级，绝不假装检索过
    status, payload = _request(
        port, "GET", "/api/memory/list?q=" + PLACEHOLDER_UIN, headers=headers)
    assert status == 503
    assert "记忆库未装配" in payload["error"]
