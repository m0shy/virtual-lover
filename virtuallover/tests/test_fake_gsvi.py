# -*- coding: utf-8 -*-
"""fake_gsvi.py 自身行为锁定（VOICE_SPEC §4「fake GSVI 契约」项）。

fake 是全组语音测试的地基——它若失真，上层 gsvi/manager/cache/lifecycle 的
测试全部建在流沙上。本文件把 fake 的可观测行为逐条钉死：

- 三怪癖保真（GSVI 源码裁定，见 fake_gsvi.py 模块 docstring 的行号依据）：
  string 参数 .lower() / 未注册情绪静默 200+wav / 未注册角色 400 JSON；
- 空角色名走"当前角色"路径（不是 400）；
- /character_list 返回注册表；
- 五种故障模式 OK/HANG/503/NON_WAV/CONN_RESET 的客户端可见行为；
- 请求起止时间记录完整（串行化断言的数据面：start/end 单调、耗时≥latency）。

客户端一律用与 voice/gptsovits.py 同源的 stdlib urllib（不带重试，逐请求
裸打），保证断言的是 fake 的行为而非客户端重试的叠加效果。

运行：python -m pytest virtuallover/tests/test_fake_gsvi.py -q
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from fake_gsvi import DEFAULT_CHARACTERS, MIN_WAV, FakeGsvi, MODES

# GSVI 真实怪癖：string 参数在服务端被 .lower()——客户端大小写再正确也会被改写
LOWER_PROOF_CHARACTER = "Huohua"
LOWER_PROOF_EMOTION = "疯批"


def _get(base_url: str, query: str, timeout: float = 5.0):
    """裸 GET（无重试）：返回 (status, body)。HTTPError 也带 status/body。"""
    url = f"{base_url}{query}"
    try:
        from virtuallover.net import smart_urlopen  # fake GSVI 在 127.0.0.1：禁代理直连
        with smart_urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _tts_query(text: str = "你好", character: str = LOWER_PROOF_CHARACTER,
               emotion: str = LOWER_PROOF_EMOTION, lang: str = "zh") -> str:
    from urllib.parse import urlencode

    return "/tts?" + urlencode({
        "text": text,
        "cha_name": character,          # 别名成对携带（SPEC §9 客户端契约）
        "character_emotion": emotion,
        "text_language": lang,
        "format": "wav",
    })


# ---------------------------------------------------------------------------
# 三怪癖保真
# ---------------------------------------------------------------------------

class TestQuirkStringLower:
    def test_string_params_lowered_effective_kept_raw(self, fake_gsvi):
        """怪癖①：string 参数在服务端被 .lower()——effective 全小写、params 留原样。

        真实 GSVI 在 tts_backend.py:125 对 string 型参数 unquote+lower；英文字符
        名 Huohua 一律变 huohua，中文（疯批）不受影响。
        """
        status, body = _get(fake_gsvi.base_url, _tts_query(text="Hello World"))
        assert status == 200 and body == MIN_WAV
        record = fake_gsvi.tts_requests[-1]
        assert record["effective"]["character"] == "huohua"
        assert record["effective"]["text"] == "hello world"
        assert record["effective"]["emotion"] == LOWER_PROOF_EMOTION  # 中文不变
        assert record["effective"]["text_language"] == "zh"
        assert record["params"]["cha_name"] == LOWER_PROOF_CHARACTER  # 原始 query 保留

    def test_emotion_lookup_after_lower(self, fake_gsvi):
        """大小写变体角色名按注册表命中（Windows 文件系统 + .lower() 组合语义）。"""
        status, _ = _get(fake_gsvi.base_url, _tts_query(character="HUTAO"))
        assert status == 200


class TestQuirkUnregisteredEmotion:
    def test_unregistered_emotion_silent_200_wav(self, fake_gsvi):
        """怪癖②：未注册情绪静默 200 + wav（load_infer_info.py:230-244 回退 default）。

        反向含义：非法情绪永不出错——联动哑火必须靠客户端注册表校验兜底
        （VOICE_SPEC §1 回退链的动机），本断言钉死这一契约面。
        """
        status, body = _get(fake_gsvi.base_url, _tts_query(emotion="根本不存在的情绪"))
        assert status == 200
        assert body.startswith(b"RIFF"), "未注册情绪必须照常回 wav，而不是报错体"

    def test_registered_emotion_also_200(self, fake_gsvi):
        status, body = _get(fake_gsvi.base_url, _tts_query())
        assert status == 200 and body == MIN_WAV


class TestQuirkUnregisteredCharacter:
    def test_unregistered_character_400_json(self, fake_gsvi):
        """怪癖③：未注册角色 → 400 + JSON error 体（tts_backend.py:153-154）。"""
        status, body = _get(fake_gsvi.base_url, _tts_query(character="不存在角色"))
        assert status == 400
        payload = json.loads(body)
        assert "error" in payload
        # 400 体不是 wav：客户端 RIFF 帧头校验 + HTTP 码双保险都不会误吞
        assert not body.startswith(b"RIFF")

    def test_empty_character_uses_current_character_200(self, fake_gsvi):
        """空/缺 cha_name → expected_path 为 None → 用当前角色照常 200
        （tts_backend.py:146-149；不落 400 路径）。"""
        from urllib.parse import urlencode

        query = "/tts?" + urlencode({"text": "只发文本"})
        status, body = _get(fake_gsvi.base_url, query)
        assert status == 200 and body == MIN_WAV


# ---------------------------------------------------------------------------
# /character_list 与注册表
# ---------------------------------------------------------------------------

def test_character_list_returns_registry(fake_gsvi):
    """/character_list 返回 {角色原始名: [情绪...]}（tts_backend.py:76-80 保真）。"""
    status, body = _get(fake_gsvi.base_url, "/character_list")
    assert status == 200
    payload = json.loads(body)
    assert payload == DEFAULT_CHARACTERS
    # 原始大小写键保留（客户端注册表缓存直接可用，无需再猜磁盘目录名）
    assert "Huohua" in payload and "Hutao" in payload


def test_custom_registry_is_honored():
    """注册表可注入：fake 不绑死默认角色（不同用例各自圈定契约面）。"""
    registry = {"测试角色": ["default", "开心"]}
    with FakeGsvi(characters=registry) as gsvi:
        status, _ = _get(gsvi.base_url, _tts_query(character="测试角色", emotion="开心"))
        assert status == 200
        status, body = _get(gsvi.base_url, _tts_query(character="别的角色"))
        assert status == 400
        status, body = _get(gsvi.base_url, "/character_list")
        assert json.loads(body) == registry


def test_unknown_route_404(fake_gsvi):
    status, _ = _get(fake_gsvi.base_url, "/infer_single")
    assert status == 404


# ---------------------------------------------------------------------------
# 故障模式（客户端可见行为）
# ---------------------------------------------------------------------------

def test_mode_503_is_http_error(fake_gsvi):
    fake_gsvi.set_mode("503")
    status, _ = _get(fake_gsvi.base_url, _tts_query())
    assert status == 503


def test_mode_nonwav_is_200_json(fake_gsvi):
    """NON_WAV：200 + JSON 报错体——真实服务端出错的坏样子（gptsovits.py 靠
    RIFF 帧头识破的防御对象）。"""
    fake_gsvi.set_mode("NON_WAV")
    status, body = _get(fake_gsvi.base_url, _tts_query())
    assert status == 200
    assert not body.startswith(b"RIFF")
    assert "error" in json.loads(body)


def test_mode_hang_blocks_client_until_timeout(fake_gsvi):
    """HANG：不回包挂起，客户端侧超时（真实引擎假死的形态）。"""
    fake_gsvi.set_mode("HANG")
    fake_gsvi.hang_seconds = 0.6  # 缩短挂起，stop() 放行不跨用例滞留
    start = time.monotonic()
    with pytest.raises(Exception) as exc_info:  # socket.timeout / TimeoutError
        _get(fake_gsvi.base_url, _tts_query(), timeout=0.2)
    assert time.monotonic() - start < 1.5  # 客户端按自己的超时先断
    assert not isinstance(exc_info.value, urllib.error.HTTPError)  # 无 HTTP 响应
    # 处理线程放行后补 end_ts（轮询到记录闭环，不留悬挂线程噪声）
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and fake_gsvi.tts_requests[-1]["end_ts"] is None:
        time.sleep(0.02)
    assert fake_gsvi.tts_requests[-1]["status"] == "hang"


def test_mode_conn_reset_abruptly_closes(fake_gsvi):
    """CONN_RESET：无响应直接断链——客户端侧 RemoteDisconnected/URLError
    （gptsovits.py:74-75 按 OSError 网络故障处理的形态）。"""
    fake_gsvi.set_mode("CONN_RESET")
    with pytest.raises((urllib.error.URLError, OSError)) as exc_info:
        _get(fake_gsvi.base_url, _tts_query())
    assert not isinstance(exc_info.value, urllib.error.HTTPError)


def test_text_prefix_overrides_mode_per_request(fake_gsvi):
    """文本前缀请求级覆盖：全局 OK 中单独一挂一断一错，互不影响后续请求。

    前缀匹配发生在 .lower() 之后的文本上（fake_gsvi.DEFAULT_TEXT_PREFIX_MODES
    全小写键）——发大写 [HANG] 也能命中，正是怪癖①的活体验证。
    """
    fake_gsvi.latency = 0.0
    fake_gsvi.hang_seconds = 0.3
    status, _ = _get(fake_gsvi.base_url, _tts_query(text="[503]这条必失败"))
    assert status == 503
    status, body = _get(fake_gsvi.base_url, _tts_query(text="[NONWAV]假成功"))
    assert status == 200 and not body.startswith(b"RIFF")
    with pytest.raises((urllib.error.URLError, OSError)):  # CONN_RESET 无响应断链
        _get(fake_gsvi.base_url, _tts_query(text="[RESET]断链"))
    status, body = _get(fake_gsvi.base_url, _tts_query(text="恢复正常"))
    assert status == 200 and body == MIN_WAV  # 全局模式仍是 OK
    modes = [r["mode"] for r in fake_gsvi.tts_requests]
    assert modes == ["503", "NON_WAV", "CONN_RESET", "OK"]


def test_set_mode_validates_and_switches(fake_gsvi):
    """set_mode 校验模式名；运行中切换立即生效。"""
    with pytest.raises(ValueError):
        fake_gsvi.set_mode("随便什么")
    for mode in MODES:
        fake_gsvi.set_mode(mode)
        assert fake_gsvi.mode == mode


# ---------------------------------------------------------------------------
# 请求起止时间记录（串行化断言的数据面）
# ---------------------------------------------------------------------------

@pytest.mark.flaky(reruns=3, reruns_delay=1)
def test_request_windows_recorded_and_sequential_non_overlapping(fake_gsvi):
    """顺序请求：每条 start/end 完整、耗时≥latency、时间窗零重叠。"""
    fake_gsvi.latency = 0.05
    _get(fake_gsvi.base_url, _tts_query(text="第一句"))
    _get(fake_gsvi.base_url, _tts_query(text="第二句"))
    records = fake_gsvi.tts_requests
    assert len(records) == 2
    for record in records:
        assert record["start_ts"] is not None and record["end_ts"] is not None
        assert record["end_ts"] - record["start_ts"] >= 0.05
        assert record["status"] == 200
    assert records[1]["start_ts"] >= records[0]["end_ts"], "顺序请求时间窗不得重叠"


def test_concurrent_requests_all_recorded(fake_gsvi):
    """并发请求：8 条全部入账、start/end 完整（零重叠断言留给 VoiceManager 层——
    fake 本身是 ThreadingHTTPServer，允许并行）。"""
    fake_gsvi.latency = 0.05
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_get, fake_gsvi.base_url, _tts_query(text=f"并发{i}"))
            for i in range(8)
        ]
        results = [f.result() for f in futures]
    assert all(status == 200 for status, _ in results)
    records = fake_gsvi.wait_for_requests(8)
    for record in records:
        assert record["start_ts"] is not None and record["end_ts"] is not None
    # start 单调有序（记录按开始时间追加）
    starts = [r["start_ts"] for r in records]
    assert starts == sorted(starts)


def test_client_sees_riff_magic_and_params_via_gptsovits(fake_gsvi):
    """与 voice.gptsovits.GptSoVitsClient 对拍：真实客户端路径下参数/字节全对。"""
    from virtuallover.voice.gptsovits import GptSoVitsClient

    client = GptSoVitsClient(base_url=fake_gsvi.base_url, character="Huohua",
                             emotion="疯批", text_lang="zh", backoff_base=0.0)
    audio = asyncio.run(client.synthesize("对拍一句"))
    assert audio == MIN_WAV
    record = fake_gsvi.tts_requests[-1]
    assert record["effective"]["character"] == "huohua"
    assert record["effective"]["emotion"] == LOWER_PROOF_EMOTION
    assert record["effective"]["format"] == "wav"
