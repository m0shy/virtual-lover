# -*- coding: utf-8 -*-
"""最近事件面板契约测试（五轮修缮 R2 任务 1：观测面）。

覆盖面：
1. RecentEventsHandler：WARNING/ERROR 全收、DEBUG 绝不收、INFO 仅白名单
   里程碑收；多行正文（异常栈）拍平单行并截断；deque 容量 50 丢最旧；
   snapshot 最新在前；handler 内异常绝不外抛。
2. read_log_tail：按 __main__._setup_logging 的行格式解析 vl.log 尾部，
   只留 WARNING+ / 关键 INFO，traceback 续行跳过；文件缺失返回 []。
3. recent_events_payload：缓冲优先（source=memory）、空则退日志（source=log）。
4. GET /api/recent-errors 端点：三重门继承（未登录 401），登录后 200。
5. /api/status 增强：counters（打断/语音降级/LLM 失败，since=进程启动）；
   brain 行「可达但最近调用失败」→ degraded=True+note（403 黄灯契约）；
   embedding 行熔断开路 → degraded=True+「记忆检索降级中」。

安全规约：全部占位值（无真实 key/token/QQ 号）；HTTP 测试自建 127.0.0.1
临时服务（test_console_voice.py 同款手法），绝不触碰真实 local/。

运行：python -m pytest virtuallover/tests/test_recent_errors.py -q
"""

from __future__ import annotations

import json
import logging
import threading
import time
import types
import http.client

import pytest

from virtuallover.console import app as console_app
from virtuallover.console import auth as console_auth
from virtuallover.console import core as console_core
from virtuallover.console import recent as recent_module


# ---------------------------------------------------------------------------
# 1) RecentEventsHandler 环形缓冲
# ---------------------------------------------------------------------------

def _make_logger(handler: logging.Handler) -> logging.Logger:
    """构造独立 logger（propagation=False，不污染 root——测试互不串音）。"""
    lg = logging.getLogger("test.recent." + str(id(handler)))
    lg.handlers = []
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    lg.addHandler(handler)
    return lg


def test_handler_captures_warning_error_and_key_info_only():
    handler = recent_module.RecentEventsHandler()
    lg = _make_logger(handler)

    lg.debug("debug 噪音不进缓冲")
    lg.info("普通 info 也不进")
    lg.info("模块 virtuallover.brain.history 装配完成")   # 白名单里程碑
    lg.warning("第一条警告：%s", "限流")
    lg.error("chat HTTP 403：error.Forbidden/AccountOverdueError")

    events = handler.snapshot()
    levels = [e["level"] for e in events]
    assert levels == ["ERROR", "WARNING", "INFO"], f"最新在前契约违约：{levels}"
    assert "chat HTTP 403" in events[0]["line"]
    assert "装配完成" in events[2]["line"]
    assert handler.dropped_non_key >= 2  # debug + 普通 info 被白名单挡下


def test_handler_flattens_multiline_and_truncates():
    handler = recent_module.RecentEventsHandler()
    lg = _make_logger(handler)
    stack = "LLM 轮处理异常\nTraceback (most recent call last):\n  File \"x.py\", line 1\nLLMError: chat HTTP 403"
    lg.error(stack)
    (event,) = handler.snapshot()
    assert "\n" not in event["line"], f"事件正文含换行（单行契约违约）：{event['line']!r}"
    assert " ⏎ " in event["line"], "多行应拍平为单行并带 ⏎ 标记"
    assert len(event["line"]) <= recent_module.MAX_LINE_CHARS


def test_handler_deque_capacity_fifty_drops_oldest():
    handler = recent_module.RecentEventsHandler(capacity=recent_module.MAX_EVENTS)
    lg = _make_logger(handler)
    for i in range(60):
        lg.warning("警告 %d", i)
    events = handler.snapshot()
    assert len(events) == recent_module.MAX_EVENTS
    assert "警告 59" in events[0]["line"], "最新事件应在前"
    assert "警告 0" not in [e["line"] for e in events], "最旧事件应被环形缓冲挤掉"


def test_handler_emit_never_raises():
    """emit 遇到坑 record 绝不外抛（观测面故障不反噬业务日志管线）。"""

    class _Boom:
        def getMessage(self):  # noqa: N802 —— logging 约定名
            raise RuntimeError("record 炸了")

    handler = recent_module.RecentEventsHandler()
    record = logging.LogRecord("t", logging.ERROR, "p", 1, "正常消息", (), None)
    handler.emit(record)  # 正常路径
    boom = logging.LogRecord("t", logging.ERROR, "p", 1, "x", (), None)
    boom.getMessage = _Boom().getMessage  # type: ignore[method-assign]
    handler.emit(boom)    # 坑 record：吞掉不抛
    assert len(handler) == 1


def test_attach_global_handler_is_idempotent():
    root = logging.getLogger("test.attach.root")
    root.handlers = []
    h1 = recent_module.attach_global_handler(root)
    h2 = recent_module.attach_global_handler(root)
    assert h1 is h2
    assert sum(1 for x in root.handlers if x is h1) == 1, "重复装配会双份入缓冲"


