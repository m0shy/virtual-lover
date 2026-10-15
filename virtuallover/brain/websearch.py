# -*- coding: utf-8 -*-
"""brain/websearch.py —— 零 pip 依赖的联网搜索（多模态能力工程）。

职责（任务书三条接口）：
- **DuckDuckGo 免费搜索**（无 key）：GET https://html.duckduckgo.com/html/?q=<query>，
  标准库 html.parser 解析结果页 HTML，提取前 5 条的 标题+摘要（不引第三方）；
- **Tavily 可选**（有 key 更准）：POST https://api.tavily.com/search
  ``{api_key, query, max_results:5}``，JSON 响应直接取
  results[].title/content/url；
- **统一入口 search()**：按 [tools].search_provider 分派；provider=tavily 但
  tavily_api_key 为空时回落 DuckDuckGo（无 key 兜底语义）。

HTML 结构依据（html.duckduckgo.com/html 结果页，两类独立公开资料一致）：
- 结果卡片容器 ``div.result``；标题链接 ``a.result__a``（href 承载目标 URL）；
- 摘要 ``a.result__snippet``（内含 <b> 加粗等内联标签）；
- 目标 URL 常以重定向形态出现：``//duckduckgo.com/l/?uddg=<url 编码真链>&rut=…``
  （或 http://html.duckduckgo.com/l/?uddg=… 同构），取 uddg 参数一次解码即真链；
- 广告结果点击链指向 ``duckduckgo.com/y.js``，一律跳过；
- 反爬挑战页含 "Unfortunately, bots use DuckDuckGo too" 文案 → 抛 SearchError
  （不能当「无结果」静默吞掉，也不该让假页混进对话资料）；
- 无结果页含 ``<div class="no-results">`` → 返回空列表（调用方降级不注入）。

聊天管线集成语义（brain/pipeline.py 与 console/core.py 共用）：
- ``has_search_intent``：消息含搜索意图关键词（搜|查|搜索|最新|新闻|今天.*什么|
  now|latest|news，英文大小写不敏感）才触发；
- ``maybe_search_block``：开关关 / 无意图 / 检索失败 / 零结果 一律返回空串
  （降级=不注入，对话绝不因搜索故障中断）；
- 命中则把检索结果渲染成「资料引用块」返回，调用方 prepend 到 user_text 前。
  块格式复用 persona 的资料引用定界符与防护句（<资料引用开始>…<资料引用结束>），
  与记忆/mood 注入同一隔离手法，防网页内容冒充指令。

阻塞语义：网络请求是同步阻塞（标准库 urllib.request，对齐 brain/llm.py 手法），
聊天管线一律经 ``maybe_search_block``（内部 asyncio.to_thread）调用，不卡事件循环；
urlopen 可注入（测试 mock HTTP 响应），默认 net.smart_urlopen（回环直连、公网走
系统代理的既有语义）。

敏感值约定：tavily_api_key 只从 [tools] 配置传入本模块，日志/异常一律不携带
key 原文。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, List

from virtuallover.brain.persona import INJECTION_GUARD, MATERIAL_CLOSE, MATERIAL_OPEN
from virtuallover.net import smart_urlopen as _default_urlopen

__all__ = [
    "SearchError", "SearchResult",
    "MAX_RESULTS",
    "has_search_intent", "extract_search_query",
    "duckduckgo_search", "tavily_search", "search",
    "format_results_block", "maybe_search_block",
]

logger = logging.getLogger("virtuallover.brain.websearch")

#: 每次搜索最多取用的结果条数（任务书：前 5 条）
MAX_RESULTS = 5

#: DuckDuckGo HTML 端点（免费、无 key；GET q= 查询词）
DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
#: Tavily 搜索端点（有 key；POST JSON）
TAVILY_ENDPOINT = "https://api.tavily.com/search"

#: 真浏览器 UA：默认 Python UA 会被 DuckDuckGo 403/挑战页概率显著抬高
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
#: 中文查询优先中文结果（Accept-Language 提示）
_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

#: urlopen 注入形态（对齐 brain/llm.py：签名 (req, timeout=...) -> 可 with 的响应）
UrlOpen = Any


class SearchError(RuntimeError):
    """联网搜索失败（网络/HTTP/反爬挑战页/响应格式异常）。文案中文、不携带密钥。"""


@dataclass
class SearchResult:
    """一条搜索结果（标题 + 摘要 + 来源 URL）。"""

    title: str
    snippet: str
    url: str


# ---------------------------------------------------------------------------
# 搜索意图检测与查询词提取
# ---------------------------------------------------------------------------

# 任务书关键词表：搜|查|搜索|最新|新闻|今天.*什么|now|latest|news
# （英文词大小写不敏感；re.search 语义=消息任意位置命中即算意图）
_SEARCH_INTENT_RE = re.compile(
    r"搜|查|搜索|最新|新闻|今天.*什么|now|latest|news", re.IGNORECASE)

# 查询词清洗：去掉开头常见的「客套+触发」短语（帮我搜一下/帮我查查/请搜索…），
# 让 DDG/Tavily 拿到更接近检索本体的查询词；其余原样保留（整句搜索对中文
# 口语化问题同样有效，不做激进改写）。
_QUERY_PREFIX_RE = re.compile(
    r"^\s*(?:帮我|请|麻烦你?|给我|帮忙|你)?\s*"
    r"(?:搜索|搜一下|搜一?下|搜个?|查一下|查查|查询|查个?|查一查)?"
    r"[:：,，、]?\s*")


def has_search_intent(text: str) -> bool:
    """消息文本是否含搜索意图关键词（任务书词表，见 _SEARCH_INTENT_RE）。"""
    return bool(text) and bool(_SEARCH_INTENT_RE.search(str(text)))


def extract_search_query(text: str, *, max_chars: int = 120) -> str:
    """从消息文本提取检索查询词：剥掉引号/客套前缀，压空白，限长。

    - 清洗后为空（整句都是触发词）则回退原文——绝不返回空查询；
    - max_chars 上限防把超长消息整段丢给搜索引擎。
    """
    raw = str(text or "").strip()
    query = _QUERY_PREFIX_RE.sub("", raw, count=1).strip()
    # 剥掉成对的包裹引号（含全角引号与中英文弯引号）
    query = query.strip("「」『』\u201c\u201d\u2018\u2019\"'")
    query = re.sub(r"\s+", " ", query).strip()
    if not query:
        query = raw
    return query[:max(1, int(max_chars))]


# ---------------------------------------------------------------------------
# DuckDuckGo：HTML 解析器（标准库 html.parser，零第三方依赖）
# ---------------------------------------------------------------------------


class _DDGResultParser(HTMLParser):
    """html.duckduckgo.com/html 结果页解析器。

    手法（html.parser 推入式，禁用任何 HTML 重写）：
    - 遇 ``a`` 且 class 含 ``result__a``：开一条新结果（记 href，累积标题文本）；
    - 遇 ``a``/``div``/``span`` 且 class 含 ``result__snippet``：开始累积摘要文本，
      对应到「最近一条还没有摘要的结果」（页面里每卡片=标题锚+摘要锚，文档序配对）；
    - 标签内文本一律累积 data（convert_charrefs=True 自动还原 HTML 实体，
      <b>/<wbr> 等内联标签被忽略不进文本）；
    - 广告点击链（href 含 duckduckgo.com/y.js）丢弃不收。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[SearchResult] = []
        self._cur_title: "str | None" = None      # 正在累积的标题（None=不在标题锚内）
        self._cur_href: str = ""
        self._cur_snippet: "str | None" = None    # 正在累积的摘要（None=不在摘要锚内）
        self.no_results = False                   # 命中 <div class="no-results">
        self.anomaly = False                      # 命中反爬挑战页文案

    # -- 工具：class 属性拆词 -------------------------------------------------

    @staticmethod
    def _classes(attrs: "list[tuple[str, str | None]]") -> List[str]:
        for key, value in attrs:
            if key == "class" and value:
                return value.split()
        return []

    @staticmethod
    def _attr(attrs: "list[tuple[str, str | None]]", name: str) -> str:
        for key, value in attrs:
            if key == name and value:
                return value
        return ""

    # -- HTMLParser 钩子 -------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        classes = self._classes(attrs)
        joined = " ".join(classes)
        # 反爬挑战页标记（页面标题/容器带 anomaly 字样）
        if "anomaly" in joined or "captchapage" in joined:
            self.anomaly = True
        if tag == "div" and "no-results" in classes:
            self.no_results = True
        if tag == "a" and "result__a" in classes:
            self._cur_href = self._attr(attrs, "href")
            self._cur_title = ""
            return
        if tag in ("a", "div", "span") and "result__snippet" in classes:
            self._cur_snippet = ""
            return

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._cur_title is not None:
            title = html.unescape(self._cur_title).strip()
            self._cur_title = None
            if title:
                self._append_result(title)
        elif tag in ("a", "div", "span") and self._cur_snippet is not None:
            snippet = html.unescape(self._cur_snippet).strip()
            self._cur_snippet = None
            if snippet and self.results and not self.results[-1].snippet:
                self.results[-1].snippet = snippet

    def handle_data(self, data: str) -> None:
        if self._cur_title is not None:
            self._cur_title += data
        elif self._cur_snippet is not None:
            self._cur_snippet += data

    # -- 收尾 -------------------------------------------------------------------

    def _append_result(self, title: str) -> None:
        href = self._cur_href
        self._cur_href = ""
        # 广告点击链跳过（duckduckgo.com/y.js 是 DDG 广告跟踪点击域）
        if "/y.js" in href:
            return
        url = _resolve_ddg_href(href)
        if not url:
            return
        self.results.append(SearchResult(title=title, snippet="", url=url))

    def close(self) -> None:  # noqa: D102
        super().close()
        # 文档结束仍未闭合的锚（残缺 HTML）：把已累积的标题兜底收进来
        if self._cur_title is not None:
            title = html.unescape(self._cur_title).strip()
            self._cur_title = None
            if title:
                self._append_result(title)


