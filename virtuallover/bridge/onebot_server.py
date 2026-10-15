# -*- coding: utf-8 -*-
"""OneBot v11 反向 WebSocket 服务端（NapCat → 本框架，SPEC §3 九条契约）。

本模块是整个框架唯一的外部消息入口：NapCat 容器作为反向 WS 客户端主动
连入本进程，事件由此分发、回复由此发出。九条硬性契约逐条落实如下：

1. ``websockets`` 库反向 WS 服务端，绑 **127.0.0.1:6199**，路径 ``/ws``；
2. ``Authorization: Bearer <token>`` **恒定时间比较**（hmac.compare_digest）；
   缺失→401、错误→403 语义断开（握手期 HTTP 状态码拒绝，连接根本不升级，
   鉴权先于任何 payload 处理，失败不回任何 action）；并校验 ``X-Self-ID``
   == 配置账号，缺失/不符→403；
3. token 生成走 ``generate_token()``（``secrets.token_urlsafe(32)``），由
   配置/迁移层落盘 local/，本模块**缺失 token 直接 fail-fast 拒启**（禁弱
   token 默认值）；
4. 单账号单连接：新连接顶掉旧连接（旧连接以 4000 关闭码断开）；
   ``max_size`` 默认 32 MiB（防大图 1009 断链）；``ping_interval=10`` 探测
   半开（协议层，与应用层心跳事件是两回事）；
5. ``post_type=meta_event``（heartbeat/lifecycle）只记日志不回复（NapCat
   心跳单向）；轮询类按部署规约降 DEBUG；
6. 事件 message 按 **array** 格式解析；段类型 text/face/at/reply/image/
   record（textutil.segments_to_plain），未知类型降级占位文本+日志；
   user_id/self_id/message_id/reply id 兼容 Number|String；
7. reply 段保留 ``reply_id`` 随事件上行；``get_msg`` 回查**不在帧分发路径
   内联执行**（PERF_SPEC §2-O4：回查随事件进 per-user 管线，≤10s 超时由
   管线负责）——回查未决期间后续帧照常接收、echo 照常路由、事件照常上抛；
8. 发送一律构造数组段，永不拼 CQ 码；语音/图片 =
   ``{type: record|image, data: {file: "base64://<b64>"}}``；QQ 小黄脸 =
   ``{type: face, data: {id: "<白名单 id>"}}``（faces.SEND_WHITELIST）；
   多条回复顺序发送（分段间隔由上层 persona 决定，本模块保证顺序）；
9. action 必带 ``echo`` 关联响应；10s 超时判失败；``retcode != 0`` 落告警。

边界规则（SPEC §3 末段）：群消息（message_type != private）记日志忽略；
白名单外 user_id 静默忽略（§8.6 门禁）；收语音 ``get_record(file=<收到的>,
out_format='mp3')`` 取 base64；媒体 URL 到达即后台转存 local/vl_data/media/。

实现参考：botuniverse/onebot-11 开放规范（clean-room，未读任何上游源码）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from virtuallover.bridge.textutil import (
    cq_to_plain,
    find_reply_id,
    segments_to_plain,
)
from virtuallover.net import smart_urlopen

logger = logging.getLogger("virtuallover.bridge.onebot_server")

# ---------------------------------------------------------------------------
# 常量与路径基准
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]  # virtuallover/bridge/ -> virtuallover/ -> 仓库根

#: 帧大小上限：32 MiB（SPEC §3 第 4 条，防大图触发 1009 断链）
MAX_FRAME_BYTES = 32 * 1024 * 1024
#: 应用层心跳探测间隔（秒）
DEFAULT_PING_INTERVAL = 10.0
#: action echo 关联超时（秒）
DEFAULT_ACTION_TIMEOUT = 10.0
#: 单条媒体转存大小上限（与帧上限对齐）
MAX_MEDIA_BYTES = 32 * 1024 * 1024
#: 媒体下载超时（秒）
MEDIA_DOWNLOAD_TIMEOUT = 30.0
#: 新连接顶掉旧连接时使用的自定义关闭码（4000-4999 应用保留区）
CLOSE_CODE_REPLACED = 4000

#: 媒体转存默认目录（数据在 local/，不属于代码资产）
DEFAULT_MEDIA_DIR = REPO_ROOT / "local" / "vl_data" / "media"

#: URL 后缀白名单之外的段类型默认扩展名
_MEDIA_DEFAULT_EXT = {"image": ".jpg", "record": ".mp3"}
_MEDIA_KNOWN_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp3", ".amr", ".wav", ".m4a", ".sil", ".mp4",
}

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]

# ---------------------------------------------------------------------------
# 配置接口（骨架组 config.py 并行开发中，集成期接线）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeSettings:
    """桥连接配置占位（try import 失败时的简单默认值，对齐 config.BridgeConfig）。"""

    token: str = ""
    self_id: str = ""
    host: str = "127.0.0.1"
    port: int = 6199
    path: str = "/ws"


def generate_token() -> str:
    """生成新 token（``secrets.token_urlsafe(32)``）。

    首次启动/迁移由配置层调用并落盘 local/（SPEC §3 第 3 条禁沿用弱 token）；
    本模块自身绝不产生默认 token。
    """
    return secrets.token_urlsafe(32)


def load_bridge_settings() -> BridgeSettings:
    """从 ``virtuallover.config`` 读取桥配置；骨架未就绪时返回空占位。

    读取接口（config.py 已定稿）：``load_config(仓库根/local/config/vl.toml)``
    → ``cfg.bridge.{token, self_id, host, port, path}``。任何一步不可用
    （模块缺失 / 文件未迁移 / 解析失败）一律返回空 BridgeSettings，由
    OneBotBridge 构造器 fail-fast 拒启——绝不弱默认。
    """
    try:
        from virtuallover.config import load_config
    except Exception:  # noqa: BLE001 - 骨架模块尚未建立属预期路径
        return BridgeSettings()
    path = REPO_ROOT / "local" / "config" / "vl.toml"
    if not path.exists():
        return BridgeSettings()
    try:
        cfg = load_config(path)
    except Exception:  # noqa: BLE001 - 配置损坏时交由上层 fail-fast
        return BridgeSettings()
    bridge_cfg = getattr(cfg, "bridge", None)
    if bridge_cfg is None:
        return BridgeSettings()
    return BridgeSettings(
        token=str(getattr(bridge_cfg, "token", "") or ""),
        self_id=str(getattr(bridge_cfg, "self_id", "") or ""),
        host=str(getattr(bridge_cfg, "host", "") or "127.0.0.1"),
        port=int(getattr(bridge_cfg, "port", 0) or 6199),
        path=str(getattr(bridge_cfg, "path", "") or "/ws"),
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionResult:
    """一次 action 调用的结果（echo 关联响应或超时/未连接的失败占位）。"""

    ok: bool
    status: str | None = None
    retcode: int | None = None
    data: Any = None
    echo: str | None = None


@dataclass
class _ConnCtx:
    """单条已鉴权连接的上下文：echo 等待表按连接隔离，防止串台。"""

    ws: ServerConnection
    pending: dict[str, "asyncio.Future[ActionResult]"] = field(default_factory=dict)
    #: 待处理 message 事件队列（无界：帧背压由 websockets max_queue 承担，
    #: 正常事件频率远低于处理能力，洪峰仅存在于 NapCat 异常重放场景）
    queue: "asyncio.Queue[dict[str, Any]]" = field(default_factory=asyncio.Queue)
    #: 事件处理 worker（读循环只做同步路由，事件串行在 worker 里跑，
    #: 这样 reply 回查/下游回调再慢也绝不阻塞后续帧的接收与 echo 路由）
    worker: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _id_to_str(value: Any) -> str:
    """Number|String 双形态 ID 统一为字符串（SPEC §3 第 6 条）。"""
    if value is None:
        return ""
    return str(value)


def _id_param(value: Any) -> Any:
    """action params 里的 ID 参数：纯数字串转 int（OneBot v11 规范为 number），
    非数字（异常数据）原样透传并容忍 NapCat 端的宽松解析。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    s = str(value)
    if s.isdigit():
        return int(s)
    return value


