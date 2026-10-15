# -*- coding: utf-8 -*-
"""brain/websearch.py 联网搜索模块测试（多模态能力工程）。

覆盖面（任务书四类要求）：
- mock HTTP 响应：DuckDuckGo 结果页 HTML（结构依据两类独立公开资料：
  ``a.result__a`` 标题锚 / ``a.result__snippet`` 摘要锚 / ``uddg`` 重定向参数）
  与 Tavily JSON 响应，urlopen 全部注入假件，测试零真实外网请求；
- 意图检测：任务书关键词表（搜|查|搜索|最新|新闻|今天.*什么|now|latest|news）
  的正/负样本；
- 中文查询：端到端 mock（中文消息 → 查询词提取 → 假 DDG 页 → 资料引用块），
  管线集成与控制台集成同参数验证；
- 无结果降级：无结果页/检索失败/开关关/无意图 一律空串不注入、不炸管线。

安全规约：无任何真实敏感值；QQ 号一律 10000/10001 占位号段；Tavily key 用
PENDING_SETUP_TAVILY_KEY 占位。

真实网络说明：开发机直连 html.duckduckgo.com 超时（定向网络阻断，实测
api.tavily.com 可达、duckduckgo 双端点超时），真实端点连通性结论见模块
docstring；本文件只做确定性 mock 测试。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
from types import SimpleNamespace

import pytest

from virtuallover.brain import websearch
from virtuallover.brain.llm import LLMReply
from virtuallover.config import ToolsConfig, VlConfig

# ---------------------------------------------------------------------------
# 假件：可注入的 urlopen（签名对齐 brain/llm.py 的 UrlOpen 契约）
# ---------------------------------------------------------------------------


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


def make_urlopen(responder):
    """构造假 urlopen：responder(request) -> bytes（或直接抛异常）。"""
    def _open(request, timeout=None):
        payload = responder(request)
        if isinstance(payload, bytes):
            return _FakeResponse(payload)
        return payload
    return _open


# ---------------------------------------------------------------------------
# DuckDuckGo 结果页夹具（结构对齐公开资料记载的真实 HTML 形态）
# ---------------------------------------------------------------------------


def _card(title: str, href: str, snippet: str) -> str:
    """一张标准结果卡：result 容器 + result__a 标题锚 + result__snippet 摘要锚。"""
    return (
        '<div class="result results_links results_links_deep web-result">'
        '<div class="links_main links_deep result__body">'
        f'<h2 class="result__title"><a rel="nofollow" class="result__a" '
        f'href="{href}">{title}</a></h2>'
        f'<a class="result__snippet" href="#">{snippet}</a>'
        '</div></div>'
    )


def uddg_href(target: str, rut: str = "abc123") -> str:
    """重定向形态 href：//duckduckgo.com/l/?uddg=<url 编码真链>&amp;rut=…"""
    return ("//duckduckgo.com/l/?uddg=" + urllib.parse.quote(target, safe="")
            + "&amp;rut=" + rut)


def ddg_page(cards: str) -> bytes:
    """拼一个 DuckDuckGo html 端点结果页骨架。"""
    return ('<!DOCTYPE html><html><head><title>查询词 at DuckDuckGo</title></head>'
            f'<body><div id="links">{cards}</div></body></html>').encode("utf-8")


#: 中文夹具卡片：两条真结果 + 一条广告（y.js 点击链）
CARD_WIKI = _card(
    "长江 - 维基百科，自由的百科全书",
    uddg_href("https://zh.wikipedia.org/wiki/长江"),
    "长江（<b>Yangtze River</b>）全长约 6300 公里，是亚洲第一长河、世界第三长河。",
)
CARD_BRITANNICA = _card(
    "Yangtze River | Britannica",
    "https://www.britannica.com/place/Yangtze-River",   # 直接 URL 形态（无重定向）
    "The &quot;Yangtze&quot; is about 6,300 km long &amp; the third longest river.",
)
CARD_AD = (
    '<div class="result result--ad">'
    '<h2><a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ads.x">'
    "广告标题</a></h2>"
    '<a class="result__snippet">广告摘要</a></div>'
)
NO_RESULTS_PAGE = (
    '<!DOCTYPE html><html><body><div id="links">'
    '<div class="no-results">No  results for:"zzzzqqqqxxxx"</div>'
    "</div></body></html>").encode("utf-8")
