# -*- coding: utf-8 -*-
"""voice/gptsovits.py 契约测试（SPEC §9）：假 TTS server 走真实 HTTP。

覆盖：GET /tts 别名参数齐全、成功返回 wav 字节、失败/超时重试 3 次后回退
（返回 None 由发送侧回退纯文字）、200+非 wav 响应体判失败、空文本拒绝。

不引入 pytest-asyncio（依赖仅 websockets+numpy）：异步客户端用 asyncio.run
在同步测试内驱动。

运行：python -m pytest virtuallover/tests/test_gptsovits.py -q
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any, Tuple

import pytest

from virtuallover.voice.gptsovits import GptSoVitsClient

# 最小合法 wav（RIFF 帧头 + fmt/data 块，≥44 字节）
_WAV_OK = b"RIFF" + b"\x00" * 36 + b"WAVEfmt " + b"\x00" * 16 + b"data" + b"\x00" * 8
_HOST = "127.0.0.1"  # 测试自建服务，与生产绑定一致


class FakeTtsServer:
    """线程内假 GPT-SoVITS：按 text 前缀决定行为，记录全部 query 供断言。

    行为表（text 前缀 → 响应）：
      FAIL → 503（重试耗尽场景）；SLOW → 挂起 0.6s（触发客户端超时）；
      BAD  → 200 + JSON 报错体（非 wav）；其余 → 200 + 最小合法 wav。

    应答统一走 ``_respond``：客户端超时弃连后（SLOW 场景必现），服务端往
    已断开的 socket 写响应会抛 ConnectionError（Windows 下 BrokenPipe/
    ConnectionReset）——必须就地吞掉，否则 socketserver 把栈刷到 stderr，
    全量 xdist 的「stderr 无栈 + exit 0」验收（PERF_SPEC §4-O13/§7.2）被
    测试自身污染。
    """

    def __init__(self) -> None:
        self.requests: list = []
        self.lock = threading.Lock()

    def build_handler(self) -> type:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def _respond(self, status: int, body: bytes,
                         ctype: str = "audio/wav") -> None:
                """统一应答出口：对端弃连（ConnectionError 族）静默吞掉不刷栈。"""
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except ConnectionError:
                    # 客户端已超时断开（如 SLOW 用例 0.15s 弃连 vs 0.6s 应答）：
                    # 响应写不进去属测试收尾竞态，非缺陷，绝不往 stderr 刷栈
                    pass

            def do_GET(self) -> None:  # noqa: N802 —— 标准库命名约定
                query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                with outer.lock:
                    outer.requests.append(query)
                text = query.get("text", "")
                if text.startswith("FAIL"):
                    self._respond(503, b'{"code": 503, "msg": "fail"}',
                                  ctype="application/json")
                elif text.startswith("SLOW"):
                    threading.Event().wait(0.6)
                    self._respond(200, _WAV_OK)
                elif text.startswith("BAD"):
                    self._respond(200, b'{"code": 1, "msg": "server error"}',
                                  ctype="application/json")
                else:
                    self._respond(200, _WAV_OK)

            def log_message(self, *args: Any) -> None:  # 静默测试输出
                pass

        return _Handler


@pytest.fixture()
def fake_tts() -> Tuple[str, FakeTtsServer]:
    """起一个随机端口的假 TTS server，返回 (base_url, server)，用完即关。"""
    server = FakeTtsServer()
    httpd = ThreadingHTTPServer((_HOST, 0), server.build_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://{_HOST}:{httpd.server_address[1]}", server
    httpd.shutdown()
    httpd.server_close()


def _client(base_url: str, **kw: Any) -> GptSoVitsClient:
    defaults: dict = dict(base_url=base_url, character="测试角色", emotion="平静",
                          text_lang="zh", backoff_base=0.0)  # 重试不等待，加速测试
    defaults.update(kw)
    return GptSoVitsClient(**defaults)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 契约断言
# ---------------------------------------------------------------------------

def test_synthesize_ok_returns_wav(fake_tts):
    url, _ = fake_tts
    audio = _run(_client(url).synthesize("你好呀"))
    assert audio == _WAV_OK


def test_alias_params_all_sent(fake_tts):
    """SPEC §9：两组已注册别名 + format=wav 全部出现在 GET /tts 的 query 里。"""
    url, server = fake_tts
    _run(_client(url).synthesize("参数检查"))
    assert server.requests, "假服务未收到请求"
    q = server.requests[-1]
    for key in ("text", "cha_name", "character", "character_emotion",
                "emotion", "text_language", "lang", "format"):
        assert key in q, f"缺少别名参数 {key}"
    assert q["cha_name"] == q["character"] == "测试角色"
    assert q["character_emotion"] == q["emotion"] == "平静"
    assert q["text_language"] == q["lang"] == "zh"
    assert q["format"] == "wav"


def test_retry_three_times_then_fallback(fake_tts):
    """连续 503 → 恰好 3 次尝试 → 返回 None（发送侧据此回退纯文字）。"""
    url, server = fake_tts
    assert _run(_client(url, retries=3).synthesize("FAIL这条一定失败")) is None
    assert len(server.requests) == 3


def test_timeout_counts_as_failure(fake_tts):
    """响应超过超时（此处压缩为 0.15s 测试，生产 60s）→ 算失败并重试后回退。"""
    url, _ = fake_tts
    assert _run(_client(url, timeout=0.15).synthesize("SLOW慢响应")) is None


def test_non_wav_body_is_failure(fake_tts):
    """200 + JSON 报错体（无 RIFF 帧头）→ 判失败并重试耗尽。"""
    url, server = fake_tts
    assert _run(_client(url, retries=2).synthesize("BAD假成功")) is None
    assert len(server.requests) == 2


def test_empty_text_rejected():
    """空文本是编程错误（发送侧应先过滤），直接 ValueError 而非打服务。"""
    with pytest.raises(ValueError):
        _run(GptSoVitsClient().synthesize("   "))
