# -*- coding: utf-8 -*-
"""brain/router.py —— LLM 主备智能降级路由（五轮修缮第二轮核心）。

实证（vl.log「chat HTTP 403：error.Forbidden/AccountOverdueError」）：ark 欠费
403 是**不可重试**错误，`llm.py` 的 fallback 链（llm.py:316-330）因
build_chat_clients 从未接线 fallbacks 而空转——她整整哑了 8 小时，控制台还
全绿（可达性探测把 403 也算「通」，console/core.py http_reachability_probe）。

本模块做**降级链参谋裁决**，三层语义（对照 voice/lifecycle.py 金样板的
状态机分寸：瞬态不粘、鉴权即粘、恢复靠探针不靠赌）：

- (a) 瞬态故障（5xx/超时/网络，即 LLMError.retriable=True）→ **per-request
  fallback**：主客户端 max_retries 降为 1（fallback 场景不再 4 连击晾用户），
  失败即试本地大脑客户端，下一轮仍先试云端（瞬态可自愈，不粘）；
- (b) 计费/鉴权（401/403）→ **黏性保命**：连续 2 轮即置 sticky_local，此后
  直发本地大脑、不再碰云端；触发时按需拉起 llama-server（LocalBrainManager
  adopt-or-start，参照 voice/lifecycle：端口有应答即收编、绝不杀外部实例）、
  打显著告警、控制台亮「云端故障已切本地，恢复后可切回」；
- (c) 恢复检测 → **30min 半开探针**（1-token chat，成本可忽略）：sticky 期间
  周期探云端，成功只置 cloud_recovered 亮提示，**不自动切回**——欠费恢复
  与计费扣款由人裁决（/brain cloud 或控制台切回）。

安全性裁定：
- 本地客户端与 spawn 参数走独立配置键 ``[chat] fallback_*``，总开关
  ``fallback_enabled`` 默认关——云端用户不装本地大脑也安全（构造工厂直接
  返回 None，装配零行为变化）；fallback 不可用时黏性模式**只告警不 spawn**，
  请求照旧抛原错误（与关闭降级时的「轮静默」行为一致）；
- fallback 路径对 assembled.messages 做 4096 token 预算截断（本地 llama-server
  ``-c 6144`` 窗口小，防装爆；persona 同一套字符近似估算）；
- 本地客户端是独立 ChatClient 实例（独立超时/重试），主云端实例的 extra_body
  （ark thinking、prompt_cache_key）绝不带给本地。

全部外部交互（本地探活/spawn/时钟/睡眠）可注入——pytest 用假客户端 + 假
manager + 假钟直测（对齐 voice/lifecycle 的测试手法）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from virtuallover.brain.llm import ChatClient, LLMError
from virtuallover.brain import persona

logger = logging.getLogger("virtuallover.brain.router")

__all__ = [
    "BrainRouter", "LocalBrainManager", "DegradationState", "SpawnError",
    "build_brain_router", "truncate_for_fallback",
    "MODE_CLOUD", "MODE_STICKY_LOCAL",
    "AUTH_FAIL_STATUSES", "STICKY_AUTH_THRESHOLD",
    "HALFOPEN_PROBE_INTERVAL_SECONDS", "PROBE_MAX_TOKENS",
]

# ---------------------------------------------------------------------------
# 常量（数值出处见各注释）
# ---------------------------------------------------------------------------

MODE_CLOUD = "cloud"              # 常态：直发云端（瞬态失败可 per-request 兜底）
MODE_STICKY_LOCAL = "sticky_local"  # 黏性保命：直发本地，30min 半开探针

AUTH_FAIL_STATUSES = frozenset({401, 403})  # 计费/鉴权故障（AccountOverdueError 实证 403）
STICKY_AUTH_THRESHOLD = 2        # 连续 2 轮鉴权/计费失败 → 黏性（lifecycle「2 死判死」同分寸）

HALFOPEN_PROBE_INTERVAL_SECONDS = 1800.0  # 半开探针周期 30min
PROBE_MAX_TOKENS = 1             # 探针 1-token chat（成本可忽略）

READY_BUDGET_SECONDS = 90.0      # spawn 后就绪预算（brain_switch.chat_probe max_wait 90s 同源）
READY_PROBE_INTERVAL_SECONDS = 1.0
LOCAL_UNAVAILABLE_WARN_INTERVAL = 60.0    # 本地不可用告警节流（防每轮刷屏）
FALLBACK_LOCAL_RETRY_HINT = "重试"

CREATE_NO_WINDOW = 0x08000000    # Windows 不弹控制台窗口（voice/lifecycle 同款）
_IS_WINDOWS = os.name == "nt"

# R7（FUSION_SPEC §3.3）：Router 拉起 llama-server 的 --alias 固定值——与
# scripts/brain_switch.py 的 LLAMA_MARKER/LOCAL_MODEL 同源（scripts/ 独立运行
# 不能 import 本包，两处常量人工同步；漂移=击杀核验恒拒杀 Router 拉起的进程）
LOCAL_BRAIN_ALIAS = "local-brain"


class SpawnError(RuntimeError):
    """llama-server 拉起失败（exe/gguf 缺失、Popen 异常）。只告警，绝不阻塞主链。"""


# ---------------------------------------------------------------------------
# 上下文截断（fallback 路径专用）
# ---------------------------------------------------------------------------


def truncate_for_fallback(messages: "list[dict]", budget_tokens: int) -> "list[dict]":
    """把 assembled.messages 截到 budget_tokens 内（本地窗口小，防装爆）。

    结构保全策略（persona 同一套 turn 语义）：
    - 开头连续 system（人设全文 + begin_dialogs + 记忆/情绪注入块）**不碰**；
    - 最后一条消息（正在回复的当前 user 消息）**不碰**；
    - 中间历史按 turn 从最旧往最新丢，复用 persona.truncate_history 的 O(n)
      实现（truncate_ratio=1.0 即预算不打折）；
    - 总量本就在预算内 → 原列表原样返回（零拷贝零行为差异）。

    返回新列表（消息 dict 本体不复制——ChatClient.chat 内部 _flatten_messages
    会再做每条拷贝，此处只做列表级拼接）。
    """
    msgs = list(messages)
    if not msgs:
        return msgs
    per_msg = persona.estimate_token_list(msgs)
    if sum(per_msg) <= budget_tokens:
        return msgs

    # 前缀：开头连续 system 消息
    prefix_end = 0
    for msg in msgs:
        if str(msg.get("role", "")) == "system":
            prefix_end += 1
        else:
            break
    prefix = msgs[:prefix_end]
    rest = msgs[prefix_end:]
    if not rest:  # 只有 system 且超预算：只能原样发（人设不能砍），交给本地端点报错
        logger.warning(
            "[brain] fallback 上下文超预算（%d > %d）且除 system 外无历史可丢——原样发送",
            sum(per_msg), budget_tokens)
        return msgs
    suffix = rest[-1:]
    middle = rest[:-1]
    prefix_tok = sum(per_msg[:prefix_end])
    suffix_tok = per_msg[prefix_end + len(middle)]
    kept, dropped = persona.truncate_history(
        middle,
        fixed_prefix_tokens=prefix_tok,
        suffix_tokens=suffix_tok,
        max_context_tokens=budget_tokens,
        truncate_ratio=1.0,
    )
    if dropped:
        logger.warning(
            "[brain] fallback 上下文截断：丢最旧 %d 个 turn（预算 %d token，"
            "system 与当前消息不碰）", dropped, budget_tokens)
    return prefix + kept + suffix


# ---------------------------------------------------------------------------
# 共享降级状态（主/后台路由共用一份：一个大脑一个状态）
# ---------------------------------------------------------------------------


class DegradationState:
    """降级状态与计数器（线程安全；console /api/brain/status 消费 status()）。"""

    def __init__(self, *, wall_fn: "Callable[[], float]" = time.time) -> None:
        self._lock = threading.Lock()
        self._wall = wall_fn
        self.mode: str = MODE_CLOUD
        self.consec_auth_fails: int = 0      # 连续鉴权/计费失败轮数（云端成功清零）
        self.fallback_count: int = 0         # 累计 per-request / 黏性本地兜底成功次数
        self.sticky_switches: int = 0        # 累计进入黏性次数
        self.cloud_recovered: bool = False   # 半开探针成功（只亮提示，不自动切回）
        self.local_ready: bool = False       # 最近一次本地探活/拉起结果
        self.last_error: "dict | None" = None   # {status, kind, message, at}
        self.last_local_error: "dict | None" = None
        self.last_probe_mono: float = 0.0    # 半开探针时刻（monotonic 基准）
        self.last_ensure_fail_mono: float = 0.0  # 本地 ensure 失败时刻（告警/重试节流）
        self.notice_sent: bool = False       # 首次降级提示只发一次

    # ---- 主链路转移 ----

    def record_primary_success(self) -> None:
        """云端成功：清鉴权连败（瞬态不需要清——per-request 语义天然不粘）。"""
        with self._lock:
            self.consec_auth_fails = 0

    def record_primary_error(self, exc: LLMError) -> str:
        """记录最近 LLM 错误（含 HTTP code）并返回错误类别。"""
        kind = ("auth" if exc.status in AUTH_FAIL_STATUSES
                else "transient" if exc.retriable else "other")
        with self._lock:
            self.last_error = {
                "status": exc.status,
                "kind": kind,
                "message": str(exc)[:300],
                "at": self._wall(),
            }
        return kind

    def bump_auth_fails(self) -> int:
        with self._lock:
            self.consec_auth_fails += 1
            return self.consec_auth_fails

    def enter_sticky(self) -> bool:
        """置黏性模式；返回是否为本次进入（首次=True，用于显著告警只打一遍）。"""
        with self._lock:
            first = self.mode != MODE_STICKY_LOCAL
            if first:
                self.mode = MODE_STICKY_LOCAL
                self.sticky_switches += 1
                self.cloud_recovered = False
        return first

    def exit_sticky(self) -> None:
        """回常态（手动 /brain cloud 或黏性期云端意外成功）。"""
        with self._lock:
            self.mode = MODE_CLOUD
            self.consec_auth_fails = 0
            self.cloud_recovered = False

    def mark_recovered(self) -> None:
        with self._lock:
            self.cloud_recovered = True

    # ---- 观测/计数 ----

    def bump_fallback_count(self) -> None:
        with self._lock:
            self.fallback_count += 1

    def record_local_error(self, exc: LLMError) -> None:
        with self._lock:
            self.last_local_error = {
                "status": exc.status,
                "message": str(exc)[:300],
                "at": self._wall(),
            }

    def try_consume_notice(self) -> bool:
        """首次降级提示只发一次：未发过返回 True 并置位。"""
        with self._lock:
            if self.notice_sent:
                return False
            self.notice_sent = True
            return True

    def snapshot(self) -> dict:
        """状态快照（console 面板；last_error 含 HTTP code）。"""
        with self._lock:
            return {
                "mode": self.mode,
                "consec_auth_fails": self.consec_auth_fails,
                "fallback_count": self.fallback_count,
                "sticky_switches": self.sticky_switches,
                "cloud_recovered": self.cloud_recovered,
                "local_ready": self.local_ready,
                "last_error": dict(self.last_error) if self.last_error else None,
                "last_local_error": (dict(self.last_local_error)
                                     if self.last_local_error else None),
                "notice_sent": self.notice_sent,
            }


# ---------------------------------------------------------------------------
# 本地 llama-server 生命周期（adopt-first + 按需 spawn）
# ---------------------------------------------------------------------------


def _default_probe(host: str, port: int, timeout: float) -> "Callable[[], bool]":
    """就绪探测闭包：GET /health 必须 200 才算就绪（R7，FUSION_SPEC §3.3）。

    与 scripts/brain_switch.py _server_ready 同语义（llama-server 先开监听、
    模型加载完才对外服务，加载期 /health 回 503——「有响应」≠「能对话」）；
    连接拒绝/超时 = 未起。改 /health 的原因：R7 起本地大脑带 --api-key 武装
    （实测 llama.cpp b11040：/v1/models 未带 Bearer 恒 401，探测恒假阴性；
    /health 免鉴权——armed 后带任意/无 Bearer 都 200，加载期 503）。
    同步实现，chat 线程（asyncio.to_thread）内直跑。
    经 net.smart_urlopen：回环目标禁代理直连（本机系统代理劫持 127.0.0.1
    会把「活着」误判成「死了」，探活面与业务面必须同一直连口径）。
    """
    from virtuallover.net import smart_urlopen  # 局部 import：防装配环
    url = f"http://{host}:{port}/health"

    def _probe() -> bool:
        try:
            with smart_urlopen(url, timeout=timeout) as resp:
                return int(getattr(resp, "status", None) or resp.getcode()) == 200
        except Exception:  # noqa: BLE001 —— 503/连接拒绝/超时一律按未就绪计
            return False
    return _probe


class LocalBrainManager:
    """本地大脑进程管理：adopt-first 收编 + 预算内 spawn + 就绪探测。

    对齐 voice/lifecycle 的分寸：
    - adopt-first：端口已有 llama-server（/v1/models 200）即收编为 external，
      **绝不杀**——Router 全生命周期都不持有击杀路径（保命通道只能由
      scripts/brain_switch.py stop / 控制台 brain_stop 显式停）；
    - pid 文件与 brain_switch.py 同一路径（local/vl_data/llama_server_<port>.pid）：
      Router 拉起的进程同样能被控制台「引擎与人格」页 brain_stop 停掉，
      生态一致；
    - spawn 失败/预算耗尽只记 last_error 返回 False（黏性模式降级为只告警），
      **绝不抛异常打断 chat 主链**。
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        spawn_exe: str = "",
        spawn_args: "list[str] | None" = None,
        spawn_cwd: str = "",
        pid_path: "str | Path" = "",
        log_path: "str | Path" = "",
        alias: str = "",
        ready_budget: float = READY_BUDGET_SECONDS,
        probe_interval: float = READY_PROBE_INTERVAL_SECONDS,
        probe: "Callable[[], bool] | None" = None,
        spawner: "Callable[[list, str], int] | None" = None,
        allow_spawn: bool = True,
        clock: "Callable[[], float]" = time.monotonic,
        sleeper: "Callable[[float], None]" = time.sleep,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.spawn_exe = str(spawn_exe or "")
        self.spawn_args = list(spawn_args or [])
        self.spawn_cwd = str(spawn_cwd or "")
        # [chat].fallback_auto_spawn（R5 接线）：false=探测到本地无应答也只告警
        # 不拉起（保守用户不让框架私自启动推理进程）；true=按需 spawn。修前该键
        # 零消费——ensure_running 只看 spawn_exe，llm.py 装配日志打的「spawn=关」
        # 与实际行为不符（false 时配了 exe 照样拉起）。
        self.allow_spawn = bool(allow_spawn)
        self.pid_path = Path(pid_path) if pid_path else None
        self.log_path = Path(log_path) if log_path else None
        self.alias = str(alias or "")
        self.ready_budget = float(ready_budget)
        self.probe_interval = float(probe_interval)
        self._probe_fn = probe or _default_probe(host, port, 2.0)
        self._spawner = spawner
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()   # ensure_running 复入保护（并发 chat 双触发）
        # 观测状态
        self.state = "not_started"      # not_started/ready/starting/failed
        self.ownership: "str | None" = None  # owned/external/None（external 绝不杀）
        self.pid: "int | None" = None
        self.last_error: str = ""

    # ---- 工厂：从 [chat] fallback_* 配置构造 ----

    @classmethod
    def from_chat_cfg(
        cls, chat_cfg: Any, *, repo_root: "str | Path" = "."
    ) -> "LocalBrainManager":
        base = str(getattr(chat_cfg, "fallback_api_base", "") or
                   "http://127.0.0.1:8002/v1").strip()
        parts = urlsplit(base if "://" in base else "http://" + base)
        host = (parts.hostname or "127.0.0.1").lower()
        if host in ("localhost", "::1"):
            host = "127.0.0.1"   # 与 tts.base_url 归一化同口径
        port = parts.port or 8002
        exe = str(getattr(chat_cfg, "fallback_llama_exe", "") or "")
        gguf = str(getattr(chat_cfg, "fallback_gguf", "") or "")
        extra = str(getattr(chat_cfg, "fallback_llama_args", "") or "")
        # 参数按空格切分（路径含空格请改放无空格目录——docstring 已注明）
        spawn_args = []
        if gguf:
            spawn_args += ["-m", gguf]
        spawn_args += [a for a in extra.split() if a]
        # R7（FUSION_SPEC §3.4）spawn host clamp：非回环端点拒绝拉起——llama-server
        # 会被 --host 绑到该地址暴露给局域网，且把保命通道架在跨网段链路上。
        # 只探测（allow_spawn=False）+ 告警，端点本来活着时 adopt 照常可用。
        allow_spawn = bool(getattr(chat_cfg, "fallback_auto_spawn", True))
        if allow_spawn and host != "127.0.0.1":
            logger.error(
                "[brain] fallback_api_base 指向非回环地址（%s:%d）——拒绝 spawn"
                "（llama-server 只允许绑 127.0.0.1；远程大脑请自管进程），"
                "降级为只探测不拉起", host, port)
            allow_spawn = False
        spawn_args += ["--host", host, "--port", str(port)]
        # R7：--alias 固定中性进程标记（与 scripts/brain_switch.py LLAMA_MARKER
        # 同源 "local-brain"）——Router 拉起的进程同样能被 brain_switch stop /
        # brain_stop 的击杀核验认出；单模型 server 忽略请求体 model 字段（实测
        # b11040），fallback_model 只作客户端请求字段、不再作 alias。
        spawn_args += ["--alias", LOCAL_BRAIN_ALIAS]
        # R7：--api-key 与客户端同 key 单源（[chat].fallback_api_key）——Router
        # 自拉 server 与 brain_switch 拉的 server 武装口径一致；空 key=不武装
        # （两侧一致，老 server 照常工作）
        api_key = str(getattr(chat_cfg, "fallback_api_key", "") or "").strip()
        if api_key:
            spawn_args += ["--api-key", api_key]
        model = str(getattr(chat_cfg, "fallback_model", "") or "")
        root = Path(repo_root) if repo_root else Path(".")
        return cls(
            host=host,
            port=port,
            spawn_exe=exe,
            spawn_args=spawn_args,
            spawn_cwd=str(Path(exe).parent) if exe else "",
            pid_path=root / "local" / "vl_data" / f"llama_server_{port}.pid",
            log_path=root / "local" / "vl_data" / "logs" / f"llama_server_{port}.log",
            alias=LOCAL_BRAIN_ALIAS,
            allow_spawn=allow_spawn,
        )

    # ---- 对外接口 ----

    def probe_ready(self) -> bool:
        """本地就绪探测（毫秒级本地 HTTP；任何异常按未就绪计）。"""
        try:
            return bool(self._probe_fn())
        except Exception:  # noqa: BLE001 —— 注入的 probe 抛异常也不打断主链
            return False

    def ensure_running(self) -> bool:
        """确保本地大脑可用：有应答即收编（external）；无应答按配置 spawn 并等就绪。

        返回 True=可用；False=不可用（last_error 带原因）。绝不抛异常。
        """
        with self._lock:
            if self.probe_ready():
                first = self.state != "ready" or self.ownership != "external"
                self.state = "ready"
                self.ownership = "external"
                self.last_error = ""
                if first:
                    logger.warning(
                        "[brain] 检测到 llama-server 已在 %s:%d 运行，收编为外部实例"
                        "（adopt-first，绝不杀外部进程）", self.host, self.port)
                return True
            if not self.allow_spawn:
                # fallback_auto_spawn=false：收编照旧（外部实例不请自来也算可用），
                # 但绝不自行拉起——只探测只告警
                self.last_error = "fallback_auto_spawn=false（只探测不拉起）"
                self.state = "not_started"
                return False
            if not self.spawn_exe:
                self.last_error = "未配置 fallback_llama_exe（不 spawn，只告警）"
                self.state = "not_started"
                return False
            self.state = "starting"
            try:
                pid = self._spawn()
            except SpawnError as exc:
                self.last_error = str(exc)
                self.state = "failed"
                return False
            self.pid = pid
            self.ownership = "owned"
            self._write_pid_file(pid)
            logger.info("[brain] llama-server 拉起中（pid=%s，就绪预算 %.0fs）",
                        pid, self.ready_budget)
            deadline = self._clock() + self.ready_budget
            while self._clock() < deadline:
                if self.probe_ready():
                    self.state = "ready"
                    self.last_error = ""
                    logger.info("[brain] llama-server 就绪（pid=%s）", pid)
                    return True
                self._sleep(self.probe_interval)
            # 预算耗尽：进程可能仍在加载——**不杀**，留给下轮 adopt
            self.last_error = (f"llama-server {self.ready_budget:.0f}s 未就绪"
                               f"（日志 {self.log_path}）")
            self.state = "failed"
            return False

    def status(self) -> dict:
        return {
            "state": self.state,
            "ownership": self.ownership,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "last_error": self.last_error,
        }

    # ---- 内部 ----

    def _spawn(self) -> int:
        # exe 存在性检查只归默认 Popen 路径——注入的假 spawner（测试）场景下
        # 无真实文件也必须能断言 argv/cwd 契约（voice/lifecycle._resolve_command 同款裁定）
        if self._spawner is None and not Path(self.spawn_exe).is_file():
            raise SpawnError(f"llama-server 可执行文件不存在：{self.spawn_exe}")
        if self._spawner is not None:
            return int(self._spawner(list(self.spawn_args), self.spawn_cwd))
        return self._default_popen()

    def _default_popen(self) -> int:
        """默认 spawner：Popen + CREATE_NO_WINDOW + stdout/stderr→llama_server 日志。"""
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.log_path, "ab")  # noqa: SIM115 —— 进程生命周期与句柄同寿
        else:
            fh = subprocess.DEVNULL
        kwargs: "dict[str, Any]" = dict(
            cwd=self.spawn_cwd or None,
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        if _IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(  # noqa: S603 —— argv 为框架自构造固定值（配置白名单）
                [self.spawn_exe] + list(self.spawn_args), **kwargs)
        except OSError as exc:
            if fh not in (subprocess.DEVNULL, None):
                fh.close()
            raise SpawnError(f"llama-server 拉起失败：{exc}") from exc
        return proc.pid

    def _write_pid_file(self, pid: int) -> None:
        if self.pid_path is None:
            return
        try:
            self.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(int(pid)), encoding="ascii")
        except OSError as exc:
            logger.error("[brain] pid 文件写入失败（%s）：%s", self.pid_path, exc)