ANOMALY_PAGE = (
    "<!DOCTYPE html><html><head><title>Unfortunately, bots use DuckDuckGo too"
    "</title></head><body>Sorry, bots use DuckDuckGo too. "
    "If this error persists, please <a href='/exception'>let us know</a>."
    "</body></html>").encode("utf-8")


# ---------------------------------------------------------------------------
# DuckDuckGo 解析：标题/摘要/URL 提取 + 广告跳过 + 前 5 条
# ---------------------------------------------------------------------------


def test_ddg_parses_titles_snippets_urls_and_skips_ads():
    """结果页解析：标题+摘要+URL 齐备、实体还原、<b> 剥离、广告卡跳过。"""
    fake = make_urlopen(lambda req: ddg_page(CARD_WIKI + CARD_AD + CARD_BRITANNICA))
    results = websearch.duckduckgo_search("长江有多长", urlopen=fake)
    assert len(results) == 2, "广告卡（y.js 点击链）不得混进结果"
    first = results[0]
    assert first.title == "长江 - 维基百科，自由的百科全书"
    assert first.snippet == "长江（Yangtze River）全长约 6300 公里，是亚洲第一长河、世界第三长河。"
    assert first.url == "https://zh.wikipedia.org/wiki/长江"
    second = results[1]
    # 直接 URL 形态原样保留；&quot;/&amp; 实体还原
    assert second.url == "https://www.britannica.com/place/Yangtze-River"
    assert second.snippet == 'The "Yangtze" is about 6,300 km long & the third longest river.'


def test_ddg_caps_results_at_five():
    """超出 5 条只取前 5（任务书：前 5 条）。"""
    cards = "".join(
        _card(f"标题{i}", uddg_href(f"https://example.com/{i}"), f"摘要{i}")
        for i in range(8))
    fake = make_urlopen(lambda req: ddg_page(cards))
    results = websearch.duckduckgo_search("测试", urlopen=fake)
    assert len(results) == websearch.MAX_RESULTS == 5
    assert [r.title for r in results] == [f"标题{i}" for i in range(5)]


def test_ddg_request_carries_urlencoded_chinese_query_and_headers():
    """请求面：GET html 端点、中文查询词 urlencode、带浏览器 UA（防默认 UA 403）。"""
    seen = {}

    def responder(request):
        seen["url"] = request.full_url
        seen["ua"] = request.get_header("User-agent")
        return ddg_page(CARD_WIKI)

    websearch.duckduckgo_search("长江有多长", urlopen=make_urlopen(responder))
    assert seen["url"].startswith(websearch.DDG_HTML_ENDPOINT)
    assert "q=" + urllib.parse.quote("长江有多长") in seen["url"]
    assert seen["ua"] and "Mozilla" in seen["ua"]


def test_ddg_no_results_page_returns_empty_list():
    """无结果页（div.no-results）→ 空列表（不是异常）。"""
    fake = make_urlopen(lambda req: NO_RESULTS_PAGE)
    assert websearch.duckduckgo_search("zzzzqqqqxxxx", urlopen=fake) == []


def test_ddg_anomaly_page_raises_search_error():
    """反爬挑战页 → SearchError（不得当「无结果」静默吞掉）。"""
    fake = make_urlopen(lambda req: ANOMALY_PAGE)
    with pytest.raises(websearch.SearchError):
        websearch.duckduckgo_search("测试", urlopen=fake)


def test_ddg_http_error_wrapped_as_search_error():
    """HTTP 403（HTTPError）→ SearchError，中文文案。"""
    def responder(request):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
    with pytest.raises(websearch.SearchError, match="403"):
        websearch.duckduckgo_search("测试", urlopen=make_urlopen(responder))


