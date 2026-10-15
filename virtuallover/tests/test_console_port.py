# -*- coding: utf-8 -*-
"""console 移植适配测试（SPEC §10，集成接线任务 4）。

覆盖面：
1. 删减彻底性：console 包内无 astrbot import；AuthManager 不再托管上游密码；
   EngineProxy 只剩 NapCat 代理（无任何 ab_* 方法）；/api/engine/ab/* → 410；
2. status 进程内探测：vl_status 按装配现状如实报告（缺模块=down，不假装在线）；
3. mood 渲染走 virtuallover.soul.mood（port-of-own 引擎，中文标签）；
4. napcat 指向切换/回退动作：onebot11_*.json 改写（动态发现 + 备份）+
   vl.toml token 同步 + docker compose restart heart 固定 argv（R6 容器中性化后
   服务键/容器名为 heart/vl-heart，动作键沿用 napcat_switch/rollback）；
5. 三重门不回退：会话认证 + X-Requested-With CSRF + 127.0.0.1 绑定；
6. setup(ctx)：随主框架起停（监听就绪 + cleanup 释放端口）。

安全规约：QQ 号用 10000 占位号段，token 全部测试内生成，不落任何真实值。
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import types
from pathlib import Path

import pytest

from virtuallover.config import BridgeConfig, ConsoleConfig, VlConfig, dump_config, load_config
from virtuallover.console import app as console_app
from virtuallover.console import auth as console_auth
from virtuallover.console import control as console_control
from virtuallover.console import core as console_core
from virtuallover.console import engine as console_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = REPO_ROOT / "virtuallover" / "console"

# 占位 QQ 号（明显占位号段，禁真实值）
PLACEHOLDER_UIN = "10000"


# ---------------------------------------------------------------------------
# 1) 删减彻底性（SPEC §10：删 AstrBot 代理段）
# ---------------------------------------------------------------------------

def test_console_package_has_no_astrbot_import():
    """import 卫生在 console 新家的落点：无任何 astrbot import 语句。"""
    pattern = re.compile(r"^\s*(from astrbot|import astrbot)", re.MULTILINE)
    for py_file in CONSOLE_DIR.glob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        assert not pattern.search(source), f"{py_file.name} 含 astrbot import"


def test_auth_manager_no_longer_hosts_upstream_password(tmp_path):
    """console.json 不再托管 astrbot_password（SPEC §10 删减项）。"""
    manager = console_auth.AuthManager(tmp_path / "console.json")
    assert not hasattr(manager, "get_astrbot_password")
    assert not hasattr(manager, "set_astrbot_password")
    data = manager.load()
    assert set(data) == {"password", "session_secret"}  # 首次生成只剩两字段
    # 会话认证行为原样（三重门之一）
    assert manager.verify_login(data["password"]) is True
    assert manager.verify_login("错密码") is False
    cookie = manager.login_cookie_header()
    assert cookie.startswith(console_auth.COOKIE_NAME + "=")
    assert manager.verify_cookie(cookie) is True


def test_engine_proxy_is_napcat_only(tmp_path):
    """EngineProxy 只剩 NapCat 代理：无任何 ab_* 方法。"""
    proxy = console_engine.EngineProxy(tmp_path / "webui.json")
    assert hasattr(proxy, "nc_proxy")
    for legacy in ("ab_login", "ab_proxy", "ab_status", "_ab_try_login",
                   "_ab_force_relogin", "read_ab_username"):
        assert not hasattr(proxy, legacy), f"残留 AstrBot 代理方法：{legacy}"


def test_engine_proxy_nc_flow_with_stub_upstream(tmp_path):
    """nc 代理链路：sha256(token+'.napcat') 登录 → Bearer Credential 透传。"""
    import hashlib

    (tmp_path / "webui.json").write_text(
        json.dumps({"token": "stub-webui-token"}), encoding="utf-8")
    seen: "list[tuple[str, dict | None]]" = []

    class _Resp:
        def __init__(self, payload: dict):
            self._payload = json.dumps(payload).encode("utf-8")
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self, size: int = -1) -> bytes:
            return self._payload

        def getcode(self) -> int:
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        seen.append((request.full_url, body))
        if request.full_url.endswith("/api/auth/login"):
            digest = hashlib.sha256(b"stub-webui-token.napcat").hexdigest()
            assert body == {"hash": digest}
            return _Resp({"code": 0, "data": {"Credential": "stub-credential"},
                          "message": "success"})
        assert request.headers.get("Authorization") == "Bearer stub-credential"
        return _Resp({"code": 0, "data": {}, "message": "success"})

    proxy = console_engine.EngineProxy(tmp_path / "webui.json", urlopen=fake_urlopen)
    status, payload = proxy.nc_proxy("QQLogin/CheckLoginStatus", "", "POST", b"{}")
    assert status == 200
    assert json.loads(payload)["code"] == 0
    assert len(seen) == 2  # 登录一次 + 透传一次


# ---------------------------------------------------------------------------
# 2) status 进程内探测（SPEC §10：/api/status 改查进程内各模块）
# ---------------------------------------------------------------------------

class _StubBridge:
    def __init__(self, *, listening: bool, connected: bool):
        self._server = object() if listening else None
        self.connected = connected


class _StubStore:
    blocked_reason = None


def _runtime(**overrides) -> types.SimpleNamespace:
    """构造鸭类型 AppContext：默认全模块就绪，overrides 覆盖个别槽位。"""
    cfg = types.SimpleNamespace(tts=types.SimpleNamespace(enabled=False, base_url=""))
    slots = dict(cfg=cfg, bridge=_StubBridge(listening=True, connected=True),
                 chat_client=object(), embedding_client=object(),
                 history=object(), memory_store=object(), mood_engine=object(),
                 diary_service=object(), proactive=object())
    slots.update(overrides)
    return types.SimpleNamespace(**slots)


def test_vl_status_reports_assembled_modules():
    runtime = _runtime(memory_store=_StubStore())
    payload = console_core.vl_status(runtime, fetch_fn=None, now=1700000000)
    keys = {s["key"] for s in payload["services"]}
    assert {"bridge", "napcat", "llm", "embedding", "history", "memory",
            "mood", "diary", "proactive", "voice"} <= keys
    by_key = {s["key"]: s for s in payload["services"]}
    assert by_key["bridge"]["ok"] is True
    assert by_key["napcat"]["ok"] is True
    assert by_key["voice"]["ok"] is False  # tts.enabled=false → 不探测，如实报 down
    assert by_key["voice"]["optional"] is True
    assert payload["generated_at"] == 1700000000


def test_vl_status_reports_missing_modules_as_down():
    runtime = _runtime(bridge=_StubBridge(listening=False, connected=False),
                       chat_client=None, history=None, mood_engine=None)
    payload = console_core.vl_status(runtime, fetch_fn=None)
    by_key = {s["key"]: s for s in payload["services"]}
    assert by_key["bridge"]["ok"] is False    # 未监听
    assert by_key["napcat"]["ok"] is False    # 未连接
    assert by_key["llm"]["ok"] is False       # 未装配
    assert by_key["history"]["ok"] is False


def test_mood_payload_renders_via_soul_mood(tmp_path):
    """/api/mood 渲染复用 port-of-own 引擎（中文标签，禁复制映射表）。"""
    from virtuallover.soul.mood import LEVEL_ZH, MOOD_ZH, affection_level

    data_dir = tmp_path / "shared_plugin_data"
    state = {"affection": 92.0, "mood": "happy", "mood_intensity": 0.8,
             "daily_streak": 3, "total_messages": 42}
    mood_dir = data_dir / "vl_mood_engine"
    mood_dir.mkdir(parents=True)
    (mood_dir / "mood_states.json").write_text(
        json.dumps({PLACEHOLDER_UIN: state}), encoding="utf-8")
    payload = console_core.mood_payload(data_dir)
    assert payload["file_found"] is True
    user = payload["users"][0]
    assert user["id"] == console_core.mask_id(PLACEHOLDER_UIN)
    assert user["level_zh"] == LEVEL_ZH[affection_level(92.0)]
    assert user["mood_zh"] == MOOD_ZH["happy"]
    assert user["intensity_label"] == "强烈"


# ---------------------------------------------------------------------------
# 3) napcat 指向切换 / 回退（SPEC §10 新增动作）
# ---------------------------------------------------------------------------

def _prepare_napcat_dir(tmp_path: Path) -> Path:
    """onebot11_<占位QQ>.json 的最小 napcat 配置目录。"""
    napcat_dir = tmp_path / "napcat_config"
    napcat_dir.mkdir()
    (napcat_dir / f"onebot11_{PLACEHOLDER_UIN}.json").write_text(json.dumps({
        "network": {"websocketClients": [{
            "enable": True,
            "url": "ws://127.0.0.1:6199/ws",   # 切换前的旧指向（占位）
            "token": "old-weak-token-value-0123456789abcdef",
        }]},
    }), encoding="utf-8")
    return napcat_dir


def _prepare_vl_toml(tmp_path: Path) -> Path:
    """最小 vl.toml（token 同步目标）。"""
    cfg = VlConfig(session_id="测试人格:FriendMessage:10000", admins=["10000"],
                   whitelist=["10000"])
    cfg.bridge = BridgeConfig(host="127.0.0.1", port=6199, token="a" * 43,
                              self_id=PLACEHOLDER_UIN)
    cfg.chat.api_base, cfg.chat.api_key, cfg.chat.model = (
        "https://chat.test/v1", "sk-TEST", "test-model")
    cfg.embedding.api_base = "https://emb.test/v1"
    cfg.embedding.api_key = "sk-TEST"
    cfg.embedding.model = "emb-model"
    cfg.persona.name = "测试人格"
    cfg.persona.system_prompt = "占位人格。"
    config_dir = tmp_path / "local" / "config"
    config_dir.mkdir(parents=True)
    path = config_dir / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    return path


class _StubRunner:
    """subprocess.run 替身：记录 argv，恒成功。"""

    def __init__(self):
        self.calls: "list[list[str]]" = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _make_control(tmp_path: Path, runner: _StubRunner) -> console_control.ControlService:
    return console_control.ControlService(
        tmp_path / "local", tmp_path / "backup", tmp_path,
        vl_toml_path=tmp_path / "local" / "config" / "vl.toml",
        napcat_config_dir=tmp_path / "napcat_config", runner=runner)


def test_control_napcat_switch_rewrites_and_restarts(tmp_path):
    napcat_dir = _prepare_napcat_dir(tmp_path)
    vl_toml = _prepare_vl_toml(tmp_path)
    runner = _StubRunner()
    service = _make_control(tmp_path, runner)

    status, payload = service.dispatch({"action": "napcat_switch"})
    assert status == 200 and payload["ok"] is True
    # 固定 argv 定点重启（docker compose restart heart）
    assert runner.calls == [["docker", "compose", "restart", "heart"]]
    # onebot11 改写：url 指向新栈桥（容器视角），token 强随机
    data = json.loads(
        (napcat_dir / f"onebot11_{PLACEHOLDER_UIN}.json").read_text(encoding="utf-8"))
    client = data["network"]["websocketClients"][0]
    assert client["url"] == "ws://host.docker.internal:6199/ws"
    assert len(client["token"]) >= 32
    assert client["token"] != "old-weak-token-value-0123456789abcdef"
    # 写前备份存在
    backups = list(napcat_dir.glob(f"onebot11_{PLACEHOLDER_UIN}.json.bak-*"))
    assert len(backups) == 1
    # vl.toml bridge.token 与 napcat 端同步
    assert load_config(vl_toml).bridge.token == client["token"]


def test_control_napcat_rollback_restores_from_backup(tmp_path):
    napcat_dir = _prepare_napcat_dir(tmp_path)
    _prepare_vl_toml(tmp_path)
    runner = _StubRunner()
    service = _make_control(tmp_path, runner)

    service.dispatch({"action": "napcat_switch"})
    status, payload = service.dispatch({"action": "napcat_rollback"})
    assert status == 200 and payload["ok"] is True
    data = json.loads(
        (napcat_dir / f"onebot11_{PLACEHOLDER_UIN}.json").read_text(encoding="utf-8"))
    client = data["network"]["websocketClients"][0]
    assert client["url"] == "ws://127.0.0.1:6199/ws"  # 恢复切换前指向
    assert client["token"] == "old-weak-token-value-0123456789abcdef"
    assert runner.calls == [["docker", "compose", "restart", "heart"]] * 2


def test_control_rejects_unknown_action():
    service = console_control.ControlService(".", ".", ".")
    with pytest.raises(console_core.ApiError) as exc:
        service.dispatch({"action": "fmt_disk"})
    assert "action 必须是" in str(exc.value)


# ---------------------------------------------------------------------------
# 4) 三重门（会话认证 + CSRF + 127.0.0.1 绑定）与 410 语义
# ---------------------------------------------------------------------------

class _StubControl:
    def dispatch(self, payload):
        return 200, {"ok": True, "echo": payload}


@pytest.fixture
def console_server(tmp_path):
    """动态端口起的控制台实例（登录态经 AuthManager 共享给请求 helper）。"""
    cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(cfg.console_path)
    auth_mgr.load()
    httpd = console_app.make_server(
        "127.0.0.1", 0, cfg, auth=auth_mgr,
        control=_StubControl(),
        status_provider=lambda: {
            "services": [{"key": "bridge", "name": "OneBot 桥监听",
                          "ok": True, "ms": 0, "optional": False}],
            "generated_at": 1,
        },
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, port, auth_mgr
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, method: str, path: str, *, body: "bytes | None" = None,
             headers: "dict | None" = None) -> "tuple[int, dict, dict]":
    """发一个请求，返回 (status, JSON 响应体, 响应头小写键 dict)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return resp.status, payload, dict(resp.getheaders())
    finally:
        conn.close()


