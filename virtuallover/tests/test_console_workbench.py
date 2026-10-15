# -*- coding: utf-8 -*-
"""workbench 活断言迁移（O11⑤，PERF_SPEC §4-O11 + §7.2）——控制台三重门与动作面的
活断言在新家 virtuallover/tests 固化，workbench/ 整删后语义不丢。

四组映射（断言原样迁移，unittest → pytest 风格改写；逻辑与期望值零改动）：
- workbench/tests/test_auth.py 18 项 → 本文件 AuthManager 组
  （17 项原样 + 1 项适配：astrbot 密码托管已随 v2 删除，原 roundtrip 断言改写为
  「不再托管」矩阵项——load 结果无 astrbot_password 且类无 get/set_astrbot_password）；
- workbench/tests/test_engine.py NapCatProxyTests 9 项 → NCProxyTests
  （console/engine.py 删除了 AstrBot 段，nc_* 契约原样）；
- workbench/tests/test_control.py ComposeActionTests 6 + BackupTests 4 →
  ComposeTests / BackupTests（console/control.py 新增 napcat_switch/rollback
  不在迁移清单，已有 test_console_port 覆盖）；
- workbench/tests/test_app.py AuthGateTests 12 项 → AuthGateHTTPTests
  （+1 项 ab 代理 410 矩阵项：v2 删除 AstrBot 代理路由的语义固化）。

运行：python -m pytest virtuallover/tests -q -n auto（统一门禁入口，O11④）
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

from virtuallover.net import smart_urlopen  # 测试打 127.0.0.1 控制台：禁代理直连
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from virtuallover.console import app as console_app
from virtuallover.console import auth as console_auth
from virtuallover.console import control as console_control
from virtuallover.console import core as console_core
from virtuallover.console import engine as console_engine

# 全部为编造占位值（敏感值不进测试）
_WEBUI_TOKEN = "tok12345-placeholder"


# ---------------------------------------------------------------------------
# 通用替身
# ---------------------------------------------------------------------------


class StubCompleted:
    """subprocess.run 返回对象的鸭子类型替身（control runner 用）。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_recording_runner(result=None, exc=None):
    """记录调用参数的 runner 替身；exc 非 None 时抛出以模拟 docker 缺失/超时。"""
    calls: list[dict] = []

    def runner(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if exc is not None:
            raise exc
        return result if result is not None else StubCompleted()

    runner.calls = calls
    return runner


class MockUpstream:
    """本地 mock 上游：路由 (method, path) -> callable(record) -> (status, obj)。

    record 含 method/path(原始含 query)/headers(小写键)/body(bytes)；
    未命中路由走 echo：把收到的请求原样回显成 JSON（用于断言路径重写与请求头）。
    """

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                pass

            def _read_body(self) -> bytes:
                length = self.headers.get("Content-Length")
                return self.rfile.read(int(length)) if length else b""

            def _record(self) -> dict:
                body = self._read_body()
                record = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
                outer.requests.append(record)
                return record

            def _respond(self, status: int, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _dispatch(self):
                record = self._record()
                raw_path = self.path.split("?", 1)[0]
                handler = outer.routes.get((self.command, raw_path))
                if handler is None:
                    # echo：回显收到的请求（路径/认证头/内容类型/请求体）
                    self._respond(200, {
                        "echo": {
                            "method": record["method"],
                            "path": record["path"],
                            "authorization": record["headers"].get("authorization"),
                            "content_type": record["headers"].get("content-type"),
                            "has_cookie": "cookie" in record["headers"],
                            "body": record["body"].decode("utf-8", "replace"),
                        }
                    })
                    return
                result = handler(record)
                if len(result) == 3:  # (status, payload, content_type)
                    status, payload, content_type = result
                else:
                    status, payload = result
                    content_type = "application/json"
                self._respond(status, payload, content_type)

            do_GET = _dispatch
            do_POST = _dispatch
            do_PUT = _dispatch
            do_DELETE = _dispatch

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def calls(self, method: str, path: str) -> list[dict]:
        return [
            r for r in self.requests
            if r["method"] == method and r["path"].split("?", 1)[0] == path
        ]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def webui_token(tmp_path):
    """写一个带 token 的 webui.json，返回路径（napcat 配置注入用）。"""
    path = tmp_path / "webui.json"
    path.write_text(json.dumps({"token": _WEBUI_TOKEN}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 第一组：AuthManager（workbench/tests/test_auth.py 18 项迁移）
# ---------------------------------------------------------------------------


class AuthClock:
    """可手拨的时钟替身：tick(seconds) 前进。"""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def tick(self, seconds=0.0):
        self.now += seconds
        return self.now

    def __call__(self):
        return self.now


@pytest.fixture()
def auth_clock():
    return AuthClock()


@pytest.fixture()
def auth_mgr(tmp_path, auth_clock):
    return console_auth.AuthManager(tmp_path / "console.json", clock=auth_clock)


# ---- console.json 存取 ----


def test_console_store_first_load_generates_and_writes_file(auth_mgr, tmp_path):
    data = auth_mgr.load()
    assert len(data["password"]) == console_auth.PASSWORD_LENGTH
    assert all(c in console_auth.PASSWORD_ALPHABET for c in data["password"])
    assert len(data["session_secret"]) >= 64  # token_hex(32)
    assert auth_mgr.newly_created
    on_disk = json.loads((tmp_path / "console.json").read_text(encoding="utf-8"))
    assert on_disk["password"] == data["password"]
    assert on_disk["session_secret"] == data["session_secret"]


def test_console_store_existing_file_loaded_without_regeneration(tmp_path, auth_clock):
    (tmp_path / "console.json").write_text(json.dumps({
        "password": "fixed-pw-123",
        "session_secret": "ab" * 32,
    }), encoding="utf-8")
    manager = console_auth.AuthManager(tmp_path / "console.json", clock=auth_clock)
    data = manager.load()
    assert not manager.newly_created
    assert data["password"] == "fixed-pw-123"
    assert data["session_secret"] == "ab" * 32


def test_console_store_corrupt_file_regenerates(tmp_path, auth_clock):
    for raw in ["{not-json", "[1,2]", json.dumps({"password": ""}), json.dumps({"nope": 1})]:
        path = tmp_path / "console.json"
        path.write_text(raw, encoding="utf-8")
        manager = console_auth.AuthManager(tmp_path / "console.json", clock=auth_clock)
        data = manager.load()
        assert manager.newly_created
        assert "password" in data
        path.unlink()


def test_console_store_announce_prints_password_once_on_first_start(auth_mgr):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        auth_mgr.announce_password()
    first = stdout.getvalue()
    assert auth_mgr.load()["password"] in first
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        auth_mgr.announce_password()
    assert auth_mgr.load()["password"] not in stdout.getvalue()


def test_console_store_announce_existing_file_never_logs_full_password(
        tmp_path, auth_clock, caplog):
    (tmp_path / "console.json").write_text(json.dumps({
        "password": "topsecret99",
        "session_secret": "cd" * 32,
    }), encoding="utf-8")
    manager = console_auth.AuthManager(tmp_path / "console.json", clock=auth_clock)
    import logging as _logging
    with caplog.at_level(_logging.INFO, logger="virtuallover.console.auth"):
        manager.announce_password()
    text = "\n".join(str(r.getMessage()) for r in caplog.records)
    assert "topsecret99" not in text          # 全文绝不入日志
    assert "top" in text                      # 前 4 位打码提示
    assert "****" in text


def test_console_store_no_astrbot_password_hosting(auth_mgr):
    """适配项（原 test_astrbot_password_roundtrip_preserves_other_fields）：
    v2 已无 AstrBot 代理，console.json 不托管上游密码——schema 固化为
    password/session_secret 两键，且托管方法不复存在。"""
    data = auth_mgr.load()
    assert set(data) == {"password", "session_secret"}
    assert not hasattr(auth_mgr, "get_astrbot_password")
    assert not hasattr(auth_mgr, "set_astrbot_password")


# ---- 会话令牌签发与校验 ----


def test_token_issue_and_verify_roundtrip(auth_mgr):
    token = auth_mgr.issue_token()
    assert token.count(".") == 1
    assert auth_mgr.verify_token(token)
    payload_b64, sig = token.split(".")
    assert len(sig) == 64  # hmac_sha256 hex
    int(sig, 16)  # 全 hex


def test_token_login_cookie_header_flags_and_expiry(auth_mgr):
    header = auth_mgr.login_cookie_header()
    assert header.startswith(f"{console_auth.COOKIE_NAME}=")
    for flag in ("HttpOnly", "SameSite=Strict", "Path=/",
                 f"Max-Age={console_auth.SESSION_TTL_SECONDS}"):
        assert flag in header


def test_token_expired_token_rejected(auth_mgr, auth_clock):
    token = auth_mgr.issue_token()
    auth_clock.tick(console_auth.SESSION_TTL_SECONDS + 1)
    assert not auth_mgr.verify_token(token)


def test_token_tampered_payload_or_signature_rejected(auth_mgr, tmp_path, auth_clock):
    token = auth_mgr.issue_token()
    payload_b64, sig = token.split(".")
    assert not auth_mgr.verify_token(payload_b64[:-2] + "aa." + sig)
    assert not auth_mgr.verify_token(payload_b64 + "." + ("0" * 64))
    # 用另一个 secret 签的令牌不通过
    other = console_auth.AuthManager(tmp_path / "other.json", clock=auth_clock)
    assert not other.verify_token(token)


def test_token_garbage_tokens_rejected(auth_mgr):
    for bad in [None, "", "abc", "a.b", "..", "x.y.z", "§nonascii§.ff", "eyJ*.ff"]:
        assert not auth_mgr.verify_token(bad)


def test_token_parse_session_cookie():
    header = f"other=1; {console_auth.COOKIE_NAME}=tok123; extra=2"
    assert console_auth.AuthManager.parse_session_cookie(header) == "tok123"
    assert console_auth.AuthManager.parse_session_cookie("other=1") is None
    assert console_auth.AuthManager.parse_session_cookie(None) is None
    assert console_auth.AuthManager.parse_session_cookie(f"{console_auth.COOKIE_NAME}=") is None


def test_token_verify_cookie_roundtrip_and_logout_header(auth_mgr):
    header = auth_mgr.login_cookie_header()
    cookie_pair = header.split(";")[0]
    assert auth_mgr.verify_cookie(cookie_pair)
    assert not auth_mgr.verify_cookie(f"{console_auth.COOKIE_NAME}=forged.sig")
    logout = auth_mgr.logout_cookie_header()
    assert "Max-Age=0" in logout
    assert "HttpOnly" in logout
    assert "SameSite=Strict" in logout


# ---- 登录校验与爆破锁定 ----


def test_login_verify_login_accepts_and_rejects(auth_mgr, auth_clock):
    password = auth_mgr.load()["password"]
    assert auth_mgr.verify_login(password)
    auth_clock.tick(1)
    assert not auth_mgr.verify_login("wrong-password")


def test_login_lock_after_max_consecutive_failures(auth_mgr, auth_clock):
    for i in range(console_auth.MAX_LOGIN_FAILURES):
        assert not auth_mgr.verify_login(f"bad-{i}")
        auth_clock.tick(0.1)
    assert auth_mgr.throttle.is_locked
    with pytest.raises(console_core.ApiError) as ctx:
        auth_mgr.verify_login(auth_mgr.load()["password"])
    assert ctx.value.status == 429  # 锁定期内连正确密码也拒绝


def test_login_lock_expires_and_resets(auth_mgr, auth_clock):
    for i in range(console_auth.MAX_LOGIN_FAILURES):
        auth_mgr.verify_login(f"bad-{i}")
    auth_clock.tick(console_auth.LOGIN_LOCK_SECONDS + 1)
    assert not auth_mgr.throttle.is_locked  # 到期解锁
    assert auth_mgr.verify_login(auth_mgr.load()["password"])  # 且可重新登录


def test_login_success_resets_failure_counter(auth_mgr, auth_clock):
    password = auth_mgr.load()["password"]
    for _ in range(console_auth.MAX_LOGIN_FAILURES - 1):
        auth_mgr.verify_login("wrong")
    assert auth_mgr.verify_login(password)  # 中途成功清零
    for _ in range(console_auth.MAX_LOGIN_FAILURES - 1):
        auth_mgr.verify_login("wrong")
    assert not auth_mgr.throttle.is_locked  # 未再触发锁定


def test_login_verify_login_uses_constant_time_compare(auth_mgr):
    """防时序攻击：比较走 hmac.compare_digest（源码断言，防退化成 ==）。"""
    import inspect
    source = inspect.getsource(console_auth.AuthManager.verify_login)
    assert "compare_digest" in source


# ---------------------------------------------------------------------------
# 第二组：NapCat 引擎代理（workbench/tests/test_engine.py NapCatProxyTests 9 项）
# ---------------------------------------------------------------------------


NC_LOGIN_PATH = "/api/auth/login"


def nc_login_route(token: str, credential="cred-1"):
    def handler(record):
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        try:
            payload = json.loads(record["body"].decode("utf-8"))
        except ValueError:
            payload = {}
        if payload.get("hash") == digest:
            return 200, {"code": 0, "data": {"Credential": credential}, "message": "success"}
        return 200, {"code": -1, "message": "token is invalid"}
    return handler


@pytest.fixture()
def nc_proxy(webui_token):
    """默认 napcat 配置注入的代理（nc_base 由各测试覆写）。"""
    holders: dict = {}

    def _make(mock=None, *, clock=None):
        proxy = console_engine.EngineProxy(
            napcat_config_path=webui_token,
            nc_base=mock.base if mock is not None else "http://127.0.0.1:61990",
            clock=clock,
        )
        holders["last"] = proxy
        return proxy

    return _make


def _assert_api_error(excinfo, status, message_part=None):
    assert excinfo.value.status == status
    if message_part is not None:
        assert message_part in excinfo.value.message
    return excinfo.value


def test_nc_proxy_logins_with_sha256_hash_and_bears_credential(nc_proxy, webui_token):
    mock = MockUpstream()
    try:
        mock.routes[("POST", NC_LOGIN_PATH)] = nc_login_route(_WEBUI_TOKEN, credential="cred-1")
        clock = {"now": 1000.0}
        proxy = nc_proxy(mock, clock=lambda: clock["now"])

        status, body = proxy.nc_proxy("QQLogin/GetQQLoginQrcode", "", "POST", b"{}")

        assert status == 200
        echo = json.loads(body)["echo"]
        assert echo["path"] == "/api/QQLogin/GetQQLoginQrcode"
        assert echo["authorization"] == "Bearer cred-1"
        assert not echo["has_cookie"]
        # 登录请求体是 sha256(token + '.napcat')
        login_body = json.loads(mock.calls("POST", NC_LOGIN_PATH)[0]["body"])
        expected = hashlib.sha256((_WEBUI_TOKEN + ".napcat").encode("utf-8")).hexdigest()
        assert login_body["hash"] == expected
    finally:
        mock.close()


def test_nc_credential_cached_until_expiry_margin(nc_proxy):
    mock = MockUpstream()
    try:
        mock.routes[("POST", NC_LOGIN_PATH)] = nc_login_route(_WEBUI_TOKEN)
        clock = {"now": 1000.0}
        proxy = nc_proxy(mock, clock=lambda: clock["now"])

        proxy.nc_proxy("QQLogin/CheckLoginStatus", "", "POST", b"{}")
        proxy.nc_proxy("QQLogin/CheckLoginStatus", "", "POST", b"{}")
        assert len(mock.calls("POST", NC_LOGIN_PATH)) == 1  # 缓存生效

        clock["now"] += (console_engine.NC_CREDENTIAL_TTL_SECONDS
                         - console_engine.NC_REFRESH_MARGIN_SECONDS + 1)
        proxy.nc_proxy("QQLogin/CheckLoginStatus", "", "POST", b"{}")
        assert len(mock.calls("POST", NC_LOGIN_PATH)) == 2  # 过期前 5 分钟窗口外重登
    finally:
        mock.close()


def test_nc_http_401_forces_credential_refresh_once(nc_proxy):
    mock = MockUpstream()
    try:
        mock.routes[("POST", NC_LOGIN_PATH)] = nc_login_route(_WEBUI_TOKEN, credential="cred-new")
        clock = {"now": 0.0}
        proxy = nc_proxy(mock, clock=lambda: clock["now"])
        proxy.nc_credential = "cred-stale"
        proxy.nc_credential_at = clock["now"]
        mock.routes[("POST", "/api/QQLogin/CheckLoginStatus")] = lambda r: (
            (200, {"code": 0, "data": {"isLogin": True}, "message": "success"})
            if r["headers"].get("authorization") == "Bearer cred-new"
            else (401, {"code": -1, "message": "credential expired"})
        )

        status, body = proxy.nc_proxy("QQLogin/CheckLoginStatus", "", "POST", b"{}")

        assert status == 200
        assert json.loads(body)["data"]["isLogin"] is True
        assert len(mock.calls("POST", NC_LOGIN_PATH)) == 1
        assert len(mock.calls("POST", "/api/QQLogin/CheckLoginStatus")) == 2
    finally:
        mock.close()


def test_nc_token_missing_is_502(nc_proxy, tmp_path):
    mock = MockUpstream()
    try:
        proxy = console_engine.EngineProxy(
            napcat_config_path=tmp_path / "nope.json", nc_base=mock.base)
        with pytest.raises(console_core.ApiError) as excinfo:
            proxy.nc_proxy("QQLogin/GetQQLoginQrcode", "", "POST", b"{}")
        _assert_api_error(excinfo, 502, "token 未配置")
    finally:
        mock.close()


def test_nc_upstream_unreachable_is_502(nc_proxy, webui_token):
    dead = MockUpstream()
    port = dead.httpd.server_address[1]
    dead.close()  # 关掉制造连接拒绝
    proxy = console_engine.EngineProxy(
        napcat_config_path=webui_token, nc_base=f"http://127.0.0.1:{port}")
    with pytest.raises(console_core.ApiError) as excinfo:
        proxy.nc_proxy("QQLogin/GetQQLoginQrcode", "", "POST", b"{}")
    _assert_api_error(excinfo, 502, "不可达")


def test_nc_login_failure_message_is_502(nc_proxy):
    mock = MockUpstream()
    try:
        mock.routes[("POST", NC_LOGIN_PATH)] = lambda r: (
            200, {"code": -1, "message": "token is invalid"})
        proxy = nc_proxy(mock)
        with pytest.raises(console_core.ApiError) as excinfo:
            proxy.nc_proxy("QQLogin/GetQQLoginQrcode", "", "POST", b"{}")
        _assert_api_error(excinfo, 502, "token is invalid")
    finally:
        mock.close()


def test_nc_method_whitelist(nc_proxy):
    mock = MockUpstream()
    try:
        mock.routes[("POST", NC_LOGIN_PATH)] = nc_login_route(_WEBUI_TOKEN)
        proxy = nc_proxy(mock)
        for method in ("PUT", "DELETE", "PATCH"):
            with pytest.raises(console_core.ApiError) as excinfo:
                proxy.nc_proxy("QQLogin/CheckLoginStatus", "", method, b"{}")
            _assert_api_error(excinfo, 405)
    finally:
        mock.close()


def test_nc_empty_subpath_is_404(nc_proxy):
    mock = MockUpstream()
    try:
        mock.routes[("POST", NC_LOGIN_PATH)] = nc_login_route(_WEBUI_TOKEN)
        proxy = nc_proxy(mock)
        with pytest.raises(console_core.ApiError) as excinfo:
            proxy.nc_proxy("", "", "POST", b"{}")
        _assert_api_error(excinfo, 404)
    finally:
        mock.close()


def test_nc_control_chars_in_subpath_rejected(nc_proxy):
    mock = MockUpstream()
    try:
        mock.routes[("POST", NC_LOGIN_PATH)] = nc_login_route(_WEBUI_TOKEN)
        proxy = nc_proxy(mock)
        # 原始控制字符（CRLF 注入企图）→ 400
        with pytest.raises(console_core.ApiError) as excinfo:
            proxy.nc_proxy("x\r\nSet-Cookie: pwned", "", "GET", b"")
        _assert_api_error(excinfo, 400)
        with pytest.raises(console_core.ApiError) as excinfo:
            proxy.nc_proxy("x\x00y", "", "GET", b"")
        _assert_api_error(excinfo, 400)
        # 百分号编码的字符（%20 等）原样放行，由上游路由处理
        status, body = proxy.nc_proxy("QQLogin/GetQQLoginQrcode", "a=%20b", "POST", b"{}")
        assert status == 200
        echo = json.loads(body)["echo"]
        assert echo["path"] == "/api/QQLogin/GetQQLoginQrcode?a=%20b"
    finally:
        mock.close()


# ---------------------------------------------------------------------------
# 第三组：compose 动作与备份（workbench/tests/test_control.py 10 项）
# ---------------------------------------------------------------------------


class TestCompose:
    def test_fixed_argv_list_per_whitelisted_action(self, tmp_path):
        # 实测 compose v5 无 status 子命令：status 映射为 ps --all，其余同名透传
        expected_argv = {
            "start": ["docker", "compose", "start"],
            "stop": ["docker", "compose", "stop"],
            "restart": ["docker", "compose", "restart"],
            "status": ["docker", "compose", "ps", "--all"],
        }
        for action, argv in expected_argv.items():
            runner = make_recording_runner(StubCompleted(0, "done", ""))
            result = console_control.run_compose(action, cwd=tmp_path, runner=runner)
            assert result["ok"] is True
            assert result["output"] == "done"
            call = runner.calls[0]
            assert call["argv"] == argv
            assert isinstance(call["argv"], list)      # 列表参数，绝不进 shell
            assert call["cwd"] == str(tmp_path)
            assert call["timeout"] == 180
            assert call["capture_output"] is True
            assert call["text"] is True
            assert "shell" not in call                 # 绝不带 shell=True

    def test_stderr_appended_and_returncode_reflected(self, tmp_path):
        runner = make_recording_runner(StubCompleted(1, "partial", "boom"))
        result = console_control.run_compose("stop", cwd=tmp_path, runner=runner)
        assert result["ok"] is False
        assert "partial" in result["output"]
        assert "boom" in result["output"]

    def test_output_truncated_to_last_200_lines(self, tmp_path):
        stdout = "\n".join(f"line-{i}" for i in range(1, 301))
        runner = make_recording_runner(StubCompleted(0, stdout, ""))
        result = console_control.run_compose("status", cwd=tmp_path, runner=runner)
        lines = result["output"].splitlines()
        assert len(lines) == console_control.OUTPUT_TAIL_LINES
        assert lines[0] == "line-101"
        assert lines[-1] == "line-300"

    def test_docker_missing_returns_clear_error(self, tmp_path):
        runner = make_recording_runner(exc=FileNotFoundError("docker"))
        result = console_control.run_compose("start", cwd=tmp_path, runner=runner)
        assert result["ok"] is False
        assert "docker 命令不可用" in result["output"]

    def test_timeout_returns_clear_error(self, tmp_path):
        runner = make_recording_runner(
            exc=subprocess.TimeoutExpired(cmd="docker", timeout=180))
        result = console_control.run_compose("start", cwd=tmp_path, runner=runner)
        assert result["ok"] is False
        assert "超时" in result["output"]

    def test_invalid_action_rejected_before_spawn(self, tmp_path):
        runner = make_recording_runner()
        with pytest.raises(console_core.ApiError) as ctx:
            console_control.run_compose("rm -rf /", cwd=tmp_path, runner=runner)
        assert ctx.value.status == 400
        assert runner.calls == []  # 非法动作绝不触达 subprocess


class TestBackup:
    @pytest.fixture()
    def backup_env(self, tmp_path):
        local = tmp_path / "local"
        # astrbot_data：普通文件 + 数据库 + 运行中 SQLite 临时文件
        ab = local / "astrbot_data"
        (ab / "db").mkdir(parents=True)
        (ab / "config.json").write_text("{}", encoding="utf-8")
        (ab / "db" / "data.db").write_text("sqlite-data", encoding="utf-8")
        (ab / "db" / "data.db-wal").write_text("WAL-RUNNING", encoding="utf-8")
        (ab / "db" / "data.db-shm").write_text("SHM-RUNNING", encoding="utf-8")
        (ab / "legacy.db-journal").write_text("JOURNAL", encoding="utf-8")
        # napcat_config
        nc = local / "napcat_config"
        nc.mkdir()
        (nc / "webui.json").write_text(json.dumps({"token": "tok"}), encoding="utf-8")
        # shared_plugin_data（含中文内容）
        sp = local / "shared_plugin_data" / "vl_diary" / "diary"
        sp.mkdir(parents=True)
        (sp / "2026-10-09.md").write_text("她的日记。", encoding="utf-8")
        # napcat_data 不创建：验证缺失目录被跳过
        return local

    def test_backup_creates_zip_with_expected_contents(self, backup_env, tmp_path):
        result = console_control.create_backup(
            backup_env, tmp_path / "backup", clock=1_760_000_000.0)
        assert result["ok"] is True
        target = Path(result["path"])
        assert target.parent == tmp_path / "backup"
        # 文件名 = vl-backup-<本地时间 YYYYMMDD-HHMMSS>.zip（格式固定，时区取本机）
        assert target.name == time.strftime(
            "vl-backup-%Y%m%d-%H%M%S", time.localtime(1_760_000_000.0)) + ".zip"
        assert result["size"] == target.stat().st_size
        assert result["size"] > 0
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            assert "local/astrbot_data/config.json" in names
            assert "local/astrbot_data/db/data.db" in names
            assert "local/napcat_config/webui.json" in names
            assert "local/shared_plugin_data/vl_diary/diary/2026-10-09.md" in names
            # 运行中的 SQLite 临时文件绝不入包
            assert not any(n.endswith(("-wal", "-shm", "-journal")) for n in names)
            # 内容可读且中文不乱码（utf-8 文件名/内容）
            assert (archive.read(
                "local/shared_plugin_data/vl_diary/diary/2026-10-09.md").decode("utf-8")
                == "她的日记。")
            assert (archive.read("local/astrbot_data/db/data.db").decode("utf-8")
                    == "sqlite-data")

    def test_backup_skips_missing_dirs(self, backup_env, tmp_path):
        result = console_control.create_backup(
            backup_env, tmp_path / "backup", clock=1_760_000_000.0)
        with zipfile.ZipFile(result["path"]) as archive:
            assert not any(n.startswith("local/napcat_data/") for n in archive.namelist())

    def test_backup_all_missing_still_creates_empty_zip(self, tmp_path):
        result = console_control.create_backup(
            tmp_path / "nothing", tmp_path / "backup", clock=1_760_000_000.0)
        assert result["ok"] is True
        assert (tmp_path / "backup" / Path(result["path"]).name).is_file()

    def test_dispatch_backup_returns_ok_path_size(self, tmp_path):
        service = console_control.ControlService(
            tmp_path / "local", tmp_path / "backup", tmp_path,
            runner=make_recording_runner(), clock=lambda: 1_760_000_000.0,
        )
        status, payload = service.dispatch({"action": "backup"})
        assert status == 200
        assert set(payload) == {"ok", "path", "size"}
        assert payload["ok"] is True
        assert Path(payload["path"]).is_file()
        assert payload["size"] == Path(payload["path"]).stat().st_size


# ---------------------------------------------------------------------------
# 第四组：认证/CSRF 门 HTTP 矩阵（workbench/tests/test_app.py AuthGateTests 12 项
# + ab 代理 410 矩阵项）
# ---------------------------------------------------------------------------


class ConsoleStub:
    """被测控制台句柄：临时目录 + 注入替身 + 线程化 ThreadingHTTPServer。"""

    def __init__(self, tmp_path: Path):
        local = tmp_path / "local"
        (local / "shared_plugin_data").mkdir(parents=True)
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text(
            "<!doctype html><html><body>面板 ok</body></html>", encoding="utf-8")
        napcat_config = tmp_path / "webui.json"
        napcat_config.write_text(json.dumps({"token": "wxyz9876"}), encoding="utf-8")
        self.napcat_config = napcat_config
        self.console_path = local / "console.json"
        self.auth = console_auth.AuthManager(self.console_path)
        self.auth.load()
        self.password = self.auth.load()["password"]
        self.cfg = console_core.AppConfig(
            port=0,
            data_dir=local / "shared_plugin_data",
            local_dir=local,
            napcat_config=napcat_config,
            console_path=self.console_path,
            backup_dir=tmp_path / "backup",
            static_dir=static_dir,
            vl_toml_path=local / "config" / "vl.toml",
            napcat_config_dir=local / "napcat_config",
            fetch_fn=None,
        )
        self.httpd = console_app.make_server(
            "127.0.0.1", 0, self.cfg,
            auth=self.auth,
            engine=console_engine.EngineProxy(napcat_config_path=napcat_config),
            control=console_control.ControlService(
                self.cfg.local_dir, self.cfg.backup_dir, tmp_path,
                runner=lambda *a, **k: StubCompleted()),
            status_provider=lambda: {"services": [], "generated_at": 0},
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.cookie = None

    # ---------------- 请求帮手 ----------------

    def request(self, path, method="GET", body=None, headers=None, *,
                use_cookie=True, csrf=True):
        h = dict(headers or {})
        if use_cookie and self.cookie:
            h["Cookie"] = self.cookie
        if csrf:
            h["X-Requested-With"] = "XMLHttpRequest"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=h, method=method)
        try:
            with smart_urlopen(request, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def get(self, path, **kwargs):
        status, headers, body = self.request(path, method="GET", **kwargs)
        return status, headers.get("Content-Type"), body

    def get_json(self, path, **kwargs):
        status, headers, body = self.request(path, method="GET", **kwargs)
        return status, headers.get("Content-Type"), json.loads(body.decode("utf-8"))

    def login(self, password=None):
        """走真实 /api/auth/login 拿会话 Cookie。"""
        status, headers, _ = self.request(
            "/api/auth/login", method="POST",
            body={"password": password if password is not None else self.password},
            use_cookie=False,
        )
        assert status == 200, (status, headers)
        self.cookie = headers.get("Set-Cookie").split(";")[0]
        return self.cookie

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def console(tmp_path):
    stub = ConsoleStub(tmp_path)
    yield stub
    stub.close()


class TestAuthGateHTTP:
    def test_api_requires_session(self, console):
        status, _, body = console.get("/api/status", use_cookie=False)
        assert status == 401
        assert "error" in json.loads(body.decode("utf-8"))

    def test_api_rejects_garbage_cookie(self, console):
        console.cookie = "vl_session=forged.signature"
        status, _, _ = console.get("/api/status")
        assert status == 401

    def test_login_flow_issues_session_cookie(self, console):
        cookie = console.login()
        assert cookie.startswith("vl_session=")
        status, _, payload = console.get_json("/api/auth/whoami")
        assert status == 200
        assert payload == {"ok": True}

    def test_whoami_without_session_is_401(self, console):
        status, _, _ = console.get("/api/auth/whoami", use_cookie=False)
        assert status == 401

    def test_login_wrong_password_is_401(self, console):
        status, _, body = console.request(
            "/api/auth/login", method="POST",
            body={"password": "definitely-wrong"}, use_cookie=False,
        )
        assert status == 401
        assert not json.loads(body.decode("utf-8"))["ok"]

    def test_login_sets_httponly_samesite_cookie(self, console):
        status, headers, _ = console.request(
            "/api/auth/login", method="POST",
            body={"password": console.password}, use_cookie=False,
        )
        assert status == 200
        set_cookie = headers.get("Set-Cookie")
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert "Path=/" in set_cookie
        assert "Max-Age=28800" in set_cookie

    def test_login_missing_password_is_400(self, console):
        status, _, _ = console.request(
            "/api/auth/login", method="POST", body={}, use_cookie=False)
        assert status == 400

    def test_logout_clears_cookie_and_requires_session(self, console):
        console.login()
        status, headers, body = console.request("/api/auth/logout", method="POST")
        assert status == 200
        assert json.loads(body.decode("utf-8")) == {"ok": True}
        assert "Max-Age=0" in headers.get("Set-Cookie")
        # 未登录登出同样 401（认证门在前）
        console.cookie = None
        status, _, _ = console.request("/api/auth/logout", method="POST", use_cookie=False)
        assert status == 401

    def test_write_method_without_csrf_header_is_403(self, console):
        # 未带 X-Requested-With 的写请求一律 403（包括 login 本身）
        status, _, _ = console.request(
            "/api/auth/login", method="POST",
            body={"password": console.password}, use_cookie=False, csrf=False)
        assert status == 403
        console.login()
        status, _, _ = console.request(
            "/api/control/action", method="POST", body={"action": "status"}, csrf=False)
        assert status == 403

    def test_csrf_wrong_value_is_403(self, console):
        status, _, _ = console.request(
            "/api/auth/login", method="POST", body={"password": "x"},
            use_cookie=False, csrf=False, headers={"X-Requested-With": "fetch"})
        assert status == 403

    def test_read_endpoints_exempt_from_csrf(self, console):
        console.login()
        status, _, _ = console.get("/api/status", csrf=False)
        assert status == 200

    def test_head_serves_headers_without_body_after_auth(self, console):
        console.login()
        request = urllib.request.Request(console.base + "/api/auth/whoami",
                                         headers={"Cookie": console.cookie})
        request.get_method = lambda: "HEAD"  # type: ignore[method-assign]
        with smart_urlopen(request, timeout=10) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "application/json; charset=utf-8"
            assert resp.read() == b""

    def test_ab_proxy_route_gone_after_login(self, console):
        """矩阵适配项：v2 删除的 AstrBot 代理路由登录后同样 410（不静默 404，
        且必须先过会话门——未登录仍 401）。"""
        status, _, _ = console.get("/api/engine/ab/status", use_cookie=False)
        assert status == 401
        console.login()
        status, _, body = console.get("/api/engine/ab/status")
        assert status == 410
        payload = json.loads(body.decode("utf-8"))
        assert "已随 v2 移除" in payload["message"]