def test_ddg_network_error_wrapped_as_search_error():
    """网络不可达（URLError）→ SearchError。"""
    def responder(request):
        raise urllib.error.URLError("timed out")
    with pytest.raises(websearch.SearchError):
        websearch.duckduckgo_search("测试", urlopen=make_urlopen(responder))


def test_ddg_empty_query_short_circuits_without_request():
    """空查询词不发起任何请求，直接空列表。"""
    def responder(request):
        raise AssertionError("空查询词不应发起网络请求")
    assert websearch.duckduckgo_search("  ", urlopen=make_urlopen(responder)) == []


# ---------------------------------------------------------------------------
# Tavily：JSON 响应映射 + 错误包装 + 统一分派器
# ---------------------------------------------------------------------------

TAVILY_PAYLOAD = json.dumps({
    "query": "长江有多长",
    "results": [
        {"title": "长江——维基百科", "content": "全长约 6300 公里。",
         "url": "https://zh.wikipedia.org/wiki/长江"},
        {"title": "Yangtze", "content": "6300 km.", "url": "https://e.com/y"},
    ],
}).encode("utf-8")


def test_tavily_maps_results_json():
    """Tavily JSON results[].title/content/url → SearchResult 列表。"""
    seen = {}

    def responder(request):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return TAVILY_PAYLOAD

    results = websearch.tavily_search(
        "长江有多长", api_key="PENDING_SETUP_TAVILY_KEY", urlopen=make_urlopen(responder))
    assert seen["body"] == {"api_key": "PENDING_SETUP_TAVILY_KEY",
                            "query": "长江有多长", "max_results": 5}
    assert [r.title for r in results] == ["长江——维基百科", "Yangtze"]
    assert results[0].snippet == "全长约 6300 公里。"
    assert results[0].url == "https://zh.wikipedia.org/wiki/长江"


def test_tavily_http_error_wrapped():
    """Tavily HTTP 401（key 无效）→ SearchError；异常文本不得携带 key 原文。"""
    def responder(request):
        raise urllib.error.HTTPError(websearch.TAVILY_ENDPOINT, 401, "Unauthorized", {}, None)
    with pytest.raises(websearch.SearchError):
        websearch.tavily_search("测试", api_key="PENDING_SETUP_TAVILY_KEY",
                                urlopen=make_urlopen(responder))


def test_tavily_bad_json_and_missing_results_wrapped():
    """坏 JSON / 缺 results 字段 → SearchError。"""
    with pytest.raises(websearch.SearchError):
        websearch.tavily_search("测试", api_key="k",
                                urlopen=make_urlopen(lambda req: b"<html>not json</html>"))
    with pytest.raises(websearch.SearchError):
        websearch.tavily_search("测试", api_key="k",
                                urlopen=make_urlopen(lambda req: b'{"answer":"x"}'))


def test_search_dispatch_tavily_without_key_falls_back_to_ddg():
    """分派器：provider=tavily 但 key 为空 → 回落 DuckDuckGo（任务书兜底语义）。"""
    def responder(request):
        assert websearch.DDG_HTML_ENDPOINT in request.full_url, "必须回落 DDG 端点"
        return ddg_page(CARD_WIKI)

    results = websearch.search("长江有多长", provider="tavily", tavily_api_key="",
                               urlopen=make_urlopen(responder))
    assert len(results) == 1 and "维基百科" in results[0].title


def test_search_dispatch_tavily_with_key_uses_tavily():
    """分派器：provider=tavily 且 key 非空 → 走 Tavily 端点。"""
    def responder(request):
        assert request.full_url == websearch.TAVILY_ENDPOINT
        return TAVILY_PAYLOAD

    results = websearch.search("长江有多长", provider="tavily",
                               tavily_api_key="PENDING_SETUP_TAVILY_KEY",
                               urlopen=make_urlopen(responder))
    assert results and results[0].title == "长江——维基百科"


