# -*- coding: utf-8 -*-
"""virtuallover/net.py —— 本机端点感知的 urlopen（五轮修缮第二轮·集成修复）。

实证（本机 Windows 注册表 ``ProxyEnable=1``，``ProxyServer=127.0.0.1:26561``，
``ProxyOverride`` **不含** ``127.0.0.1``/``<local>``）：裸
``urllib.request.urlopen`` 会按系统代理发请求——ChatClient 发往本地大脑
``http://127.0.0.1:8002/v1/chat/completions`` 的每一跳都被代理劫持回
404，她整段哑火（vl.log ``POST /api/brain/ping 404``；对照第一轮实证的
403 欠费哑火，这是第二层根因：**回环流量永远不该走代理**）。

裁定：
- 目标 host 是回环/私网/链路本地（RFC1918 + 127.0.0.0/8 + link-local）→
  **禁代理直连**——这些地址按语义只在本机/内网可达，走外部代理永无正确
  结果（代理要么 404 要么拒绝）；
- 公网目标 → 保持 ``urllib.request.urlopen`` 系统默认行为不变（云端
  LLM/embedding 用户可能依赖系统代理出网，不能替人做主禁掉）；
- ``proxy_bypass()`` 不用——它依赖用户正确配置 ProxyOverride（本机就是
  反例），按 IP 语义判定才可靠。

共享单例 opener（OpenerDirector.open 可多线程并发调用，标准库语义）。
"""

from __future__ import annotations

import ipaddress
import urllib.request
from urllib.parse import urlsplit

__all__ = ["smart_urlopen", "is_local_endpoint", "direct_opener"]

# 禁代理直连 opener（模块级单例；对本地端点的毫秒级探测/chat 高频复用）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_NON_IP_LOCAL = frozenset({"localhost"})


def is_local_endpoint(url: str) -> bool:
    """判定 URL 目标是否本机/内网端点（回环、私网、链路本地）。

    host 解析不动（异常/空）按「非本地」计——保守方向：公网语义不受影响，
    解析失败最多维持旧行为（走系统代理），绝不会把公网请求误判成直连。
    """
    try:
        host = (urlsplit(str(url)).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _NON_IP_LOCAL or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 域名（含内网 DNS 名）：按公网处理，维持系统代理行为
    return ip.is_loopback or ip.is_private or ip.is_link_local


def direct_opener() -> urllib.request.OpenerDirector:
    """禁代理直连 opener（共享单例）。"""
    return _DIRECT_OPENER


def smart_urlopen(target, timeout=None, **kwargs):
    """urlopen 的本机端点感知替身：本地直连、公网走系统默认。

    签名兼容 ``urllib.request.urlopen(req, timeout=...)``——可直接作为
    ChatClient/EmbeddingClient 的 ``urlopen`` 注入默认值与各探活点调用。
    """
    url = getattr(target, "full_url", None) or str(target)
    if is_local_endpoint(url):
        return _DIRECT_OPENER.open(target, timeout=timeout, **kwargs)
    return urllib.request.urlopen(target, timeout=timeout, **kwargs)