# ---------------------------------------------------------------------------
# BrainRouter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalProviderCfg:
    """本地大脑端点配置（从 [chat] fallback_* 摘取，喂给 ChatClient 的 duck cfg）。"""

    api_base: str
    api_key: str
    model: str
    timeout: float
    max_retries: int = 1            # 本地也只重试 1 次（连黏性都别让用户久等）
    retry_on_429: bool = False      # 本地无限流语义，429 直接判失败


class BrainRouter:
    """LLM 主备智能降级路由：包同 ChatClient 接口（chat / chat_async）。

    pipeline / proactive / diary / 记忆总结零改动换用：装配层把
    ctx.chat_client / ctx.chat_client_bg 换成本类实例即可（消费方只见
    ``await client.chat_async(messages, extra_body=...) -> LLMReply``）。

    主/后台路由共享同一 DegradationState 与 LocalBrainManager（一个大脑一个
    状态），所以 diary/记忆总结走的后台路由也受同一套黏性保命约束。
    """

    def __init__(
        self,
        primary: Any,
        *,
        local_client: Any = None,
        manager: "LocalBrainManager | None" = None,
        state: "DegradationState | None" = None,
        max_context_tokens: int = 4096,
        notice_enabled: bool = True,
        notice_text: str = "",
        notice_sink: "Callable[[str], None] | None" = None,
        provider_id: str = "chat",
        clock: "Callable[[], float]" = time.monotonic,
        sleeper: "Callable[[float], None]" = time.sleep,
    ) -> None:
        self._primary = primary
        self._local = local_client
        self._manager = manager
        self._state = state or DegradationState()
        self._budget = int(max_context_tokens)
        self._notice_enabled = bool(notice_enabled)
        self._notice_text = str(notice_text or "")
        self._notice_sink = notice_sink
        self.provider_id = f"{provider_id}+fallback" if local_client is not None else provider_id
        self._clock = clock
        self._sleep = sleeper
        # fallback 场景：主客户端 max_retries 降为 1（不再 4 连击晾用户——
        # 降级链的兜底职责归 Router；实例属性改写，不回写用户配置对象）
        try:
            primary.max_retries = 1
        except (AttributeError, TypeError):
            logger.warning("[brain] 主客户端 %s 不支持 max_retries 改写（非 ChatClient？）",
                           getattr(primary, "provider_id", "?"))

    # ------------------------------------------------------------------
    # 消费方接口（包同 ChatClient）
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: "list[dict]",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> Any:
        """同步对话：cloud 常态直发云端；黏性直发本地；两端语义见模块 docstring。"""
        if self._state.mode == MODE_STICKY_LOCAL:
            self._maybe_halfopen_probe()
            if self._state.mode == MODE_STICKY_LOCAL:
                if self._local_ready_or_warn():
                    return self._chat_local(messages, temperature=temperature,
                                            max_tokens=max_tokens,
                                            extra_body=extra_body, primary_error=None)
                # 本地不可用：黏性意图保留，回试云端（成功即自动解除黏性）
                return self._chat_primary_from_sticky(messages, temperature=temperature,
                                                      max_tokens=max_tokens,
                                                      extra_body=extra_body)
        try:
            reply = self._primary.chat(messages, temperature=temperature,
                                       max_tokens=max_tokens, extra_body=extra_body)
        except LLMError as exc:
            self._on_primary_error(exc)
            # 本次已触发黏性（第 2 轮 403）：本轮就直接本地保命，不让用户再等一轮
            if self._state.mode == MODE_STICKY_LOCAL:
                if self._local_ready_or_warn():
                    return self._chat_local(messages, temperature=temperature,
                                            max_tokens=max_tokens, extra_body=extra_body,
                                            primary_error=exc)
                raise  # 本地不可用：照旧抛原错误（轮静默），只告警
            # 瞬态故障（5xx/超时/网络）→ per-request fallback（不粘，不 spawn）
            if exc.retriable and self._local is not None:
                logger.warning(
                    "[brain] 云端瞬态失败（%s），本轮降级本地大脑（不黏性，下轮仍先试云端）",
                    exc)
                return self._chat_local(messages, temperature=temperature,
                                        max_tokens=max_tokens, extra_body=extra_body,
                                        primary_error=exc)
            raise
        self._state.record_primary_success()
        return reply

    async def chat_async(self, messages: "list[dict]", **kwargs: Any) -> Any:
        """chat 的 asyncio 包装（与 ChatClient.chat_async 同签名同语义）。"""
        import asyncio
        return await asyncio.to_thread(self.chat, messages, **kwargs)

    def recent_failure(self, window_seconds: float = 600.0) -> str:
        """代理观测面（console brain 行黄灯消费，ChatClient.recent_failure 同语义）。

        Router 包住主客户端后 brain_service_row 的 callable 探测读到的是本方法；
        黏性模式下加「已切本地保命」前缀——黄灯语义从「她哑了」变为
        「云端故障但她正用本地大脑说话，可切回」。
        """
        err = self._state.snapshot()["last_error"]
        if not err:
            return ""
        if time.time() - float(err.get("at") or 0) > window_seconds:
            return ""
        message = str(err.get("message") or "")
        if self._state.mode == MODE_STICKY_LOCAL:
            return f"云端故障已切本地大脑保命：{message}"
        return message

    # ------------------------------------------------------------------
    # 主链路异常裁决
    # ------------------------------------------------------------------

    def _on_primary_error(self, exc: LLMError) -> None:
        """记录错误 → 鉴权/计费连败计数 → 凑满阈值进黏性（含 spawn + 告警 + 提示）。"""
        self._state.record_primary_error(exc)
        if exc.status not in AUTH_FAIL_STATUSES:
            return
        fails = self._state.bump_auth_fails()
        if fails < STICKY_AUTH_THRESHOLD or self._state.mode == MODE_STICKY_LOCAL:
            return
        self._enter_sticky(exc)

    def _enter_sticky(self, exc: LLMError) -> None:
        """黏性保命启动：探针时钟重置 + spawn/收编本地 + 显著告警 + 首次提示。"""
        if not self._state.enter_sticky():
            return
        # 切换瞬间云端刚失败过：半开探针从现在起算 30min
        self._state.last_probe_mono = self._clock()
        logger.error(
            "★ [brain] 云端大脑故障（HTTP %s）连续 %d 轮——已切本地大脑保命；"
            "恢复后可经 /brain cloud 或控制台「切回云端大脑」恢复（不自动切回）",
            exc.status, STICKY_AUTH_THRESHOLD)
        ok = False
        if self._local is not None:
            if self._manager is not None:
                ok = self._manager.ensure_running()
            else:
                ok = True  # 测试/独立客户端注入形态：客户端在即可用
        self._state.local_ready = ok
        if not ok:
            self._state.last_ensure_fail_mono = self._clock()
            logger.error(
                "★ [brain] 本地大脑不可用（%s）——黏性期间无备可降：继续重试云端，"
                "仅告警不阻塞（fallback 不可用时只告警不 spawn 的安全语义）",
                self._manager.last_error if self._manager is not None else "本地客户端未装配")
            return
        if self._notice_enabled and self._notice_text and self._notice_sink is not None \
                and self._state.try_consume_notice():
            try:
                self._notice_sink(self._notice_text)
                logger.info("[brain] 首次降级提示已投递（%d 字）", len(self._notice_text))
            except Exception:  # noqa: BLE001 —— 提示失败绝不影响降级主流程
                logger.exception("[brain] 降级提示投递失败（不影响降级）")

    def _chat_primary_from_sticky(self, messages, *, temperature, max_tokens, extra_body) -> Any:
        """黏性 + 本地不可用时的回试云端：成功即解除黏性（保命前提已不成立）。"""
        try:
            reply = self._primary.chat(messages, temperature=temperature,
                                       max_tokens=max_tokens, extra_body=extra_body)
        except LLMError as exc:
            self._state.record_primary_error(exc)
            raise
        logger.warning("[brain] 黏性期间云端意外成功（本地不可用）——自动解除黏性回常态")
        self._state.exit_sticky()
        return reply

    # ------------------------------------------------------------------
    # 本地路径
    # ------------------------------------------------------------------

    def _local_ready_or_warn(self) -> bool:
        """黏性路径的本地可用判定：毫秒级探活 + 失败节流重拉 + 节流告警。"""
        if self._local is None:
            self._warn_throttled(
                "本地大脑客户端未装配（[chat] fallback_* 未启用）——只告警不降级")
            return False
        if self._manager is None:
            return True  # 独立客户端注入形态（测试）：客户端在即可用
        if self._manager.probe_ready():
            self._state.local_ready = True
            return True
        # 本地掉了：60s 节流内不反复 spawn（防每轮 90s 预算阻塞）
        if self._clock() - self._state.last_ensure_fail_mono >= LOCAL_UNAVAILABLE_WARN_INTERVAL:
            ok = self._manager.ensure_running()
            self._state.last_ensure_fail_mono = 0.0 if ok else self._clock()
            if ok:
                return True
        self._warn_throttled(
            "本地大脑不可用（%s）——黏性期间回试云端，仅告警"
            % (self._manager.last_error or f"{self._manager.host}:{self._manager.port} 无应答"))
        return False

    def _chat_local(self, messages, *, temperature, max_tokens, extra_body,
                    primary_error: "LLMError | None") -> Any:
        """走本地客户端：4096 预算截断 + provider 专属键剥离 + 失败抛原错误。"""
        assert self._local is not None
        trimmed = truncate_for_fallback(list(messages), self._budget)
        body = dict(extra_body) if extra_body else {}
        # prompt_cache_key 是云端 provider 侧 KV 缓存键，本地 llama-server 无此语义
        body.pop("prompt_cache_key", None)
        try:
            reply = self._local.chat(
                trimmed, temperature=temperature, max_tokens=max_tokens,
                extra_body=body or None)
        except LLMError as exc:
            self._state.record_local_error(exc)
            logger.error("[brain] 本地大脑失败：%s", exc)
            if primary_error is not None:
                raise primary_error from exc  # 保住原始根因（403 语义不丢）
            raise
        self._state.bump_fallback_count()
        self._state.local_ready = True
        return reply

    # ------------------------------------------------------------------
    # 半开探针（恢复检测）
    # ------------------------------------------------------------------

    def _maybe_halfopen_probe(self) -> None:
        """黏性期间每 30min 一跳 1-token 探针：成功只亮提示，不自动切回。"""
        now = self._clock()
        if now - self._state.last_probe_mono < HALFOPEN_PROBE_INTERVAL_SECONDS:
            return
        self._state.last_probe_mono = now
        try:
            self._primary.chat(
                [{"role": "user", "content": "ping"}], max_tokens=PROBE_MAX_TOKENS)
        except LLMError as exc:
            logger.info("[brain] 半开探针失败（%s）——保持本地模式", exc)
            return
        self._state.mark_recovered()
        logger.warning(
            "★ [brain] 半开探针成功：云端大脑已恢复——按裁定不自动切回，保持本地模式；"
            "可经 /brain cloud 或控制台「切回云端大脑」恢复")

    # ------------------------------------------------------------------
    # 命令面（/brain）与观测
    # ------------------------------------------------------------------

    def force_local(self) -> "tuple[bool, str]":
        """/brain local：手动黏性切本地（成功才算切；失败给原因）。"""
        if self._local is None:
            return False, "本地大脑未启用（vl.toml [chat] fallback_enabled=false）"
        ok = True if self._manager is None else self._manager.ensure_running()
        if not ok:
            reason = self._manager.last_error if self._manager is not None else "本地客户端不可用"
            return False, f"本地大脑不可用：{reason}"
        self._state.enter_sticky()
        self._state.last_probe_mono = self._clock()
        self._state.local_ready = True
        logger.warning("[brain] 手动切换：大脑已切本地模式（/brain local）")
        return True, "已切到本地大脑（恢复云端用 /brain cloud）"

    def force_cloud(self) -> dict:
        """/brain cloud：回云端常态（清黏性与恢复标记；不动 llama-server 进程）。"""
        self._state.exit_sticky()
        logger.warning("[brain] 手动切换：大脑已回云端模式（/brain cloud；"
                       "本地 llama-server 进程保留，可在控制台用 brain_stop 停）")
        return self.status()

    def status(self) -> dict:
        """观测快照（console /api/brain/status 与 /brain status 消费）。"""
        snap = self._state.snapshot()
        snap.update({
            "enabled": True,
            "provider_id": self.provider_id,
            "max_context_tokens": self._budget,
            "probe_interval_s": HALFOPEN_PROBE_INTERVAL_SECONDS,
            "local": self._manager.status() if self._manager is not None else None,
        })
        return snap

    # ---- 内部 ----

    def _warn_throttled(self, message: str) -> None:
        """60s 节流告警（黏性 + 本地不可用的每轮路径防刷屏）。"""
        if self._clock() - self._state.last_ensure_fail_mono >= LOCAL_UNAVAILABLE_WARN_INTERVAL:
            self._state.last_ensure_fail_mono = self._clock()
            logger.warning("★ [brain] %s", message)


