# -*- coding: utf-8 -*-
"""管理控制台 —— NapCat 引擎代理（engine）。workbench/engine.py 移植（port-of-own）。

移植改动：**删除 AstrBot 代理段全部**（ab_login / ab_proxy / ab_status / JWT
缓存 / 托管密码重登），console.json 的 astrbot_password 托管随之移除——v2 已无
AstrBot 上游。NapCat 代理（nc_*）原样保留：

协议端（默认 http://127.0.0.1:61990——compose 中性端口宿主侧，vl.toml [console].nc_base 可配；容器内恒 6099，契约见 workbench/ENGINE_CONTRACT.md §2）：
- 用 webui.json 的 token 计算 sha256(token + '.napcat') 登录拿 Credential，
  缓存至过期前 5 分钟（有效期 3600 秒）；
- /api/engine/gw/<path>：透传到 <nc_base>/api/<path>，附 Bearer Credential
  （R6 路由段中性化 nc→gw）。

透传安全（硬约束，沿用 v2）：
- 方法白名单（nc: GET/POST）；
- 请求体上限 2MB 且必须是合法 JSON；响应体上限 5MB 且内容类型必须是 application/json；
- 上游超时 10 秒；上游不可达返回 502；
- 上游请求由本模块从零构造，绝不转发控制台的 Cookie/会话令牌。

日志安全：token/Credential 绝不写日志全文（最多前 4 位）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import core
from ..net import smart_urlopen as _default_urlopen

logger = logging.getLogger("virtuallover.console.engine")

# R7 中性端口（FUSION_SPEC §3.2）：compose 宿主侧 61990 → 容器 6099——端口表
# 不再出现上游文档默认端口 6099。可用 vl.toml [console].nc_base 覆盖（回退老
# 映射/自定义端口时改配置即可，不必改代码）。
DEFAULT_NC_BASE = "http://127.0.0.1:61990"
NC_LOGIN_PATH = "/api/auth/login"
NC_METHODS = frozenset({"GET", "POST"})

UPSTREAM_TIMEOUT_SECONDS = 10.0
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024   # 2MB
MAX_RESPONSE_BODY_BYTES = 5 * 1024 * 1024  # 5MB

NC_CREDENTIAL_TTL_SECONDS = 3600.0    # 契约：Credential 有效期 1 小时
NC_REFRESH_MARGIN_SECONDS = 300.0     # 过期前 5 分钟强制刷新

JSON_MEDIA_TYPE = "application/json"


class UpstreamUnavailable(Exception):
    """上游连接失败/超时（由调用方转成 502 ApiError）。"""


def _media_type(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def _extract_upstream_message(payload: object) -> "str | None":
    """取上游错误消息（NapCat 契约：{code, message}；AstrBot 时代的 {message} 同形）。"""
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return None


class EngineProxy:
    """NapCat 反向代理。线程安全；上游地址与 urlopen 可注入以便测试。"""

    def __init__(
        self,
        napcat_config_path: "Path | str",
        nc_base: str = DEFAULT_NC_BASE,
        timeout: float = UPSTREAM_TIMEOUT_SECONDS,
        urlopen=_default_urlopen,  # NapCat 上游 127.0.0.1:300x 回环直连（net.py）
        clock: "callable | None" = None,
    ) -> None:
        self.napcat_config_path = Path(napcat_config_path)
        self.nc_base = nc_base.rstrip("/")
        self.timeout = float(timeout)
        self._open = urlopen
        self.clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self.nc_credential: "str | None" = None
        self.nc_credential_at: float = 0.0

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _http(
        self,
        method: str,
        url: str,
        *,
        body: "bytes | None" = None,
        headers: "dict | None" = None,
    ) -> "tuple[int, str, bytes]":
        """请求上游，返回 (status, media_type, body)。网络层异常 → UpstreamUnavailable。"""
        request = urllib.request.Request(
            url, data=body, headers=dict(headers or {}), method=method
        )
        try:
            with self._open(request, timeout=self.timeout) as resp:
                payload = resp.read(MAX_RESPONSE_BODY_BYTES + 1)
                status = int(getattr(resp, "status", None) or resp.getcode())
                content_type = resp.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read(MAX_RESPONSE_BODY_BYTES + 1)
            except Exception:  # noqa: BLE001 —— 错误响应体读不动就按空处理
                payload = b""
            status = int(exc.code)
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        except core.ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 —— URLError/超时/连接拒绝等一律不可达
            raise UpstreamUnavailable(str(exc) or type(exc).__name__) from exc
        if len(payload) > MAX_RESPONSE_BODY_BYTES:
            raise core.ApiError(502, "上游响应体超过 5MB 上限，已拒绝透传")
        return status, _media_type(content_type), payload

    @staticmethod
    def _build_url(base: str, subpath: str, query: str) -> str:
        """拼上游 URL：子路径保留原始百分号编码原样转发；拒绝控制字符。"""
        if not subpath:
            raise core.ApiError(404, "not found")
        if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in subpath + (query or "")):
            raise core.ApiError(400, "代理路径包含非法字符")
        return base + "/api/" + subpath + ("?" + query if query else "")

    @staticmethod
    def _validate_request_body(body: bytes) -> bytes:
        """请求体上限 2MB 且必须是合法 JSON（只透传 JSON 内容类型）。"""
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise core.ApiError(413, "请求体超过 2MB 上限")
        if not body:
            return b""
        try:
            json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise core.ApiError(400, "代理请求体必须是合法 JSON") from None
        return body

    @staticmethod
    def _parse_json(payload: bytes, what: str) -> object:
        try:
            return json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise core.ApiError(502, f"{what}响应不是合法 JSON") from None

    @staticmethod
    def _require_json(media: str) -> None:
        """响应内容类型必须是 application/json（空类型按 JSON 放行）。"""
        if media and media != JSON_MEDIA_TYPE:
            raise core.ApiError(502, f"上游返回非 JSON 内容类型（{media}），已拒绝透传")

    # ------------------------------------------------------------------
    # NapCat
    # ------------------------------------------------------------------

    def _nc_credential_fresh(self) -> bool:
        with self._lock:
            if not self.nc_credential:
                return False
            age = self.clock() - self.nc_credential_at
            return age < NC_CREDENTIAL_TTL_SECONDS - NC_REFRESH_MARGIN_SECONDS

    def _nc_login(self) -> str:
        """sha256(token + '.napcat') 登录 NapCat WebUI，返回并缓存 Credential。"""
        token = core.read_napcat_token(self.napcat_config_path)
        if not token:
            message = "NapCat token 未配置（local/napcat_config/webui.json）"
            raise core.ApiError(502, message, payload={"status": "error", "message": message})
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        body = json.dumps({"hash": digest}).encode("utf-8")
        try:
            status, media, payload = self._http(
                "POST", self.nc_base + NC_LOGIN_PATH,
                body=body, headers={"Content-Type": JSON_MEDIA_TYPE},
            )
        except UpstreamUnavailable as exc:
            raise core.ApiError(502, f"NapCat 服务不可达：{exc}") from exc
        if media and media != JSON_MEDIA_TYPE:
            raise core.ApiError(502, f"NapCat 登录返回非 JSON 内容类型（{media}）")
        data = self._parse_json(payload, "NapCat 登录")
        credential = None
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            value = data["data"].get("Credential")
            credential = value if isinstance(value, str) and value else None
        if status != 200 or not credential:
            raise core.ApiError(
                502,
                f"NapCat WebUI 登录失败：{_extract_upstream_message(data) or '未知错误'}",
                payload={
                    "status": "error",
                    "message": f"NapCat WebUI 登录失败：{_extract_upstream_message(data) or '未知错误'}",
                },
            )
        with self._lock:
            self.nc_credential = credential
            self.nc_credential_at = self.clock()
        logger.info("NapCat WebUI 登录成功（token 前 4 位 %s***）", token[:4])
        return credential

    def _nc_ensure_credential(self, force: bool = False) -> str:
        if not force and self._nc_credential_fresh():
            with self._lock:
                return self.nc_credential  # type: ignore[return-value]
        return self._nc_login()

    def nc_proxy(self, subpath: str, query: str, method: str, body: bytes) -> "tuple[int, bytes]":
        """/api/engine/gw/<path> 通用透传（HTTP 401 时强制刷新凭据重试一次）。"""
        if method not in NC_METHODS:
            raise core.ApiError(405, "NapCat 代理仅允许 GET/POST")
        body = self._validate_request_body(body)
        url = self._build_url(self.nc_base, subpath, query)
        credential = self._nc_ensure_credential()
        headers = {"Authorization": f"Bearer {credential}"}
        if body:
            headers["Content-Type"] = JSON_MEDIA_TYPE
        try:
            status, media, payload = self._http(method, url, body=body or None, headers=headers)
        except UpstreamUnavailable as exc:
            raise core.ApiError(502, f"NapCat 服务不可达：{exc}") from exc
        if status == 401:
            credential = self._nc_ensure_credential(force=True)
            headers["Authorization"] = f"Bearer {credential}"
            try:
                status, media, payload = self._http(method, url, body=body or None, headers=headers)
            except UpstreamUnavailable as exc:
                raise core.ApiError(502, f"NapCat 服务不可达：{exc}") from exc
        self._require_json(media)
        return status, payload
