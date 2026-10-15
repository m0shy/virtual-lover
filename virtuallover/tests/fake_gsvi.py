# -*- coding: utf-8 -*-
"""fake GSVI 契约服务器（VOICE_SPEC §4）：供全组测试复用的假 GPT-SoVITS-Inference。

行为保真依据（GSVI 为 MIT 许可，源码可读；行号出语音引擎包（voice-engine/，上游名 GPT-SoVITS-Inference））：
- **string 参数 .lower()**（tts_backend.py:125）：全部 string 型参数取值时一律
  ``unquote(data[alias]).lower()``——text / cha_name / character_emotion /
  text_language / format 请求进来都被转小写。别名表见 Inference/params_config.json
  （cha_name 的别名含 character；character_emotion 的别名含 emotion）。
- **未注册情绪 → 静默 200 + wav**（load_infer_info.py:230-244）：character_emotion
  与角色 emotion_list 精确比对，未命中静默回退 "default" 继续合成——**没有任何
  报错路径**。反向含义：客户端发非法情绪不报错，联动哑火只能靠注册表在客户端
  侧校验（VOICE_SPEC §1 映射回退链的动机）。
- **未注册角色 → 400 + JSON**（tts_backend.py:153-154）：cha_name 对应目录不存在
  时 ``jsonify({"error": ...}), 400``。角色注册表 = models_path 子目录 +
  infer_config.json 的 emotion_list（load_infer_info.py:288-307）。
  注意真实服务器把 cha_name 转小写后拼路径，Windows 文件系统大小写不敏感，
  故注册匹配按大小写不敏感建模（键即 trained/ 下的原始目录名）。
- 附加保真：空/缺 cha_name → expected_path 为 None → 用当前角色照常合成 200
  （tts_backend.py:146-149）；/character_list 返回 {角色: [情绪...]} 注册表
  （tts_backend.py:76-80）。

故障模式（模拟真实引擎的四种坏样子 + 正常）：
  OK        正常 200 + wav；受 latency 控制的人为耗时（默认 0.05s，供串行化
            断言测出时间窗）；
  HANG      挂起 hang_seconds（默认 120s，长于客户端 60s 超时）不回包；
  503       HTTP 503；
  NON_WAV   200 + JSON 报错体（真实服务端出错时确实会这样——gptsovits.py
            靠 RIFF 帧头判真伪的防御对象）；
  CONN_RESET 不回包直接断开连接（客户端侧 RemoteDisconnected/URLError）。

模式选择优先级：文本前缀覆盖（lowercase 之后匹配，与 .lower() 怪癖一致）→
服务器全局 mode（可运行中切换）。

请求记录：每个请求在开始处理时即在 ``requests`` 追加一条记录（start_ts 用
time.monotonic），处理完毕补 end_ts——串行化断言（VOICE_SPEC §4 最高优先：
并发调用零重叠）直接读这两个字段；in-flight 请求也可被观测。

依赖策略：仅标准库（http.server + threading），遵守 SPEC §1 依赖白名单。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

__all__ = ["FakeGsvi", "MODES", "MIN_WAV", "DEFAULT_CHARACTERS"]

# 最小合法 wav（RIFF 帧头 + fmt/data 块，≥44 字节）——与真实 GSVI 返回完整
# wav 流同构，客户端 RIFF 帧头校验可过。
MIN_WAV = b"RIFF" + b"\x00" * 36 + b"WAVEfmt " + b"\x00" * 16 + b"data" + b"\x00" * 8

# 全部故障模式（set_mode 校验用）
MODES = ("OK", "HANG", "503", "NON_WAV", "CONN_RESET")

# 文本前缀 → 模式（请求级覆盖）。匹配发生在 .lower() 之后，故前缀全小写——
# 这本身就是 string 参数 .lower() 怪癖的活体验证。
DEFAULT_TEXT_PREFIX_MODES = {
    "[503]": "503",
    "[hang]": "HANG",
    "[nonwav]": "NON_WAV",
    "[reset]": "CONN_RESET",
}

# 默认角色注册表：对齐真实部署（trained/ 下 Huohua、Hutao 两个角色目录）与
# VOICE_SPEC §0 终裁（Huohua 为 7 情绪承载音色）。键保持磁盘上的原始大小写
# （模拟 character_info.json），请求匹配大小写不敏感。
DEFAULT_CHARACTERS = {
    "Huohua": ["default", "傻白甜", "疯批", "慢速病娇", "平常的", "聊天", "平静的"],
    "Hutao": ["default"],
}


class FakeGsvi:
    """线程内假 GSVI：随机端口 HTTP 服务 + 请求记录 + 可切换故障模式。

    用法（也见 virtuallover/tests/test_fake_gsvi.py）::

        with FakeGsvi() as gsvi:              # 或 FakeGsvi(mode="503")
            audio = await client.synthesize("你好")   # client.base_url = gsvi.base_url
            assert gsvi.tts_requests[-1]["effective"]["character"] == "huohua"
            gsvi.set_mode("HANG")             # 运行中切换故障模式

    断言面：
    - ``requests``：全部请求（含 /character_list），按开始时间追加；
    - ``tts_requests``：仅 /tts 的请求；
    - 每条记录字段：path / params（原始 query）/ effective（.lower() 怪癖后的
      生效值）/ mode / status / start_ts / end_ts（monotonic 秒）。
    """

    def __init__(
        self,
        *,
        characters: "dict[str, list[str]] | None" = None,
        mode: str = "OK",
        latency: float = 0.05,
        hang_seconds: float = 120.0,
        wav_body: bytes = MIN_WAV,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"未知模式 {mode!r}，可选：{'/'.join(MODES)}")
        # 注册表保持原始大小写键（模拟 trained/ 目录名与 character_info.json）
        self.characters: "dict[str, list[str]]" = dict(
            characters if characters is not None else DEFAULT_CHARACTERS
        )
        self.mode = mode                  # 全局模式（运行中可改，见 set_mode）
        self.latency = float(latency)     # OK/NON_WAV 模式的人为耗时
        self.hang_seconds = float(hang_seconds)
        self.wav_body = wav_body
        self.text_prefix_modes = dict(DEFAULT_TEXT_PREFIX_MODES)
        self.requests: "list[dict]" = []  # 请求记录（开始时追加，结束时补 end_ts）
        self._lock = threading.Lock()
        self._release = threading.Event()  # stop() 时放行挂起中的 HANG 处理线程
        self._httpd: "ThreadingHTTPServer | None" = None
        self._thread: "threading.Thread | None" = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> "FakeGsvi":
        """绑定随机端口（host 127.0.0.1）并起守护线程，返回 self（便于链式）。"""
        if self._httpd is not None:
            raise RuntimeError("FakeGsvi 已启动")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        httpd.daemon_threads = True
        httpd.fake = self  # handler 经 self.server.fake 反查
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """停服务并放行挂起中的 HANG 线程（幂等）。"""
        self._release.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeGsvi":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("FakeGsvi 未启动")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    # ------------------------------------------------------------------ 断言面

    @property
    def tts_requests(self) -> "list[dict]":
        """仅 /tts 请求的记录视图。"""
        return [r for r in self.requests if r["path"] == "/tts"]

    def set_mode(self, mode: str) -> None:
        """运行中切换全局故障模式（校验合法性）。"""
        if mode not in MODES:
            raise ValueError(f"未知模式 {mode!r}，可选：{'/'.join(MODES)}")
        with self._lock:
            self.mode = mode

    def wait_for_requests(self, count: int, timeout: float = 5.0) -> "list[dict]":
        """轮询等待记录数达到 count（并发用例收尾同步），超时抛 AssertionError。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.requests) >= count:
                return list(self.requests)
            time.sleep(0.01)
        raise AssertionError(
            f"等待 {count} 条请求超时（当前 {len(self.requests)} 条）"
        )

    # ------------------------------------------------------------------ 内部

    def _lookup_character(self, cha_name: str) -> "str | None":
        """按注册表查角色（大小写不敏感，建模 Windows 文件系统 + .lower() 怪癖）。

        返回注册表原始键；未注册返回 None。空名返回 ""（用当前角色，非 400 路径）。
        """
        if not cha_name:
            return ""
        lowered = cha_name.lower()
        for name in self.characters:
            if name.lower() == lowered:
                return name
        return None

    def _pick_mode(self, effective_text: str) -> str:
        """模式选择：文本前缀覆盖（在 .lower() 之后的文本上匹配）→ 全局 mode。"""
        for prefix, mode in self.text_prefix_modes.items():
            if effective_text.startswith(prefix):
                return mode
        with self._lock:
            return self.mode

    def _record_start(self, path: str, params: dict, effective: dict, mode: str) -> dict:
        """请求开始即追加记录（in-flight 可观测；结束时补 end_ts）。"""
        entry = {
            "path": path,
            "params": params,
            "effective": effective,
            "mode": mode,
            "status": None,
            "start_ts": time.monotonic(),
            "end_ts": None,
        }
        with self._lock:
            self.requests.append(entry)
        return entry

    def _record_end(self, entry: dict, status: Any) -> None:
        with self._lock:
            entry["end_ts"] = time.monotonic()
            entry["status"] = status

    def _build_handler(self) -> type:
        """构造绑定本 fake 实例的 BaseHTTPRequestHandler 子类。"""
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # 一请求一连接：CONN_RESET 语义最干净

            def do_GET(self) -> None:  # noqa: N802 —— 标准库命名约定
                parsed = urlparse(self.path)
                raw_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == "/tts":
                    self._handle_tts(raw_params)
                elif parsed.path == "/character_list":
                    self._handle_character_list()
                else:
                    self._send_json(404, {"error": f"Unknown route: {parsed.path}"})

            def _handle_character_list(self) -> None:
                entry = outer._record_start("/character_list", {}, {}, "OK")
                outer._record_end(entry, 200)
                # 真实端点返回 update_character_info()['characters_and_emotions']
                # （tts_backend.py:76-80），即 {角色原始名: [情绪...]}
                self._send_json(200, dict(outer.characters))

            def _handle_tts(self, raw_params: dict) -> None:
                # 怪癖①：string 型参数一律 .lower()（tts_backend.py:125）。
                # text / cha_name / character_emotion / text_language / format 全在此列。
                effective = {
                    "text": str(raw_params.get("text", "")).lower(),
                    "character": str(raw_params.get("cha_name", "")).lower(),
                    "emotion": str(raw_params.get("character_emotion", "")).lower(),
                    "text_language": str(raw_params.get("text_language", "")).lower(),
                    "format": str(raw_params.get("format", "")).lower(),
                }
                mode = outer._pick_mode(effective["text"])
                entry = outer._record_start("/tts", raw_params, effective, mode)

                done = False  # end_ts 恰好落一次：正常路径记真实状态，写失败兜底
                try:
                    # 怪癖③：未注册角色 → 400 + JSON（tts_backend.py:153-154）。
                    # 空/缺 cha_name 走"当前角色"路径（tts_backend.py:146-149），
                    # 不是 400。
                    registered = outer._lookup_character(effective["character"])
                    if effective["character"] and registered is None:
                        body = {
                            "error": (
                                f"Directory trained/{effective['character']} does not "
                                f"exist. Using the current character."
                            )
                        }
                        self._send_json(400, body)
                        done = True
                        outer._record_end(entry, 400)
                    elif mode == "503":
                        self.send_error(503)
                        done = True
                        outer._record_end(entry, 503)
                    elif mode == "HANG":
                        # 挂起不回包；end_ts 记放行时刻；stop() 置位可提前放行
                        outer._release.wait(outer.hang_seconds)
                        done = True
                        outer._record_end(entry, "hang")
                    elif mode == "NON_WAV":
                        # 真实服务端出错的坏样子：200 + JSON 报错体（非 wav）
                        if outer.latency:
                            outer._release.wait(outer.latency)
                        self._send_json(200, {"error": "server error (non-wav body)"})
                        done = True
                        outer._record_end(entry, 200)
                    elif mode == "CONN_RESET":
                        self._abort_connection()
                        done = True
                        outer._record_end(entry, "conn-reset")
                    else:  # OK
                        if outer.latency:
                            outer._release.wait(outer.latency)
                        self._send_wav(outer.wav_body)
                        done = True
                        outer._record_end(entry, 200)
                finally:
                    if not done:
                        # 客户端先超时/断开（HANG、CONN_RESET 的常态）：写失败吞掉，
                        # end_ts 仍要落，串行化断言面保持完整。
                        outer._record_end(entry, "client-aborted")

            def _send_json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_wav(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _abort_connection(self) -> None:
                """CONN_RESET：一个字节都不回，直接掐断 socket。

                客户端侧表现为 http.client.RemoteDisconnected（ConnectionResetError
                子类）或 URLError——gptsovits 客户端按 OSError 网络故障重试。
                """
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.connection.close()
                except OSError:
                    pass

            def log_message(self, *args: Any) -> None:  # 静默测试输出
                pass

        return _Handler
