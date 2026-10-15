# -*- coding: utf-8 -*-
"""管理控制台 —— HTTP 入口（app）。workbench/app.py 移植（port-of-own）。

移植改动（相对 workbench/app.py）：
- 相对包导入（virtuallover.console 子包，`python -m virtuallover.console.app` 可独立运行）；
- **删除 AstrBot 代理路由**（/api/engine/ab/* 一律 410 Gone 语义 JSON，提示已移除）；
- /api/status 改进程内探测：经 server.status_provider（console.setup 注入，
  core.vl_status 查本进程各模块）；
- 服务控制新增 napcat_switch / napcat_rollback（control.py 白名单内分发）；
- 语音页（VOICE_SPEC §3）：/api/voice/* 六端点经 server.voice_service
  （console/voice.py 逻辑层，setup 注入）分发。

三重门原样：会话认证 + X-Requested-With CSRF + 127.0.0.1 绑定。
- http.server.ThreadingHTTPServer + 自定义 BaseHTTPRequestHandler，路由手写分发；
  仅绑定 127.0.0.1（compose 只对本机发布）；
- 认证（auth.py）：除 POST /api/auth/login 与静态首页/资源外，
  所有 /api/* 必须带有效会话 Cookie；
- CSRF：写方法（POST/PUT/DELETE）必须带请求头 X-Requested-With: XMLHttpRequest，否则 403；
- JSON 响应统一 ensure_ascii=False、Content-Type application/json; charset=utf-8；
- 日志只记方法与路径（不含 query），绝不打印密码/token/会话令牌。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from . import auth as auth_module
from . import config_page as config_page_module
from . import control as control_module
from . import core
from . import engine as engine_module
from . import recent as recent_module
from virtuallover.brain import vision as vision_module
from . import voice as voice_module

logger = logging.getLogger("virtuallover.console.app")

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"


class ConsoleHTTPServer(ThreadingHTTPServer):
    """控制台 HTTP 服务器：关 SO_REUSEADDR（五轮修缮 R4 多副本隔离）。

    http.server 默认 allow_reuse_address=1；Windows 的 SO_REUSEADDR 语义比
    POSIX 激进——存活监听还占着端口时二次 bind 也能成功：双开 vl（或 git
    archive 出去的测试副本）静默共存，请求被先占者接走，端口冲突排查线索
    全无。关掉后端口被占 → bind 直接 OSError（Address already in use）
    fail-fast，与「6199/8787 被占用」的既排障口径一致。
    """

    allow_reuse_address = False

# /static/ 静态资源白名单（前端单页通常内联，这里仅作兜底）
STATIC_CONTENT_TYPES = {
    ".html": HTML_CONTENT_TYPE,
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": JSON_CONTENT_TYPE,
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}

CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "XMLHttpRequest"
WRITE_METHODS = frozenset({"POST", "PUT", "DELETE"})
READ_METHODS = frozenset({"GET"})

# 全 API 请求体上限与引擎透传一致（2MB）；登录/控制动作远小于此
MAX_API_BODY_BYTES = engine_module.MAX_REQUEST_BODY_BYTES
# 超限时最多排空的字节数：小超额读完再回 413（客户端能读到响应），
# 大超额直接断连（不无限消耗带宽）
MAX_DRAIN_BYTES = 8 * 1024 * 1024

NC_PREFIX = "/api/engine/gw/"   # R6 中性化（原 /api/engine/nc/；gw=gateway 协议端网关）
AB_GONE_PREFIX = "/api/engine/ab/"
AB_GONE_MESSAGE = "旧版框架代理已随 v2 移除（v2 为独立框架，SPEC §10）；模型与人格改由 vl.toml 维护"

CHUNK_SIZE = 65536


def _dumps(payload: dict) -> bytes:
    """契约序列化：ensure_ascii=False + UTF-8。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ConsoleHandler(BaseHTTPRequestHandler):
    """管理控制台请求处理器。依赖经 server.cfg / server.auth / server.engine /
    server.control / server.status_provider 注入。"""

    server_version = "VLConsole/2.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # 方法分发
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    # HEAD：同样的路由与响应头，不写包体（_send_raw 内按 command 判断）
    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _reject_method(self) -> None:
        # 方法白名单之外（PATCH/OPTIONS 等）一律 405 JSON，不进任何代理；
        # 未读请求体，断开连接防止 keep-alive 下的字节流错位
        self.close_connection = True
        self._send_json(405, {"error": "method not allowed"})

    do_PATCH = _reject_method
    do_OPTIONS = _reject_method

    def _dispatch(self, method: str) -> None:
        if method not in WRITE_METHODS and self.headers.get("Content-Length") not in (None, "0"):
            # 带请求体的 GET/HEAD 不读取：直接断开，防止残留体污染 keep-alive
            self.close_connection = True
        parts = urllib.parse.urlsplit(self.path)
        raw_path, query = parts.path, parts.query
        path = urllib.parse.unquote(raw_path)
        try:
            result = self._handle(method, path, raw_path, query)
        except core.ApiError as exc:
            payload = exc.payload if exc.payload is not None else {"error": exc.message}
            self._send_json(exc.status, payload)
        except Exception as exc:  # noqa: BLE001 —— 兜底：未预期异常转 500，绝不断连
            logger.exception("处理 %s %s 时发生未预期异常", method, path)
            self._send_json(500, {"error": str(exc)})
        else:
            status, content_type, body, extra_headers = result
            self._send_raw(status, content_type, body, extra_headers)

    def _handle(
        self, method: str, path: str, raw_path: str, query: str
    ) -> "tuple[int, str, bytes, list | None]":
        # 写方法先读体（保持连接一致），再过 CSRF 门，再过认证门
        body = self._read_body() if method in WRITE_METHODS else b""
        if method in WRITE_METHODS and self.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
            raise core.ApiError(403, f"缺少 {CSRF_HEADER}: {CSRF_HEADER_VALUE} 请求头（CSRF 防护）")
        # 认证门：/api/* 除 POST /api/auth/login 与 GET /api/auth/hint 外全部需要有效会话
        # hint：仅绑 127.0.0.1（localhost 信任域），返回当前密码供登录页展示——
        # 个人本机工具，密码文件本就对同机进程可读，此处只是免开文件
        if path.startswith("/api/") and path not in ("/api/auth/login", "/api/auth/hint"):
            if not self.server.auth.verify_cookie(self.headers.get("Cookie")):  # type: ignore[attr-defined]
                raise core.ApiError(401, "未登录或会话已过期")
        return self._route(method, path, raw_path, query, body)

    # ------------------------------------------------------------------
    # 请求体
    # ------------------------------------------------------------------

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            # 不支持分块传输：拒绝并断开，避免连接状态不可知
            self.close_connection = True
            raise core.ApiError(411, "不支持 Transfer-Encoding，请使用 Content-Length")
        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            return b""
        try:
            length = int(length_raw)
        except ValueError:
            self.close_connection = True
            raise core.ApiError(400, "Content-Length 非法") from None
        if length < 0:
            self.close_connection = True
            raise core.ApiError(400, "Content-Length 非法")
        if length > MAX_API_BODY_BYTES:
            if length <= MAX_DRAIN_BYTES:
                # 有限排空后正常应答，客户端能读到 413
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, CHUNK_SIZE))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True  # 太大不排空，直接断开
            raise core.ApiError(413, f"请求体超过 {MAX_API_BODY_BYTES} 字节上限")
        chunks: "list[bytes]" = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, CHUNK_SIZE))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _json_object(body: bytes) -> dict:
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise core.ApiError(400, "请求体必须是合法 JSON") from None
        if not isinstance(payload, dict):
            raise core.ApiError(400, "请求体必须是 JSON 对象")
        return payload

    # ------------------------------------------------------------------
    # 路由表（手写分发，返回 (status, content_type, body, extra_headers)）
    # ------------------------------------------------------------------

    def _route(
        self, method: str, path: str, raw_path: str, query: str, body: bytes
    ) -> "tuple[int, str, bytes, list | None]":
        cfg = self.server.cfg  # type: ignore[attr-defined]
        if path == "/":
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            return (*self._serve_index(cfg), None)
        if path.startswith("/static/"):
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            return (*self._serve_static(cfg, path[len("/static/"):]), None)

        # ---- 认证（login 免会话；whoami/logout 已过认证门） ----
        if path == "/api/auth/login":
            if method != "POST":
                raise core.ApiError(404, "not found")
            return self._handle_login(body)
        if path == "/api/auth/hint":
            # 登录页密码提示（仅 localhost 信任域，个人本机工具免开文件）
            if method != "GET":
                raise core.ApiError(404, "not found")
            password = self.server.auth.load().get("password", "")  # type: ignore[attr-defined]
            return self._json_ok({"password": password})
        if path == "/api/auth/whoami":
            if method != "GET":
                raise core.ApiError(404, "not found")
            return self._json_result(200, {"ok": True})
        if path == "/api/auth/logout":
            if method != "POST":
                raise core.ApiError(404, "not found")
            cookie = self.server.auth.logout_cookie_header()  # type: ignore[attr-defined]
            return self._json_result(200, {"ok": True}, [("Set-Cookie", cookie)])

        # ---- 只读端点（GET）：status 走进程内探测，其余读 local/ 数据文件 ----
        if method in READ_METHODS:
            if path == "/api/status":
                provider = getattr(self.server, "status_provider", None)
                if provider is None:
                    raise core.ApiError(503, "状态探测未装配（status_provider 为空）")
                return self._json_ok(provider())
            if path == "/api/mood":
                return self._json_ok(core.mood_payload(cfg.data_dir))
            if path == "/api/diary":
                return self._json_ok(core.list_diary(cfg.data_dir))
            if path.startswith("/api/diary/"):
                return self._json_ok(core.read_diary(cfg.data_dir, path[len("/api/diary/"):]))
            if path == "/api/eval":
                return self._json_ok(core.list_eval(cfg.data_dir))
            if path.startswith("/api/eval/"):
                return self._json_ok(core.read_eval(cfg.data_dir, path[len("/api/eval/"):]))
            # ---- 我们的回忆页（只读聚合：锚点/里程碑/消息计数/首次语音/日记足迹） ----
            if path == "/api/memories":
                runtime = getattr(self.server, "runtime", None)
                return self._json_ok(core.memories_payload(
                    cfg.data_dir, cfg.vl_data_dir,
                    getattr(runtime, "history", None)))
            # ---- 最近事件（五轮修缮 R2）：进程内环形缓冲，空则退 vl.log 尾部 ----
            if path == "/api/recent-errors":
                return self._json_ok(recent_module.recent_events_payload(cfg.log_path))

        # ---- 测她一句话（五轮修缮 R2）：真 1-token 对话探针（写方法过 CSRF 门） ----
        if path == "/api/brain/ping":
            if method != "POST":
                raise core.ApiError(404, "not found")
            return self._json_ok(core.brain_ping_payload(getattr(self.server, "runtime", None)))


        # ---- QQ 运营调试 ----
        if path == "/api/qq/stream":
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            return self._json_ok(core.qq_stream_payload(getattr(self.server, "runtime", None)))
        if path == "/api/qq/stats":
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            return self._json_ok(core.qq_stats_payload(getattr(self.server, "runtime", None)))

        # ---- 控制台直接聊天（UX 审计 #1 缺失）----
        if path == "/api/brain/chat":
            if method != "POST":
                raise core.ApiError(404, "not found")
            payload = self._json_object(body)
            message = str(payload.get("message", "")).strip()
            if not message:
                raise core.ApiError(400, "message 不能为空")
            # 长度门只校验文本部分：粘贴图片 base64（多模态③）必然远超
            # 2000 字符；图片引用提取是纯函数，body 总量另有 2MiB 上限兜底
            text_only, _refs = vision_module.extract_image_refs(message)
            if len(text_only or message) > 2000:
                raise core.ApiError(400, "message 超过 2000 字符上限")
            return self._json_ok(core.brain_chat_payload(
                getattr(self.server, "runtime", None), message))

        # ---- 智能降级观测（五轮修缮第二轮）：黏性状态/兜底计数/最近 LLM 错误 ----
        if path == "/api/brain/status":
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            return self._json_ok(core.brain_status_payload(getattr(self.server, "runtime", None)))

        # ---- 配置页（五轮修缮 R1）：GET 打码读 / PUT 金样板写，三重门自动继承 ----
        if path.startswith("/api/config/"):
            section = path[len("/api/config/"):]
            if method in READ_METHODS:
                return self._json_ok(self.server.config_service.get_section(section))  # type: ignore[attr-defined]
            if method == "PUT":
                payload = self.server.config_service.put_section(  # type: ignore[attr-defined]
                    section, self._json_object(body))
                return self._json_ok(payload)
            raise core.ApiError(404, "not found")

        # ---- 记忆检索只读面（GET）：q 必填非空，登录门随 /api/* 继承 ----
        if path == "/api/memory/list":
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            params = urllib.parse.parse_qs(query)
            q = (params.get("q") or [""])[0]
            return self._json_ok(control_module.memory_search_payload(
                getattr(self.server, "runtime", None), q))

        # ---- 对话回放只读面（R9：GET /api/history/list?date=&page=，
        #      三重门随 /api/* 继承，分页 ≤200 条/页） ----
        if path == "/api/history/list":
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            params = urllib.parse.parse_qs(query)
            return self._json_ok(control_module.history_day_payload(
                getattr(self.server, "runtime", None), params))

        # ---- 服务控制（协议端指向切换/回退、大脑切换动作在 control 白名单内） ----
        if path == "/api/control/action":
            if method != "POST":
                raise core.ApiError(404, "not found")
            status, payload = self.server.control.dispatch(self._json_object(body))  # type: ignore[attr-defined]
            return self._json_result(status, payload)

        # ---- 语音页（VOICE_SPEC §3）：三重门随 /api/* 全局继承，不另设旁路 ----
        if path == "/api/voice/status":
            if method not in READ_METHODS:
                raise core.ApiError(404, "not found")
            return self._json_ok(self.server.voice_service.status())  # type: ignore[attr-defined]
        if path == "/api/voice/preview":
            if method != "POST":
                raise core.ApiError(404, "not found")
            result = self.server.voice_service.preview(self._json_object(body))  # type: ignore[attr-defined]
            # 试听响应为整段 base64 JSON（VOICE_SPEC §3）：明示不支持分段请求
            return self._json_result(200, result, [("Accept-Ranges", "none")])
        if path == "/api/voice/start":
            if method != "POST":
                raise core.ApiError(404, "not found")
            return self._json_ok(self.server.voice_service.start())  # type: ignore[attr-defined]
        if path == "/api/voice/stop":
            if method != "POST":
                raise core.ApiError(404, "not found")
            return self._json_ok(self.server.voice_service.stop())  # type: ignore[attr-defined]
        if path == "/api/voice/emotion-map":
            if method in READ_METHODS:
                return self._json_ok(self.server.voice_service.get_emotion_map())  # type: ignore[attr-defined]
            if method == "PUT":
                payload = self.server.voice_service.put_emotion_map(self._json_object(body))  # type: ignore[attr-defined]
                return self._json_ok(payload)
            raise core.ApiError(404, "not found")
        if path == "/api/voice/trigger-probability":
            # R3-2e：语音触发概率旋钮（真实生效——运行时热改 + vl.toml 持久化）
            if method in READ_METHODS:
                return self._json_ok(self.server.voice_service.get_trigger_probability())  # type: ignore[attr-defined]
            if method == "PUT":
                payload = self.server.voice_service.set_trigger_probability(  # type: ignore[attr-defined]
                    self._json_object(body))
                return self._json_ok(payload)
            raise core.ApiError(404, "not found")
        if path == "/api/voice/fix-base-url":
            if method != "POST":
                raise core.ApiError(404, "not found")
            return self._json_ok(self.server.voice_service.fix_base_url())  # type: ignore[attr-defined]

        # ---- 协议端代理（QR 扫码重登面，SPEC §10 保留） ----
        if raw_path.startswith(NC_PREFIX):
            status, payload = self.server.engine.nc_proxy(  # type: ignore[attr-defined]
                raw_path[len(NC_PREFIX):], query, method, body
            )
            return status, JSON_CONTENT_TYPE, payload, None

        # ---- 旧版框架代理：v2 已移除，410 语义明确回执（不静默 404） ----
        if raw_path.startswith(AB_GONE_PREFIX):
            raise core.ApiError(
                410, AB_GONE_MESSAGE,
                payload={"status": "error", "message": AB_GONE_MESSAGE},
            )

        raise core.ApiError(404, "not found")

    def _handle_login(self, body: bytes) -> "tuple[int, str, bytes, list | None]":
        payload = self._json_object(body)
        password = payload.get("password")
        if not isinstance(password, str) or not password:
            raise core.ApiError(400, "缺少 password 字段")
        if not self.server.auth.verify_login(password):  # type: ignore[attr-defined]
            return self._json_result(401, {"ok": False, "error": "密码错误"})
        cookie = self.server.auth.login_cookie_header()  # type: ignore[attr-defined]
        return self._json_result(200, {"ok": True}, [("Set-Cookie", cookie)])

    def _json_ok(self, payload: dict) -> "tuple[int, str, bytes, list | None]":
        return self._json_result(200, payload)

    def _json_result(
        self, status: int, payload: dict, extra_headers: "list | None" = None
    ) -> "tuple[int, str, bytes, list | None]":
        return status, JSON_CONTENT_TYPE, _dumps(payload), extra_headers

    # ------------------------------------------------------------------
    # 响应发送
    # ------------------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        self._send_raw(status, JSON_CONTENT_TYPE, _dumps(payload))

    def _send_raw(
        self,
        status: int,
        content_type: str,
        body: bytes,
        extra_headers: "list | None" = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ------------------------------------------------------------------
    # 静态文件
    # ------------------------------------------------------------------

    def _serve_index(self, cfg: core.AppConfig) -> "tuple[int, str, bytes]":
        try:
            body = (Path(cfg.static_dir) / "index.html").read_bytes()
        except OSError:
            raise core.ApiError(404, "not found") from None
        return 200, HTML_CONTENT_TYPE, body

    def _serve_static(self, cfg: core.AppConfig, name: str) -> "tuple[int, str, bytes]":
        # 仅允许 static/ 的直接子文件：含分隔符或以 . 开头一律拒绝，杜绝目录穿越
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise core.ApiError(404, "not found")
        content_type = STATIC_CONTENT_TYPES.get(Path(name).suffix.lower())
        if content_type is None:
            raise core.ApiError(404, "not found")
        try:
            body = (Path(cfg.static_dir) / name).read_bytes()
        except OSError:
            raise core.ApiError(404, "not found") from None
        return 200, content_type, body

    # ------------------------------------------------------------------
    # 日志：每请求一行，只记路径不含 query（密码/token 类参数绝不入日志）
    # ------------------------------------------------------------------

    def log_request(self, code: "int | str" = "-", size: "int | str" = "-") -> None:
        self.log_message("%s %s %s", self.command,
                         urllib.parse.urlsplit(self.path).path, code)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 —— 基类签名如此
        logger.info("%s - %s", self.address_string(), format % args)


def make_server(
    host: str,
    port: int,
    cfg: core.AppConfig,
    auth: "auth_module.AuthManager | None" = None,
    engine: "engine_module.EngineProxy | None" = None,
    control: "control_module.ControlService | None" = None,
    status_provider: "callable | None" = None,
    voice_service: "voice_module.VoicePageService | None" = None,
    config_service: "config_page_module.ConfigPageService | None" = None,
    runtime: "object | None" = None,
) -> ThreadingHTTPServer:
    """构造 HTTP 服务器；依赖经 server 属性注入给 handler（测试可全部替换）。

    voice_service 缺省时构造 runtime=None 的语音页服务：/api/voice/* 如实降级
    （状态卡 not_started、动作 503），绝不 404 掩盖端点存在；config_service
    缺省时同样构造 runtime=None 的配置页服务（/api/config/* 如实降级 503）。
    runtime 仅供 /api/memory/list 读 memory_store 槽位（缺省 None → 503 降级）。
    """
    httpd = ConsoleHTTPServer((host, port), ConsoleHandler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    httpd.auth = auth if auth is not None else auth_module.AuthManager(cfg.console_path)  # type: ignore[attr-defined]
    httpd.engine = engine if engine is not None else engine_module.EngineProxy(  # type: ignore[attr-defined]
        napcat_config_path=cfg.napcat_config,
    )
    httpd.control = control if control is not None else control_module.ControlService(  # type: ignore[attr-defined]
        cfg.local_dir, cfg.backup_dir, core.CONSOLE_DIR.parents[1],
        vl_toml_path=cfg.vl_toml_path, napcat_config_dir=cfg.napcat_config_dir,
    )
    httpd.status_provider = status_provider  # type: ignore[attr-defined]
    httpd.voice_service = voice_service if voice_service is not None else (  # type: ignore[attr-defined]
        voice_module.VoicePageService(runtime=None)
    )
    httpd.config_service = config_service if config_service is not None else (  # type: ignore[attr-defined]
        config_page_module.ConfigPageService(runtime=None)
    )
    httpd.runtime = runtime  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def _record_pid(auth: "auth_module.AuthManager") -> None:
    """把当前进程 PID 写入 local/console.json 的 pid 字段（一键停止.bat 据此精准结束）。

    load() 返回的就是 AuthManager 内部缓存 dict 本体：就地补 pid 后整体重写，
    写失败仅告警，不影响控制台运行。
    """
    try:
        data = auth.load()
        if data.get("pid") != os.getpid():
            data["pid"] = os.getpid()
            auth._write(data)  # noqa: SLF001 —— 同包入口模块复用其落盘实现
    except OSError as exc:
        logger.warning("PID 写入 %s 失败（不影响控制台运行）: %s", auth.path, exc)


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def _napcat_dir_from_cfg(ctx, app_cfg: "core.AppConfig") -> Path:
    """[bridge].napcat_config_dir 接线（R5）：napcat_switch/rollback 改写
    onebot11_*.json 的目录跟随 vl.toml——此前该键由 init/migrate 写入但控制台
    一直用 core.AppConfig 硬拼的 local/napcat_config（键写了没人读的死键）。

    相对路径锚定仓库根；绝对路径 Path 拼接语义自然生效；旧桩配置（缺 bridge
    段）回退 core 默认（R3 proactive 装配对 SimpleNamespace 的同款宽容）。
    """
    bridge_dir = str(getattr(
        getattr(ctx.cfg, "bridge", None), "napcat_config_dir", "") or "")
    return (Path(ctx.repo_root) / bridge_dir) if bridge_dir else app_cfg.napcat_config_dir


def _vl_data_dir_from_cfg(ctx) -> "Path | None":
    """[storage].data_dir 接线：anniversary.json / first_voice.json 落点跟随
    vl.toml（路径解析与 __main__._anniversary_tick 的
    Path(repo_root) / storage.data_dir / "anniversary.json" 同源；相对路径锚定
    仓库根，空值返回 None → for_root 取默认 local/vl_data）。"""
    data_dir = str(getattr(
        getattr(ctx.cfg, "storage", None), "data_dir", "") or "")
    return (Path(ctx.repo_root) / data_dir) if data_dir else None


def setup(ctx) -> "Optional[Callable[[], None]]":
    """随主框架启动控制台：独立线程跑 ThreadingHTTPServer，退出时优雅关闭。

    - 端口占用：中文报错日志但不阻断主框架（管理面故障不应拖垮对话面）；
    - status_provider 闭包引用 ctx（AppContext 鸭类型），/api/status 实时反映
      本进程各模块现状；
    - 三重门随 app.py 原样生效：会话认证 + CSRF + 127.0.0.1 绑定。
    """
    console_cfg = ctx.cfg.console
    if not console_cfg.enable:
        logger.info("控制台未启用（console.enable=false），跳过装配")
        return None
    app_cfg = core.AppConfig.for_root(
        ctx.repo_root, port=console_cfg.port,
        vl_data_dir=_vl_data_dir_from_cfg(ctx))
    auth_mgr = auth_module.AuthManager(app_cfg.console_path)
    auth_mgr.load()
    auth_mgr.announce_password()
    # R7（FUSION_SPEC §3.2）：代理上游 nc_base 可配——vl.toml [console].nc_base
    # （默认 http://127.0.0.1:61990，compose 中性端口宿主侧）；空值回退模块默认
    engine_proxy = engine_module.EngineProxy(
        app_cfg.napcat_config,
        nc_base=str(getattr(console_cfg, "nc_base", "") or ""
                    ) or engine_module.DEFAULT_NC_BASE,
    )
    # [bridge].napcat_config_dir 接线（R5）：napcat_switch/rollback 改写 onebot11_*.json
    # 的目录跟随 vl.toml（此前用 core.AppConfig 硬拼 local/napcat_config，键写了没人读）
    control_svc = control_module.ControlService(
        app_cfg.local_dir, app_cfg.backup_dir, ctx.repo_root,
        vl_toml_path=app_cfg.vl_toml_path,
        napcat_config_dir=_napcat_dir_from_cfg(ctx, app_cfg),
    )

    def status_provider() -> dict:
        return core.vl_status(
            ctx, fetch_fn=app_cfg.fetch_fn, timeout=app_cfg.probe_timeout,
            brain_fetch_fn=app_cfg.brain_fetch_fn)

    # 语音页服务：请求时实时读 ctx.voice_manager（装配序在控制台之前，槽位
    # 缺失=语音框架未就绪，端点如实降级，见 console/voice.py 契约）
    voice_svc = voice_module.VoicePageService(runtime=ctx)
    # 配置页服务：GET 文件层优先（与 PUT 同基准）、降级内存快照；
    # /api/memory/list 经 server.runtime 实时读 ctx.memory_store
    config_svc = config_page_module.ConfigPageService(runtime=ctx)

    try:
        httpd = make_server(console_cfg.host, console_cfg.port, app_cfg,
                            auth=auth_mgr, engine=engine_proxy, control=control_svc,
                            status_provider=status_provider, voice_service=voice_svc,
                            config_service=config_svc, runtime=ctx)
    except OSError as exc:
        logger.error("控制台端口 %d 监听失败（不阻断主框架运行）：%s", console_cfg.port, exc)
        return None
    _record_pid(auth_mgr)
    thread = threading.Thread(target=httpd.serve_forever, name="vl-console", daemon=True)
    thread.start()
    logger.info("管理控制台已启动: http://%s:%d （数据目录: %s，仅本机可访问）",
                console_cfg.host, httpd.server_address[1], app_cfg.data_dir)

    def cleanup() -> None:
        try:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        except Exception:  # noqa: BLE001 —— 清理失败不阻断其余清理
            logger.exception("控制台关闭失败（忽略）")

    return cleanup


def main() -> int:
    """独立运行入口（python -m virtuallover.console.app）：默认仓库根为运行根。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repo_root = core.CONSOLE_DIR.parents[1]  # virtuallover/console -> virtuallover -> 仓库根
    cfg = core.AppConfig.for_root(repo_root)
    # 端口环境变量（五轮修缮 R4 正名）：WB_PORT 是 workbench 时代的遗留名，新名
    # VL_CONSOLE_PORT 优先；旧名仍被读取（兼容既有脚本/快捷方式），但仅出现旧名时
    # 打告警提示迁移。两处均非整数时回退 cfg.port 默认值。
    raw_port = os.environ.get("VL_CONSOLE_PORT") or os.environ.get("WB_PORT")
    if raw_port:
        try:
            cfg.port = int(raw_port)
        except ValueError:
            logger.warning("端口环境变量 %r 非整数，回退默认 %d", raw_port, cfg.port)
    if os.environ.get("WB_PORT") and not os.environ.get("VL_CONSOLE_PORT"):
        logger.warning("环境变量 WB_PORT 是 workbench 时代遗留名（仍兼容），"
                       "请改用 VL_CONSOLE_PORT")
    auth_mgr = auth_module.AuthManager(cfg.console_path)
    auth_mgr.load()
    auth_mgr.announce_password()
    try:
        httpd = make_server("127.0.0.1", cfg.port, cfg, auth=auth_mgr)
    except OSError as exc:
        logger.error("端口 %d 监听失败: %s", cfg.port, exc)
        return 1
    _record_pid(auth_mgr)
    logger.info(
        "管理控制台已启动: http://127.0.0.1:%d （数据目录: %s，仅本机可访问）",
        httpd.server_address[1], cfg.data_dir,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
