# -*- coding: utf-8 -*-
"""OneBot 桥契约测试：以 websockets 客户端模拟 NapCat，逐条断言 SPEC §3 九条契约。

契约 → 测试映射总览：

======================  =======================================================
SPEC §3 契约            测试
======================  =======================================================
1 绑定 127.0.0.1:6199   test_defaults_bind_loopback_6199 / test_wrong_path_404
2 Bearer 恒定时间鉴权   test_auth_*（401/403 语义、先于 payload）
3 token 强随机          test_generate_token_* / test_empty_config_fail_fast
4 单连接顶替/32MB/ping  test_new_connection_replaces_old / test_max_size_* /
                        test_oversize_frame_closed_1009 / test_ping_interval_default
5 心跳只记日志          test_heartbeat_only_logged
6 array 解析+兼容       test_private_event_* / test_group_message_ignored /
                        test_string_cq_fallback / test_whitelist_*
7 reply 回查            test_reply_segment_dispatches_without_inline_lookup
                        （§2-O4 修订：回查随事件进 per-user 管线）
8 数组段发送            test_send_*（永不拼 CQ 码、base64://、多条顺序）
9 echo 关联 10s         test_echo_* / test_action_timeout / test_retcode_*
======================  =======================================================

边界规则（§3 末段）：群消息忽略、白名单门禁、get_record 取语音、媒体 URL
转存，均有对应测试。断开重连与并发不串台见 test_disconnect_reconnect /
test_concurrent_actions_no_echo_mix / test_concurrent_events_no_cross。

说明：
- 客户端连接统一带 X-Client-Role 头模拟 NapCat 习惯（服务端按 SPEC 只校验
  Authorization 与 X-Self-ID，未知头忽略不校验）；
- 全部 QQ 号/token 均为占位值，真实值只能来自迁移脚本读 local/；
- 异步用 asyncio.run 手工驱动，不引入 pytest-asyncio 依赖。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
# （骨架 tests/conftest.py 就绪后此块幂等无害）
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import port_in_use, vl_test_port  # noqa: E402 —— 本目录 conftest

from virtuallover.bridge.onebot_server import (  # noqa: E402
    CLOSE_CODE_REPLACED,
    DEFAULT_ACTION_TIMEOUT,
    DEFAULT_PING_INTERVAL,
    MAX_FRAME_BYTES,
    OneBotBridge,
    _seg_face,
    generate_token,
)

# ---------------------------------------------------------------------------
# 占位常量（禁真实账号/token；真值由 tools/config_migrate.py 从 local/ 读）
# ---------------------------------------------------------------------------

TOKEN = "vl-test-token-placeholder"
SELF_ID = "10000"  # 占位机器人账号
USER = "10001"     # 占位白名单用户
OTHER = "99999"    # 占位白名单外用户
ROLE_HEADER = "X-Client-Role"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def run(coro):
    """异步测试统一入口：不引入 pytest-asyncio。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 客户端/服务端辅助
# ---------------------------------------------------------------------------


@asynccontextmanager
async def connect_napcat(
    port: int,
    *,
    token: str | None = TOKEN,
    self_id: str | None = SELF_ID,
    path: str = "/ws",
    with_role: bool = True,
) -> AsyncIterator[ClientConnection]:
    """模拟 NapCat 反向 WS 客户端连入（async with 上下文，退出即断开）。"""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if self_id is not None:
        headers["X-Self-ID"] = self_id
    if with_role:
        headers[ROLE_HEADER] = "NapCat"
    ws = await connect(f"ws://127.0.0.1:{port}{path}", additional_headers=headers)
    try:
        yield ws
    finally:
        with contextlib.suppress(ConnectionClosed):
            await ws.close()


async def try_connect_bad(
    port: int,
    *,
    token: str | None = TOKEN,
    self_id: str | None = SELF_ID,
    path: str = "/ws",
) -> None:
    """鉴权负向用例专用：普通协程连接，异常直接上抛给 pytest.raises。"""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if self_id is not None:
        headers["X-Self-ID"] = self_id
    await connect(f"ws://127.0.0.1:{port}{path}", additional_headers=headers)