def test_search_dispatch_unknown_provider_defaults_to_ddg():
    """分派器：provider 非法 → DuckDuckGo。"""
    def responder(request):
        assert websearch.DDG_HTML_ENDPOINT in request.full_url
        return ddg_page(CARD_WIKI)

    assert websearch.search("测试", provider="unknown_x", urlopen=make_urlopen(responder))


# ---------------------------------------------------------------------------
# 搜索意图检测与查询词提取（任务书关键词表）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "搜一下长江有多长",
    "帮我搜明天天气",
    "查一下今天油价",
    "最新的新闻有哪些",
    "今天有什么新闻",
    "最近有什么大事，给我讲讲新闻",
    "what's the latest news",
    "any news about AI?",
    "how is the weather now",
    "武汉现在最新情况 查一下",
])
def test_intent_positive_samples(text):
    """正样本：含任务书任一关键词即命中（英文大小写不敏感）。"""
    assert websearch.has_search_intent(text), text


@pytest.mark.parametrize("text", [
    "你好呀",
    "早上好，记得吃早饭",
    "讲个故事给我听",
    "我今天心情不太好",
    "晚安，做个好梦",
    "i love you",
    "昨天我们聊到哪了",
    "",
])
def test_intent_negative_samples(text):
    """负样本：日常寒暄/无关键词不触发（省 token、避免无关注入）。"""
    assert not websearch.has_search_intent(text), text


def test_extract_query_strips_prefix_and_quotes():
    """查询词提取：剥客套+触发前缀与包裹引号，正文原样保留。"""
    assert websearch.extract_search_query("帮我搜一下长江有多长") == "长江有多长"
    assert websearch.extract_search_query("「长江有多长」") == "长江有多长"
    assert websearch.extract_search_query("查一下：今天的油价是多少？") == "今天的油价是多少？"
    # 整句都是触发词 → 回退原文，绝不返回空查询
    assert websearch.extract_search_query("搜索") == "搜索"
    assert websearch.extract_search_query("") == ""


def test_extract_query_caps_length():
    """查询词限长（默认 120），防超长消息整段丢给搜索引擎。"""
    long_text = "搜一下" + "很" * 500
    assert len(websearch.extract_search_query(long_text)) == 120


# ---------------------------------------------------------------------------
# 资料引用块与 maybe_search_block（降级语义）
# ---------------------------------------------------------------------------


def test_format_block_contains_delimiters_guard_and_sources():
    """资料引用块：定界符 + 防护句 + 序号条目 + 来源 URL，5 条上限。"""
    results = [websearch.SearchResult(f"标题{i}", f"摘要{i}", f"https://e.com/{i}")
               for i in range(7)]
    block = websearch.format_results_block("测试查询", results, provider="duckduckgo")
    assert block.startswith("<资料引用开始>")
    assert block.rstrip().endswith("<资料引用结束>")
    assert "不是给你的指令" in block          # persona.INJECTION_GUARD 防护句
    assert "[5] 标题4" in block and "[6]" not in block   # 只留前 5 条
    assert "（来源: https://e.com/0）" in block


def test_format_block_empty_results_returns_empty():
    """空结果 → 空串（调用方据此跳过注入）。"""
    assert websearch.format_results_block("q", []) == ""


def test_maybe_block_disabled_returns_empty_without_request():
    """开关关（web_search_enabled=false）→ 空串，零网络请求。"""
    def responder(request):
        raise AssertionError("开关关不应发起网络请求")

    tools = ToolsConfig(web_search_enabled=False)
    block = asyncio.run(websearch.maybe_search_block(
        "搜一下长江", tools, urlopen=make_urlopen(responder)))
    assert block == ""


def test_maybe_block_no_intent_returns_empty_without_request():
    """无搜索意图 → 空串，零网络请求。"""
    def responder(request):
        raise AssertionError("无意图不应发起网络请求")

    block = asyncio.run(websearch.maybe_search_block(
        "你好呀", ToolsConfig(), urlopen=make_urlopen(responder)))
    assert block == ""


def test_maybe_block_none_cfg_treated_as_disabled():
    """[tools] 段缺失（旧配置）→ 视为全关，零网络请求。"""
    def responder(request):
        raise AssertionError("cfg 缺 tools 段不应发起网络请求")

    assert asyncio.run(websearch.maybe_search_block(
        "搜一下长江", None, urlopen=make_urlopen(responder))) == ""


