# -*- coding: utf-8 -*-
"""soul/weather.py —— 天气感知（主动陪伴升级：她永远不聊天气 → 现在会了）。

数据源：wttr.in 的 JSON 端点（免 key、中国可直连）::

    GET https://wttr.in/<city>?format=j1&lang=zh

- ``current_condition[0]``：当前气温 temp_C 与天气描述（lang=zh 时带
  lang_zh 中文描述数组，缺失回退英文 weatherDesc）；
- ``weather[0]``：今日 min/max 温度（mintempC / maxtempC）。

输出一行资料行（供聊天管线 prepend / 早安 prompt 注入）::

    [资料·天气] {city} 当前晴 22°C（今日 18~28°C）

缓存：45 分钟（``<cache_dir>/weather_cache.json``，含 ts 量纲与 city 归属
——换城市立即重新拉取）。任何失败（网络/HTTP/解析/缺字段）一律静默返回
None，由调用方决定省略——天气是锦上添花，绝不拖垮消息。

网络语义照抄 brain/websearch.py：标准库 urllib + ``net.smart_urlopen``
（本地直连、公网走系统代理），urlopen 可注入（测试 mock HTTP 响应），
阻塞请求由调用方经 ``asyncio.to_thread`` 调用。

隐私约定：city 是用户在 vl.toml 里自愿配置的值，只落在资料行正文里发给
她本人；本模块日志一律不携带 city 原文。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from virtuallover.net import smart_urlopen as _default_urlopen

logger = logging.getLogger("virtuallover.soul.weather")

__all__ = [
    "WTTR_ENDPOINT", "CACHE_TTL_SECONDS",
    "has_weather_intent", "get_weather_line", "maybe_weather_block",
]

#: wttr.in JSON 端点前缀（城市名 URL 编码后拼接）
WTTR_ENDPOINT = "https://wttr.in/"

#: 缓存有效期：45 分钟（任务书口径）
CACHE_TTL_SECONDS = 45 * 60

#: 请求超时（秒）
_TIMEOUT_SECONDS = 8.0

#: 缓存文件名（落在 <storage.data_dir>，即 local/vl_data/ 下）
_CACHE_FILENAME = "weather_cache.json"

#: 真浏览器 UA（默认 Python UA 拉高被拒概率，websearch 同款手法）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

#: 用户消息命中才拉天气（任务书关键词表）
_WEATHER_INTENT_RE = re.compile(r"天气|下雨|下雪|带伞|冷不冷|热不热|气温|温度")

#: urlopen 注入形态（对齐 brain/websearch.py：签名 (req, timeout=...) -> 可 with 的响应）
UrlOpen = Any


def has_weather_intent(text: str) -> bool:
    """消息是否命中天气话题关键词（任务书词表，re.search 任意位置命中即算）。"""
    return bool(text) and bool(_WEATHER_INTENT_RE.search(str(text)))


# ---------------------------------------------------------------------------
# j1 载荷解析（宽松取数，缺字段返回空由上层静默）
# ---------------------------------------------------------------------------

def _clean_number(raw: Any) -> str:
    """wttr.in 的温度字段规整为整数字符串；非法返回空串。"""
    text = str(raw or "").strip().replace("°C", "").strip()
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return ""


def _weather_desc(condition: dict) -> str:
    """当前天气描述：lang=zh 附带的 lang_zh 中文优先，缺失回退英文 weatherDesc。"""
    for key in ("lang_zh", "weatherDesc"):
        entries = condition.get(key)
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            value = str(entries[0].get("value") or "").strip()
            if value:
                return value
    return ""


def render_weather_line(city: str, payload: Any) -> str:
    """j1 载荷 → 一行天气资料；缺关键字段返回空串（调用方按失败处理）。"""
    if not isinstance(payload, dict):
        return ""
    conditions = payload.get("current_condition")
    if not (isinstance(conditions, list) and conditions
            and isinstance(conditions[0], dict)):
        return ""
    condition = conditions[0]
    temp = _clean_number(condition.get("temp_C"))
    desc = _weather_desc(condition)
    if not temp or not desc:
        return ""
    days = payload.get("weather")
    if isinstance(days, list) and days and isinstance(days[0], dict):
        low = _clean_number(days[0].get("mintempC"))
        high = _clean_number(days[0].get("maxtempC"))
        if low and high:
            return f"[资料·天气] {city} 当前{desc} {temp}°C（今日 {low}~{high}°C）"
    return f"[资料·天气] {city} 当前{desc} {temp}°C"


# ---------------------------------------------------------------------------
# 拉取与缓存
# ---------------------------------------------------------------------------

def _fetch_payload(city: str, urlopen: "UrlOpen | None") -> dict | None:
    """GET wttr.in j1 端点并解析为 dict；任何网络/解析失败返回 None（只告警）。"""
    open_url = urlopen or _default_urlopen
    url = WTTR_ENDPOINT + urllib.parse.quote(city, safe="") + "?format=j1&lang=zh"
    request = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with open_url(request, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        logger.warning("[weather] 天气端点返回 HTTP %s（本次省略天气）", exc.code)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("[weather] 天气端点连接失败（本次省略天气）：%s", exc)
        return None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        logger.warning("[weather] 天气响应不是合法 JSON（本次省略天气）")
        return None
    return payload if isinstance(payload, dict) else None


def _read_cache(cache_dir: "str | Path", city: str, now: float) -> str:
    """读 45 分钟内的缓存行；过期/换城市/损坏返回空串。"""
    path = Path(cache_dir) / _CACHE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    if str(data.get("city") or "") != city:
        return ""
    try:
        ts = float(data.get("ts") or 0.0)
    except (TypeError, ValueError):
        return ""
    if now < ts or now - ts > CACHE_TTL_SECONDS:
        return ""
    return str(data.get("line") or "")


def _write_cache(cache_dir: "str | Path", city: str, line: str, now: float) -> None:
    """原子落盘缓存（tmp + os.replace）；OSError 只告警。"""
    path = Path(cache_dir) / _CACHE_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"ts": float(now), "city": city, "line": line},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("[weather] 天气缓存写入失败：%s", exc)


def get_weather_line(
    city: str,
    *,
    now: float,
    cache_dir: "str | Path",
    urlopen: "UrlOpen | None" = None,
) -> "str | None":
    """取一行天气资料（``[资料·天气] …``）；任何失败静默返回 None。

    - city：城市名（用户在 [tools].weather_city 自愿配置）；空串直接 None；
    - now：当前 epoch 秒（缓存 TTL 判定；测试注入固定值）；
    - cache_dir：缓存目录（生产= <storage.data_dir>，测试= tmp_path）；
    - 45 分钟内同城市命中缓存不发起网络请求。
    """
    name = str(city or "").strip()
    if not name:
        return None
    now = float(now)
    cached = _read_cache(cache_dir, name, now)
    if cached:
        return cached
    payload = _fetch_payload(name, urlopen)
    line = render_weather_line(name, payload) if payload is not None else ""
    if not line:
        logger.warning("[weather] 天气实况拉取失败（本次省略天气行）。")
        return None
    _write_cache(cache_dir, name, line, now)
    return line


def maybe_weather_block(
    text: str,
    city: str,
    *,
    now: float,
    cache_dir: "str | Path",
    urlopen: "UrlOpen | None" = None,
) -> "str | None":
    """聊天管线集成入口：用户消息命中天气话题且 city 已配置时返回天气行。

    与 brain/websearch.maybe_search_block 同款降级语义——无意图/未配置城市/
    拉取失败一律返回 None（调用方跳过注入，对话绝不因天气故障中断）；
    命中时返回一行 ``[资料·天气] …``，由调用方 prepend 到 user 侧（定界
    风格对齐 websearch 资料块，由管线接缝统一处理）。
    """
    name = str(city or "").strip()
    message = str(text or "")
    if not name or not message or not _WEATHER_INTENT_RE.search(message):
        return None
    return get_weather_line(name, now=now, cache_dir=cache_dir, urlopen=urlopen)