# ---------------------------------------------------------------------------
# 装配工厂
# ---------------------------------------------------------------------------


def build_brain_router(
    primary: Any,
    chat_cfg: Any,
    *,
    provider_id: str = "chat",
    repo_root: "str | Path" = ".",
    notice_sink: "Callable[[str], None] | None" = None,
    shared: "BrainRouter | None" = None,
) -> "BrainRouter | None":
    """按 [chat] fallback_* 构造 BrainRouter；fallback_enabled=false 返回 None。

    - ``shared``：主路由先建，后台路由（diary/记忆总结）传入主路由复用同一
      DegradationState 与 LocalBrainManager（一个大脑一个状态）；
    - 默认关（云端用户不装本地也安全）：本工厂返回 None，装配零行为变化；
    - 本地 ChatClient 独立实例：独立超时（fallback_timeout）与重试（1 次），
      云端实例的 ark thinking extra_body 绝不带给本地（ChatClient 实例级
      extra_body 隔离，PERF_SPEC §2 O1 同款手法）。
    """
    if not bool(getattr(chat_cfg, "fallback_enabled", False)):
        return None
    if shared is not None:
        return BrainRouter(
            primary,
            local_client=shared._local,
            manager=shared._manager,
            state=shared._state,
            max_context_tokens=shared._budget,
            notice_enabled=False,      # 提示只由主路由发一次
            provider_id=provider_id,
        )
    local_cfg = LocalProviderCfg(
        api_base=str(getattr(chat_cfg, "fallback_api_base", "") or
                     "http://127.0.0.1:8002/v1"),
        # R7：客户端与 server 同 key 单源（from_chat_cfg 的 --api-key 用同一值；
        # 空=server 不武装，占位 "local" 会被无鉴权 server 忽略）
        api_key=str(getattr(chat_cfg, "fallback_api_key", "") or "local"),
        model=str(getattr(chat_cfg, "fallback_model", "") or "qwen2.5-7b-instruct"),
        timeout=float(getattr(chat_cfg, "fallback_timeout", 120) or 120),
    )
    local_client = ChatClient(local_cfg, provider_id="chat_local")
    manager = LocalBrainManager.from_chat_cfg(chat_cfg, repo_root=repo_root)
    return BrainRouter(
        primary,
        local_client=local_client,
        manager=manager,
        max_context_tokens=int(getattr(chat_cfg, "fallback_max_context_tokens", 4096) or 4096),
        notice_enabled=bool(getattr(chat_cfg, "fallback_notice_enabled", True)),
        notice_text=str(getattr(chat_cfg, "fallback_notice_text", "") or ""),
        notice_sink=notice_sink,
        provider_id=provider_id,
    )