def _mask_id(value: str) -> str:
    """日志里对 QQ 号打码（对齐仓库日志规约：不输出敏感标识全文）。"""
    if not value:
        return "***"
    return value[:2] + "***"


def _seg_text(text: str) -> dict[str, Any]:
    """纯文本 → text 数组段（发送侧唯一入口，永不拼 CQ 码）。"""
    return {"type": "text", "data": {"text": text}}


def _seg_media_file(seg_type: str, b64: str) -> dict[str, Any]:
    """base64 媒体 → record/image 数组段（NapCat file.ts 核实的 base64:// 形态）。"""
    return {"type": seg_type, "data": {"file": f"base64://{b64}"}}


def _seg_face(face_id: int | str) -> dict[str, Any]:
    """QQ 小黄脸 id → face 数组段（规范形态 id 为字符串）。

    id 应取 faces.SEND_WHITELIST 白名单内的经典小黄脸（发送层用
    faces.extract_face_markers 从 LLM 回复剥出 ``{{face:id}}`` 标记后
    传入）；本函数只做段构造，不再校验白名单（白名单在标记剥出时把关）。
    """
    return {"type": "face", "data": {"id": str(int(face_id))}}


def _download_to(url: str, dest: Path) -> None:
    """同步下载（在线程池里跑）：媒体 URL 转存落盘，超限/失败由调用方处理。

    R7（FUSION_SPEC §3.4）：urlopen 换 net.smart_urlopen——代理修复的最后缺口。
    修前裸 urlopen 按系统代理出网：回环媒体源（本机 GSVI/本地服务产的媒体 URL）
    被系统代理劫持 404、且系统代理抽风时全部媒体转存连坐失败；smart_urlopen
    对回环/私网目标禁代理直连、公网目标保持系统默认（用户代理配置不受影响）。
    """
    request = urllib.request.Request(url, headers={"User-Agent": "virtuallover-bridge/1.0"})
    with smart_urlopen(request, timeout=MEDIA_DOWNLOAD_TIMEOUT) as resp:
        payload = resp.read(MAX_MEDIA_BYTES + 1)
    if len(payload) > MAX_MEDIA_BYTES:
        raise ValueError(f"媒体超过 {MAX_MEDIA_BYTES} 字节上限，放弃转存")
    dest.write_bytes(payload)


