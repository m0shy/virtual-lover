# -*- coding: utf-8 -*-
"""管理控制台 —— 纯逻辑层（core）。workbench/core.py 移植（port-of-own）。

移植改动（相对 workbench/core.py，逻辑零改动项不重述）：
- **删除 AstrBot 段**：SERVICE_TABLE 六容器探测、docker_health_probe、
  cmd_config_path 与 load_engine（动态 import plugins/vl_mood_engine）全部移除；
- 情绪渲染改用本项目自有引擎 ``virtuallover.soul.mood``（port-of-own 并入体，
  MoodState / affection_level / LEVEL_ZH / MOOD_ZH / intensity_label 同名可用，
  不再动态加载、不再需要防遮蔽校验）；
- /api/status 改进程内探测（新增 :func:`vl_status`）：查本进程各模块实例 +
  语音后端 HTTP 探针（唯一保留的外部探测，optional）；
- AppConfig 改 :meth:`AppConfig.for_root`：路径锚定运行根（local/… 与 backup/），
  新增 vl_toml_path / napcat_config_dir（napcat 指向切换动作的改写对象）。

设计约束（沿用 v2）：
- 本模块绝不在测试路径上发真实网络请求：探测一律通过注入的 fetch_fn；
- 所有文件读取均有 try/except，任何缺失/损坏都降级为可序列化的结构，绝不抛裸异常；
- 中文标签必须复用 virtuallover.soul.mood，禁止复制映射表；
- 日志绝不打印 token/密码全文（最多前 4 位）；
- 配置类 JSON 一律按 utf-8-sig 读取（实测带 BOM）。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from virtuallover.net import smart_urlopen as _smart_open  # 回环直连（防系统代理劫持 127.0.0.1 探活）
from virtuallover.soul import anniversary as anniversary_module
from virtuallover.soul import mood as mood_module

logger = logging.getLogger("virtuallover.console.core")

# ---------------------------------------------------------------------------
# 路径基准
# ---------------------------------------------------------------------------

CONSOLE_DIR = Path(__file__).resolve().parent          # .../virtuallover/console
DEFAULT_PORT = 8787
DEFAULT_PROBE_TIMEOUT = 3.0
# brain 行探针超时（本地 /v1/models 探活与云端可达性共用；与语音探针的
# 3s 分开——云端公网 RTT 波动大，给足 5s）
BRAIN_PROBE_TIMEOUT = 5.0
# brain 行显示名（api_base 命中本机 = scripts/brain_switch.py 的本地大脑；
# 否则 = 云端 ark。scripts/brain_switch.py cmd_status 同源文案）
# 模型名从 vl.toml [chat].model 动态读取（不再硬编码）
# 同上
# 本机 host 白名单（brain 行判定 api_base 是否指向本地大脑；
# load_config 已把 host.docker.internal/localhost 归一化为 127.0.0.1，
# 这里仍全量列出防文件层残留）
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# ---------------------------------------------------------------------------
# 校验规则（与 API 契约逐字一致）
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVAL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.md$")
DIARY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")

# fetch_fn(url, timeout, headers=None) -> (ok, ms)
#   （R7 扩展第三参：语音引擎 Basic 武装后探针需带 Authorization 头；
#    headers 仅 http_probe 生产实现消费，测试假件可忽略）
FetchFn = "callable"

# 语音引擎 Basic 认证用户名（与 virtuallover.voice.gsvi.GSVI_AUTH_USER 同源，
# FUSION_SPEC §3.1；口令单源 vl.toml [tts].tts_auth_password——此处只取值
# 不直接 import，保持 core 对 voice 子包零依赖）
TTS_AUTH_USER = "vl"


class ApiError(Exception):
    """携带 HTTP 状态码的业务错误（400/404 等），由 app 层转成 JSON 响应。

    payload 非 None 时 app 层原样下发该响应体（如引擎代理要求的
    {status:'error', message:...} 形态）；否则下发 {"error": message}。
    """

    def __init__(self, status: int, message: str, payload: "dict | None" = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.payload = payload


# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    """控制台运行配置；fetch_fn 可注入 stub，测试不发真实请求。"""

    port: int = DEFAULT_PORT
    data_dir: Path = field(default_factory=lambda: Path("./local/shared_plugin_data"))
    local_dir: Path = field(default_factory=lambda: Path("./local"))
    napcat_config: Path = field(default_factory=lambda: Path("./local/napcat_config/webui.json"))
    console_path: Path = field(default_factory=lambda: Path("./local/console.json"))
    backup_dir: Path = field(default_factory=lambda: Path("./backup"))
    static_dir: Path = CONSOLE_DIR / "static"
    vl_toml_path: Path = field(default_factory=lambda: Path("./local/config/vl.toml"))
    napcat_config_dir: Path = field(default_factory=lambda: Path("./local/napcat_config"))
    # 运行时状态目录（[storage].data_dir，默认 local/vl_data）——anniversary.json /
    # first_voice.json 落点（「我们的回忆」页 /api/memories 读这里）
    vl_data_dir: Path = field(default_factory=lambda: Path("./local/vl_data"))
    # 主日志落点（五轮修缮 R2：/api/recent-errors 跨重启兜底的解析对象）
    log_path: Path = field(default_factory=lambda: Path("./local/logs/vl.log"))
    fetch_fn: "FetchFn | None" = None
    brain_fetch_fn: "FetchFn | None" = None
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT

    @classmethod
    def for_root(cls, root: "Path | str", *, port: "int | None" = None,
                 vl_data_dir: "Path | str | None" = None) -> "AppConfig":
        """路径锚定运行根（默认仓库根；冒烟/测试可指向 tmp）构建配置。

        vl_data_dir：[storage].data_dir 锚定运行根后的运行态目录（缺省 local/vl_data）。
        """
        root = Path(root)
        local = root / "local"
        return cls(
            port=int(port) if port else DEFAULT_PORT,
            data_dir=local / "shared_plugin_data",
            local_dir=local,
            napcat_config=local / "napcat_config" / "webui.json",
            console_path=local / "console.json",
            backup_dir=root / "backup",
            static_dir=CONSOLE_DIR / "static",
            vl_toml_path=local / "config" / "vl.toml",
            napcat_config_dir=local / "napcat_config",
            vl_data_dir=Path(vl_data_dir) if vl_data_dir else local / "vl_data",
            log_path=local / "logs" / "vl.log",
            fetch_fn=http_probe,
            brain_fetch_fn=http_reachability_probe,
        )


# ---------------------------------------------------------------------------
# /api/status —— 进程内探测（SPEC §10 新增语义）
# ---------------------------------------------------------------------------


def http_probe(url: str, timeout: float,
               headers: "dict[str, str] | None" = None) -> "tuple[bool, int]":
    """HTTP 探测实现：GET url，2xx/3xx 视为存活；任何异常一律返回 (False, 0)。

    R7：headers 可选（FUSION_SPEC §3.1 引擎 Basic 武装后语音探针需带
    Authorization 头，见 vl_status 的 voice 行）。
    """
    start = time.perf_counter()
    try:
        merged = {"User-Agent": "vl-console/2.0"}
        if headers:
            merged.update(headers)
        request = urllib.request.Request(url, headers=merged)
        with _smart_open(request, timeout) as resp:
            status = resp.status if getattr(resp, "status", None) else resp.getcode()
            ok = 200 <= int(status) < 400
        return ok, max(0, int(round((time.perf_counter() - start) * 1000)))
    except Exception:  # noqa: BLE001 —— 超时/连接拒绝/DNS 失败等一律按不可用计
        return False, 0


def http_reachability_probe(url: str, timeout: float) -> "tuple[bool, int]":
    """可达性探测实现：任何 HTTP 应答（含 401/404/5xx）都算可达。

    与 http_probe 的差别：云端 API 端点对裸 GET 常回 401（未带密钥）/405
    （方法不允许），但「有应答」本身就是可达性证明；只有连接拒绝/超时/DNS
    失败才算不可达。HTTPError 由 urlopen 在 4xx/5xx 时抛出，单独接住。
    """
    start = time.perf_counter()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "vl-console/2.0"})
        with _smart_open(request, timeout):
            pass
    except urllib.error.HTTPError:
        pass  # 服务器有应答 → 可达
    except Exception:  # noqa: BLE001 —— 连接拒绝/超时/DNS 失败等按不可达计
        return False, 0
    return True, max(0, int(round((time.perf_counter() - start) * 1000)))


def is_local_api_base(api_base: str) -> bool:
    """chat.api_base 是否指向本机（本地大脑判定；空值按云端处理——无可探）。"""
    host = urllib.parse.urlsplit(str(api_base or "")).hostname
    return bool(host) and host.lower() in _LOCAL_HOSTS


def local_models_probe_url(api_base: str) -> str:
    """从 chat.api_base 归出本地大脑探活 URL：scheme://host:port + /health。

    兼容 api_base 带/不带 /v1 后缀两种写法（scripts/brain_switch.py 写入的
    本地形态是 http://127.0.0.1:8002/v1）。netloc 缺失返回 ""（无可探）。
    R7（FUSION_SPEC §3.3）：--api-key 武装后 /v1/models 未带 Bearer 恒 401，
    严格 2xx 探针把健康的大脑画成红灯（R6-R8 真机复现）；/health 是
    llama.cpp 的健康端点且**免鉴权**（armed 实测仍 200，模型加载期 503 →
    红灯符合「未就绪」语义），探活与密钥解耦——scripts/brain_switch.py
    _server_ready 同款裁定。
    """
    parts = urllib.parse.urlsplit(str(api_base or "").strip())
    if not parts.netloc:
        return ""
    return urllib.parse.urlunsplit(
        (parts.scheme or "http", parts.netloc, "/health", "", ""))


def brain_service_row(
    runtime: object,
    *,
    fetch_fn: "FetchFn | None" = None,
    brain_fetch_fn: "FetchFn | None" = None,
    timeout: float = BRAIN_PROBE_TIMEOUT,
) -> dict:
    """brain 行（/api/status 增强）：按 chat.api_base 判定本地/云端并探活。

    - api_base 命中本机 → 名「大脑·本地模型」，对 <base>/v1/models
      探活（fetch_fn 严格 2xx/3xx——404 说明占端口的不是 llama-server，须报红）；
    - 否则 → 名「大脑·云端 ark」，对 api_base 做可达性探测
      （brain_fetch_fn 宽容语义——401/405 也是「通」）；
    - 探活失败 ok=False（前端渲染红点/「失败」，即任务要求的「失败标红」；
      故 optional=False——大脑挂了她就开不了口，不是可降级组件）。

    真探活降级（五轮修缮 R2，实证 vl.log:4420 ark 欠费 403 哑 8 小时全绿）：
    探活只证「可达」，不证「能说话」。chat 客户端 recent_failure()（10 分钟
    窗口内有调用失败记录）非空时，ok 仍按探活真值，但行内附
    degraded=True + note=失败原因——前端渲染黄灯，不再把 403 哑火画成全绿。
    """
    cfg = getattr(runtime, "cfg", None)
    api_base = str(getattr(getattr(cfg, "chat", None), "api_base", "") or "")
    model_name = str(getattr(getattr(cfg, "chat", None), "model", "") or "").strip()
    if is_local_api_base(api_base):
        name = f"大脑·本地 {model_name}" if model_name else "大脑·本地"
        probe_url, probe_fn = local_models_probe_url(api_base), fetch_fn
    else:
        name = f"大脑·云端 {model_name}" if model_name else "大脑·云端"
        probe_url = api_base.strip()
        probe_fn = brain_fetch_fn or fetch_fn
    ok, ms = False, 0
    if probe_url and probe_fn is not None:
        try:
            ok, ms = probe_fn(probe_url, timeout)
        except Exception as exc:  # noqa: BLE001 —— stub 或网络层异常都兜住
            logger.warning("大脑探针异常，按不可用计: %s", exc)
            ok, ms = False, 0
    # 真探活叠加：探活 ok 但最近调用失败（如 403 欠费）→ 黄灯降级提示
    chat_client = getattr(runtime, "chat_client", None)
    recent = ""
    if chat_client is not None and callable(getattr(chat_client, "recent_failure", None)):
        try:
            recent = str(chat_client.recent_failure() or "")
        except Exception:  # noqa: BLE001 —— 观测面故障不影响探活主行
            recent = ""
    row = _svc("brain", name, ok, ms)
    if ok and recent:
        row["degraded"] = True
        row["note"] = f"最近调用失败（{recent}）"
    else:
        row["degraded"] = False
    return row


def _svc(key: str, name: str, ok: bool, ms: int = 0, optional: bool = False) -> dict:
    """单服务状态项（响应契约形态与 workbench v1/v2 一致）。"""
    return {"key": key, "name": name, "ok": bool(ok), "ms": int(ms), "optional": bool(optional)}


# ---------------------------------------------------------------------------
# 观测计数 chips（五轮修缮 R2：总览服务卡尾行「打断 N 轮/语音降级 N 次/
# LLM 失败 N 次」，since=进程启动）
# ---------------------------------------------------------------------------

#: 进程启动时刻（模块导入时刻≈装配时刻；chips 的 since 锚点，秒级 epoch）
PROCESS_START_TS = time.time()

#: _read_history 反推 limit 的口径常量（pipeline 侧消费同源数值，控制台展示用）
HISTORY_EST_TOKENS_PER_MESSAGE = 32  # 实测 158 条 ≈ 6201 token（~39/条），取 32 保守多读


def counters_payload(runtime: object) -> dict:
    """聚合进程启动以来的关键计数（非零才亮的 chips 数据源）。

    - interrupt：管线打断丢弃轮数（MessagePipeline.superseded_count）；
    - llm_fail：LLM 调用最终失败次数（R2 新增 llm_fail_count）；
    - voice_degraded：语音降级次数（VoiceManager.stats.degraded_skips——
      引擎未就绪/熔断冷却期跳过合成，该段保持纯文字）；
    - since：进程启动 epoch 秒（前端提示「自 xx:xx 起」）。
    各槽位缺失（模块未装配/测试桩）一律 0，绝不抛异常。
    """
    pipeline = getattr(runtime, "message_pipeline", None)
    interrupt = int(getattr(pipeline, "superseded_count", 0) or 0)
    llm_fail = int(getattr(pipeline, "llm_fail_count", 0) or 0)
    voice = getattr(runtime, "voice_manager", None)
    stats = getattr(voice, "stats", None)
    voice_degraded = int(stats.get("degraded_skips", 0)) if isinstance(stats, dict) else 0
    return {
        "interrupt": interrupt,
        "llm_fail": llm_fail,
        "voice_degraded": voice_degraded,
        "since": int(PROCESS_START_TS),
    }


def _tts_auth_probe(fetch_fn: "FetchFn", tts_password: str) -> "FetchFn":
    """语音探针认证包装（R7，FUSION_SPEC §3.1）：引擎 Basic 武装后
    /character_list 未带认证恒 401，严格 2xx 探针把活着的引擎画成红灯
    （R6-R8 真机复现：合成成功而状态卡语音行标红）。认证头与语音客户端
    同源：用户名固定 TTS_AUTH_USER、口令单源 vl.toml [tts].tts_auth_password。
    客户端侧（voice/gsvi.py _auth_headers）在引擎未武装时带头也会被放行，
    故探针无条件带头，与「引擎侧开关状态」解耦，谁先重启都对齐。
    """
    token = base64.b64encode(
        f"{TTS_AUTH_USER}:{tts_password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}"}

    def probe(url: str, timeout: float) -> "tuple[bool, int]":
        return fetch_fn(url, timeout, headers)

    return probe


def vl_status(
    runtime: object,
    *,
    fetch_fn: "FetchFn | None" = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    now: "float | None" = None,
    brain_fetch_fn: "FetchFn | None" = None,
    brain_timeout: float = BRAIN_PROBE_TIMEOUT,
) -> dict:
    """聚合本进程各模块状态，返回 /api/status 响应体（SPEC §10：改查进程内）。

    runtime 为鸭类型的装配上下文（virtuallover.__main__.AppContext）：
    各模块实例槽位缺失（未装配/装配失败）即如实报 down，绝不假装在线。
    fetch_fn 仅用于语音后端 HTTP 探针（optional，外部进程）；brain_fetch_fn
    用于云端大脑可达性探测（brain 行，见 brain_service_row）。
    """
    cfg = getattr(runtime, "cfg", None)
    bridge = getattr(runtime, "bridge", None)
    listening = False
    if bridge is not None:
        listening = getattr(bridge, "_server", None) is not None

    services: "list[dict]" = [
        _svc("bridge", "OneBot 桥监听", bridge is not None and listening),
        _svc("napcat", "协议端连接", bridge is not None and bool(bridge.connected)),   # key 沿用 napcat（API 契约），文案 R6 中性化
        _svc("llm", "LLM 客户端", getattr(runtime, "chat_client", None) is not None),
    ]
    # embedding 行（R2 熔断黄灯）：客户端在但熔断开路（连续失败 3 次→10min
    # 跳过）→ degraded=True + 「记忆检索降级中」——检索不注入，对话照常
    embedding_client = getattr(runtime, "embedding_client", None)
    emb_row = _svc("embedding", "embedding 客户端", embedding_client is not None)
    if embedding_client is not None and callable(getattr(embedding_client, "breaker_open", None)):
        try:
            if embedding_client.breaker_open():
                remaining_fn = getattr(embedding_client, "breaker_remaining_seconds", None)
                remaining = float(remaining_fn()) if callable(remaining_fn) else 0.0
                emb_row["degraded"] = True
                emb_row["note"] = f"记忆检索降级中（embedding 熔断，{remaining:.0f}s 后半开）"
            else:
                emb_row["degraded"] = False
        except Exception:  # noqa: BLE001 —— 观测面故障不影响主行
            emb_row["degraded"] = False
    services.append(emb_row)
    # brain 行（/api/status 增强）：当前生效 chat provider 的活体探测——
    # 本地大脑探 8002/v1/models，云端探 api_base 可达性（失败标红，optional=False）
    services.append(brain_service_row(
        runtime, fetch_fn=fetch_fn, brain_fetch_fn=brain_fetch_fn,
        timeout=brain_timeout))
    history = getattr(runtime, "history", None)
    history_ok = False
    if history is not None:
        try:
            history.count(None)  # 全表计数（只读；库打不开会抛异常）
            history_ok = True
        except Exception:  # noqa: BLE001
            history_ok = False
    services.append(_svc("history", "历史库", history_ok))
    store = getattr(runtime, "memory_store", None)
    services.append(_svc("memory", "记忆库",
                         store is not None
                         and getattr(store, "blocked_reason", None) is None))
    services.append(_svc("mood", "情绪引擎", getattr(runtime, "mood_engine", None) is not None))
    services.append(_svc("diary", "日记服务", getattr(runtime, "diary_service", None) is not None))
    services.append(_svc("proactive", "主动消息", getattr(runtime, "proactive", None) is not None))

    # 语音后端：唯一的外部 HTTP 探针（可选组件，down 不影响框架）
    tts_base = getattr(getattr(cfg, "tts", None), "base_url", "") or ""
    tts_enabled = bool(getattr(getattr(cfg, "tts", None), "enabled", False))
    voice_ok, voice_ms = False, 0
    if tts_enabled and tts_base and fetch_fn is not None:
        try:
            voice_probe = fetch_fn
            tts_password = str(getattr(getattr(cfg, "tts", None),
                                       "tts_auth_password", "") or "")
            if tts_password:
                voice_probe = _tts_auth_probe(fetch_fn, tts_password)
            voice_ok, voice_ms = voice_probe(tts_base.rstrip("/") + "/character_list", timeout)
        except Exception as exc:  # noqa: BLE001 —— stub 或网络层的任何异常都兜住
            logger.warning("探测语音后端异常，按不可用计: %s", exc)
            voice_ok, voice_ms = False, 0
    services.append(_svc("voice", "语音后端", voice_ok, voice_ms, optional=True))

    generated_at = int(time.time() if now is None else now)
    payload = {"services": services, "generated_at": generated_at}
    # R2 计数器 chips（打断/语音降级/LLM 失败；非零才亮，前端渲染）
    payload["counters"] = counters_payload(runtime)
    return payload


# ---------------------------------------------------------------------------
# /api/brain/ping —— 「测她一句话」真 1-token 对话（五轮修缮 R2）
# ---------------------------------------------------------------------------

#: 探针 prompt：只回一个字，压低耗时与计费；内容不含任何敏感值
PING_SYSTEM_PROMPT = "你是连通性探针。只回复一个字：好。"
PING_USER_PROMPT = "ping"
PING_MAX_TOKENS = 1
#: 探针回复截断上限（观测值，防异常上游灌长文进状态卡）
PING_REPLY_MAX_CHARS = 40


async def brain_ping_async(runtime: object, *, timeout: float = 30.0) -> dict:
    """真发一次 1-token 对话（chat_async），返回 {ok, ms, reply, provider_id}。

    - chat 客户端未装配 → ok=False + 原因（绝不假装成功）；
    - 调用异常（含 403 欠费/超时/网络）→ ok=False + 截断原因串——
      这正是「可达性探活全绿但她哑了」时的真话来源；
    - 成功后顺带读 recent_failure()（应为空，探针本身会把失败清零）。
    """
    chat_client = getattr(runtime, "chat_client", None)
    if chat_client is None or not callable(getattr(chat_client, "chat_async", None)):
        return {"ok": False, "error": "chat 客户端未装配，无法对话"}
    started = time.perf_counter()
    try:
        reply = await chat_client.chat_async(
            [
                {"role": "system", "content": PING_SYSTEM_PROMPT},
                {"role": "user", "content": PING_USER_PROMPT},
            ],
            max_tokens=PING_MAX_TOKENS,
            extra_body={"prompt_cache_key": "vl-console-ping"},
        )
    except Exception as exc:  # noqa: BLE001 —— 探针把失败如实带回控制台
        ms = int(round((time.perf_counter() - started) * 1000))
        return {"ok": False, "ms": ms, "error": str(exc)[:PING_REPLY_MAX_CHARS * 4]}
    ms = int(round((time.perf_counter() - started) * 1000))
    content = str(getattr(reply, "content", "") or "").strip()
    return {
        "ok": True,
        "ms": ms,
        "reply": content[:PING_REPLY_MAX_CHARS],
        "provider_id": str(getattr(reply, "provider_id", "") or ""),
    }


def brain_ping_payload(runtime: object) -> dict:
    """控制台线程入口：无事件循环线程内就地驱动 brain_ping_async。"""
    import asyncio

    return asyncio.run(brain_ping_async(runtime))


# ---------------------------------------------------------------------------
# /api/brain/status —— 智能降级观测（五轮修缮第二轮）
# ---------------------------------------------------------------------------


def brain_status_payload(runtime: object) -> dict:
    """/api/brain/status 响应体：BrainRouter 降级状态透传 + 未启用兜底。

    与 brain 行可达性探活的分工：探活证「端口通」，本端点证「降级链状态」——
    黏性模式、兜底次数、最近 LLM 错误（含 HTTP code）、半开探针恢复标记。
    Router 未装配（[chat] fallback_enabled=false，正常态）时如实返回
    enabled=false，绝不假装在线。
    """
    router = getattr(runtime, "brain_router", None)
    if router is None:
        return {
            "enabled": False,
            "mode": "cloud",
            "fallback_count": 0,
            "sticky_switches": 0,
            "consec_auth_fails": 0,
            "cloud_recovered": False,
            "local_ready": False,
            "last_error": None,
            "last_local_error": None,
            "note": "智能降级未启用（[chat] fallback_enabled=false）",
        }
    try:
        payload = router.status()
    except Exception as exc:  # noqa: BLE001 —— 观测面故障不拖垮控制台整页
        logger.warning("brain_router.status() 读取失败，按未启用计: %s", exc)
        return {"enabled": False, "mode": "cloud", "note": f"状态读取失败：{exc}"}
    payload.setdefault("enabled", True)
    return payload


# ---------------------------------------------------------------------------
# /api/mood —— 情绪引擎状态渲染
# ---------------------------------------------------------------------------


def mask_id(user_id: str) -> str:
    """用户 id 打码：≥8 位保留前 3 后 4（如 123***8904）；2~7 位保留首尾；其余全打码。"""
    uid = str(user_id)
    n = len(uid)
    if n >= 8:
        return f"{uid[:3]}***{uid[-4:]}"
    if n >= 2:
        return f"{uid[0]}***{uid[-1]}"
    return "***"


def mood_payload(data_dir: "Path | str") -> dict:
    """构建 /api/mood 响应体（渲染复用 virtuallover.soul.mood，禁复制映射表）。

    文件缺失 / JSON 损坏 / 顶层非 dict 一律 file_found=False、users=[]（契约）。
    值非 dict 的坏条目跳过（与引擎 storage 层行为一致）。
    """
    state_file = Path(data_dir) / "vl_mood_engine" / "mood_states.json"
    if not state_file.is_file():
        return {"file_found": False, "users": [], "note": None}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("mood_states.json 读取/解析失败，按损坏处理: %s", exc)
        return {"file_found": False, "users": [], "note": None}
    if not isinstance(raw, dict):
        logger.warning("mood_states.json 顶层不是 dict，按损坏处理")
        return {"file_found": False, "users": [], "note": None}
    users = []
    for user_id in sorted(str(k) for k in raw):
        entry = raw[user_id]
        if not isinstance(entry, dict):
            logger.warning("mood_states.json 条目 %s 值非 dict，已跳过", mask_id(user_id))
            continue
        try:
            users.append(_render_user(user_id, entry))
        except Exception as exc:  # noqa: BLE001 —— 单条异常不拖垮整个响应
            logger.warning("用户 %s 状态渲染失败，已跳过: %s", mask_id(user_id), exc)
    return {"file_found": True, "users": users, "note": None}


def _render_user(user_id: str, entry: dict) -> dict:
    """渲染单用户：标签函数全部来自 virtuallover.soul.mood（port-of-own 引擎）。"""
    state = mood_module.MoodState.from_dict(entry)
    level = mood_module.affection_level(state.affection)
    mood = str(state.mood)
    return {
        "id": mask_id(user_id),
        "affection": float(state.affection),
        "level": str(level),
        "level_zh": str(mood_module.LEVEL_ZH.get(level, level)),
        "mood": mood,
        "mood_zh": str(mood_module.MOOD_ZH.get(mood, mood)),
        "intensity_label": str(mood_module.intensity_label(state.mood_intensity)),
        "streak": int(state.daily_streak),
        "total_messages": int(state.total_messages),
        "last_interaction": float(state.last_interaction),
        "updated_at": float(state.updated_at),
    }


# ---------------------------------------------------------------------------
# /api/diary —— 日记
# ---------------------------------------------------------------------------


def list_diary(data_dir: "Path | str") -> dict:
    """日记列表：仅收 YYYY-MM-DD.md，按日期倒序；目录缺失返回空列表。"""
    diary_dir = Path(data_dir) / "vl_diary" / "diary"
    items: "list[dict]" = []
    try:
        children = sorted(diary_dir.iterdir())
    except OSError:
        children = []
    for child in children:
        match = DIARY_FILE_RE.match(child.name)
        if match is None or not child.is_file():
            continue
        try:
            size = child.stat().st_size
        except OSError as exc:
            logger.warning("日记 %s 大小读取失败，已跳过: %s", child.name, exc)
            continue
        items.append({"date": match.group(1), "size": int(size)})
    items.sort(key=lambda item: item["date"], reverse=True)
    return {"items": items, "latest": items[0]["date"] if items else None}


def read_diary(data_dir: "Path | str", date: str) -> dict:
    """按日读取日记。date 不匹配 YYYY-MM-DD 抛 400；不存在抛 404。"""
    if not DATE_RE.fullmatch(date):
        raise ApiError(400, "date 必须匹配 YYYY-MM-DD")
    path = Path(data_dir) / "vl_diary" / "diary" / f"{date}.md"
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ApiError(404, f"日记不存在: {date}") from exc
    except OSError as exc:
        raise ApiError(500, f"日记读取失败: {exc}") from exc
    return {"date": date, "content": content}


# ---------------------------------------------------------------------------
# /api/eval —— 记忆评测报告
# ---------------------------------------------------------------------------


def _is_eval_report(name: str) -> bool:
    """报告文件名规则：eval_*.md（glob 语义，星号可为空）。"""
    return name.startswith("eval_") and name.endswith(".md")


def list_eval(data_dir: "Path | str") -> dict:
    """评测报告列表：仅收 eval_*.md，按文件名倒序；目录缺失返回空列表。"""
    reports_dir = Path(data_dir) / "vl_eval" / "reports"
    items: "list[dict]" = []
    try:
        children = sorted(reports_dir.iterdir())
    except OSError:
        children = []
    for child in children:
        if not _is_eval_report(child.name) or not child.is_file():
            continue
        try:
            size = child.stat().st_size
        except OSError as exc:
            logger.warning("报告 %s 大小读取失败，已跳过: %s", child.name, exc)
            continue
        items.append({"name": child.name, "size": int(size)})
    items.sort(key=lambda item: item["name"], reverse=True)
    return {"items": items}


def read_eval(data_dir: "Path | str", name: str) -> dict:
    """按名读取报告。name 含 .. 或不匹配白名单正则抛 400；不存在抛 404。"""
    if ".." in name or not EVAL_NAME_RE.fullmatch(name):
        raise ApiError(400, "name 非法：仅允许 [A-Za-z0-9_.-] 组成的 *.md，且不得包含 ..")
    path = Path(data_dir) / "vl_eval" / "reports" / name
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ApiError(404, f"报告不存在: {name}") from exc
    except OSError as exc:
        raise ApiError(500, f"报告读取失败: {exc}") from exc
    return {"name": name, "content": content}


# ---------------------------------------------------------------------------
# /api/memories —— 「我们的回忆」页（认识锚点 + 里程碑时间线 + 消息计数 +
#                   首次语音时刻 + 日记足迹）
# ---------------------------------------------------------------------------

#: 回忆页日记足迹条数（日期倒序截断）
MEMORIES_DIARY_MAX = 30
#: 回忆页时间线条数上限（防历史脏数据把卡片撑爆；正常远到不了）
MEMORIES_TIMELINE_MAX = 60

#: 首次语音纪念文件名（<storage.data_dir>/first_voice.json，voice/manager.py 落盘：
#: {"ts": epoch 秒, "text": 截断 50 字}；与 anniversary.json 同目录）
FIRST_VOICE_FILENAME = "first_voice.json"

# 键前缀（soul/anniversary.mark_key_sent 的幂等键形态；旧档纯数字键同 days:N）
_KEY_DAYS = "days:"
_KEY_YEARLY = "yearly:"
_KEY_BIRTHDAY_USER = "birthday:user:"
_KEY_BIRTHDAY_HER = "birthday:her:"
_KEY_HOLIDAY = "holiday:"


def _milestone_label(key: str) -> str:
    """把 milestones_sent 的幂等键翻成中文时间线标签。

    键语义与 soul/anniversary.ceremony_due/mark_key_sent 同源（标签映射表
    anniversary 侧没有，属展示层文案，这里就近维护）：
    - ``days:N`` / 旧档纯数字键 ``N`` → 认识第 N 天；
    - ``yearly:N`` → 认识 N÷365 周年（步长复用 anniversary.YEARLY_STEP_DAYS）；
    - ``birthday:user:YYYY`` / ``birthday:her:YYYY`` → 对应侧生日；
    - ``holiday:YYYY-MM-DD`` → 节日名（anniversary.HOLIDAYS，未收录回退日期）；
    - 未知键原样返回（宁显键名不编文案）。
    """
    key = str(key)
    if key.isdigit():  # 旧档兼容：纯数字键与 days:N 同义
        return f"认识第 {int(key)} 天"
    if key.startswith(_KEY_DAYS):
        return f"认识第 {key[len(_KEY_DAYS):]} 天"
    if key.startswith(_KEY_YEARLY):
        try:
            days = int(key[len(_KEY_YEARLY):])
        except ValueError:
            return key
        return f"认识 {days // anniversary_module.YEARLY_STEP_DAYS} 周年"
    if key.startswith(_KEY_BIRTHDAY_USER):
        return f"{key[len(_KEY_BIRTHDAY_USER):]} 年他的生日"
    if key.startswith(_KEY_BIRTHDAY_HER):
        return f"{key[len(_KEY_BIRTHDAY_HER):]} 年她的生日"
    if key.startswith(_KEY_HOLIDAY):
        raw = key[len(_KEY_HOLIDAY):]
        try:
            name = anniversary_module.holiday_today(datetime.strptime(raw, "%Y-%m-%d"))
        except ValueError:
            name = None
        return f"{name or ''}纪念日" if name else raw
    return key


def _fmt_date(ts: float) -> str:
    """epoch 秒 → 本机日历日 YYYY-MM-DD（展示层统一口径）。"""
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def _memories_anchor(state: dict, now_ts: float) -> "dict | None":
    """从纪念日 state 提取认识锚点（日期 + 认识天数）；锚点缺失/非法/在未来
    （时钟回拨/脏数据）一律 None，宁缺勿猜（soul/anniversary.relation_days_line
    同口径：认识当天即第 1 天）。"""
    anchor = state.get("anchor_ts")
    if not isinstance(anchor, (int, float)) or anchor <= 0:
        return None
    anchor = float(anchor)
    try:
        now_date = datetime.fromtimestamp(now_ts).date()
        anchor_date = datetime.fromtimestamp(anchor).date()
    except (OverflowError, OSError, ValueError):
        return None
    elapsed = (now_date - anchor_date).days
    if elapsed < 0:
        return None
    return {
        "ts": anchor,
        "date": str(state.get("anchor_date") or _fmt_date(anchor)),
        "days": elapsed + 1,  # 认识当天即第 1 天（relation_days_line 同口径）
    }


def _memories_timeline(state: dict) -> "list[dict]":
    """已发里程碑 → 时间线条目（按发送时刻升序：从相识讲到如今的叙事顺序）。

    milestones_sent 缺失/非 dict（旧档/损坏）按空处理；单条时刻缺失按 0 沉底。
    """
    sent = state.get("milestones_sent")
    if not isinstance(sent, dict):
        return []
    items: "list[dict]" = []
    for key, raw_ts in sent.items():
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            ts = 0.0
        items.append({
            "key": str(key),
            "label": _milestone_label(str(key)),
            "date": _fmt_date(ts) if ts > 0 else "",
            "ts": ts,
        })
    items.sort(key=lambda item: (item["ts"], item["key"]))
    return items[:MEMORIES_TIMELINE_MAX]


def _read_first_voice(vl_data_dir: "Path | str") -> "dict | None":
    """读 first_voice.json（{"ts", "text"}）；文件缺失/损坏/形态不对一律 None
    （任务规格：文件存在才显示）。"""
    path = Path(vl_data_dir) / FIRST_VOICE_FILENAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("first_voice.json 读取/解析失败，按缺失处理: %s", exc)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("ts"), (int, float)):
        return None
    ts = float(raw["ts"])
    if ts <= 0:
        return None
    text = str(raw.get("text") or "")
    return {"ts": ts, "date": _fmt_date(ts), "text": text}


def _count_messages(history: object) -> "int | None":
    """历史库全表消息计数；库缺失/计数失败返回 None（前端显示「—」）。"""
    counter = getattr(history, "count", None)
    if not callable(counter):
        return None
    try:
        return int(counter(None))  # session_id=None → 全表计数
    except Exception as exc:  # noqa: BLE001 —— 观测面故障不拖垮回忆页
        logger.warning("历史库消息计数失败，按不可得处理: %s", exc)
        return None


def memories_payload(
    data_dir: "Path | str",
    vl_data_dir: "Path | str | None" = None,
    history: "object | None" = None,
    *,
    now: "float | None" = None,
) -> dict:
    """构建 /api/memories 响应体（「我们的回忆」页，全部只读、零写盘）。

    - ``anchor``：认识锚点（日期 + 认识天数），anniversary.json（soul/
      anniversary.DEFAULT_STATE_RELPATH 同款 <storage.data_dir> 落点）缺席或
      未建锚点时为 None；
    - ``timeline``：已发里程碑时间线（升序，中文标签见 _milestone_label）；
    - ``total_messages``：history 库全表消息计数（不可得为 None）；
    - ``first_voice``：首次语音时刻（local/vl_data/first_voice.json，文件存在
      才显示，缺失/损坏为 None）；
    - ``diary_dates``：日记日期倒序 ≤MEMORIES_DIARY_MAX 条（复用 list_diary）。

    所有文件缺失/损坏一律降级为可序列化结构，绝不抛裸异常；now 可注入
    （测试固定时钟；缺省本机当前时刻）。
    """
    now_ts = time.time() if now is None else float(now)
    state = anniversary_module.load_state(
        Path(vl_data_dir) / "anniversary.json") if vl_data_dir is not None else {}
    diary = list_diary(data_dir) if data_dir is not None else {"items": []}
    dates = [str(item["date"]) for item in diary.get("items", [])]
    return {
        "ok": True,
        "anchor": _memories_anchor(state, now_ts),
        "timeline": _memories_timeline(state),
        "total_messages": _count_messages(history),
        "first_voice": _read_first_voice(vl_data_dir) if vl_data_dir is not None else None,
        "diary_dates": dates[:MEMORIES_DIARY_MAX],
    }


# ---------------------------------------------------------------------------
# NapCat token 读取（引擎代理 napcat 登录用）
# ---------------------------------------------------------------------------


def read_napcat_token(napcat_config: "Path | str") -> "str | None":
    """读 webui.json 的 token 字段；文件缺失/损坏/字段为空返回 None。

    日志安全：任何情况下都不打印 token 全文，debug 级仅记录前 4 位。
    按 utf-8-sig 读取（兼容带/不带 BOM 的文件）。
    """
    path = Path(napcat_config)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.info("NapCat webui.json 不可读（%s），按 token 未配置处理", type(exc).__name__)
        return None
    if not isinstance(raw, dict):
        return None
    token = raw.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    logger.debug("NapCat WebUI token 已加载: %s***（日志已打码）", token[:4])
    return token


# ---------------------------------------------------------------------------
# /api/brain/chat —— 控制台直接跟她聊天（UX 审计：#1 缺失功能）
# ---------------------------------------------------------------------------

#: 聊天页系统提示：轻量包装（不注入完整 1308 字人格——那是 QQ 私聊用的）
CHAT_FALLBACK_SYSTEM = (
    "你是一位温暖的虚拟伴侣。用中文简短回复（1-3句），语气自然亲切。"
)


async def _resolve_console_images(cfg: object, chat_client: object,
                                  message: str) -> "tuple[str, bool]":
    """控制台消息里的图片引用 → 视觉描述（多模态③，QQ 管线同链路）。

    - 提取（brain/vision.extract_image_refs）：data URI base64 / 图片 URL /
      裸 base64 粘贴三种形态；
    - 未开启视觉（supports_vision=false）：消息原样返回、零网络调用——
      不能把用户消息里的 URL/base64 静默吃掉（一次性指引日志在 vision 内）；
    - 开启视觉：每张图识别为描述，消息重写为「清理后文本 +（图片：描述）」
      块，后续走与纯文本完全相同的 persona/记忆/情绪管线；
    - 返回（处理后消息, 是否真的做过视觉识别）。
    """
    from virtuallover.brain import vision as vision_mod

    text, refs = vision_mod.extract_image_refs(message)
    if not refs:
        return message, False
    vision_on = vision_mod.supports_vision(cfg)
    if not vision_on:
        return message, False
    blocks: list = []
    for kind, value in refs[:vision_mod.MAX_IMAGES_PER_MESSAGE]:
        desc = vision_mod.PLACEHOLDER_IMAGE
        try:
            if kind == "url":
                desc = await vision_mod.process_image(
                    value, text, chat_client=chat_client,
                    supports_vision_flag=True)
            else:  # b64（data URI / 裸 base64）：解码后走内存字节路径
                try:
                    data = base64.b64decode(value, validate=False)
                except Exception:  # noqa: BLE001 —— 非法 base64 按占位继续
                    logger.warning("控制台图片 base64 解码失败（长度 %d）", len(value))
                else:
                    desc = await vision_mod.process_image_bytes(
                        data, text, chat_client=chat_client)
        except Exception:  # noqa: BLE001 —— 单图失败不阻塞整条消息
            logger.exception("控制台图片识别异常（按占位继续）")
            desc = vision_mod.PLACEHOLDER_IMAGE
        blocks.append(vision_mod.PLACEHOLDER_IMAGE if desc == vision_mod.PLACEHOLDER_IMAGE
                      else f"（图片：{desc}）")
    text = text.strip()
    merged = "\n".join([text] + blocks) if text else "\n".join(blocks)
    return merged, True

async def brain_chat_async(runtime: object, message: str, *, timeout: float = 120.0) -> dict:
    """控制台聊天：完整管线（人格+情绪+记忆+表情包处理），与 QQ 私聊体验一致。

    v2 修复（用户反馈"对话超奇怪/表情包也没有了"）：
    - 用 persona.assemble 组装（含 mood 片段+记忆注入+时间感），不再裸拼
    - LLM 回复后跑 memes.process_reply 处理 &&tag&& 标记
    - _jpg 后缀归一化（与 QQ 管线一致）

    多模态（图片识别③）：消息里的图片 URL / data URI / 裸 base64 粘贴 →
    brain/vision 同一链路（_resolve_console_images），描述并入消息文本。
    """
    chat_client = getattr(runtime, "chat_client", None)
    if chat_client is None or not callable(getattr(chat_client, "chat_async", None)):
        return {"ok": False, "error": "chat 客户端未装配（vl 未启动或云端未配置）"}

    cfg = getattr(runtime, "cfg", None)

    # ---- 图片识别（多模态③，与 QQ 管线同链路）----
    message, vision_used = await _resolve_console_images(cfg, chat_client, message)
    if not message.strip():
        return {"ok": False, "error": "消息内容为空（只识别到图片且未开启视觉？"
                                     "请配 [chat].supports_vision=true + 视觉模型）"}
    session_id = getattr(cfg, "session_id", "") if cfg else ""
    persona_prompt = getattr(getattr(cfg, "persona", None), "system_prompt", "") if cfg else ""
    if not persona_prompt:
        persona_prompt = CHAT_FALLBACK_SYSTEM

    # 取最近历史
    history = getattr(runtime, "history", None)
    recent: list = []
    if history and hasattr(history, "get_messages") and session_id:
        try:
            rows = history.get_messages(session_id, limit=12)
            recent = [{"role": str(r.get("role", "user")),
                       "content": str(r.get("content", ""))} for r in rows if r.get("content")]
        except Exception:  # noqa: BLE001
            recent = []

    # ---- 情绪快照（与 QQ 管线一致：mood fragment 注入）----
    mood_fragment = ""
    mood_engine = getattr(runtime, "mood_engine", None)
    if mood_engine and cfg:
        try:
            user_id = session_id.rsplit(":", 1)[-1] if ":" in session_id else ""
            from virtuallover.soul.mood import effective_mood
            state = mood_engine.storage.get(user_id) if hasattr(mood_engine, "storage") else None
            if state:
                eff = effective_mood(state, now=time.time())
                if eff and eff[1] >= 0.15:  # 有效强度阈值
                    mood_fragment = mood_engine.render_fragment(user_id)
        except Exception:  # noqa: BLE001
            pass

    # ---- 记忆检索（失败降级为不注入）----
    # 多模态集成修正：MemoryService 没有 search 方法（只有 on_message/remember），
    # 旧代码 hasattr 守卫恒 False → 控制台聊天记忆注入一直是哑的。改用与
    # pipeline._recall 同源的 memory_store.search（余弦+关键词重排，参数取
    # [mnemosyne] 配置），封锁态/未接线/检索失败一律不注入，对话继续。
    memories = None
    memory_store = getattr(runtime, "memory_store", None)
    if memory_store and hasattr(memory_store, "search") and session_id:
        try:
            mnemo_cfg = getattr(cfg, "mnemosyne", None)
            hits = await memory_store.search(
                session_id, message,
                top_k=getattr(mnemo_cfg, "top_k", 3) or 3,
                candidates=getattr(mnemo_cfg, "candidate_k", 8) or 8,
                min_cosine=getattr(mnemo_cfg, "min_similarity", 0.3),
            )
            if hits:
                memories = hits
        except Exception:  # noqa: BLE001 —— 封锁态/降级不注入，聊天继续
            pass

    # ---- 联网搜索（多模态③，brain/websearch.py）：消息含搜索意图关键词
    #      （搜|查|搜索|最新|新闻|今天.*什么|now|latest|news）→ 检索 → 资料
    #      引用块 prepend 到用户消息前。与 QQ 管线（pipeline._search_block）
    #      同源语义：开关关/无意图/检索失败/零结果一律空串不注入，绝不因
    #      搜索故障中断聊天；网络请求在 to_thread 工作线程执行。
    search_block = ""
    try:
        from virtuallover.brain import websearch as websearch_mod
        search_block = await websearch_mod.maybe_search_block(
            message, getattr(cfg, "tools", None))
    except Exception:  # noqa: BLE001 —— 双保险：搜索故障不影响控制台聊天
        logger.exception("[websearch] 控制台搜索资料块构造异常（不注入）")
        search_block = ""

    # ---- persona.assemble 组装（完整管线）----
    try:
        from virtuallover.brain import persona as persona_mod
        # 定界符中和函数定义在 brain/pipeline.py（O3⑥），persona 里没有——
        # 从 persona 导入会 ImportError 且被下方 except 吞成简拼分支
        # （人格/记忆块/截断全不生效）。多模态集成修正：改从 pipeline 导入。
        from virtuallover.brain.pipeline import neutralize_delimiters
        safe_msg = neutralize_delimiters(message)
        # 搜索资料块是 websearch 自产的受控文本（非用户输入），不做定界符
        # 中和——它的 <资料引用开始/结束> 定界符必须原样成立才能隔离资料。
        user_text = (f"{search_block}\n\n{safe_msg}" if search_block else safe_msg)
        # 「她此刻」作息行（soul/schedule；与 QQ 管线 pipeline._schedule_line 同源）
        schedule_line = ""
        try:
            from virtuallover.soul import schedule as schedule_mod
            sched = getattr(cfg, "schedule", None)
            if sched is not None:
                schedule_line = schedule_mod.render_line(
                    time.time(), sched.entries, enabled=sched.enabled) or ""
        except Exception:  # noqa: BLE001 —— 课表渲染失败不注入，聊天继续
            logger.exception("[schedule] 控制台作息行渲染异常（不注入）")
        assembled = persona_mod.assemble(
            persona_name=getattr(getattr(cfg, "persona", None), "name", "") or "",
            persona_prompt=persona_prompt,
            user_text=user_text,
            history=recent,
            begin_dialogs=getattr(getattr(cfg, "persona", None), "begin_dialogs", None),
            memories=memories,
            mood_fragment=mood_fragment,
            schedule_line=schedule_line,
            inject_mood=bool(mood_fragment),
            inject_time=True,
            now=time.time(),
            max_context_tokens=getattr(getattr(cfg, "context", None), "max_context_tokens", 32768),
            truncate_ratio=getattr(getattr(cfg, "context", None), "truncate_ratio", 0.82),
            tz_label="CST",
            mood_position=getattr(getattr(cfg, "mood", None), "fragment_position", "user"),
        )
        messages = assembled.messages
    except ImportError:
        # persona.assemble 不可用——退回简拼（搜索块同样 prepend）
        merged = (f"{search_block}\n\n{message}" if search_block else message)
        messages = [{"role": "system", "content": persona_prompt + ("\n" + mood_fragment if mood_fragment else "")}] + recent + \
                  [{"role": "user", "content": merged}]

    started = time.perf_counter()
    try:
        reply = await chat_client.chat_async(
            messages, extra_body={"prompt_cache_key": "vl-console-chat"})
    except Exception as exc:  # noqa: BLE001
        ms = int(round((time.perf_counter() - started) * 1000))
        return {"ok": False, "ms": ms, "error": str(exc)[:200]}
    ms = int(round((time.perf_counter() - started) * 1000))
    content = str(getattr(reply, "content", "") or "").strip()

    # ---- 表情包处理（与 QQ 管线一致）----
    meme_tags = []
    display_reply = content
    try:
        from virtuallover.soul import memes as memes_mod
        memes_cfg_enable = getattr(getattr(cfg, "memes", None), "enable", True)
        if memes_cfg_enable and content:
            memes_dir = getattr(getattr(cfg, "memes", None), "memes_dir",
                               "local/shared_plugin_data/meme_manager/memes")
            cleaned, tags = memes_mod.process_reply(content, memes_dir, probability=100)
            display_reply = cleaned
            meme_tags = tags  # 提取到的标签列表（前端可显示为 [表情:blush] 等）
    except Exception:  # noqa: BLE001
        # memes 不可用——至少做 _jpg 后缀清理 + 标记提取
        import re as _re
        raw_tags = _re.findall(r'&&([^&\n]+)&&', content)
        meme_tags = [t.removesuffix('_jpg') for t in raw_tags]
        display_reply = _re.sub(r'&&[^&\n]+&&', '', content).strip()

    # 写回历史
    if history and session_id:
        try:
            if hasattr(history, "append_pair"):
                now_ms = int(time.time() * 1000)
                history.append_pair(session_id, message, display_reply, now_ms)
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True, "ms": ms, "reply": display_reply[:1000],
            "meme_tags": meme_tags,
            "vision_used": vision_used,
            "provider_id": str(getattr(reply, "provider_id", "") or "")}


def brain_chat_payload(runtime: object, message: str) -> dict:
    """控制台线程入口。"""
    import asyncio
    return asyncio.run(brain_chat_async(runtime, message))


# ---------------------------------------------------------------------------
# /api/qq/stream + /api/qq/stats —— QQ 运营调试（UX 修缮）
# ---------------------------------------------------------------------------

def qq_stream_payload(runtime: object, *, limit: int = 30) -> dict:
    """/api/qq/stream：最近 N 条消息（时间+角色+内容截断），供运营页实时流。"""
    history = getattr(runtime, "history", None)
    cfg = getattr(runtime, "cfg", None)
    session_id = getattr(cfg, "session_id", "") if cfg else ""
    items: list = []
    if history and hasattr(history, "get_messages") and session_id:
        try:
            rows = history.get_messages(session_id, limit=limit)
            for r in rows:
                ts = r.get("created_at")
                # 毫秒→可读时间
                import datetime
                if isinstance(ts, (int, float)) and ts > 1e12:
                    dt = datetime.datetime.fromtimestamp(ts / 1000)
                    time_str = dt.strftime("%m-%d %H:%M")
                elif isinstance(ts, (int, float)) and ts > 1e9:
                    dt = datetime.datetime.fromtimestamp(ts)
                    time_str = dt.strftime("%m-%d %H:%M")
                else:
                    time_str = "较早"
                items.append({
                    "role": str(r.get("role", "?")),
                    "content": str(r.get("content", ""))[:120],
                    "time": time_str,
                })
            items.reverse()  # 最新在前
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "items": items, "count": len(items)}


def qq_stats_payload(runtime: object) -> dict:
    """/api/qq/stats：连接详情+消息计数——QQ 运营页的状态卡数据源。"""
    bridge = getattr(runtime, "bridge", None)
    connected = bridge is not None and bool(getattr(bridge, "connected", False))

    # 消息计数
    history = getattr(runtime, "history", None)
    cfg = getattr(runtime, "cfg", None)
    session_id = getattr(cfg, "session_id", "") if cfg else ""
    total = 0
    today = 0
    if history and hasattr(history, "count") and session_id:
        try:
            total = history.count(session_id) or 0
        except Exception:  # noqa: BLE001
            pass
    if history and hasattr(history, "list_day") and session_id:
        try:
            import datetime
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            day_rows = history.list_day(session_id, today_str, 0, 1000)
            today = len(day_rows) if isinstance(day_rows, list) else 0
        except Exception:  # noqa: BLE001
            pass

    # 主动消息状态
    proactive = getattr(runtime, "proactive_service", None) or getattr(runtime, "proactive", None)
    proactive_state = "未装配"
    next_trigger = "—"
    if proactive:
        try:
            enabled = getattr(proactive, "enabled", None)
            if enabled is None:
                enabled = getattr(getattr(cfg, "proactive", None), "enable", False)
            proactive_state = "启用" if enabled else "已关闭"
            ntt = getattr(proactive, "next_trigger_ts", None) or getattr(proactive, "_next_trigger_ts", None)
            if ntt and isinstance(ntt, (int, float)) and ntt > 0:
                import datetime
                remaining = ntt - time.time()
                if remaining > 0:
                    mins = int(remaining // 60)
                    next_trigger = f"{mins} 分钟后" if mins > 0 else "即将触发"
                else:
                    next_trigger = "已到期（等待调度）"
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "connected": connected,
        "total_messages": total,
        "today_messages": today,
        "proactive_state": proactive_state,
        "proactive_next": next_trigger,
    }
