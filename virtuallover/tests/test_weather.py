# -*- coding: utf-8 -*-
"""soul/weather.py 天气感知契约测试（主动陪伴升级：她现在会聊天气了）。

覆盖点（任务书）：
- j1 载荷解析：current_condition 的 temp_C/weatherDesc/lang_zh、今日 min/max，
  输出行格式 ``[资料·天气] {city} 当前{desc} {t}°C（今日 {min}~{max}°C）``；
- 缓存：45 分钟内同城市命中缓存不再发请求；过期/换城市重新拉取；
- 静默失败：urlopen 异常 / HTTP 错误 / 非法 JSON / 缺字段 / 空 city 一律 None；
- 意图门：maybe_weather_block 只在消息命中（天气|下雨|下雪|带伞|冷不冷|
  热不热|气温|温度）且 city 非空时拉取。

urlopen 全部注入假件（test_websearch 同款手法），零真实外网请求。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul import weather as weather_mod

CITY = "测试市"


class _FakeResponse:
    """最小响应假件：with 语义 + read()。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _RecordingUrlopen:
    """记录请求并按脚本应答的假 urlopen（签名对齐 websearch 的 UrlOpen 契约）。"""

    def __init__(self, payload: bytes | Exception):
        self.payload = payload
        self.urls: list[str] = []
        self.timeouts: "list[float | None]" = []

    def __call__(self, request, timeout=None):
        self.urls.append(str(request.full_url))
        self.timeouts.append(timeout)
        if isinstance(self.payload, Exception):
            raise self.payload
        return _FakeResponse(self.payload)


def j1_payload(temp="22", desc_en="Sunny", desc_zh="晴", low="18", high="28") -> bytes:
    """wttr.in ?format=j1 形态的最小夹具（字段名照公开 j1 结构）。"""
    condition = {"temp_C": temp, "weatherDesc": [{"value": desc_en}]}
    if desc_zh is not None:
        condition["lang_zh"] = [{"value": desc_zh}]
    return json.dumps({
        "current_condition": [condition],
        "weather": [{"mintempC": low, "maxtempC": high}],
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# 解析与输出行格式
# ---------------------------------------------------------------------------

def test_render_line_full_format():
    """lang_zh 中文描述 + 今日 min/max 全在场时的精确输出。"""
    line = weather_mod.render_weather_line(CITY, json.loads(j1_payload()))
    assert line == f"[资料·天气] {CITY} 当前晴 22°C（今日 18~28°C）"


def test_render_line_falls_back_to_english_desc():
    """无 lang_zh 数组（lang=zh 未生效）回退英文 weatherDesc。"""
    line = weather_mod.render_weather_line(
        CITY, json.loads(j1_payload(desc_zh=None)))
    assert line == f"[资料·天气] {CITY} 当前Sunny 22°C（今日 18~28°C）"


def test_render_line_without_today_bounds():
    """缺今日 min/max 时只输出当前态，不编造区间。"""
    payload = json.loads(j1_payload())
    payload.pop("weather")
    line = weather_mod.render_weather_line(CITY, payload)
    assert line == f"[资料·天气] {CITY} 当前晴 22°C"


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(current_condition=[]),
    lambda p: p.pop("current_condition"),
    lambda p: p["current_condition"][0].pop("temp_C"),
    lambda p: p["current_condition"][0].update(temp_C=""),
    lambda p: p["current_condition"][0].pop("weatherDesc")
    and p["current_condition"][0].pop("lang_zh"),
])
def test_render_line_missing_fields_is_empty(mutate):
    """关键字段缺失 → 空串（调用方按失败静默，绝不输出半残行）。"""
    payload = json.loads(j1_payload())
    mutate(payload)
    assert weather_mod.render_weather_line(CITY, payload) == ""


# ---------------------------------------------------------------------------
# get_weather_line：端点、缓存、静默失败
# ---------------------------------------------------------------------------