# ---------------------------------------------------------------------------
# 2) read_log_tail 跨重启兜底
# ---------------------------------------------------------------------------

def test_read_log_tail_parses_vl_log_format(tmp_path):
    log = tmp_path / "vl.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    log.write_text(
        f"{stamp} INFO virtuallover.main: vl.toml 加载成功（schema v1）\n"
        f"{stamp} INFO virtuallover.brain.sender: [sender] 限流触发，stall 等待 2.22s\n"
        f"{stamp} ERROR virtuallover.brain.pipeline: LLM 轮处理异常\n"
        "Traceback (most recent call last):\n"
        "  File \"pipeline.py\", line 1, in <module>\n"
        "virtuallover.brain.llm.LLMError: chat HTTP 403：error.Forbidden/AccountOverdueError\n"
        f"{stamp} WARNING virtuallover.bridge.onebot_server: action send_private_msg 调用失败\n",
        encoding="utf-8",
    )
    events = recent_module.read_log_tail(log)
    assert [e["level"] for e in events] == ["WARNING", "ERROR", "INFO"], (
        f"应留 WARNING+ 与白名单 INFO（限流 INFO 挡下），最新在前：{events}")
    assert "chat HTTP 403" in events[1]["line"]
    assert all("\n" not in e["line"] for e in events)
    # 启动里程碑 INFO（vl.toml 加载成功）是白名单行——最新的排最前但应存在
    assert any("vl.toml 加载成功" in e["line"] for e in events), "关键 INFO 里程碑应进兜底结果"


def test_read_log_tail_missing_file_returns_empty(tmp_path):
    assert recent_module.read_log_tail(tmp_path / "nope.log") == []


# ---------------------------------------------------------------------------
# 3) recent_events_payload：缓冲优先、日志兜底
# ---------------------------------------------------------------------------

def test_payload_prefers_memory_then_falls_back_to_log(tmp_path):
    log = tmp_path / "vl.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    log.write_text(f"{stamp} ERROR virtuallover.x: 重启前的旧错误（403）\n", encoding="utf-8")

    # 缓冲为空 → 退日志尾部（跨重启兜底）
    empty_handler = recent_module.RecentEventsHandler()
    payload = recent_module.recent_events_payload(log, handler=empty_handler)
    assert payload["source"] == "log"
    assert payload["count"] == 1
    assert "403" in payload["events"][0]["line"]

    # 缓冲非空 → 进程内优先
    live = recent_module.RecentEventsHandler()
    lg = _make_logger(live)
    lg.error("本进程内的 403")
    payload2 = recent_module.recent_events_payload(log, handler=live)
    assert payload2["source"] == "memory"
    assert "本进程内的 403" in payload2["events"][0]["line"]


# ---------------------------------------------------------------------------
# 4) GET /api/recent-errors 端点（三重门继承）
# ---------------------------------------------------------------------------

def _request(port: int, method: str, path: str, *, body: "bytes | None" = None,
             headers: "dict | None" = None) -> "tuple[int, dict]":
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return resp.status, payload
    finally:
        conn.close()