def _resolve_ddg_href(href: str) -> str:
    """把结果锚的 href 还原成真实目标 URL。

    - 重定向形态 ``…/l/?uddg=<url 编码真链>&rut=…``：取 uddg 参数（parse_qs
      自带一次百分号解码），只认 http/https；
    - 协议相对 ``//host/path``：补 https:；
    - 站内相对 ``/path``：按 duckduckgo.com 补全（理论上只该出现在广告链，
      已被 y.js 规则跳过，这里兜底）；
    - 其余（javascript: 等）丢弃。
    """
    href = (href or "").strip()
    if not href:
        return ""
    if "uddg=" in href:
        try:
            query = urllib.parse.urlsplit(href).query
        except ValueError:
            query = ""
        target = (urllib.parse.parse_qs(query, keep_blank_values=True).get("uddg")
                  or [""])[0].strip()
        if target.startswith(("http://", "https://")):
            return target
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/") and not href.startswith("//"):
        return "https://duckduckgo.com" + href
    return ""


# ---------------------------------------------------------------------------
# DuckDuckGo / Tavily 检索实现
# ---------------------------------------------------------------------------


def duckduckgo_search(query: str, *, urlopen: "UrlOpen | None" = None,
                      timeout: float = 8.0) -> List[SearchResult]:
    """DuckDuckGo 免费搜索（无 key）：GET html 端点 → 解析前 MAX_RESULTS 条。

    - 挑战页/anomaly 页抛 SearchError（提示换 Tavily）；
    - 无结果（no-results 页或零锚点）返回空列表；
    - 网络异常（URLError/超时/OSError）统一包装成 SearchError。
    """
    open_url = urlopen or _default_urlopen
    q = str(query or "").strip()
    if not q:
        return []
    url = DDG_HTML_ENDPOINT + "?" + urllib.parse.urlencode({"q": q})
    request = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with open_url(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:      # 403/429 等明确 HTTP 失败
        raise SearchError(f"DuckDuckGo 返回 HTTP {exc.code}（查询被拒或限流）") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SearchError(f"DuckDuckGo 连接失败：{exc}") from exc

    page = raw.decode("utf-8", errors="replace")
    # 反爬挑战页：文案命中即抛错（不能当「无结果」也不能让假页混进资料）
    if "Unfortunately, bots use DuckDuckGo too" in page:
        raise SearchError(
            "DuckDuckGo 返回反爬挑战页（本机出口被风控）；可改配 "
            "[tools] search_provider='tavily' 并填 tavily_api_key")
    parser = _DDGResultParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception as exc:  # noqa: BLE001 —— 残缺 HTML 也按失败处理，不静默
        raise SearchError(f"DuckDuckGo 结果页解析失败：{exc}") from exc
    if parser.anomaly:
        raise SearchError(
            "DuckDuckGo 返回验证页（anomaly）；可改配 "
            "[tools] search_provider='tavily' 并填 tavily_api_key")
    if parser.no_results:
        return []
    return parser.results[:MAX_RESULTS]


def tavily_search(query: str, *, api_key: str, urlopen: "UrlOpen | None" = None,
                  timeout: float = 12.0) -> List[SearchResult]:
    """Tavily 搜索（有 key 更准）：POST JSON {api_key, query, max_results:5}。

    响应 ``results[].title/content/url`` 直接映射 SearchResult（content=摘要）。
    HTTP 错误/JSON 解析失败统一抛 SearchError；日志与异常不携带 api_key 原文。
    """
    open_url = urlopen or _default_urlopen
    q = str(query or "").strip()
    if not q:
        return []
    body = json.dumps({
        "api_key": str(api_key or ""),
        "query": q,
        "max_results": MAX_RESULTS,
    }).encode("utf-8")
    request = urllib.request.Request(
        TAVILY_ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json", **_HEADERS})
    try:
        with open_url(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 只取状态码与截断的错误体，绝不回显请求体（含 key）
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise SearchError(f"Tavily 返回 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SearchError(f"Tavily 连接失败：{exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SearchError(f"Tavily 响应不是合法 JSON：{exc}") from exc
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise SearchError("Tavily 响应缺少 results 列表")
    out: List[SearchResult] = []
    for item in items[:MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("content") or "").strip()
        url = str(item.get("url") or "").strip()
        if title or snippet or url:
            out.append(SearchResult(title=title, snippet=snippet, url=url))
    return out


# ---------------------------------------------------------------------------
# Bing 中国版搜索（默认：免 key、中国直连、中文效果最佳）
# ---------------------------------------------------------------------------

BING_ENDPOINT = "https://cn.bing.com/search"
_BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def bing_search(query: str, *, urlopen: "UrlOpen | None" = None,
                timeout: float = 8.0) -> List[SearchResult]:
    """Bing 中国版免费搜索（无 key，中国直连）：GET cn.bing.com → 解析前 5 条。

    HTML 结构（实测 cn.bing.com/search?q=<urlencoded>）：
    - 结果卡片 ``<li class="b_algo">…</li>``（每页约 10 个）
    - 标题 ``<h2><a href="<真链>">标题</a></h2>``
    - 摘要 ``<p>…</p>``（b_algo 内首个 p，含 <strong> 等内联标签）
    - 实测中文查询「Python 教程」返回 10 条有效结果
    """
    open_url = urlopen or _default_urlopen
    q = str(query or "").strip()
    if not q:
        return []
    url = BING_ENDPOINT + "?" + urllib.parse.urlencode({"q": q})
    request = urllib.request.Request(url, headers=_BING_HEADERS, method="GET")
    try:
        with open_url(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise SearchError(f"Bing 返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SearchError(f"Bing 连接失败：{exc}") from exc

    page = raw.decode("utf-8", errors="replace")
    results: List[SearchResult] = []
    # 逐块解析 b_algo
    for block in re.finditer(r'<li class="b_algo"[^>]*>(.*?)</li>', page, re.S):
        if len(results) >= MAX_RESULTS:
            break
        block_text = block.group(1)
        # 标题+URL
        title_m = re.search(
            r'<h2>\s*<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>\s*</h2>',
            block_text, re.S)
        if not title_m:
            continue
        url_found = html.unescape(title_m.group(1).strip())
        title = html.unescape(
            re.sub(r"<[^>]+>", "", title_m.group(2))).strip()
        if not title:
            continue
        # 摘要（b_algo 内首个 p）
        snippet_m = re.search(r"<p[^>]*>(.*?)</p>", block_text, re.S)
        snippet = ""
        if snippet_m:
            snippet = html.unescape(
                re.sub(r"<[^>]+>", "", snippet_m.group(1))).strip()
        results.append(SearchResult(
            title=title, snippet=snippet, url=url_found))
    return results


def search(query: str, *, provider: str = "bing",
           tavily_api_key: str = "", urlopen: "UrlOpen | None" = None,
           timeout: float | None = None) -> List[SearchResult]:
    """统一检索入口：按 provider 分派。

    优先级（v2：Bing 为默认，中国直连零 key）：
    - provider='bing'（默认）→ Bing 中国版（cn.bing.com，免 key，中国直连）
    - provider='tavily' 且 tavily_api_key 非空 → Tavily（最准，需 key）
    - provider='duckduckgo' → DuckDuckGo（海外网络）
    - tavily 无 key 回落 Bing；Bing 失败不自动回落（调用方降级不注入）
    """
    p = str(provider or "").strip().lower()
    if p == "tavily" and str(tavily_api_key or "").strip():
        kwargs: dict[str, Any] = {"api_key": tavily_api_key, "urlopen": urlopen}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return tavily_search(query, **kwargs)
    kwargs = {"urlopen": urlopen}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if p == "bing":
        return bing_search(query, **kwargs)
    # tavily 无 key / duckduckgo / 未知：回落 DDG（测试兼容 + 海外兜底）
    return duckduckgo_search(query, **kwargs)


# ---------------------------------------------------------------------------
# 资料引用块渲染与管线集成入口
# ---------------------------------------------------------------------------


def format_results_block(query: str, results: "List[SearchResult]",
                         provider: str = "duckduckgo") -> str:
    """检索结果 → 资料引用块（prepend 给 user_text 的形态）。

    - 定界符与防护句复用 persona 的 <资料引用开始>/<资料引用结束> +
      INJECTION_GUARD（与记忆/mood 注入同一隔离手法）；
    - 条目形态 ``[n] 标题 —— 摘要（来源: url）``，url 逐字保留供她转述出处；
    - 空结果返回空串（调用方据此跳过注入）。
    """
    if not results:
        return ""
    lines = [
        f"以下是通过联网搜索（{provider}）为「{str(query).strip()}」查到的公开资料。"
        f"{INJECTION_GUARD}",
        "",
    ]
    for index, item in enumerate(results[:MAX_RESULTS], start=1):
        parts = [f"[{index}] {item.title.strip()}"]
        if item.snippet:
            parts.append(f"—— {item.snippet.strip()}")
        if item.url:
            parts.append(f"（来源: {item.url.strip()}）")
        lines.append(" ".join(part for part in parts if part))
    return f"{MATERIAL_OPEN}\n" + "\n".join(lines) + f"\n{MATERIAL_CLOSE}"


async def maybe_search_block(text: str, tools_cfg: Any, *,
                             urlopen: "UrlOpen | None" = None) -> str:
    """聊天管线集成入口（pipeline.py 与 console/core.py 共用）。

    全程降级安全：开关关 / 无搜索意图 / 检索失败 / 零结果 一律返回空串，
    绝不抛出、绝不阻塞对话（网络请求在 asyncio.to_thread 工作线程执行）。
    命中时返回资料引用块，调用方 prepend 到 user_text 前。

    tools_cfg：[tools] 段（config.ToolsConfig 或等价鸭子类型；None 视为全关）。
    """
    if tools_cfg is None or not getattr(tools_cfg, "web_search_enabled", False):
        return ""
    message = str(text or "").strip()
    if not message or not has_search_intent(message):
        return ""
    query = extract_search_query(message)
    provider = str(getattr(tools_cfg, "search_provider", "") or "").strip().lower()
    api_key = str(getattr(tools_cfg, "tavily_api_key", "") or "")
    try:
        results = await asyncio.to_thread(
            search, query, provider=provider, tavily_api_key=api_key,
            urlopen=urlopen)
    except asyncio.CancelledError:
        raise
    except SearchError as exc:
        logger.warning("[websearch] 搜索失败（降级不注入）：%s", exc)
        return ""
    except Exception:  # noqa: BLE001 —— 任何检索侧故障都不拖垮聊天
        logger.exception("[websearch] 搜索出现意外错误（降级不注入）")
        return ""
    if not results:
        logger.info("[websearch] 「%s」无搜索结果（降级不注入）",
                    query[:32] + ("…" if len(query) > 32 else ""))
        return ""
    logger.info("[websearch] 已注入 %d 条搜索资料（provider=%s，query=%r）",
                len(results), provider or "duckduckgo",
                query[:32] + ("…" if len(query) > 32 else ""))
    return format_results_block(query, results, provider=provider or "duckduckgo")