def test_triple_gates_and_status(console_server):
    _httpd, port, auth_mgr = console_server
    password = auth_mgr.load()["password"]

    # 门 1：未登录访问受保护端点 → 401
    status, _, _ = _request(port, "GET", "/api/status")
    assert status == 401

    # 登录（免会话端点）：密码正确 → 200 + Set-Cookie
    status, payload, headers = _request(
        port, "POST", "/api/auth/login",
        body=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    assert status == 200 and payload["ok"] is True
    set_cookie = headers.get("Set-Cookie", "")
    assert set_cookie.startswith(console_auth.COOKIE_NAME + "=")
    cookie_header = {"Cookie": set_cookie.split(";")[0]}
    # 错密码 → 401 且不下发会话
    status, payload, headers = _request(
        port, "POST", "/api/auth/login",
        body=json.dumps({"password": "绝对不是密码"}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"})
    assert status == 401 and payload["ok"] is False
    assert "Set-Cookie" not in {k.lower() for k in headers}

    # 门 2（CSRF）：已登录但缺 X-Requested-With 的写方法 → 403
    status, _, _ = _request(port, "POST", "/api/control/action",
                            body=json.dumps({"action": "status"}).encode("utf-8"),
                            headers={"Content-Type": "application/json", **cookie_header})
    assert status == 403

    # 三关齐过：CSRF 头 + 会话 → 控制动作分发成功
    status, payload, _ = _request(
        port, "POST", "/api/control/action",
        body=json.dumps({"action": "status"}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest", **cookie_header})
    assert status == 200 and payload["ok"] is True

    # /api/status 进程内探测：status_provider 产物原样返回
    status, payload, _ = _request(port, "GET", "/api/status", headers=cookie_header)
    assert status == 200
    assert payload["services"][0]["key"] == "bridge"

    # AstrBot 代理段：410 明确回执（不静默 404）
    status, payload, _ = _request(port, "GET", "/api/engine/ab/status",
                                  headers=cookie_header)
    assert status == 410
    assert "已随 v2 移除" in payload["message"]


# ---------------------------------------------------------------------------
# 5) setup(ctx)：随主框架起停
# ---------------------------------------------------------------------------

def test_console_setup_starts_and_cleans_up(tmp_path, caplog):
    """console.setup(ctx)：监听就绪（日志含实际端口，TCP 可连）+ cleanup 释放端口。"""
    ctx = types.SimpleNamespace(
        cfg=types.SimpleNamespace(
            console=ConsoleConfig(enable=True, host="127.0.0.1", port=0)),
        repo_root=tmp_path,
    )
    with caplog.at_level("INFO", logger="virtuallover.console.app"):
        cleanup = console_app.setup(ctx)
    assert callable(cleanup)
    matched = [record for record in caplog.records if "管理控制台已启动" in record.message]
    assert matched, "控制台启动日志缺失"
    port = int(re.search(r":(\d+)", matched[0].message).group(1))
    probe = socket.create_connection(("127.0.0.1", port), timeout=3)
    probe.close()
    cleanup()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_console_setup_disabled_is_noop(tmp_path):
    ctx = types.SimpleNamespace(
        cfg=types.SimpleNamespace(console=ConsoleConfig(enable=False)),
        repo_root=tmp_path,
    )
    assert console_app.setup(ctx) is None


def test_napcat_dir_follows_vl_toml(tmp_path):
    """R5 接线回归：[bridge].napcat_config_dir 跟随 vl.toml（修前是死键，控制台
    一律用 core.AppConfig 硬拼的 local/napcat_config）。"""
    from virtuallover.console.core import AppConfig
    from virtuallover.config import BridgeConfig, VlConfig

    app_cfg = AppConfig.for_root(tmp_path)
    # ① vl.toml 配了相对路径 → 锚定仓库根
    ctx = types.SimpleNamespace(
        cfg=types.SimpleNamespace(
            console=ConsoleConfig(enable=False),
            bridge=BridgeConfig(napcat_config_dir="local/napcat_config_custom")),
        repo_root=tmp_path)
    got = console_app._napcat_dir_from_cfg(ctx, app_cfg)
    assert got == tmp_path / "local" / "napcat_config_custom"
    # ② 绝对路径原样生效（Path 拼接语义）
    ctx.cfg.bridge = BridgeConfig(napcat_config_dir=r"D:\elsewhere\napcat")
    assert console_app._napcat_dir_from_cfg(ctx, app_cfg) == Path(r"D:\elsewhere\napcat")
    # ③ 旧桩配置（缺 bridge 段）/ 空值 → 回退 core 默认
    ctx2 = types.SimpleNamespace(
        cfg=types.SimpleNamespace(console=ConsoleConfig(enable=False)),
        repo_root=tmp_path)
    assert console_app._napcat_dir_from_cfg(ctx2, app_cfg) == app_cfg.napcat_config_dir
    ctx3 = types.SimpleNamespace(
        cfg=types.SimpleNamespace(console=ConsoleConfig(enable=False),
                                  bridge=BridgeConfig(napcat_config_dir="")),
        repo_root=tmp_path)
    assert console_app._napcat_dir_from_cfg(ctx3, app_cfg) == app_cfg.napcat_config_dir
    # ④ 默认值与 core 默认同指一处（不配=行为不变）
    ctx4 = types.SimpleNamespace(
        cfg=types.SimpleNamespace(console=ConsoleConfig(enable=False),
                                  bridge=VlConfig().bridge),
        repo_root=tmp_path)
    assert console_app._napcat_dir_from_cfg(ctx4, app_cfg) == app_cfg.napcat_config_dir


def test_static_page_drops_astrbot_ui():
    """五轮修缮 R1 后的前端：AstrBot 残留 UI 全删，引擎/人格/记忆页改走新契约。

    死代码（providers/persona 旧管理全链 JS + card-ab/card-providers/pv-mask
    三块 HTML）已删；新页面按后端组最终契约（console/config_page.py 与
    control.memory_search_payload）写端点。
    """
    html = (CONSOLE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    # AstrBot 代理与旧管理链零残留
    assert "AB_BASE" not in html
    assert "/api/engine/ab/" not in html
    assert "card-ab" not in html
    assert "card-providers" not in html
    assert "pv-mask" not in html
    assert "AstrBot" not in html
    # 新契约端点已在页面里
    assert "EP_CONFIG_CHAT      = '/api/config/chat'" in html
    assert "EP_CONFIG_PERSONA   = '/api/config/persona'" in html
    assert "EP_MEMORY_LIST = '/api/memory/list'" in html
    # 大脑切换动作与 napcat 指向按钮就位
    assert "brain_local" in html and "brain_cloud" in html
    assert 'data-action="napcat_switch"' in html
    assert 'data-action="napcat_rollback"' in html


def test_console_standalone_entry_compiles():
    """独立运行入口（python -m virtuallover.console.app）可被 py_compile 校验。"""
    import py_compile
    py_compile.compile(str(CONSOLE_DIR / "app.py"), doraise=True)


# ---------------------------------------------------------------------------
# 7) 备份排除可再生数据（VOICE_SPEC §1：voice_cache 不进备份；与
#    scripts/backup.bat 的 robocopy /XD 两处同步，本节把两处都钉住）
# ---------------------------------------------------------------------------

def test_backup_excludes_regenerable_voice_cache(tmp_path):
    """create_backup 跳过 local/vl_data/voice_cache（纯可再生语音缓存）。"""
    cache_file = tmp_path / "local" / "vl_data" / "voice_cache" / "abc.wav"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"RIFF-fake-wav")
    keep_file = tmp_path / "local" / "vl_data" / "history.db"
    keep_file.write_bytes(b"sqlite")  # 同目录下的库文件必须照常入包

    result = console_control.create_backup(tmp_path / "local", tmp_path / "backup",
                                           clock=1700000000.0)
    assert result["ok"] is True
    with __import__("zipfile").ZipFile(result["path"]) as archive:
        names = archive.namelist()
    assert any(n.endswith("vl_data/history.db") for n in names), "库文件丢失"
    assert not any("voice_cache" in n for n in names), "可再生缓存不得进备份"


def test_backup_exclude_list_synced_with_backup_bat():
    """两处备份清单同步契约：backup.bat 的 vl_data 行必须 /XD voice_cache，
    且 control.py 的排除清单含 vl_data/voice_cache（防单边漂移）。"""
    bat_path = REPO_ROOT / "scripts" / "backup.bat"
    try:
        bat_text = bat_path.read_bytes().decode("utf-8")  # bat 已统一 UTF-8
    except UnicodeDecodeError:
        bat_text = bat_path.read_bytes().decode("gbk")  # 旧检出（GBK）兼容
    vl_data_lines = [ln for ln in bat_text.splitlines()
                     if ln.strip().startswith("robocopy local" + chr(92) + "vl_data")]
    assert len(vl_data_lines) == 1, "backup.bat 应恰有一条 vl_data 备份行"
    assert "/XD voice_cache" in vl_data_lines[0]
    assert "vl_data/voice_cache" in console_control.BACKUP_EXCLUDE_RELPATHS
