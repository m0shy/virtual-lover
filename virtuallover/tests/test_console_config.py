# -*- coding: utf-8 -*-
"""配置页端点测试（五轮修缮 R1）：GET/PUT /api/config/{chat,embedding,persona}。

覆盖面（任务契约逐项）：
1. 正常写回读：PUT → vl.toml 落盘（load_config 回读核对）+ GET 打码读回；
2. 非法值 400：thinking/style 三态词、dim 区间与类型、begin_dialogs 偶数校验、
   未知字段、空请求体、空密钥；
3. key 不回原文：GET 一律 {"len": N}，密钥原文不出现在任何响应字节里；
4. .bak 生成：PUT 后 vl.toml.bak-<时间戳> 存在且内容是改写前原文（金样板纪律）；
5. 401/403：三重门自动继承（未登录 401；登录但缺 X-Requested-With 的 PUT 403）。

安全规约：api_key 一律 sk-TEST 占位值，QQ 号用 10000 占位号段，不落任何真实值。
"""

from __future__ import annotations

import http.client
import json
import threading
import types
from pathlib import Path

import pytest

from virtuallover.config import VlConfig, dump_config, load_config
from virtuallover.console import app as console_app
from virtuallover.console import auth as console_auth
from virtuallover.console import config_page as config_page_module
from virtuallover.console import core as console_core

# 占位密钥（明显占位值，禁真实 key）
TEST_KEY = "sk-TEST-placeholder-key"
TEST_KEY_NEW = "sk-TEST-new-key-value"


# ---------------------------------------------------------------------------
# 夹具：最小合法 vl.toml（与 test_console_port._prepare_vl_toml 同构）+ 服务
# ---------------------------------------------------------------------------

def _make_cfg() -> VlConfig:
    cfg = VlConfig(session_id="测试人格:FriendMessage:10000", admins=["10000"],
                   whitelist=["10000"])
    cfg.chat.api_base = "https://chat.test/v1"
    cfg.chat.api_key = TEST_KEY
    cfg.chat.model = "test-model"
    cfg.embedding.api_base = "https://emb.test/v1"
    cfg.embedding.api_key = TEST_KEY
    cfg.embedding.model = "emb-model"
    cfg.persona.name = "测试人格"
    cfg.persona.system_prompt = "占位人格全文。"
    cfg.persona.begin_dialogs = ["你好呀", "嗯，你来啦。"]
    return cfg


def _write_vl_toml(tmp_path: Path, cfg: "VlConfig | None" = None) -> Path:
    path = tmp_path / "local" / "config" / "vl.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_config(cfg or _make_cfg()), encoding="utf-8")
    return path


def _service(tmp_path: Path, cfg_snapshot: "VlConfig | None" = None,
             clock_epoch: float = 1700000000.0) -> config_page_module.ConfigPageService:
    runtime = types.SimpleNamespace(
        repo_root=tmp_path,
        cfg=cfg_snapshot if cfg_snapshot is not None else _make_cfg(),
    )
    return config_page_module.ConfigPageService(
        runtime, clock=lambda: clock_epoch)


# ---------------------------------------------------------------------------
# 1) 正常写回读
# ---------------------------------------------------------------------------

def test_put_chat_then_get_roundtrip(tmp_path):
    """PUT 改 model/thinking → 文件落盘 + GET 读回新值；未提及字段保持原值。"""
    toml_path = _write_vl_toml(tmp_path)
    service = _service(tmp_path)

    payload = service.put_section("chat", {"model": "new-model", "thinking": "enabled"})
    assert payload["ok"] is True
    assert payload["need_restart"] is True
    assert payload["applied_keys"] == ["model", "thinking"]
    assert payload["backup"]  # .bak 文件名非空

    cfg = load_config(toml_path)
    assert cfg.chat.model == "new-model"
    assert cfg.chat.thinking == "enabled"
    assert cfg.chat.api_key == TEST_KEY          # 未提及 = 保持现值
    assert cfg.chat.api_base == "https://chat.test/v1"
    # 其他分节不受牵连
    assert cfg.embedding.model == "emb-model"
    assert cfg.persona.name == "测试人格"

    got = service.get_section("chat")
    assert got["ok"] is True and got["source"] == "file"
    assert got["config"]["model"] == "new-model"
    assert got["config"]["thinking"] == "enabled"