def _media_ext(url: str, seg_type: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in _MEDIA_KNOWN_EXTS:
        return ext
    return _MEDIA_DEFAULT_EXT.get(seg_type, ".bin")


# ---------------------------------------------------------------------------
# 桥服务端
# ---------------------------------------------------------------------------


class OneBotBridge:
    """反向 WS 服务端 + action 客户端二合一。

    生命周期（供 ``__main__`` 装配）::

        bridge = OneBotBridge(token=..., self_id=..., on_event=总线分发)
        async with asyncio.timeout(None):
            await bridge.serve_forever()      # 守护运行
        # 或测试场景： await bridge.start(); ...; await bridge.stop()

    收发对称：事件经 ``on_event(event)`` 异步回调上行；回复经
    ``send_private_*`` 系列下行，全部走 action/echo 往返。
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        self_id: str | None = None,
        host: str = "127.0.0.1",
        port: int = 6199,
        path: str = "/ws",
        on_event: EventCallback | None = None,
        whitelist: Collection[str] | None = None,
        max_size: int = MAX_FRAME_BYTES,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
        media_dir: Path | str | None = DEFAULT_MEDIA_DIR,
    ) -> None:
        if token is None or self_id is None:
            settings = load_bridge_settings()
            token = settings.token if token is None else token
            self_id = settings.self_id if self_id is None else self_id
        # SPEC §3 第 3 条：缺失 token 不允许弱默认，直接拒启
        if not token:
            raise ValueError(
                "OneBot 桥 token 为空：应由配置层提供 generate_token() 生成的强 token（fail-fast）"
            )
        if not self_id:
            raise ValueError("OneBot 桥 self_id 为空：应由配置层提供本机账号")
        self.token = token
        self.self_id = _id_to_str(self_id)
        self.host = host
        self.port = port
        self.path = path
        self.on_event = on_event
        self.whitelist = frozenset(_id_to_str(u) for u in whitelist) if whitelist is not None else None
        self.max_size = max_size
        self.ping_interval = ping_interval
        self.action_timeout = action_timeout
        self.media_dir = Path(media_dir) if media_dir is not None else None
        self._server: Server | None = None
        self._current: _ConnCtx | None = None
        self._bg_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """当前是否有已鉴权的 NapCat 连接。"""
        return self._current is not None

    @property
    def bound_port(self) -> int:
        """实际绑定端口（port=0 动态分配时供测试/日志读取）。"""
        if self._server is None:
            raise RuntimeError("OneBotBridge 未启动")
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        """启动监听（绑 127.0.0.1:6199；端口占用时 serve 直接抛 OSError，不静默）。"""
        if self._server is not None:
            raise RuntimeError("OneBotBridge 已启动")
        self._server = await serve(
            self._handler,
            self.host,
            self.port,
            process_request=self._process_request,
            max_size=self.max_size,
            ping_interval=self.ping_interval,
        )
        logger.info("OneBot 反向 WS 服务端已监听 %s:%s%s", self.host, self.port, self.path)

    async def stop(self) -> None:
        """停止监听并断开当前连接（测试/优雅退出用；幂等可重复调用）。"""
        server, self._server = self._server, None
        if server is not None:
            # websockets v17：Server.close() 是协程，必须 await（否则
            # "Server._close was never awaited" 且 wait_closed 永不完成）
            with contextlib.suppress(Exception):
                await server.close()
                await server.wait_closed()
        ctx, self._current = self._current, None
        if ctx is not None:
            with contextlib.suppress(Exception):
                await ctx.ws.close(code=1001, reason="server shutdown")
            _fail_pending(ctx, "server shutdown")
        for task in list(self._bg_tasks):
            task.cancel()

    async def serve_forever(self) -> None:
        """守护运行：未启动则先 start，随后挂起直到外部取消（幂等，可重复调用）。"""
        if self._server is None:
            await self.start()
        server = self._server
        assert server is not None
        try:
            await server.serve_forever()
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # 握手鉴权（契约 2：鉴权先于任何 payload 处理）
    # ------------------------------------------------------------------

    def _process_request(self, connection: ServerConnection, request: Any) -> Any:
        """握手期 HTTP 拦截：不合格的连接根本不升级成 WebSocket。

        401=缺失 Bearer、403=token 错误 / X-Self-ID 缺失或不符（401/403
        语义断开）；路径不符 404。返回 None 表示放行升级。
        """
        if request.path.split("?", 1)[0] != self.path:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        auth = request.headers.get("Authorization")
        if not auth:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "missing bearer token\n")
        scheme, _, credentials = auth.partition(" ")
        if scheme.strip().lower() != "bearer":
            return connection.respond(HTTPStatus.FORBIDDEN, "bad auth scheme\n")
        # 恒定时间比较：token 全等判定不因前缀命中而提前泄露（hmac.compare_digest）
        if not hmac.compare_digest(credentials.strip().encode("utf-8"), self.token.encode("utf-8")):
            logger.warning("鉴权失败：token 不符（403 语义断开）")
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        peer_self_id = request.headers.get("X-Self-ID")
        if peer_self_id is None or not hmac.compare_digest(
            _id_to_str(peer_self_id).encode("utf-8"), self.self_id.encode("utf-8")
        ):
            logger.warning("鉴权失败：X-Self-ID 缺失或与配置账号不符（403 语义断开）")
            return connection.respond(HTTPStatus.FORBIDDEN, "self id mismatch\n")
        return None

    # ------------------------------------------------------------------
    # 连接处理（契约 4：单连接顶替）
    # ------------------------------------------------------------------

    async def _handler(self, ws: ServerConnection) -> None:
        """已鉴权连接的主循环：同步收帧路由 + 顶替 + 清理。

        读循环内**绝不 await 业务逻辑**：reply 回查（最长 10s）与 on_event
        下游回调都在事件 worker 里执行，帧接收与 echo 路由保持实时。
        """
        ctx = _ConnCtx(ws=ws)
        old, self._current = self._current, ctx
        if old is not None:
            logger.info("新连接顶掉旧连接（单账号单连接，旧连接以 %s 关闭）", CLOSE_CODE_REPLACED)
            with contextlib.suppress(Exception):
                await old.ws.close(code=CLOSE_CODE_REPLACED, reason="replaced by newer connection")
        # 反连上线锚点日志（PERF_SPEC §7.1-7 ②：setup.bat 收尾判定= /api/status 200
        # 且日志见本行；账号按 _mask_id 打码，绝不落完整 QQ 号）
        logger.info("协议端已反连上线（账号 %s）", _mask_id(self.self_id))
        ctx.worker = asyncio.get_running_loop().create_task(self._event_worker(ctx))
        try:
            async for raw in ws:
                self._route_frame(ctx, raw)
        except ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001 - handler 崩溃会连带守护，必须兜底
            logger.exception("连接处理出现未预期异常（连接已保留/已关闭按 websockets 状态）")
        finally:
            if self._current is ctx:
                self._current = None
                logger.info("NapCat 连接断开（%s）", _mask_id(self.self_id))
            _fail_pending(ctx, "connection closed")
            if ctx.worker is not None:
                ctx.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ctx.worker

    # ------------------------------------------------------------------
    # 帧路由（契约 5/6/9：同步、零 await，保证响应帧实时处理）
    # ------------------------------------------------------------------

    def _route_frame(self, ctx: _ConnCtx, raw: Any) -> None:
        """一帧 JSON 的同步路由：echo 响应 → action 表；meta_event → 日志；
        message 事件 → 事件 worker 队列。"""
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("收到非 JSON 帧（%d 字节），忽略", len(raw))
            return
        if not isinstance(payload, dict):
            logger.warning("收到非对象 JSON 帧，忽略")
            return

        # 契约 9：echo 关联响应（含 retcode != 0 落告警）
        echo = payload.get("echo")
        if echo is not None:
            fut = ctx.pending.pop(_id_to_str(echo), None)
            if fut is not None and not fut.done():
                retcode = _retcode_int(payload.get("retcode"))
                fut.set_result(
                    ActionResult(
                        ok=retcode == 0,
                        status=_id_to_str(payload.get("status")) or None,
                        retcode=retcode,
                        data=payload.get("data"),
                        echo=_id_to_str(echo),
                    )
                )
                if retcode != 0:
                    logger.warning(
                        "action(echo=%s) retcode=%s status=%s —— 落告警",
                        _id_to_str(echo), retcode, payload.get("status"),
                    )
                return
            logger.debug("收到未匹配 pending 的 echo=%s 迟到响应，忽略", _id_to_str(echo))
            return

        # 契约 5：meta_event（heartbeat/lifecycle）只记日志不回复
        post_type = payload.get("post_type")
        if post_type == "meta_event":
            logger.debug("meta_event(%s) 只记日志不回复", payload.get("meta_event_type", "<无类型>"))
            return
        if post_type == "message":
            # 事件入队由 worker 串行处理（含白名单门禁与 reply 回查）
            ctx.queue.put_nowait(payload)
            return
        logger.debug("忽略事件 post_type=%r", post_type)

    async def _event_worker(self, ctx: _ConnCtx) -> None:
        """事件处理 worker：从队列串行处理 message 事件。

        串行保证事件顺序；单个事件异常不杀死 worker；连接关闭时被 cancel
        （此时在途未处理事件一并丢弃——连接已不存在，处理无意义）。
        """
        while True:
            payload = await ctx.queue.get()
            try:
                if payload.get("post_type") == "message":
                    await self._handle_message_event(ctx, payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单个事件失败不影响后续
                logger.exception("消息事件处理异常（连接保持）")


    async def _handle_group_event(self, ctx: _ConnCtx, payload: dict[str, Any]) -> None:
        """群消息处理：白名单内用户 @机器人时，剥离 @ 段后当私聊处理。

        MVP（UX 修缮）：只响白名单用户的 @提及；非 @ 或非白名单记 DEBUG 忽略。
        回复走 send_group_msg（群聊上下文，同一 OneBot action 面）。
        """
        user_id = _id_to_str(payload.get("user_id"))
        if not user_id or (self.whitelist is not None and user_id not in self.whitelist):
            logger.debug("群消息 user_id=%s 非白名单，忽略", _mask_id(user_id or "?"))
            return
        raw = payload.get("message")
        segments = raw if isinstance(raw, list) else []
        # 检查是否 @了机器人
        self_id = str(self.self_id or "")
        at_self = any(
            seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == self_id
            for seg in segments if isinstance(seg, dict)
        )
        if not at_self:
            logger.debug("群消息 user_id=%s 未 @机器人，忽略", _mask_id(user_id))
            return
        # 剥离 @机器人段后走私聊管线（per-user 队列、LLM、分段、表情包）
        stripped = [seg for seg in segments
                    if not (isinstance(seg, dict) and seg.get("type") == "at"
                            and str(seg.get("data", {}).get("qq", "")) == self_id)]
        payload["message"] = stripped
        # 标记群聊上下文（下游可据此选择 send_group_msg vs send_private_msg）
        payload["_vl_group_id"] = payload.get("group_id")
        logger.info("群消息 user_id=%s @机器人，走私聊管线", _mask_id(user_id))
        # 复用私聊处理（管线会按 user_id 走 per-user 队列）
        payload["message_type"] = "private"  # MVP：私聊管线处理内容
        await self._handle_message_event(ctx, payload)

    async def _handle_message_event(self, ctx: _ConnCtx, payload: dict[str, Any]) -> None:
        """message 事件 → 白名单门禁 → 段降级 → 媒体转存 → 上抛。

        PERF_SPEC §2-O4：本方法属帧分发路径，只做零业务 await 的路由工作——
        reply 回查已移入 per-user 管线（brain/pipeline.py），on_event 下游的
        管线自身只做入队（业务在 per-user 队列异步消化）。
        """
        # 群消息：白名单内用户 @机器人时回复（其余记日志忽略）
        msg_type = payload.get("message_type")
        if msg_type != "private":
            if msg_type == "group":
                return await self._handle_group_event(ctx, payload)
            logger.info("非私聊消息（message_type=%r）记日志忽略", msg_type)
            return
        user_id = _id_to_str(payload.get("user_id"))
        if not user_id:
            logger.debug("缺少 user_id 的私聊消息，忽略")
            return
        # §8.6 门禁：白名单外静默忽略
        if self.whitelist is not None and user_id not in self.whitelist:
            logger.debug("user_id=%s 不在白名单，静默忽略", _mask_id(user_id))
            return

        raw_message = payload.get("message")
        if isinstance(raw_message, str):
            # 容错：字符串形态（CQ 码）→ 直接整体降级（无段级信息可用）
            segments: list[dict[str, Any]] | None = None
            text = cq_to_plain(raw_message)
        else:
            segments = raw_message if isinstance(raw_message, list) else []
            text = segments_to_plain(segments)

        # 契约 7（PERF_SPEC §2-O4 修订）：reply 段只解析 reply_id 随事件上行，
        # get_msg 回查随事件进 per-user 管线执行（≤10s）——帧分发路径零回查
        # await，回查未决不阻塞本连接后续帧、echo 路由与其他用户的事件上抛。
        reply_id = find_reply_id(segments)
        reply_text: str | None = None

        # 边界规则：媒体 URL 到达即转存（后台，不阻塞事件分发）
        if segments:
            self._spawn_media_saves(_id_to_str(payload.get("message_id")), segments)

        event: dict[str, Any] = {
            "type": "message",
            "time": payload.get("time"),
            "self_id": _id_to_str(payload.get("self_id")),
            "user_id": user_id,
            "message_id": _id_to_str(payload.get("message_id")),
            "text": text,
            "segments": segments or [],
            "reply_id": reply_id,
            "reply_text": reply_text,
            "raw": payload,
        }
        if self.on_event is not None:
            try:
                await self.on_event(event)
            except Exception:  # noqa: BLE001 - 上游异常不能打死连接
                logger.exception("on_event 回调抛出异常（连接保持）")

    # ------------------------------------------------------------------
    # 媒体转存（边界规则：URL 到达即转存）
    # ------------------------------------------------------------------

    def _spawn_media_saves(self, message_id: str, segments: list[dict[str, Any]]) -> None:
        if self.media_dir is None:
            return
        for index, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            if stype not in ("image", "record"):
                continue
            url = (seg.get("data") or {}).get("url")
            if not isinstance(url, str) or not url:
                continue
            task = asyncio.get_running_loop().create_task(
                self._save_media(url, str(stype), message_id, index)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _save_media(self, url: str, seg_type: str, message_id: str, index: int) -> None:
        assert self.media_dir is not None
        stem = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        dest = self.media_dir / f"{message_id or 'evt'}_{index}_{stem}{_media_ext(url, seg_type)}"
        try:
            if dest.exists():
                return  # 同 URL 幂等转存
            self.media_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(_download_to, url, dest)
            logger.info("媒体已转存：%s", dest.name)
        except Exception as exc:  # noqa: BLE001 - 转存失败只告警不致命
            logger.warning("媒体转存失败（%s）：%s", dest.name, exc)

    # ------------------------------------------------------------------
    # action 客户端（契约 9：echo 关联 + 10s 超时 + retcode 告警）
    # ------------------------------------------------------------------

    async def call_action(
        self, action: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> ActionResult:
        """向 NapCat 发一个 action 并等待 echo 关联响应。

        超时（默认 10s）、未连接、发送失败均返回 ``ok=False`` 的 ActionResult，
        不抛异常——调用方按布尔处理即可。
        """
        ctx = self._current
        if ctx is None:
            logger.warning("action %s 调用失败：当前无 NapCat 连接", action)
            return ActionResult(ok=False, status="no-connection")
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ActionResult] = loop.create_future()
        ctx.pending[echo] = fut
        try:
            await ctx.ws.send(
                json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 - 断线竞争窗口
            ctx.pending.pop(echo, None)
            logger.warning("action %s 发送失败：%s", action, exc)
            return ActionResult(ok=False, status="send-failed")
        try:
            return await asyncio.wait_for(fut, timeout if timeout is not None else self.action_timeout)
        except TimeoutError:
            ctx.pending.pop(echo, None)
            logger.warning("action %s（echo=%s）%ss 超时判失败", action, echo, self.action_timeout)
            return ActionResult(ok=False, status="timeout")

    # ------------------------------------------------------------------
    # 发送 API（契约 8：一律数组段，永不拼 CQ 码；多条顺序发送）
    # ------------------------------------------------------------------

    async def send_private_segments(self, user_id: Any, segments: list[dict[str, Any]]) -> ActionResult:
        """发送一条私聊消息（segments 必须是数组段列表）。

        数组段通道：text/face/image/record 段都原样透传（face 段用
        ``_seg_face`` 构造），本方法不做段类型过滤。
        """
        return await self.call_action(
            "send_private_msg", {"user_id": _id_param(user_id), "message": list(segments)}
        )

    async def send_private_text(self, user_id: Any, text: str) -> ActionResult:
        """发送一条纯文本私聊消息（内部构造 text 数组段）。"""
        return await self.send_private_segments(user_id, [_seg_text(text)])

    async def send_private_texts(self, user_id: Any, texts: list[str]) -> ActionResult:
        """多条回复顺序发送（分段间隔由上层控制，本方法只保证顺序与失败短路）。"""
        result = ActionResult(ok=True)
        for text in texts:
            result = await self.send_private_text(user_id, text)
            if not result.ok:
                return result
        return result

    async def send_image_b64(self, user_id: Any, b64: str) -> ActionResult:
        """发送图片（base64://，不含 data: 前缀的裸 base64）。"""
        return await self.send_private_segments(user_id, [_seg_media_file("image", b64)])

    async def send_record_b64(self, user_id: Any, b64: str) -> ActionResult:
        """发送语音（base64://，NapCat file.ts 已核实支持）。"""
        return await self.send_private_segments(user_id, [_seg_media_file("record", b64)])

    async def send_group_msg(self, group_id: Any, segments: list[dict[str, Any]]) -> ActionResult:
        """发送一条群消息（最小封装：action 透传 ``send_group_msg``）。

        segments 必须是数组段列表（与 send_private_segments 同口径）。
        此前群聊回复只能经 call_action 透传，本封装给群聊上下文一个
        与私聊发送面对称的具名入口（_handle_group_event 的文档承诺）。
        """
        return await self.call_action(
            "send_group_msg", {"group_id": _id_param(group_id), "message": list(segments)}
        )

    # ------------------------------------------------------------------
    # 回查 / 收语音 API（契约 7 + 边界规则）
    # ------------------------------------------------------------------

    async def get_msg_text(self, message_id: Any) -> str | None:
        """``get_msg`` 回查消息并降级为纯文本；失败/超时返回 None。

        响应 data.message 双形态兼容：字符串（CQ 码）走 cq_to_plain，
        数组段走 segments_to_plain。
        """
        if message_id is None:
            return None
        result = await self.call_action("get_msg", {"message_id": _id_param(message_id)})
        if not result.ok or not isinstance(result.data, dict):
            return None
        msg = result.data.get("message")
        if isinstance(msg, str):
            return cq_to_plain(msg)
        if isinstance(msg, list):
            return segments_to_plain(msg)
        return None

    async def get_record_base64(self, file: str, out_format: str = "mp3") -> str | None:
        """``get_record(file=<收到的>, out_format='mp3')`` 取语音 base64（不含前缀）。"""
        result = await self.call_action("get_record", {"file": file, "out_format": out_format})
        if not result.ok or not isinstance(result.data, dict):
            return None
        b64 = result.data.get("base64")
        if b64:
            return str(b64)
        fvalue = result.data.get("file")
        if isinstance(fvalue, str) and fvalue.startswith("base64://"):
            return fvalue[len("base64://") :]
        return None


def _retcode_int(value: Any) -> int | None:
    """retcode 规范为 int；兼容异常实现里的数字字符串。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _fail_pending(ctx: _ConnCtx, reason: str) -> None:
    """连接断开时把在途 echo 全部判失败（不等 10s 超时）。"""
    for echo, fut in ctx.pending.items():
        if not fut.done():
            fut.set_result(ActionResult(ok=False, status=f"connection-closed: {reason}", echo=echo))
    ctx.pending.clear()


# ---------------------------------------------------------------------------
# __main__ 装配入口（_SETUP_TARGETS 约定：setup(ctx) -> cleanup | None）
# ---------------------------------------------------------------------------


def setup(ctx: Any) -> Callable[[], None]:
    """装配 OneBotBridge：cfg.bridge 取参、门禁并集、事件上行接总线。

    - token/self_id/host/port/path 读 ``ctx.cfg.bridge``；token 空时兜底调
      ``ensure_bridge_token``（幂等；正常路径 ``__main__`` 已生成）；
    - 门禁集合 = ``cfg.whitelist ∪ cfg.admins``（§8.6：白名单/管理员才触发；
      两者皆空时置 None 全通——M1 未迁移配置的自检路径，集成后必非空）；
    - 事件上行：on_event → ``ctx.bus.emit(EVENT_MESSAGE, event)``（净文本 +
      段列表 + 回查文本，见 _handle_message_event 的 event 字典）；
    - ``ctx.bridge``（实例）挂到 AppContext：``__main__._amain`` 在事件循环内
      ``await bridge.start()`` 后以守护任务消费 ``bridge.serve_forever()``
      （幂等启动，端口占用时 start 抛 OSError 由 _amain 报错退出非 0）；
    - 返回同步 cleanup（main() 的 finally 在无 loop 环境调用它收尾）。
    """
    from virtuallover.events import EVENT_MESSAGE

    bridge_cfg = ctx.cfg.bridge
    token = bridge_cfg.token
    if not token:
        from virtuallover.config import ensure_bridge_token
        token = ensure_bridge_token(
            ctx.cfg, Path(ctx.repo_root) / "local" / "config" / "vl.toml")

    async def _on_event(event: dict[str, Any]) -> None:
        await ctx.bus.emit(EVENT_MESSAGE, event)

    gate_ids = {str(u) for u in ctx.cfg.whitelist} | {str(a) for a in ctx.cfg.admins}
    media_dir = Path(ctx.repo_root) / ctx.cfg.storage.data_dir / "media"
    bridge = OneBotBridge(
        token=token,
        self_id=bridge_cfg.self_id,
        host=bridge_cfg.host,
        port=bridge_cfg.port,
        path=bridge_cfg.path,
        on_event=_on_event,
        whitelist=gate_ids or None,
        media_dir=media_dir,
    )
    ctx.bridge = bridge

    def cleanup() -> None:
        """同步收尾：停止监听并断开 NapCat 连接（失败不阻断其余清理）。"""
        try:
            asyncio.run(bridge.stop())
        except Exception:  # noqa: BLE001 - 对齐 _import_setups 的清理容忍约定
            logger.exception("OneBotBridge cleanup 失败（忽略）")

    logger.info("OneBotBridge 装配完成（%s:%s%s，门禁 %d 人）",
                bridge_cfg.host, bridge_cfg.port, bridge_cfg.path, len(gate_ids))
    return cleanup