async def send_json(ws: ClientConnection, payload: dict) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def recv_json(ws: ClientConnection, timeout: float = 3.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    return json.loads(raw)


async def respond_ok(ws: ClientConnection, request: dict, data=None) -> None:
    """客户端按 OneBot 规范回 action 响应（echo 原样带回）。"""
    await send_json(
        ws,
        {"status": "ok", "retcode": 0, "data": data, "echo": request.get("echo")},
    )


async def wait_until(pred, timeout: float = 3.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("条件等待超时")
        await asyncio.sleep(interval)


@asynccontextmanager
async def running_bridge(**kwargs):
    """启动一个测试桥：on_event 事件进 asyncio.Queue。"""
    queue: asyncio.Queue = asyncio.Queue()
    kwargs.setdefault("media_dir", None)  # 测试默认关闭媒体转存（专项测试自行打开）
    bridge = OneBotBridge(token=TOKEN, self_id=SELF_ID, on_event=queue.put, **kwargs)
    await bridge.start()
    try:
        yield bridge, queue
    finally:
        await bridge.stop()


def make_message_event(
    user_id=USER,
    text="你好呀",
    message_id=111,
    message_type="private",
    segments=None,
) -> dict:
    """构造贴近 NapCat 下行形态的私聊 message 事件。"""
    if segments is None:
        segments = [{"type": "text", "data": {"text": text}}]
    return {
        "time": 1710000000,
        "self_id": int(SELF_ID),
        "post_type": "message",
        "message_type": message_type,
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": user_id,
        "message": segments,
        "raw_message": "（原始 CQ 串）",
        "font": 0,
        "sender": {"user_id": user_id, "nickname": "占位昵称"},
    }


async def pump_actions(ws: ClientConnection, count: int, *, delay: float = 0.0, data_fn=None):
    """客户端 NPC：依次收 count 个 action，逐个回 ok 响应（可选延迟/数据）。"""
    for _ in range(count):
        request = await recv_json(ws)
        if delay:
            await asyncio.sleep(delay)
        await respond_ok(ws, request, None if data_fn is None else data_fn(request))


# ---------------------------------------------------------------------------
# 契约 1：绑 127.0.0.1:6199，路径 /ws
# ---------------------------------------------------------------------------


class TestContract1Bind:
    def test_defaults_bind_loopback_6199(self):
        """默认参数必须正是 127.0.0.1:6199/ws，且真实端口可连入。

        端口基线隔离（VOICE_SPEC §4 前置）：默认值断言是纯常量检查照常执行；
        真实绑定连入部分用 vl_test_port()（环境变量 VL_TEST_PORT 可覆盖），
        端口被在跑 vl 进程占坑时 skip 并说明，不再连环 failed。
        """
        # 常量断言（不依赖端口空闲）：默认构造必须正是 127.0.0.1:6199/ws
        bridge = OneBotBridge(token=TOKEN, self_id=SELF_ID, media_dir=None)
        assert bridge.host == "127.0.0.1"
        assert bridge.port == 6199
        assert bridge.path == "/ws"

        port = vl_test_port()
        if port_in_use(port):
            pytest.skip(
                f"测试桥端口 {port} 已被监听（很可能是在跑的 vl 常驻进程）。"
                f"真实绑定连入部分跳过（默认值断言已通过）；"
                f"可设环境变量 VL_TEST_PORT=<空闲端口> 后重跑。"
            )

        async def scenario():
            async with running_bridge(port=port) as (bridge, queue):
                assert bridge.bound_port == port
                async with connect_napcat(port) as ws:
                    assert bridge.connected
                await wait_until(lambda: not bridge.connected)
        run(scenario())

    def test_auth_ok_logs_anti_channel_anchor(self, caplog):
        """反连上线锚点日志（PERF_SPEC §7.1-7 ②）：setup.bat 收尾判定靠 grep 本行；
        账号经 _mask_id 打码，完整 QQ 号绝不入日志。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                with caplog.at_level(logging.INFO,
                                     logger="virtuallover.bridge.onebot_server"):
                    async with connect_napcat(bridge.bound_port) as ws:
                        assert bridge.connected
                await wait_until(lambda: not bridge.connected)
        run(scenario())
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "协议端已反连上线" in text
        assert SELF_ID not in text  # 打码：完整占位账号也不该出现

    def test_wrong_path_404(self):
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                with pytest.raises(InvalidStatus) as exc_info:
                    await try_connect_bad(bridge.bound_port, path="/other")
                assert exc_info.value.response.status_code == 404
        run(scenario())


# ---------------------------------------------------------------------------
# 契约 2：Bearer 恒定时间鉴权 + X-Self-ID 校验，先于任何 payload
# ---------------------------------------------------------------------------


class TestContract2Auth:
    def test_auth_ok_connects(self):
        """正确 token + X-Self-ID（附 X-Client-Role 头）连入成功。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    assert bridge.connected
                    # 鉴权通过后连接可用：客户端发心跳，服务端正常记录不踢
                    await send_json(ws, heartbeat_event())
                    await asyncio.sleep(0.1)
                await wait_until(lambda: not bridge.connected)
        run(scenario())

    def test_missing_token_401(self):
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                with pytest.raises(InvalidStatus) as exc_info:
                    await try_connect_bad(bridge.bound_port, token=None)
                assert exc_info.value.response.status_code == 401
                assert not bridge.connected and queue.empty()
        run(scenario())

    def test_wrong_token_403(self):
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                with pytest.raises(InvalidStatus) as exc_info:
                    await try_connect_bad(bridge.bound_port, token=TOKEN + "x")
                assert exc_info.value.response.status_code == 403
                assert queue.empty()
        run(scenario())

    def test_non_bearer_scheme_403(self):
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                headers = {"Authorization": f"Basic {TOKEN}", "X-Self-ID": SELF_ID}
                with pytest.raises(InvalidStatus) as exc_info:
                    await connect(
                        f"ws://127.0.0.1:{bridge.bound_port}/ws", additional_headers=headers
                    )
                assert exc_info.value.response.status_code == 403
        run(scenario())

    def test_self_id_missing_403(self):
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                with pytest.raises(InvalidStatus) as exc_info:
                    await try_connect_bad(bridge.bound_port, self_id=None)
                assert exc_info.value.response.status_code == 403
        run(scenario())

    def test_self_id_mismatch_403(self):
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                with pytest.raises(InvalidStatus) as exc_info:
                    await try_connect_bad(bridge.bound_port, self_id=OTHER)
                assert exc_info.value.response.status_code == 403
        run(scenario())

    def test_no_payload_before_auth(self):
        """鉴权先于任何 payload 处理：未通过鉴权的连接上没有任何事件被分发。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                with pytest.raises(InvalidStatus):
                    await try_connect_bad(bridge.bound_port, token="bad")
                await asyncio.sleep(0.1)
                assert queue.empty()
        run(scenario())


# ---------------------------------------------------------------------------
# 契约 3：token 强随机生成，缺失即拒启
# ---------------------------------------------------------------------------


class TestContract3Token:
    def test_generate_token_strong(self):
        """token_urlsafe(32)：43 字符、URL 安全、两次生成不同（禁弱 token）。"""
        token = generate_token()
        assert len(token) == 43
        assert generate_token() != token

    def test_empty_config_fail_fast(self):
        """token/self_id 缺失直接 ValueError 拒启，绝不弱默认。"""
        with pytest.raises(ValueError):
            OneBotBridge(token="", self_id=SELF_ID, media_dir=None)
        with pytest.raises(ValueError):
            OneBotBridge(token=TOKEN, self_id="", media_dir=None)


# ---------------------------------------------------------------------------
# 契约 4：单连接顶替、max_size 32MB、ping 10s
# ---------------------------------------------------------------------------


def heartbeat_event(interval=5000) -> dict:
    return {
        "time": 1710000000,
        "self_id": int(SELF_ID),
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "interval": interval,
    }


class TestContract4Connection:
    def test_new_connection_replaces_old(self):
        """同账号第二连接顶掉第一连接：旧连接收到 4000 关闭，新连接可用。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as old_ws:
                    assert bridge.connected
                    async with connect_napcat(bridge.bound_port) as new_ws:
                        assert bridge.connected
                        # 旧连接被顶掉
                        with pytest.raises(ConnectionClosed) as exc_info:
                            await asyncio.wait_for(old_ws.recv(), 3.0)
                        assert exc_info.value.rcvd is not None
                        assert exc_info.value.rcvd.code == CLOSE_CODE_REPLACED
                        # 新连接可用：能收到下行 action
                        await bridge.send_private_text(USER, "新连接在线")
                        request = await recv_json(new_ws)
                        assert request["action"] == "send_private_msg"
                await wait_until(lambda: not bridge.connected)
        run(scenario())

    def test_max_size_default_32mb(self):
        assert MAX_FRAME_BYTES == 32 * 1024 * 1024
        bridge = OneBotBridge(token=TOKEN, self_id=SELF_ID, media_dir=None)
        assert bridge.max_size == 32 * 1024 * 1024

    def test_oversize_frame_closed_1009(self):
        """超过 32MB 的帧触发 1009 关闭（防大图断链上限生效）。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    big = '{"post_type":"meta_event","blob":"' + "x" * (33 * 1024 * 1024) + '"}'
                    await ws.send(big)
                    with pytest.raises(ConnectionClosed) as exc_info:
                        await asyncio.wait_for(ws.recv(), 10.0)
                    assert exc_info.value.rcvd is not None
                    assert exc_info.value.rcvd.code == 1009
        run(scenario())

    def test_ping_interval_default_10s(self):
        assert DEFAULT_PING_INTERVAL == 10.0
        bridge = OneBotBridge(token=TOKEN, self_id=SELF_ID, media_dir=None)
        assert bridge.ping_interval == 10.0


# ---------------------------------------------------------------------------
# 契约 5：heartbeat meta_event 只记日志不回复
# ---------------------------------------------------------------------------


class TestContract5Heartbeat:
    def test_heartbeat_only_logged(self):
        """心跳被吞掉：无任何下行帧，连接保持存活（NapCat 心跳单向）。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, heartbeat_event())
                    await send_json(ws, {"post_type": "meta_event", "meta_event_type": "lifecycle",
                                         "sub_type": "connect", "self_id": int(SELF_ID)})
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), 0.5)  # 无回复
                    assert queue.empty()
                    # 连接未断：仍能处理后续事件
                    await send_json(ws, make_message_event())
                    event = await asyncio.wait_for(queue.get(), 3.0)
                    assert event["text"] == "你好呀"
        run(scenario())