def test_put_embedding_roundtrip(tmp_path):
    """embedding：style/dim 合法值写入 + 回读；api_base 换端点同样落盘。"""
    toml_path = _write_vl_toml(tmp_path)
    service = _service(tmp_path)

    payload = service.put_section("embedding", {
        "style": "multimodal_single", "dim": 1024,
        "api_base": "https://emb-new.test/v1"})
    assert payload["ok"] is True and payload["need_restart"] is True

    cfg = load_config(toml_path)
    assert cfg.embedding.style == "multimodal_single"
    assert cfg.embedding.dim == 1024
    assert cfg.embedding.api_base == "https://emb-new.test/v1"
    assert cfg.embedding.api_key == TEST_KEY


def test_put_persona_roundtrip(tmp_path):
    """persona：name/system_prompt/begin_dialogs 写入 + 回读。"""
    toml_path = _write_vl_toml(tmp_path)
    service = _service(tmp_path)

    payload = service.put_section("persona", {
        "name": "新名字",
        "system_prompt": "新的人格全文。",
        "begin_dialogs": ["在吗？", "在的，怎么啦？", "没事", "那就好呀。"]})
    assert payload["ok"] is True

    cfg = load_config(toml_path)
    assert cfg.persona.name == "新名字"
    assert cfg.persona.system_prompt == "新的人格全文。"
    assert cfg.persona.begin_dialogs == [
        "在吗？", "在的，怎么啦？", "没事", "那就好呀。"]


def test_put_creates_bak_with_preimage(tmp_path):
    """.bak 是改写前原文（金样板纪律：备份先于覆盖）。"""
    import re
    import time

    toml_path = _write_vl_toml(tmp_path)
    preimage = toml_path.read_text(encoding="utf-8")
    service = _service(tmp_path)  # clock 固定 epoch 1700000000 → 时间戳确定性

    payload = service.put_section("chat", {"model": "another-model"})
    backup = toml_path.with_name(payload["backup"])
    assert backup.is_file()
    # 名字形态 vl.toml.bak-<YYYYMMDD-HHMMSS>（本地时区，与 clock 注入值一致）
    expected_stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(1700000000.0))
    assert re.fullmatch(r"vl\.toml\.bak-\d{8}-\d{6}", backup.name)
    assert backup.name == f"vl.toml.bak-{expected_stamp}"
    assert backup.read_text(encoding="utf-8") == preimage
    assert toml_path.read_text(encoding="utf-8") != preimage


# ---------------------------------------------------------------------------
# 2) 非法值 400
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section,bad_payload,fragment", [
    ("chat", {"thinking": "always"}, "thinking"),
    ("chat", {"bg_thinking": "yes"}, "bg_thinking"),
    ("chat", {"api_base": "ftp://x.test"}, "api_base"),
    ("chat", {"api_base": "not-a-url"}, "api_base"),
    ("chat", {"api_key": ""}, "api_key"),
    ("chat", {"model": ""}, "model"),
    ("embedding", {"style": "gemini"}, "style"),
    ("embedding", {"dim": 0}, "dim"),
    ("embedding", {"dim": -1}, "dim"),
    ("embedding", {"dim": "2048"}, "dim"),      # 字符串数字也拒绝（类型校验）
    ("embedding", {"dim": True}, "dim"),        # bool 不是 int
    ("persona", {"name": ""}, "name"),
    ("persona", {"system_prompt": " "}, "system_prompt"),
    ("persona", {"begin_dialogs": ["单数"]}, "偶数"),
    ("persona", {"begin_dialogs": "你好"}, "列表"),
    ("persona", {"begin_dialogs": ["a", None]}, "非空字符串"),
])
def test_put_rejects_invalid_values(tmp_path, section, bad_payload, fragment):
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    with pytest.raises(console_core.ApiError) as exc:
        service.put_section(section, bad_payload)
    assert exc.value.status == 400
    assert fragment in exc.value.message
    # 拒绝写 = 文件不动（金样板「任何一步失败都在写盘前中止」）
    assert not list((tmp_path / "local" / "config").glob("vl.toml.bak-*"))