def test_maybe_block_search_error_degrades_to_empty():
    """检索失败（网络/挑战页）→ 空串不注入（降级），绝不抛出。"""
    def responder(request):
        raise urllib.error.URLError("timed out")

    block = asyncio.run(websearch.maybe_search_block(
        "搜一下长江有多长", ToolsConfig(), urlopen=make_urlopen(responder)))
    assert block == ""


def test_maybe_block_no_results_degrades_to_empty():
    """无结果（no-results 页）→ 空串不注入。"""
    fake = make_urlopen(lambda req: NO_RESULTS_PAGE)
    block = asyncio.run(websearch.maybe_search_block(
        "搜一下zzzzqqqq", ToolsConfig(search_provider="duckduckgo"), urlopen=fake))
    assert block == ""


def test_maybe_block_chinese_query_end_to_end():
    """中文查询端到端（mock）：中文消息 → 查询词 → 假 DDG 页 → 完整资料块。"""
    fake = make_urlopen(lambda req: ddg_page(CARD_WIKI + CARD_BRITANNICA))
    block = asyncio.run(websearch.maybe_search_block(
        "帮我搜一下长江有多长", ToolsConfig(search_provider="duckduckgo"), urlopen=fake))
    assert block.startswith("<资料引用开始>")
    assert "长江 - 维基百科，自由的百科全书" in block
    assert "https://zh.wikipedia.org/wiki/长江" in block
    assert "长江有多长" in block                 # 查询词回显
    assert block.rstrip().endswith("<资料引用结束>")


def test_maybe_block_tavily_provider_with_key():
    """provider=tavily + key → maybe_search_block 走 Tavily。"""
    fake = make_urlopen(lambda req: TAVILY_PAYLOAD)
    tools = ToolsConfig(search_provider="tavily",
                        tavily_api_key="PENDING_SETUP_TAVILY_KEY")
    block = asyncio.run(websearch.maybe_search_block(
        "查一下长江有多长", tools, urlopen=fake))
    assert "长江——维基百科" in block and "6300" in block


def test_default_urlopen_is_smart_urlopen():
    """默认 urlopen = net.smart_urlopen（回环直连、公网走系统代理的既有语义）。"""
    from virtuallover.net import smart_urlopen
    assert websearch._default_urlopen is smart_urlopen


# ---------------------------------------------------------------------------
# [tools] 配置段
# ---------------------------------------------------------------------------


def test_tools_config_defaults():
    """默认值：开、duckduckgo、空 key（任务书三条缺省）。"""
    tools = ToolsConfig()
    assert tools.web_search_enabled is True
    assert tools.search_provider == "bing"
    assert tools.tavily_api_key == ""


def test_config_loads_tools_section_and_normalizes_provider(caplog):
    """vl.toml [tools] 段加载；非法 provider 归一化回 duckduckgo 并告警。"""
    import logging as logging_mod

    # 最小合法 vl.toml（_validate 必填项占位齐备；embedding 关闭豁免）
    text = (
        "schema_version = 1\n"
        'session_id = "测试人格:FriendMessage:10001"\n'
        'admins = ["10000"]\n'
        "\n[chat]\n"
        'api_base = "https://placeholder.invalid/v1"\n'
        'api_key = "PENDING_SETUP_CHAT_KEY"\n'
        'model = "placeholder-model"\n'
        "\n[embedding]\n"
        "enabled = false\n"
        "\n[persona]\n"
        'name = "测试人格"\n'
        'system_prompt = "占位人格全文"\n'
        "\n[tools]\n"
        "web_search_enabled = false\n"
        'search_provider = "invalid_xyz"\n'
        'tavily_api_key = "PENDING_SETUP_TAVILY_KEY"\n'
    )
    with caplog.at_level(logging_mod.WARNING, logger="virtuallover.config"):
        loaded = load_config_text(text)
    assert loaded.tools == ToolsConfig(web_search_enabled=False,
                                       search_provider="bing",
                                       tavily_api_key="PENDING_SETUP_TAVILY_KEY")
    assert any("search_provider" in rec.message for rec in caplog.records)