def test_get_weather_line_hits_expected_endpoint(tmp_path):
    fake = _RecordingUrlopen(j1_payload())
    line = weather_mod.get_weather_line(
        CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake)
    assert line is not None and line.startswith("[资料·天气] ")
    assert len(fake.urls) == 1
    assert fake.urls[0].startswith("https://wttr.in/")
    assert fake.urls[0].endswith("?format=j1&lang=zh")
    assert urllib.parse.quote(CITY, safe="") in fake.urls[0]
    assert fake.timeouts == [weather_mod._TIMEOUT_SECONDS]


def test_cache_within_ttl_skips_network(tmp_path):
    fake = _RecordingUrlopen(j1_payload())
    first = weather_mod.get_weather_line(CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake)
    second = weather_mod.get_weather_line(
        CITY, now=1000.0 + weather_mod.CACHE_TTL_SECONDS, cache_dir=tmp_path, urlopen=fake)
    assert first == second
    assert len(fake.urls) == 1                       # 第二次走缓存
    cached = json.loads(
        (tmp_path / "weather_cache.json").read_text(encoding="utf-8"))
    assert cached["ts"] == 1000.0 and cached["city"] == CITY


def test_cache_expired_refetches(tmp_path):
    fake = _RecordingUrlopen(j1_payload())
    weather_mod.get_weather_line(CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake)
    weather_mod.get_weather_line(
        CITY, now=1000.0 + weather_mod.CACHE_TTL_SECONDS + 1, cache_dir=tmp_path, urlopen=fake)
    assert len(fake.urls) == 2                       # 过期重新拉取


def test_cache_is_per_city(tmp_path):
    fake = _RecordingUrlopen(j1_payload())
    weather_mod.get_weather_line(CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake)
    weather_mod.get_weather_line("另一市", now=1000.0 + 1, cache_dir=tmp_path, urlopen=fake)
    assert len(fake.urls) == 2                       # 换城市不复用缓存


@pytest.mark.parametrize("payload", [
    urllib.error.HTTPError("https://wttr.in/", 503, "unavailable", None, None),
    OSError("connection refused"),
    b"not-json{",
    json.dumps({"current_condition": "oops"}).encode("utf-8"),
])
def test_failures_are_silent_none(tmp_path, payload):
    """网络/HTTP/解析/结构失败一律 None（天气是锦上添花，绝不抛错拖垮调用方）。"""
    fake = _RecordingUrlopen(payload)
    assert weather_mod.get_weather_line(
        CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake) is None
    assert not (tmp_path / "weather_cache.json").exists()  # 失败不写缓存


def test_empty_city_is_none_without_network(tmp_path):
    fake = _RecordingUrlopen(j1_payload())
    assert weather_mod.get_weather_line(
        "  ", now=1000.0, cache_dir=tmp_path, urlopen=fake) is None
    assert fake.urls == []


# ---------------------------------------------------------------------------
# maybe_weather_block：意图门 + 集成形态
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "今天天气怎么样", "外面下雨了吗", "会不会下雪呀", "出门要带伞吗",
    "你那边冷不冷", "今天热不热", "现在气温多少", "温度多少度",
])
def test_intent_keywords_trigger(tmp_path, text):
    fake = _RecordingUrlopen(j1_payload())
    line = weather_mod.maybe_weather_block(
        text, CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake)
    assert line == f"[资料·天气] {CITY} 当前晴 22°C（今日 18~28°C）"
    assert len(fake.urls) == 1


@pytest.mark.parametrize("text", ["你好呀", "在干嘛", "晚安", ""])
def test_no_intent_skips_network(tmp_path, text):
    fake = _RecordingUrlopen(j1_payload())
    assert weather_mod.maybe_weather_block(
        text, CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake) is None
    assert fake.urls == []


def test_no_city_skips_network(tmp_path):
    fake = _RecordingUrlopen(j1_payload())
    assert weather_mod.maybe_weather_block(
        "今天天气怎么样", "", now=1000.0, cache_dir=tmp_path, urlopen=fake) is None
    assert fake.urls == []


def test_intent_with_fetch_failure_is_none(tmp_path):
    fake = _RecordingUrlopen(OSError("down"))
    assert weather_mod.maybe_weather_block(
        "今天天气怎么样", CITY, now=1000.0, cache_dir=tmp_path, urlopen=fake) is None