def test_put_rejects_unknown_field_and_empty_body(tmp_path):
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    with pytest.raises(console_core.ApiError) as exc:
        service.put_section("chat", {"max_context_tokens": 32768})  # 死键也不收
    assert exc.value.status == 400 and "未知字段" in exc.value.message
    with pytest.raises(console_core.ApiError) as exc:
        service.put_section("persona", {})
    assert exc.value.status == 400 and "不含任何可写字段" in exc.value.message


def test_put_non_dict_payload_400(tmp_path):
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    for bad in ("model=x", ["model"], None):
        with pytest.raises(console_core.ApiError) as exc:
            service.put_section("chat", bad)
        assert exc.value.status == 400


# ---------------------------------------------------------------------------
# 3) key 不回原文（GET 只回 {"len": N}）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section", ["chat", "embedding"])
def test_get_masks_api_key(tmp_path, section):
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    payload = service.get_section(section)
    assert payload["ok"] is True
    assert payload["config"]["api_key"] == {"len": len(TEST_KEY)}
    # 密钥原文绝不出现在响应序列化字节里
    assert TEST_KEY not in json.dumps(payload, ensure_ascii=False)
    assert TEST_KEY_NEW not in json.dumps(payload, ensure_ascii=False)


def test_get_masks_api_key_after_put(tmp_path):
    """PUT 换 key 后 GET 仍只回新长度，绝不回显。"""
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    service.put_section("chat", {"api_key": TEST_KEY_NEW})
    payload = service.get_section("chat")
    assert payload["config"]["api_key"] == {"len": len(TEST_KEY_NEW)}
    assert TEST_KEY_NEW not in json.dumps(payload, ensure_ascii=False)


def test_get_field_surface_per_section(tmp_path):
    """三节字段面与任务契约一致（chat 含 thinking；embedding 含 style/dim）。"""
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    assert set(service.get_section("chat")["config"]) == {
        "api_base", "model", "api_key", "thinking", "bg_thinking",
        "fallback_llama_exe", "fallback_gguf", "fallback_api_base",
        "fallback_enabled", "fallback_model"}
    assert set(service.get_section("embedding")["config"]) == {
        "api_base", "model", "api_key", "style", "dim"}
    persona = service.get_section("persona")["config"]
    assert set(persona) == {"name", "system_prompt", "begin_dialogs"}
    assert persona["begin_dialogs"] == ["你好呀", "嗯，你来啦。"]


# ---------------------------------------------------------------------------
# 降级与错误路径（GET 内存快照 / 503 / 404 / 500）
# ---------------------------------------------------------------------------

def test_get_falls_back_to_memory_snapshot_when_file_missing(tmp_path):
    """vl.toml 缺失：GET 降级内存快照并标注 source=memory，不假装读了文件。"""
    service = _service(tmp_path)  # 未写 vl.toml，runtime.cfg 是启动快照
    payload = service.get_section("chat")
    assert payload["source"] == "memory"
    assert payload["file_exists"] is False
    assert payload["config"]["model"] == "test-model"


def test_get_503_when_no_file_and_no_snapshot(tmp_path):
    runtime = types.SimpleNamespace(repo_root=tmp_path, cfg=None)
    service = config_page_module.ConfigPageService(runtime, clock=lambda: 0.0)
    with pytest.raises(console_core.ApiError) as exc:
        service.get_section("chat")
    assert exc.value.status == 503