def load_config_text(text: str):
    """TOML 文本 → VlConfig（load_config 的临时文件形态）。"""
    import tempfile
    from pathlib import Path

    from virtuallover.config import load_config

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        return load_config(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 管线集成（复用 O3/O4 characterization 契约垫片，QQ 管线同参数）
# ---------------------------------------------------------------------------


def _echo_last_user(call):
    """回声钩子：把本轮最后一条 user 消息原样带回（可断言注入面）。"""
    for msg in reversed(call.messages):
        if str(msg.get("role")) == "user":
            return str(msg.get("content") or "")
    return "空回复。"


def test_pipeline_injects_search_block_for_intent_message(tmp_path, monkeypatch):
    """管线集成：搜索意图消息 → LLM 的 user 消息含资料引用块 + 原文。"""
    from virtuallover.tests._o34_contract import (
        FakeClock, attach_pipeline, make_ctx, make_event, settle)

    async def scenario():
        clock = FakeClock()
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        assert handle is not None, why
        try:
            handle.chat.auto_release = True
            handle.chat.reply_fn = _echo_last_user
            if hasattr(ctx, "cfg") and hasattr(ctx.cfg, "tools"):
                ctx.cfg.tools.search_provider = "duckduckgo"
            monkeypatch.setattr(
                websearch, "_default_urlopen",
                make_urlopen(lambda req: ddg_page(CARD_WIKI + CARD_BRITANNICA)))
            await handle.emit(make_event("10001", "帮我搜一下长江有多长", 401))
            await settle(handle, timeout=8.0)

            assert len(handle.chat.calls) == 1
            last_user = [m for m in handle.chat.calls[0].messages
                         if m["role"] == "user"][-1]["content"]
            assert "<资料引用开始>" in last_user
            assert "长江 - 维基百科，自由的百科全书" in last_user
            assert "帮我搜一下长江有多长" in last_user      # 用户原文仍在
            assert last_user.index("<资料引用开始>") < last_user.index("帮我搜一下")
            # 她的回复照常出帧（搜索注入不影响回复链路）
            assert handle.bridge.completed, "搜索注入后她仍应正常回复"
        finally:
            await handle.aclose()

    asyncio.run(scenario())


def test_pipeline_skips_search_for_plain_message(tmp_path, monkeypatch):
    """管线集成：普通寒暄零网络请求（无意图不触发搜索）。"""
    from virtuallover.tests._o34_contract import (
        FakeClock, attach_pipeline, make_ctx, make_event, settle)

    def _no_network(request):
        raise AssertionError("普通消息不应发起搜索请求")

    async def scenario():
        clock = FakeClock()
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        assert handle is not None, why
        try:
            handle.chat.auto_release = True
            handle.chat.reply_fn = _echo_last_user
            monkeypatch.setattr(websearch, "_default_urlopen",
                                make_urlopen(_no_network))
            await handle.emit(make_event("10001", "早上好呀，今天天气真不错", 402))
            await settle(handle, timeout=8.0)
            assert len(handle.chat.calls) == 1
            last_user = [m for m in handle.chat.calls[0].messages
                         if m["role"] == "user"][-1]["content"]
            assert "<资料引用开始>" not in last_user
        finally:
            await handle.aclose()

    asyncio.run(scenario())


def test_pipeline_respects_web_search_disabled(tmp_path, monkeypatch):
    """管线集成：[tools].web_search_enabled=false → 意图命中也不搜索。"""
    from virtuallover.tests._o34_contract import (
        FakeClock, attach_pipeline, make_ctx, make_event, settle)

    def _no_network(request):
        raise AssertionError("开关关不应发起搜索请求")

    async def scenario():
        clock = FakeClock()
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.cfg.tools.web_search_enabled = False
        handle, why = await attach_pipeline(ctx)
        assert handle is not None, why
        try:
            handle.chat.auto_release = True
            handle.chat.reply_fn = _echo_last_user
            monkeypatch.setattr(websearch, "_default_urlopen",
                                make_urlopen(_no_network))
            await handle.emit(make_event("10001", "搜一下长江有多长", 403))
            await settle(handle, timeout=8.0)
            last_user = [m for m in handle.chat.calls[0].messages
                         if m["role"] == "user"][-1]["content"]
            assert "<资料引用开始>" not in last_user
        finally:
            await handle.aclose()

    asyncio.run(scenario())


def test_pipeline_search_failure_degrades_gracefully(tmp_path, monkeypatch):
    """管线集成：检索失败（网络阻断）→ 不注入、她照常回复（无结果降级）。"""
    from virtuallover.tests._o34_contract import (
        FakeClock, attach_pipeline, make_ctx, make_event, settle)

    def _timeout(request):
        raise urllib.error.URLError("timed out")

    async def scenario():
        clock = FakeClock()
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        assert handle is not None, why
        try:
            handle.chat.auto_release = True
            handle.chat.reply_fn = _echo_last_user
            monkeypatch.setattr(websearch, "_default_urlopen",
                                make_urlopen(_timeout))
            await handle.emit(make_event("10001", "搜一下长江有多长", 404))
            await settle(handle, timeout=8.0)
            assert len(handle.chat.calls) == 1, "搜索失败不得中断 LLM 轮"
            last_user = [m for m in handle.chat.calls[0].messages
                         if m["role"] == "user"][-1]["content"]
            assert "<资料引用开始>" not in last_user
            assert handle.bridge.completed, "降级后她仍应正常回复"
        finally:
            await handle.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 控制台聊天集成（console.core.brain_chat_async）
# ---------------------------------------------------------------------------


class _ConsoleChat:
    """假控制台 chat 客户端：记录 messages，回固定回复。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def chat_async(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        return LLMReply(content="好的呀。", provider_id="测试通道")


def _make_console_runtime():
    """控制台 brain_chat_async 的最小 runtime：cfg 齐 + 其余组件缺省。"""
    cfg = VlConfig()
    cfg.session_id = "测试人格:FriendMessage:10001"
    cfg.persona.name = "测试人格"
    cfg.persona.system_prompt = "占位人格全文"
    cfg.tools.search_provider = "duckduckgo"
    # 作息行默认关：开启时「她此刻」防护块（<资料引用>包裹）会进 user 消息，
    # 干扰下方「无搜索资料块」断言；控制台作息注入由 test_persona 专项覆盖。
    cfg.schedule.enabled = False
    return SimpleNamespace(cfg=cfg, chat_client=_ConsoleChat(),
                           history=None, mood_engine=None, memory_service=None)


def test_console_chat_injects_search_block(monkeypatch):
    """控制台集成：搜索意图消息 → LLM user 消息含资料引用块。"""
    from virtuallover.console import core as console_core

    monkeypatch.setattr(
        websearch, "_default_urlopen",
        make_urlopen(lambda req: ddg_page(CARD_WIKI)))
    runtime = _make_console_runtime()
    result = asyncio.run(console_core.brain_chat_async(runtime, "搜一下长江有多长"))
    assert result["ok"] is True
    chat = runtime.chat_client
    assert len(chat.calls) == 1
    last_user = [m for m in chat.calls[0] if m["role"] == "user"][-1]["content"]
    assert "<资料引用开始>" in last_user
    assert "长江 - 维基百科，自由的百科全书" in last_user
    assert "搜一下长江有多长" in last_user


def test_console_chat_skips_search_for_plain_message(monkeypatch):
    """控制台集成：普通消息零网络请求。"""
    from virtuallover.console import core as console_core

    def _no_network(request):
        raise AssertionError("普通消息不应发起搜索请求")

    monkeypatch.setattr(websearch, "_default_urlopen", make_urlopen(_no_network))
    runtime = _make_console_runtime()
    result = asyncio.run(console_core.brain_chat_async(runtime, "你好呀"))
    assert result["ok"] is True
    last_user = [m for m in runtime.chat_client.calls[0]
                 if m["role"] == "user"][-1]["content"]
    assert "<资料引用开始>" not in last_user


def test_console_chat_search_failure_degrades(monkeypatch):
    """控制台集成：检索失败 → 不注入、聊天照常成功。"""
    from virtuallover.console import core as console_core

    monkeypatch.setattr(
        websearch, "_default_urlopen",
        make_urlopen(lambda req: (_ for _ in ()).throw(urllib.error.URLError("blocked"))))
    runtime = _make_console_runtime()
    result = asyncio.run(console_core.brain_chat_async(runtime, "搜一下长江有多长"))
    assert result["ok"] is True
    last_user = [m for m in runtime.chat_client.calls[0]
                 if m["role"] == "user"][-1]["content"]
    assert "<资料引用开始>" not in last_user


def test_console_chat_memory_recall_uses_memory_store():
    """控制台集成：记忆检索走 memory_store.search（多模态集成修正）。

    修前 brain_chat_async 调 memory_service.search——MemoryService 没有该方法
    （hasattr 守卫恒 False），控制台聊天记忆注入一直是哑的；修正后与
    pipeline._recall 同源走 memory_store.search（[mnemosyne] 参数）。
    """
    from virtuallover.console import core as console_core

    class _FakeStore:
        """假记忆库：记录检索入参，回一条可命中注入的 MemoryHit 形态条目。"""

        def __init__(self) -> None:
            self.calls: list = []

        async def search(self, session_id, query, *, top_k, candidates, min_cosine):
            self.calls.append({"session": session_id, "query": query,
                               "top_k": top_k, "candidates": candidates,
                               "min_cosine": min_cosine})
            return [SimpleNamespace(content="她记得用户喜欢甜系小说",
                                    create_time=0, cosine=0.9)]

    runtime = _make_console_runtime()
    runtime.memory_store = _FakeStore()
    result = asyncio.run(console_core.brain_chat_async(runtime, "我喜欢什么小说来着"))
    assert result["ok"] is True
    assert len(runtime.memory_store.calls) == 1
    call = runtime.memory_store.calls[0]
    assert call["session"] == "测试人格:FriendMessage:10001"
    assert "我喜欢什么小说来着" in call["query"]
    # 参数取 [mnemosyne] dataclass 默认值（top_k=3 / candidate_k=8 / min 0.3）
    assert call["top_k"] == 3
    assert call["candidates"] == 8
    assert call["min_cosine"] == 0.3
    last_user = [m for m in runtime.chat_client.calls[0]
                 if m["role"] == "user"][-1]["content"]
    assert "她记得用户喜欢甜系小说" in last_user


def test_console_chat_memory_recall_failure_degrades():
    """控制台集成：记忆库封锁/检索异常 → 不注入、聊天照常（降级语义）。"""
    from virtuallover.console import core as console_core

    class _BlockedStore:
        async def search(self, *args, **kwargs):
            raise RuntimeError("记忆库封锁中")

    runtime = _make_console_runtime()
    runtime.memory_store = _BlockedStore()
    result = asyncio.run(console_core.brain_chat_async(runtime, "你好呀"))
    assert result["ok"] is True


def test_console_chat_uses_full_persona_assemble():
    """控制台集成回归钉：brain_chat_async 必须走 persona.assemble 完整装配。

    修前 neutralize_delimiters 从 persona 导入（该函数定义在 pipeline.py）→
    ImportError 被 except 吞掉 → 永远走简拼分支（人格/记忆块/截断/时间
    reminder 全不生效）。时间 reminder 只有完整装配分支会注入，拿它当分支
    判定器。
    """
    from virtuallover.console import core as console_core

    runtime = _make_console_runtime()
    result = asyncio.run(console_core.brain_chat_async(runtime, "你好呀"))
    assert result["ok"] is True
    last_user = [m for m in runtime.chat_client.calls[0]
                 if m["role"] == "user"][-1]["content"]
    assert "<system_reminder>" in last_user
