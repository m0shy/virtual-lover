# -*- coding: utf-8 -*-
"""scripts/brain_switch.py ``_log`` 断管韧性测试（五轮修缮 R1 集成）。

场景：控制台 brain 动作以 capture_output 子进程跑 brain_switch；local/cloud
完成前的 restart_vl 会杀掉 vl 进程（含控制台父进程）——此后 stdout 管道读端
关闭，print 抛 OSError。修前：异常把 local/cloud 流程打断在拉起新 vl 之前
（有看护时靠看护兜底，无看护时 vl 直接死）；修后：断管被吞，切换流程走完。

安全规约：不落任何真实密钥/token/QQ 号。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "brain_switch.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("brain_switch_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BrokenPipeStdout(io.TextIOBase):
    """模拟父进程死后管道读端关闭：任何 write 都抛 OSError。"""

    def write(self, _s: str) -> int:
        raise OSError(22, "Broken pipe")

    def flush(self) -> None:
        raise OSError(22, "Broken pipe")


def test_log_survives_broken_pipe(capsys):
    """stdout 断管时 _log 不抛异常（切换流程不再被父进程死亡打断）。"""
    module = _load_module()
    real_stdout = sys.stdout
    sys.stdout = _BrokenPipeStdout()
    try:
        module._log("任意进度行")          # 修前在此抛 OSError
        module._log("第二行")
    finally:
        sys.stdout = real_stdout
    captured = capsys.readouterr()          # 断言未写到真 stdout（全部进断管桩）
    assert captured.out == ""


def test_log_normal_path_still_prints(capsys):
    """stdout 正常时行为不变：消息原样输出且 flush。"""
    module = _load_module()
    module._log("正常进度行")
    assert "正常进度行" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _server_ready：/v1/models 必须 200 才算就绪（503=模型加载中≠就绪）
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeUrlopen:
    """按序回放响应/异常的最小 urlopen 替身。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)   # int 状态码或异常实例
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        item = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


def _http_error(code: int):
    import urllib.error
    return urllib.error.HTTPError("http://127.0.0.1/x", code, "reason", {}, io.BytesIO(b""))


def test_server_ready_requires_200():
    """200→就绪；503（加载中）/连接拒绝→未就绪（修前 _port_listening 把 503 当活）。"""
    module = _load_module()
    assert module._server_ready(8002, urlopen=_FakeUrlopen([200])) is True
    assert module._server_ready(8002, urlopen=_FakeUrlopen([_http_error(503)])) is False
    import urllib.error
    assert module._server_ready(
        8002, urlopen=_FakeUrlopen([urllib.error.URLError("conn refused")])) is False


def test_chat_probe_retries_503_then_succeeds():
    """chat_probe：首个 503（加载中）重试，随后 200 的对话响应正常返回。"""
    module = _load_module()
    completion = json.dumps(
        {"choices": [{"message": {"content": "你好呀主人"}}],
         "usage": {"completion_tokens": 5}}).encode("utf-8")

    class _ChatResponse(_FakeResponse):
        def read(self):
            return completion

    class _ChatUrlopen(_FakeUrlopen):
        def __call__(self, request, timeout=None):
            self.calls += 1
            item = self.outcomes.pop(0) if self.outcomes else 200
            if isinstance(item, Exception):
                raise item
            return _ChatResponse(item)

    fake = _ChatUrlopen([_http_error(503), 200])
    reply = module.chat_probe(max_wait_seconds=10, urlopen=fake)
    assert reply == "你好呀主人"
    assert fake.calls == 2


def test_chat_probe_non_503_raises_immediately():
    """非 503 的 HTTP 错误不重试，立即抛（快速失败语义保持）。"""
    module = _load_module()
    fake = _FakeUrlopen([_http_error(500)])
    try:
        module.chat_probe(max_wait_seconds=10, urlopen=fake)
    except Exception as exc:
        assert getattr(exc, "code", None) == 500
    else:
        raise AssertionError("非 503 错误应抛出")
    assert fake.calls == 1