def test_put_404_when_vl_toml_missing(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(console_core.ApiError) as exc:
        service.put_section("chat", {"model": "x"})
    assert exc.value.status == 404 and "vl.toml" in exc.value.message


def test_put_500_when_vl_toml_corrupt(tmp_path):
    """vl.toml 损坏：load_config 校验失败 → 500 中止，绝不覆盖坏文件。"""
    path = _write_vl_toml(tmp_path)
    path.write_text("这不是 TOML [[[", encoding="utf-8")
    service = _service(tmp_path)
    with pytest.raises(console_core.ApiError) as exc:
        service.put_section("chat", {"model": "x"})
    assert exc.value.status == 500


def test_unknown_section_404(tmp_path):
    _write_vl_toml(tmp_path)
    service = _service(tmp_path)
    with pytest.raises(console_core.ApiError) as exc:
        service.get_section("bridge")  # 敏感分节不在白名单
    assert exc.value.status == 404
    with pytest.raises(console_core.ApiError) as exc:
        service.put_section("paths", {"character": "Hutao"})
    assert exc.value.status == 404


# ---------------------------------------------------------------------------
# 5) HTTP 全链路：三重门自动继承（401 / 403 / 200）
# ---------------------------------------------------------------------------

class _StubControl:
    def dispatch(self, payload):
        return 200, {"ok": True, "echo": payload}


@pytest.fixture
def config_server(tmp_path):
    """动态端口控制台实例：config_service 指向 tmp 运行根（真实 vl.toml 零接触）。"""
    _write_vl_toml(tmp_path)
    cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(cfg.console_path)
    auth_mgr.load()
    runtime = types.SimpleNamespace(repo_root=tmp_path, memory_store=None, cfg=None)
    httpd = console_app.make_server(
        "127.0.0.1", 0, cfg, auth=auth_mgr,
        control=_StubControl(),
        status_provider=lambda: {"services": [], "generated_at": 1},
        config_service=config_page_module.ConfigPageService(runtime),
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


def test_config_endpoints_http_gates_and_roundtrip(config_server):
    _httpd, port, auth_mgr = config_server
    password = auth_mgr.load()["password"]

    # 门 1：未登录 GET → 401；未登录 PUT（带 CSRF 头、无会话）→ 401
    # （不带 CSRF 头的未登录 PUT 会先撞 CSRF 门得 403——app._handle 的门序：
    #   写方法先 CSRF 后认证，既有框架行为，这里两种形态都钉住）
    status, _, _ = _request(port, "GET", "/api/config/chat")
    assert status == 401
    status, _, _ = _request(
        port, "PUT", "/api/config/chat",
        body=json.dumps({"model": "x"}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    assert status == 401
    status, _, _ = _request(port, "PUT", "/api/config/chat",
                            body=json.dumps({"model": "x"}).encode("utf-8"),
                            headers={"Content-Type": "application/json"})
    assert status == 403

    # 登录拿会话
    status, _, headers = _request(
        port, "POST", "/api/auth/login",
        body=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    assert status == 200
    cookie = {"Cookie": headers["Set-Cookie"].split(";")[0]}

    # 门 2：登录但缺 X-Requested-With 的 PUT → 403（CSRF）
    status, _, _ = _request(
        port, "PUT", "/api/config/persona",
        body=json.dumps({"name": "新名"}).encode("utf-8"),
        headers={"Content-Type": "application/json", **cookie})
    assert status == 403

    # 门 3：DELETE 方法不在路由内 → 404（方法白名单照旧）
    status, _, _ = _request(
        port, "DELETE", "/api/config/chat",
        headers={"X-Requested-With": "XMLHttpRequest", **cookie})
    assert status == 404

    # 三关齐过：GET 打码 → PUT → GET 读回
    status, payload, _ = _request(port, "GET", "/api/config/chat", headers=cookie)
    assert status == 200
    assert payload["config"]["api_key"] == {"len": len(TEST_KEY)}
    assert TEST_KEY not in json.dumps(payload, ensure_ascii=False)

    status, payload, _ = _request(
        port, "PUT", "/api/config/chat",
        body=json.dumps({"model": "http-model", "thinking": "auto"}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest", **cookie})
    assert status == 200
    assert payload["ok"] is True and payload["need_restart"] is True
    assert "重启" in payload["note"]

    status, payload, _ = _request(port, "GET", "/api/config/chat", headers=cookie)
    assert status == 200
    assert payload["config"]["model"] == "http-model"
    assert payload["config"]["thinking"] == "auto"

    # 未知节：404（含 410 已删的 ab 语义不回归）
    status, _, _ = _request(port, "GET", "/api/config/bridge", headers=cookie)
    assert status == 404


def test_config_endpoints_registered_on_config_sections(config_server):
    """embedding/persona 两节的 PUT 同样走通（防只接了 chat 一节）。"""
    _httpd, port, auth_mgr = config_server
    password = auth_mgr.load()["password"]
    _, _, headers = _request(
        port, "POST", "/api/auth/login",
        body=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    cookie = {"Cookie": headers["Set-Cookie"].split(";")[0]}
    for section, body in [
        ("embedding", {"dim": 512}),
        ("persona", {"begin_dialogs": ["早", "早呀。"]}),
    ]:
        status, payload, _ = _request(
            port, "PUT", f"/api/config/{section}",
            body=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Requested-With": "XMLHttpRequest", **cookie})
        assert status == 200 and payload["ok"] is True, section