# ---------------------------------------------------------------------------
# 契约 6：array 段解析、未知降级、Number|String、群消息/白名单边界
# ---------------------------------------------------------------------------


class TestContract6Events:
    def test_private_event_full_segments(self):
        """六类段降级 + 未知段占位 + Number|String 统一成 str。

        PERF_SPEC §2-O4 后：reply 段只解析 reply_id 随事件上行（reply_text
        置 None），get_msg 回查随事件进 per-user 管线——桥分发零回查等待。
        """
        segments = [
            {"type": "reply", "data": {"id": 555}},
            {"type": "text", "data": {"text": "看图 "}},
            {"type": "face", "data": {"id": 3}},
            {"type": "at", "data": {"qq": 10002}},
            {"type": "image", "data": {"file": "a.jpg", "url": "http://example.invalid/a.jpg"}},
            {"type": "record", "data": {"file": "a.amr"}},
            {"type": "json", "data": {"data": "{}"}},
        ]

        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(segments=segments))
                    event = await asyncio.wait_for(queue.get(), 3.0)
                    assert event["type"] == "message"
                    assert event["text"] == "[回复]看图 [发呆]@10002[图片][语音][CQ:json]"
                    assert event["segments"] == segments
                    assert event["user_id"] == USER
                    assert event["message_id"] == "111"
                    assert event["self_id"] == SELF_ID
                    assert event["reply_id"] == "555"
                    # 回查随事件进管线（O4）：桥上抛时回查未执行，文本为空
                    assert event["reply_text"] is None
        run(scenario())

    def test_unknown_segment_degrades_with_log(self, caplog):
        """未知段类型 → [CQ:<type>] 占位 + debug 日志。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(
                        segments=[{"type": "forward", "data": {"id": 1}},
                                  {"type": "text", "data": {"text": "ok"}}]))
                    event = await asyncio.wait_for(queue.get(), 3.0)
                    assert event["text"] == "[CQ:forward]ok"
        # 段降级日志出自 virtuallover.bridge.textutil，logger 级别需一并放开
        with caplog.at_level(logging.DEBUG, logger="virtuallover.bridge.textutil"), \
                caplog.at_level(logging.DEBUG, logger="virtuallover.bridge.onebot_server"):
            run(scenario())
        assert any("forward" in rec.message for rec in caplog.records)

    def test_string_cq_fallback(self):
        """字符串形态 message（CQ 码）容错降级。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    event = make_message_event()
                    event["message"] = "[CQ:face,id=1]哈哈[CQ:at,qq=10002]"
                    await send_json(ws, event)
                    got = await asyncio.wait_for(queue.get(), 3.0)
                    assert got["text"] == "[撇嘴]哈哈@10002"
        run(scenario())

    def test_number_string_ids_unified(self):
        """user_id/self_id/message_id/reply id 的 Number|String 双形态统一为 str。"""
        segments = [{"type": "reply", "data": {"id": "555"}},
                    {"type": "text", "data": {"text": "x"}}]
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(
                        user_id=int(USER), message_id=111, segments=segments))
                    event = await asyncio.wait_for(queue.get(), 3.0)
                    assert event["user_id"] == USER and isinstance(event["user_id"], str)
                    assert event["self_id"] == SELF_ID
                    assert event["message_id"] == "111"
                    assert event["reply_id"] == "555"
                    assert event["reply_text"] is None  # 回查随事件进管线（O4）
        run(scenario())

    def test_group_message_ignored(self):
        """message_type != private 记日志忽略：不分发、不回复。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(message_type="group"))
                    await asyncio.sleep(0.15)
                    assert queue.empty()
                    assert bridge.connected  # 连接未受影响
        run(scenario())

    def test_whitelist_gate(self):
        """白名单外 user_id 静默忽略；白名单内正常分发（§8.6 门禁）。"""
        async def scenario():
            async with running_bridge(port=0, whitelist={USER}) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(user_id=OTHER))
                    await send_json(ws, make_message_event(user_id=USER, text="在的"))
                    event = await asyncio.wait_for(queue.get(), 3.0)
                    assert event["user_id"] == USER and event["text"] == "在的"
                    assert queue.empty()  # 白名单外那条被静默吞掉
        run(scenario())


# ---------------------------------------------------------------------------
# 契约 7：reply 段（PERF_SPEC §2-O4 修订：回查随事件进 per-user 管线）
# ---------------------------------------------------------------------------


class TestContract7Reply:
    def test_reply_segment_dispatches_without_inline_lookup(self):
        """带 reply 段的事件即时上抛，回查不在帧分发路径内联执行（O4②）。

        PERF_SPEC §2-O4 后的契约 7 语义：
        - reply_id 解析后随事件上行（供管线回查），reply_text 置 None；
        - 事件分发**不等任何回查**——客户端不回应任何 action 也即时可达；
        - 帧分发路径零 get_msg 出帧（原「桥内联回查后随事件分发」语义随 O4
          移入 brain/pipeline.py，由 test_pipeline_lookup_attaches_reply_text
          在管线层钉住）。
        """
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(segments=[
                        {"type": "reply", "data": {"id": 555}},
                        {"type": "text", "data": {"text": "你说得对"}},
                    ]))
                    got = await asyncio.wait_for(queue.get(), 3.0)
                    assert got["reply_id"] == "555"
                    assert got["reply_text"] is None
                    assert got["text"] == "[回复]你说得对"
                    # 分发路径零回查 action：事件已上抛且客户端不再收到任何请求帧
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), 0.3)
                        raise AssertionError(
                            "桥在帧分发路径内联发送了 get_msg（违背 §2-O4："
                            "回查必须随事件进 per-user 管线）")
        run(scenario())

    def test_reply_lookup_not_required_for_dispatch(self):
        """事件分发不依赖回查应答：客户端永不回应 action，事件照常上抛。

        （原「get_msg 失败/超时不阻塞事件」的 O4 等价语义：回查职责整体
        移入管线，桥分发零等待、零 action 依赖。）
        """
        async def scenario():
            async with running_bridge(port=0, action_timeout=0.2) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(segments=[
                        {"type": "reply", "data": {"id": 555}},
                        {"type": "text", "data": {"text": "x"}},
                    ]))
                    got = await asyncio.wait_for(queue.get(), 3.0)
                    assert got["reply_id"] == "555"
                    assert got["reply_text"] is None
        run(scenario())


# ---------------------------------------------------------------------------
# 契约 8：发送一律数组段，永不拼 CQ 码；base64://；多条顺序
# ---------------------------------------------------------------------------


class TestContract8Send:
    def test_send_private_text_array_no_cq(self):
        """send_private_text 构造 text 数组段，message 必为数组。
        编排：桥在等 echo，而收发响应的就是本协程——必须先建发送 task。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    task = asyncio.create_task(bridge.send_private_text(USER, "晚上好"))
                    request = await recv_json(ws)
                    assert request["action"] == "send_private_msg"
                    assert request["params"]["user_id"] == int(USER)
                    assert request["params"]["message"] == [{"type": "text", "data": {"text": "晚上好"}}]
                    assert isinstance(request["params"]["message"], list)
                    assert request["echo"]
                    await respond_ok(ws, request, data={"message_id": 42})
                    result = await task
                    assert result.ok and result.data == {"message_id": 42}
        run(scenario())

    def test_send_image_record_segment_shapes(self):
        """图片/语音段 = {type, data:{file: 'base64://<b64>'}}（NapCat file.ts 核实形态）。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    await bridge.send_image_b64(USER, "QUJD")
                    request = await recv_json(ws)
                    seg = request["params"]["message"][0]
                    assert seg == {"type": "image", "data": {"file": "base64://QUJD"}}
                    await respond_ok(ws, request)
                    await bridge.send_record_b64(USER, "REVG")
                    request = await recv_json(ws)
                    seg = request["params"]["message"][0]
                    assert seg == {"type": "record", "data": {"file": "base64://REVG"}}
                    await respond_ok(ws, request)
        run(scenario())

    def test_send_multiple_texts_in_order(self):
        """多条回复顺序发送（顺序保证，间隔由上层控制）。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    pump = asyncio.create_task(pump_actions(ws, 3))
                    result = await bridge.send_private_texts(USER, ["一", "二", "三"])
                    await pump
                    assert result.ok
        run(scenario())

    def test_send_multiple_texts_order_on_wire(self):
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    task = asyncio.create_task(bridge.send_private_texts(USER, ["一", "二", "三"]))
                    texts = []
                    for _ in range(3):
                        request = await recv_json(ws)
                        texts.append(request["params"]["message"][0]["data"]["text"])
                        await respond_ok(ws, request)
                    await task
                    assert texts == ["一", "二", "三"]
        run(scenario())

    def test_send_group_msg_and_face_segment(self):
        """send_group_msg 最小封装 + face 段经数组段通道透传（小黄脸能力）。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    segments = [
                        {"type": "text", "data": {"text": "大家看"}},
                        _seg_face(85),
                    ]
                    task = asyncio.create_task(bridge.send_group_msg("10086", segments))
                    request = await recv_json(ws)
                    assert request["action"] == "send_group_msg"
                    assert request["params"]["group_id"] == 10086
                    assert request["params"]["message"] == [
                        {"type": "text", "data": {"text": "大家看"}},
                        {"type": "face", "data": {"id": "85"}},
                    ]
                    await respond_ok(ws, request, data={"message_id": 7})
                    result = await task
                    assert result.ok
        run(scenario())

    def test_send_without_connection_fails(self):
        """无连接时发送直接失败占位（ok=False），不挂死。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                result = await bridge.send_private_text(USER, "hi")
                assert not result.ok and result.status == "no-connection"
        run(scenario())


