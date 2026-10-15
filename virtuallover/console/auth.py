# -*- coding: utf-8 -*-
"""管理控制台 —— 会话认证（auth）。workbench/auth.py 移植（port-of-own）。

移植改动：仅删除 AstrBot 引擎密码托管段（get/set_astrbot_password）——
v2 已无 AstrBot 代理，console.json 不再落任何上游凭据。其余行为原样：
- local/console.json：首次启动生成随机 10 位控制台密码与 session_secret 写入；
  已存在则直接读（文件在 gitignored 的 local/ 本机信任域内）。
- 登录：对 console.json 密码做恒定时间比较；连续失败 5 次锁 60 秒（内存计数）。
- 会话：无状态 HMAC 签名令牌 ``vl_session=<payload>.<hmac_sha256(payload, secret)>``，
  payload 为 base64url(JSON {iat, exp})，有效期 8 小时；
  Cookie 属性 HttpOnly; SameSite=Strict; Path=/。
- 登出：下发 Max-Age=0 的清除 Cookie（无状态令牌不做服务端吊销）。
- 日志安全：密码/令牌绝不写日志全文（最多前 4 位）；完整密码仅首次生成时
  按规格打印一次到控制台 stdout，之后只提示见文件。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from . import core

logger = logging.getLogger("virtuallover.console.auth")

COOKIE_NAME = "vl_session"
SESSION_TTL_SECONDS = 8 * 3600
PASSWORD_LENGTH = 10
# 去掉易混淆字符（0/o/O/1/l/I）的字母表，随机 10 位
PASSWORD_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 60.0


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class LoginThrottle:
    """内存计数登录防爆破：连续失败 max_failures 次锁定 lock_seconds 秒。

    单进程全局计数（单人控制台）；锁定到期后清零重新计数，登录成功立即清零。
    """

    def __init__(
        self,
        max_failures: int = MAX_LOGIN_FAILURES,
        lock_seconds: float = LOGIN_LOCK_SECONDS,
        clock: "callable | None" = None,
    ) -> None:
        self.max_failures = int(max_failures)
        self.lock_seconds = float(lock_seconds)
        self.clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self.failures = 0
        self.locked_until = 0.0

    def check(self) -> None:
        """锁定中抛 ApiError(429)；锁定已到期则清零计数。"""
        with self._lock:
            now = self.clock()
            if self.locked_until:
                if now < self.locked_until:
                    raise core.ApiError(
                        429,
                        f"登录失败次数过多，已锁定，请约 {int(self.locked_until - now) + 1} 秒后重试",
                    )
                self.locked_until = 0.0
                self.failures = 0

    def on_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.max_failures:
                self.locked_until = self.clock() + self.lock_seconds
                self.failures = 0
                logger.warning("控制台密码连续错误达 %d 次，锁定 %d 秒",
                               self.max_failures, int(self.lock_seconds))

    def on_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.locked_until = 0.0

    @property
    def is_locked(self) -> bool:
        with self._lock:
            return bool(self.locked_until) and self.clock() < self.locked_until


class AuthManager:
    """console.json 存取 + 会话签发/校验。线程安全。"""

    def __init__(self, console_path: "Path | str", clock: "callable | None" = None) -> None:
        self.path = Path(console_path)
        self.clock = clock if clock is not None else time.time
        self.throttle = LoginThrottle(clock=clock)
        self._lock = threading.RLock()
        self._data: "dict | None" = None
        self._newly_created = False

    # ---------------- console.json 存取 ----------------

    def load(self) -> dict:
        """读取（或首次生成）console.json，返回内部 dict。

        文件缺失/损坏/字段非法 → 重新生成并覆盖写入（自愈）。
        """
        with self._lock:
            if self._data is not None:
                return self._data
            data = self._read_file()
            if data is None:
                data = {
                    "password": self._generate_password(),
                    "session_secret": secrets.token_hex(32),
                }
                self._newly_created = True
                self._write(data)
                logger.info("已生成控制台凭据文件 %s", self.path)
            self._data = data
            return data

    def _read_file(self) -> "dict | None":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        password = raw.get("password")
        secret = raw.get("session_secret")
        if not isinstance(password, str) or not password:
            return None
        if not isinstance(secret, str) or not secret:
            return None
        return dict(raw)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _generate_password() -> str:
        return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))

    @property
    def newly_created(self) -> bool:
        return self._newly_created

    def announce_password(self) -> None:
        """首次生成时把完整密码打印到控制台一次；已存在时只打码提示。

        打印后闭锁 _newly_created，同一进程内绝不二次打印完整密码。
        """
        data = self.load()
        if self._newly_created:
            self._newly_created = False
            print("=" * 64)
            print("管理控制台首次启动，已生成随机密码（也已写入 %s）：" % self.path)
            print("    控制台密码: %s" % data["password"])
            print("请妥善保存；丢失可删除该文件后重启重新生成。")
            print("=" * 64)
        else:
            logger.info(
                "控制台密码见 %s（前 4 位 %s****，日志不打全文）",
                self.path,
                str(data["password"])[:4],
            )

    # ---------------- 登录校验 ----------------

    def verify_login(self, password: str) -> bool:
        """校验密码：恒定时间比较；连续失败计入锁定。"""
        if not isinstance(password, str):
            password = ""
        stored = self.load()["password"]
        self.throttle.check()
        ok = hmac.compare_digest(
            stored.encode("utf-8"),
            password.encode("utf-8", errors="replace"),
        )
        if ok:
            self.throttle.on_success()
        else:
            self.throttle.on_failure()
        return ok

    # ---------------- 会话令牌 ----------------

    def _sign(self, payload_b64: bytes) -> str:
        secret = self.load()["session_secret"].encode("utf-8")
        return hmac.new(secret, payload_b64, hashlib.sha256).hexdigest()

    def issue_token(self) -> str:
        """签发 ``<payload_b64>.<hex_sig>``，有效期 SESSION_TTL_SECONDS。"""
        now = int(self.clock())
        payload = json.dumps(
            {"iat": now, "exp": now + SESSION_TTL_SECONDS},
            separators=(",", ":"),
        ).encode("utf-8")
        payload_b64 = _b64url_encode(payload)
        return payload_b64 + "." + self._sign(payload_b64.encode("ascii"))

    def verify_token(self, token: "str | None") -> bool:
        """校验令牌签名与有效期；任何结构异常一律 False。"""
        data = self.load()
        if not token or token.count(".") != 1:
            return False
        payload_b64, sig = token.split(".")
        try:
            expected = self._sign(payload_b64.encode("ascii")).encode("ascii")
        except (UnicodeEncodeError, ValueError):
            return False
        if not hmac.compare_digest(expected, sig.encode("utf-8", errors="replace")):
            return False
        try:
            payload = json.loads(_b64url_decode(payload_b64))
            exp = payload["exp"]
        except (ValueError, KeyError, TypeError):
            return False
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return False
        return self.clock() < float(exp)

    # ---------------- Cookie ----------------

    @staticmethod
    def parse_session_cookie(cookie_header: "str | None") -> "str | None":
        """从 Cookie 请求头提取 vl_session 值。"""
        if not cookie_header:
            return None
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value
        return None

    def verify_cookie(self, cookie_header: "str | None") -> bool:
        return self.verify_token(self.parse_session_cookie(cookie_header))

    def login_cookie_header(self) -> str:
        """登录成功后的 Set-Cookie 值（HttpOnly; SameSite=Strict; Path=/）。"""
        return (
            f"{COOKIE_NAME}={self.issue_token()}; "
            f"HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SECONDS}"
        )

    def logout_cookie_header(self) -> str:
        """登出时的清除 Set-Cookie 值。"""
        return f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