def _login(port: int, auth_mgr: console_auth.AuthManager) -> dict:
    """登录拿会话 Cookie 头（test_console_voice.py 同款手法）。"""
    password = auth_mgr.load()["password"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/api/auth/login",
                     body=json.dumps({"password": password}).encode("utf-8"),
                     headers={"Content-Type": "application/json",
                              "X-Requested-With": "XMLHttpRequest"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200 and body["ok"] is True
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        return {"Cookie": cookie}
    finally:
        conn.close()


@pytest.fixture
def events_env(tmp_path):
    """动态端口控制台：/api/recent-errors + /api/status（带 counters 契约）。"""
    app_cfg = console_core.AppConfig.for_root(tmp_path, port=0)
    auth_mgr = console_auth.AuthManager(app_cfg.console_path)
    auth_mgr.load()
    status_payload = {
        "services": [{"key": "llm", "name": "LLM 客户端", "ok": True, "ms": 1,
                      "optional": False}],
        "generated_at": 1,
        "counters": {"interrupt": 2, "llm_fail": 1, "voice_degraded": 0, "since": 1},
    }
    httpd = console_app.make_server(
        "127.0.0.1", 0, app_cfg, auth=auth_mgr,
        status_provider=lambda: status_payload,
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(httpd=httpd, port=port, auth=auth_mgr,
                                    app_cfg=app_cfg, root=tmp_path)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_recent_errors_requires_session(events_env):
    status, payload = _request(events_env.port, "GET", "/api/recent-errors")
    assert status == 401, f"未登录应 401（三重门继承）：{payload}"


def test_recent_errors_ok_after_login(events_env):
    """登录后 200，且端点真的读进程级单例（403 事件经 root 流转出现在响应里）。"""
    cookie = _login(events_env.port, events_env.auth)
    handler = recent_module.get_global_handler()
    lg = logging.getLogger("test.recent.endpoint")
    lg.handlers = []
    lg.propagate = False
    lg.addHandler(handler)
    try:
        lg.error("chat HTTP 403：error.Forbidden/AccountOverdueError")
        status, payload = _request(events_env.port, "GET", "/api/recent-errors",
                                   headers=cookie)
        assert status == 200
        assert payload["source"] == "memory"
        assert payload["count"] == len(payload["events"]) >= 1
        assert any("chat HTTP 403" in e["line"] for e in payload["events"]), (
            f"端点应回出进程级单例里的 403 事件：{payload!r}")
    finally:
        lg.removeHandler(handler)


def test_recent_errors_falls_back_to_log_after_restart(events_env):
    """进程内缓冲为空（刚重启）→ 端点回 vl.log 尾部（跨重启兜底契约）。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    log_path = events_env.app_cfg.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"{stamp} ERROR virtuallover.brain.pipeline: LLM 调用最终失败：chat HTTP 403\n",
        encoding="utf-8")
    recent_module.get_global_handler()  # 单例存在但为空
    events = recent_module.read_log_tail(log_path)
    assert len(events) == 1 and "403" in events[0]["line"]
    payload = recent_module.recent_events_payload(log_path,
                                                  handler=recent_module.RecentEventsHandler())
    assert payload["source"] == "log" and payload["count"] == 1


# ---------------------------------------------------------------------------
# 5) /api/status 增强：counters + brain 黄灯 + embedding 降级
# ---------------------------------------------------------------------------

class _FakeChatClient:
    """鸭类型 ChatClient：recent_failure 可控。"""

    def __init__(self, failure: str = "") -> None:
        self._failure = failure

    def recent_failure(self, window_seconds: float = 600.0) -> str:
        return self._failure


class _FakeEmbeddingClient:
    """鸭类型 EmbeddingClient：breaker_open 可控。"""

    def __init__(self, open_: bool, remaining: float = 123.0) -> None:
        self._open = open_
        self._remaining = remaining

    def breaker_open(self) -> bool:
        return self._open

    def breaker_remaining_seconds(self) -> float:
        return self._remaining


def _fake_runtime(chat_failure: str = "", emb_open: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(
            chat=types.SimpleNamespace(api_base="https://ark.example/api/v3"),  # 占位端点
            tts=types.SimpleNamespace(enabled=False, base_url="")),
        chat_client=_FakeChatClient(chat_failure),
        embedding_client=_FakeEmbeddingClient(emb_open),
    )


def test_brain_row_yellow_when_probe_ok_but_recent_call_failed():
    """brain 行：探活可达 + 10min 内有调用失败 → ok 保持真值 + degraded 黄灯。"""
    row = console_core.brain_service_row(
        _fake_runtime(chat_failure="chat HTTP 403：error.Forbidden/AccountOverdueError"),
        fetch_fn=lambda url, timeout: (True, 12),
        brain_fetch_fn=lambda url, timeout: (True, 12),
    )
    assert row["key"] == "brain" and row["ok"] is True
    assert row["degraded"] is True
    assert "403" in row["note"] and "最近调用失败" in row["note"]


def test_brain_row_green_when_no_recent_failure():
    row = console_core.brain_service_row(
        _fake_runtime(chat_failure=""),
        fetch_fn=lambda url, timeout: (True, 12),
        brain_fetch_fn=lambda url, timeout: (True, 12),
    )
    assert row["ok"] is True and row["degraded"] is False and "note" not in row


def test_embedding_row_degraded_when_breaker_open():
    row = console_core.vl_status(_fake_runtime(emb_open=True))
    emb = next(s for s in row["services"] if s["key"] == "embedding")
    assert emb["ok"] is True and emb["degraded"] is True
    assert "记忆检索降级中" in emb["note"]


def test_embedding_row_normal_when_breaker_closed():
    row = console_core.vl_status(_fake_runtime(emb_open=False))
    emb = next(s for s in row["services"] if s["key"] == "embedding")
    assert emb["ok"] is True and not emb.get("degraded")


def test_vl_status_includes_counters_since_process_start():
    payload = console_core.vl_status(_fake_runtime())
    counters = payload["counters"]
    assert set(counters) >= {"interrupt", "llm_fail", "voice_degraded", "since"}
    assert counters["interrupt"] == 0  # fake runtime 无管线 → 0，绝不抛
    assert 0 < counters["since"] <= int(time.time()) + 1  # since=进程启动锚点


def test_counters_payload_reads_pipeline_and_voice_stats():
    runtime = types.SimpleNamespace(
        message_pipeline=types.SimpleNamespace(superseded_count=3, llm_fail_count=2),
        voice_manager=types.SimpleNamespace(stats={"degraded_skips": 5}),
    )
    counters = console_core.counters_payload(runtime)
    assert counters == {"interrupt": 3, "llm_fail": 2, "voice_degraded": 5,
                        "since": counters["since"]}