# ---------------------------------------------------------------------------
# 契约 9：action echo 关联、10s 超时、retcode!=0 告警
# ---------------------------------------------------------------------------


class TestContract9Echo:
    def test_default_action_timeout_10s(self):
        bridge = OneBotBridge(token=TOKEN, self_id=SELF_ID, media_dir=None)
        assert bridge.action_timeout == DEFAULT_ACTION_TIMEOUT == 10.0

    def test_echo_roundtrip(self):
        """echo 往返：响应与请求按 echo 一一配对。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    task = asyncio.create_task(bridge.call_action("get_status"))
                    request = await recv_json(ws)
                    assert request["action"] == "get_status" and request["echo"]
                    await respond_ok(ws, request, data={"online": True})
                    result = await task
                    assert result.ok and result.echo == request["echo"]
                    assert result.data == {"online": True} and result.retcode == 0
        run(scenario())

    def test_echo_wrong_order_paired(self):
        """两个并发 action，客户端按相反顺序回：echo 配对不串台。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    t1 = asyncio.create_task(bridge.call_action("get_msg", {"message_id": 1}))
                    t2 = asyncio.create_task(bridge.call_action("get_version_info"))
                    r1 = await recv_json(ws)
                    r2 = await recv_json(ws)
                    await respond_ok(ws, r2, data={"app_name": "napcat"})
                    await respond_ok(ws, r1, data={"message_id": 1})
                    result1, result2 = await asyncio.gather(t1, t2)
                    assert result1.data == {"message_id": 1}
                    assert result2.data == {"app_name": "napcat"}
                    assert result1.echo == r1["echo"] and result2.echo == r2["echo"]
        run(scenario())

    def test_concurrent_actions_no_echo_mix(self):
        """20 个并发 action、响应带乱序延迟：echo 全部正确配对（不串台）。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    delays = {i: 0.001 * ((i * 7) % 20) for i in range(20)}

                    async def responder():
                        for _ in range(20):
                            request = await recv_json(ws)
                            await asyncio.sleep(delays[request["params"]["n"]])
                            await respond_ok(ws, request, data={"n": request["params"]["n"]})

                    pump = asyncio.create_task(responder())
                    tasks = [bridge.call_action("echo_test", {"n": i}) for i in range(20)]
                    results = await asyncio.gather(*tasks)
                    await pump
                    for i, result in enumerate(results):
                        assert result.ok and result.data == {"n": i}
                        assert results[i].echo == results[i].echo  # 占位自检
                    assert len({r.echo for r in results}) == 20  # echo 全局唯一
        run(scenario())

    def test_action_timeout(self):
        """无响应 → 超时判失败（注入 0.3s 验证行为；默认值 10s 另测）。"""
        async def scenario():
            async with running_bridge(port=0, action_timeout=0.3) as (bridge, _):
                async with connect_napcat(bridge.bound_port):
                    result = await bridge.call_action("no_reply_action")
                    assert not result.ok and result.status == "timeout"
        run(scenario())

    def test_retcode_nonzero_warns(self, caplog):
        """retcode != 0 落告警日志，结果 ok=False。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    task = asyncio.create_task(bridge.call_action("send_private_msg", {}))
                    request = await recv_json(ws)
                    await send_json(ws, {"status": "failed", "retcode": 1200,
                                         "data": None, "echo": request["echo"]})
                    result = await task
                    assert not result.ok and result.retcode == 1200
        with caplog.at_level(logging.WARNING, logger="virtuallover.bridge.onebot_server"):
            run(scenario())
        assert any("1200" in rec.message for rec in caplog.records)

    def test_pending_fails_on_disconnect(self):
        """在途 action 因连接断开立即判失败（不等超时）。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    task = asyncio.create_task(bridge.call_action("slow_action"))
                    await asyncio.sleep(0.1)
                    await ws.close()
                    result = await task
                    assert not result.ok and result.status.startswith("connection-closed")
        run(scenario())


# ---------------------------------------------------------------------------
# 边界规则：断开重连、get_record、媒体转存、on_event 异常隔离
# ---------------------------------------------------------------------------


class TestEdgeRules:
    def test_disconnect_reconnect(self):
        """断开后 current 清空，重连恢复收发能力。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws1:
                    assert bridge.connected
                await wait_until(lambda: not bridge.connected)
                # 重连
                async with connect_napcat(bridge.bound_port) as ws2:
                    assert bridge.connected
                    await bridge.send_private_text(USER, "重连成功")
                    request = await recv_json(ws2)
                    assert request["action"] == "send_private_msg"
                    await respond_ok(ws2, request, data={"message_id": 1})
                await wait_until(lambda: not bridge.connected)
        run(scenario())

    def test_get_record_base64(self):
        """收语音：get_record(file=<收到的>, out_format='mp3') 取 base64。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, _):
                async with connect_napcat(bridge.bound_port) as ws:
                    task = asyncio.create_task(bridge.get_record_base64("xxx.amr"))
                    request = await recv_json(ws)
                    assert request["action"] == "get_record"
                    assert request["params"] == {"file": "xxx.amr", "out_format": "mp3"}
                    await respond_ok(ws, request, data={"base64": base64.b64encode(b"AUD").decode()})
                    b64 = await task
                    assert base64.b64decode(b64) == b"AUD"
        run(scenario())

    def test_media_url_saved_to_disk(self, tmp_path):
        """媒体 URL 到达即转存（用 file:// URL 黑盒验证，零网络依赖）。"""
        media_dir = tmp_path / "media"
        src = tmp_path / "pic.png"
        src.write_bytes(b"\x89PNG-placeholder")
        async def scenario():
            async with running_bridge(port=0, media_dir=media_dir) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event(segments=[
                        {"type": "image", "data": {"file": "p.png", "url": src.as_uri()}},
                    ]))
                    await asyncio.wait_for(queue.get(), 3.0)
                    await wait_until(lambda: not bridge._bg_tasks)
                    saved = list(media_dir.glob("*_*.png"))
                    assert len(saved) == 1 and saved[0].read_bytes() == b"\x89PNG-placeholder"
        run(scenario())

    def test_media_url_dedup_idempotent(self, tmp_path):
        """同 URL 二次到达不重复转存（幂等）。"""
        media_dir = tmp_path / "media"
        src = tmp_path / "p.jpg"
        src.write_bytes(b"jpg")
        async def scenario():
            async with running_bridge(port=0, media_dir=media_dir) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    for _ in range(2):
                        await send_json(ws, make_message_event(segments=[
                            {"type": "image", "data": {"file": "p.jpg", "url": src.as_uri()}}]))
                        await asyncio.wait_for(queue.get(), 3.0)
                    await wait_until(lambda: not bridge._bg_tasks)
                    assert len(list(media_dir.glob("*_*.jpg"))) == 1
        run(scenario())

    def test_on_event_exception_keeps_connection(self):
        """on_event 抛异常不打死连接：后续事件照常分发。

        port=0 随机端口：默认 6199 会被在跑 vl 常驻进程占坑（基线隔离，
        与契约 1 的处置一致）。
        """
        async def scenario():
            async def bad_callback(event):
                raise RuntimeError("下游未就绪")

            queue: asyncio.Queue = asyncio.Queue()
            bridge = OneBotBridge(token=TOKEN, self_id=SELF_ID,
                                  on_event=bad_callback, media_dir=None, port=0)
            await bridge.start()
            try:
                async with connect_napcat(bridge.bound_port) as ws:
                    await send_json(ws, make_message_event())
                    await asyncio.sleep(0.15)
                    assert bridge.connected
                    # 换回正常回调验证通道仍然可用
                    bridge.on_event = queue.put
                    await send_json(ws, make_message_event(text="第二条"))
                    event = await asyncio.wait_for(queue.get(), 3.0)
                    assert event["text"] == "第二条"
            finally:
                await bridge.stop()
        run(scenario())

    def test_non_json_frame_tolerated(self, caplog):
        """非 JSON 帧告警并忽略，不断连接。"""
        async def scenario():
            async with running_bridge(port=0) as (bridge, queue):
                async with connect_napcat(bridge.bound_port) as ws:
                    await ws.send("这不是 JSON")
                    await asyncio.sleep(0.1)
                    assert bridge.connected
                    await send_json(ws, make_message_event())
                    event = await asyncio.wait_for(queue.get(), 3.0)
                    assert event["text"] == "你好呀"
        with caplog.at_level(logging.WARNING, logger="virtuallover.bridge.onebot_server"):
            run(scenario())
        assert any("非 JSON" in rec.message for rec in caplog.records)
