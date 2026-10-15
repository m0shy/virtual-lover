# -*- coding: utf-8 -*-
"""GSVI 进程生命周期（VOICE_SPEC §1）：adopt-first 状态机 + 自愈 + 熔断。

状态机（``state`` 字符串即控制台状态卡与 §2 引擎状态门的取值）::

    not_started ──auto_start/探测──▶ starting ──预算内就绪──▶ ready
         ▲                            │                      │ 30s 周期探测
         │                            │ 预算 300s 耗尽        ▼
         │                       (计死亡事件)             suspected_down
         │                                               │ 连续 2 次判死
    degraded ◀──10min 内第 3 次死亡── restarting ◀────────┘
         │            （重新拉起 → starting → ready / 再死亡）
         └─冷却 ≥5min 后半开：重试一次（maybe_recover）

所有权两态：``owned``（本框架 Popen 拉起，可杀可停）/ ``external``（adopt-first
收编的外部实例——**绝不杀**，控制台停用动作按 ownership 门禁拒绝）。

关键裁定逐条（VOICE_SPEC §1）：
- adopt-first：启动先探测端口，有应答即收编 external，绝不杀外部实例；
- auto_start 拉起 Popen argv 固定 ``[<引擎根>/runtime/python.exe,
  <仓库根>/scripts/voice_engine_entry.py]``（R8 起走 vl 自有 wrapper——上游
  ``__main__`` 硬编码绑 0.0.0.0，wrapper 只绑 127.0.0.1 回环且命令行不再含上游
  脚本名）、cwd=引擎根——错 cwd 会静默降级 classic 模式（tts_backend.py:6-8 以
  ``os.getcwd()`` 拼 sys.path，``GPT_SoVITS/TTS_infer_pack`` 只在仓库根下）；
  Windows 加 CREATE_NO_WINDOW 不弹窗；
- stdout/stderr → local/vl_data/logs/voice-engine.log（5MB×3 轮转——Popen 重定向需要真实
  文件句柄，不能走 logging，故手工按大小换名轮转）；
- 就绪预算 300s（冷启 1-2 分钟），预算内 1s TCP 探测；character_list 每调写盘
  character_info.json（load_infer_info.py:304），**禁作轮询探测**（§0 终裁）；
- 崩溃检测 30s 周期、连续 2 次判死；10 分钟内 3 次死亡熔断 degraded；
  冷却 ≥5 分钟后半开重试一次；
- PID 落 local/vl_data/voice/voice-engine.pid；杀前命令行核验（命令行不含
  voice_engine_entry.py 一律不杀，防误杀）；外部实例无 pid 记录（adopt 时清除
  遗留 pid 文件）；
- cleanup 终止自有进程；HANG 型故障（TCP 通但合成挂起）由 manager 侧
  ``report_synth_failure`` 喂入同一死亡处理链（死亡处理的重拉起在后台任务进行，
  绝不阻塞合成调用方——§2「该条降级纯文字，后台继续等」）。

全部外部交互（探测/拉起/击杀/命令行读取/时钟/睡眠）可注入——宿主机 pytest
用假 spawner + 假钟直测（VOICE_SPEC §4）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .gsvi import GSVI_AUTH_USER

__all__ = [
    "GsviLifecycle", "LifecycleParams", "SpawnError",
    "STATE_NOT_STARTED", "STATE_STARTING", "STATE_READY",
    "STATE_SUSPECTED_DOWN", "STATE_RESTARTING", "STATE_DEGRADED",
    "OWNERSHIP_OWNED", "OWNERSHIP_EXTERNAL",
    "READY_BUDGET_SECONDS", "STEADY_PROBE_INTERVAL", "DEATH_CONFIRM_COUNT",
    "BREAKER_WINDOW_SECONDS", "BREAKER_THRESHOLD", "COOLDOWN_SECONDS",
]

logger = logging.getLogger("virtuallover.voice.lifecycle")

# ---------------------------------------------------------------------------
# 状态与常量（VOICE_SPEC §1 实测参数）
# ---------------------------------------------------------------------------

STATE_NOT_STARTED = "not_started"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_SUSPECTED_DOWN = "suspected_down"
STATE_RESTARTING = "restarting"
STATE_DEGRADED = "degraded"

OWNERSHIP_OWNED = "owned"
OWNERSHIP_EXTERNAL = "external"

READY_BUDGET_SECONDS = 300.0    # 就绪预算：冷启 1-2 分钟 + 余量（热启 15s）
STARTUP_PROBE_INTERVAL = 1.0    # 预算内探测周期（TCP connect，§0 终裁）
STEADY_PROBE_INTERVAL = 30.0    # 崩溃检测周期
DEATH_CONFIRM_COUNT = 2         # 连续 2 次探测失败判死
BREAKER_WINDOW_SECONDS = 600.0  # 熔断窗口：10 分钟
BREAKER_THRESHOLD = 3           # 窗口内 3 次死亡 → degraded
COOLDOWN_SECONDS = 300.0        # degraded 冷却 ≥5 分钟，之后半开重试一次

LOG_MAX_BYTES = 5 * 1024 * 1024  # voice-engine.log 轮转阈值（对齐 SPEC §13 ≤5MB×3）
LOG_BACKUP_COUNT = 3

# Windows 下不弹控制台窗口（GSVI 前台进程由框架持有）
CREATE_NO_WINDOW = 0x08000000

# 命令行核验标记：命令行不含该串一律不杀（防误杀，§1）。
# R8 起拉起走 scripts/voice_engine_entry.py wrapper，标记取其尾部子串——
# 与 _resolve_command 的 argv[1] 必须同源（漂移=核验恒拒杀，引擎死不掉了），
# 测试 test_voice_lifecycle 锁这对契约。
_CMDLINE_MARKER = "entry.py"

_IS_WINDOWS = os.name == "nt"


class SpawnError(RuntimeError):
    """GSVI 拉起失败（runtime/python.exe 缺失 / Popen 异常）。计入熔断窗口。"""


@dataclass
class LifecycleParams:
    """生命周期参数（默认值=VOICE_SPEC §1 实测裁定；测试可整体替换）。"""

    auto_start: bool = True                  # 无外部实例时是否自行拉起
    gsvi_root: str = "voice-engine"  # 语音引擎根（argv[0]/cwd 基准；R6 改名，原 GPT-SoVITS-Inference/）
    host: str = "127.0.0.1"                  # 探测目标（来自 base_url 归一化）
    port: int = 5000
    ready_budget: float = READY_BUDGET_SECONDS
    startup_probe_interval: float = STARTUP_PROBE_INTERVAL
    probe_interval: float = STEADY_PROBE_INTERVAL
    death_confirm_count: int = DEATH_CONFIRM_COUNT
    breaker_window: float = BREAKER_WINDOW_SECONDS
    breaker_threshold: int = BREAKER_THRESHOLD
    cooldown: float = COOLDOWN_SECONDS
    pid_path: str = "local/vl_data/voice/voice-engine.pid"   # R6 中性化（原 gsvi.pid）
    log_path: str = "local/vl_data/logs/voice-engine.log"    # R6 中性化（原 gsvi.log）
    # FUSION_SPEC §3.1：引擎 HTTP Basic 口令（单源 vl.toml [tts].tts_auth_password；
    # 空=不武装，维持旧行为）。engine.setup 从 cfg.tts 传入，_spawn 拉起前注入
    # 引擎 Inference/config.json
    auth_password: str = ""


# ---------------------------------------------------------------------------
# 默认外部交互实现（全部可被注入替换）
# ---------------------------------------------------------------------------


def _default_cmdline_reader(pid: int) -> "Optional[str]":
    """读取进程命令行（/proc 或 PowerShell CIM）；读不到返回 None。"""
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():  # Linux / WSL
        try:
            return proc_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            return None
    if not _IS_WINDOWS:
        return None
    try:  # Windows：wmic 在新系统已移除，走 PowerShell CIM
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
             ".CommandLine"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (out.stdout or "").strip()
    if not line or line.lower() == "null":
        return None
    return line


def _default_killer(pid: int) -> bool:
    """终止进程（Windows 下 os.kill+SIGTERM 即 TerminateProcess）。"""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError as exc:
        logger.warning("[lifecycle] 终止进程 %s 失败：%s", pid, exc)
        return False


def _default_probe(host: str, port: int, timeout: float) -> Callable[[], bool]:
    """TCP connect 探测闭包（同步；async 侧经 to_thread 调用）。"""
    import socket

    def _probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False
    return _probe


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class GsviLifecycle:
    """GSVI 进程状态机：adopt-first / 自愈 / 熔断 / PID 核验。"""

    def __init__(
        self,
        params: LifecycleParams,
        *,
        repo_root: "str | Path" = ".",
        probe: "Callable[[], bool] | None" = None,
        spawner: "Callable[[list, str], int] | None" = None,
        killer: "Callable[[int], bool] | None" = None,
        cmdline_reader: "Callable[[int], Optional[str]] | None" = None,
        clock: "Callable[[], float]" = time.monotonic,
        sleeper: "Callable[[float], Any] | None" = None,
    ) -> None:
        self.params = params
        self.repo_root = Path(repo_root)
        self.pid_path = Path(params.pid_path)
        if not self.pid_path.is_absolute():
            self.pid_path = self.repo_root / self.pid_path
        self.log_path = Path(params.log_path)
        if not self.log_path.is_absolute():
            self.log_path = self.repo_root / self.log_path

        self._probe_fn = probe or _default_probe(params.host, params.port, 1.0)
        self._spawner = spawner or self._default_popen
        self._killer = killer or _default_killer
        self._cmdline_reader = cmdline_reader or _default_cmdline_reader
        self._clock = clock
        self._sleep = sleeper or asyncio.sleep

        # ---- 状态 ----
        self.state: str = STATE_NOT_STARTED
        self.ownership: "Optional[str]" = None      # owned / external / None
        self.pid: "Optional[int]" = None            # 自有进程 PID
        self.started_at: float = 0.0                # 最近一次进入 ready 的时刻
        self.last_error: str = ""                   # 最近一次故障原因（状态卡用）
        self._consec_probe_fails = 0                # 30s 周期探测连败数
        self._consec_synth_fails = 0                # 合成失败连败数（HANG 型喂入）
        self._death_events: "list[float]" = []      # 熔断窗口内的死亡时刻
        # 距最近一次 ready 的连败数：熔断第二门——窗口按时间剪枝会让「背靠背
        # 快速死亡」（如 300s 预算连败）永远凑不满窗口计数而无限重启，故
        # 两者任一达阈值即熔断；ready（就绪/收编）与半开恢复时归零。
        self._deaths_since_ready = 0
        self._degraded_since: float = 0.0
        self._log_fh = None                         # Popen 重定向句柄（自有进程持有）
        self._spawn_busy = False                    # 重拉起任务复入保护
        self._respawn_task: "Optional[asyncio.Task]" = None
        self._firewall_warned = False

    # ------------------------------------------------------------------
    # 观测面（控制台状态卡 / §2 状态门）
    # ------------------------------------------------------------------

    def state_name(self) -> str:
        """当前状态名（§2 引擎状态门按 ==\"ready\" 放行）。"""
        return self.state

    @property
    def is_ready(self) -> bool:
        return self.state == STATE_READY

    @property
    def owned(self) -> bool:
        return self.ownership == OWNERSHIP_OWNED

    def status(self) -> dict:
        """状态卡数据（控制台 /api/voice/status 消费，键扁平）。

        uptime_s 在本类内部用同一只 ``_clock``（默认 time.monotonic）求差——
        started_at 是 monotonic 值，**禁止**拿到控制台侧再用 time.time() 相减
        （时钟基准不同源，差值会变成"系统开机时长"）。
        """
        uptime = None
        if self.state == STATE_READY and self.started_at > 0:
            uptime = max(0.0, self._clock() - self.started_at)
        return {
            "lifecycle": self.state,
            "ownership": self.ownership,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_s": uptime,
            "auto_start": self.params.auto_start,
            "log_path": str(self.log_path),
            "last_error": self.last_error,
            "death_events": len(self._death_events),
        }

    # ------------------------------------------------------------------
    # 启动：adopt-first / 拉起
    # ------------------------------------------------------------------

    async def adopt_or_start(self) -> str:
        """启动路径：端口有应答即收编（external，绝不杀）；否则按 auto_start 拉起。

        防重入守卫：already ready/starting/restarting/suspected_down 时**原样返回**，
        绝不重复探测收编——控制台「启动」在框架已 Popen 拉起（owned/ready）后再点，
        会被下面的 _adopt() 把 ownership 从 owned 篡改成 external 并清掉 pid 记录，
        此后自己拉起的进程反而停不掉（控制台「owned 才可停」门禁被自己的误收编锁死）。
        degraded / not_started 不在守卫内：前者允许「冷却期外部实例回来了→收编复活」，
        后者是正常启动路径。
        """
        if self.state in (STATE_READY, STATE_STARTING, STATE_RESTARTING,
                          STATE_SUSPECTED_DOWN):
            return self.state
        if await asyncio.to_thread(self._probe_fn):
            self._adopt()
            return self.state
        self._deaths_since_ready = 0  # 全新启动尝试
        if not self.params.auto_start:
            logger.info("[lifecycle] 引擎未在运行且 auto_start=false，保持 not_started"
                        "（语音降级纯文字，等待外部启动）")
            return self.state
        return await self._spawn_and_wait()

    def _adopt(self) -> None:
        """收编外部实例：ready + external；清遗留 pid 文件（绝不杀外部进程）。"""
        self.state = STATE_READY
        self.ownership = OWNERSHIP_EXTERNAL
        self.started_at = self._clock()
        self._consec_probe_fails = 0
        self._consec_synth_fails = 0
        self._deaths_since_ready = 0
        self.last_error = ""
        try:
            if self.pid_path.exists():
                self.pid_path.unlink(missing_ok=True)
                logger.warning("[lifecycle] 已清除遗留 pid 文件 %s（外部实例不纳入击杀范围）",
                               self.pid_path)
        except OSError:
            pass
        logger.warning("[lifecycle] 检测到端口 %s:%d 已有 GSVI 在跑，收编为外部实例"
                       "（adopt-first，绝不杀外部进程；控制台停用将不可用）",
                       self.params.host, self.params.port)

    async def _spawn_and_wait(self) -> str:
        """拉起自有进程并按 300s 预算探测就绪；预算耗尽按死亡处理。"""
        self.state = STATE_RESTARTING if self._death_events else STATE_STARTING
        try:
            self.pid = await asyncio.to_thread(self._spawn)
        except SpawnError as exc:
            self.last_error = str(exc)
            logger.error("[lifecycle] 拉起失败：%s", exc)
            await self._handle_death("拉起失败", respawn=False)
            return self.state
        self.ownership = OWNERSHIP_OWNED
        self._write_pid_file(self.pid)
        logger.info("[lifecycle] GSVI 进程已拉起（pid=%s，就绪预算 %.0fs，日志 %s）",
                    self.pid, self.params.ready_budget, self.log_path)
        deadline = self._clock() + self.params.ready_budget
        while self._clock() < deadline:
            if await asyncio.to_thread(self._probe_fn):
                self.state = STATE_READY
                self.ownership = OWNERSHIP_OWNED
                self.started_at = self._clock()
                self._consec_probe_fails = 0
                self._consec_synth_fails = 0
                self._deaths_since_ready = 0
                self.last_error = ""
                logger.info("[lifecycle] GSVI 就绪（pid=%s，冷启等待 %.0fs）",
                            self.pid, self._clock() - (deadline - self.params.ready_budget))
                return self.state
            await self._sleep(self.params.startup_probe_interval)
        # 预算耗尽：按死亡处理（进程可能仍在初始化——先判死再杀，熔断计数兜底）
        await self._handle_death("就绪预算 300s 耗尽", respawn=False)
        return self.state

    # ------------------------------------------------------------------
    # 稳态：崩溃检测（30s 周期）与 HANG 喂入
    # ------------------------------------------------------------------

    async def probe_tick(self) -> None:
        """30s 周期探测一跳：连续 death_confirm_count 次失败判死。"""
        if self.state not in (STATE_READY, STATE_SUSPECTED_DOWN):
            return  # starting/restarting 由预算循环负责；degraded 等冷却
        if await asyncio.to_thread(self._probe_fn):
            if self.state == STATE_SUSPECTED_DOWN:
                logger.info("[lifecycle] 探测恢复，解除 suspected_down")
            self.state = STATE_READY
            self._consec_probe_fails = 0
            return
        self._consec_probe_fails += 1
        if self.state == STATE_READY:
            self.state = STATE_SUSPECTED_DOWN
            logger.warning("[lifecycle] 探测失败 1 次，suspected_down（再 %d 次判死）",
                           self.params.death_confirm_count - self._consec_probe_fails)
        if self._consec_probe_fails >= self.params.death_confirm_count:
            await self._handle_death("连续 %d 次探测失败" % self._consec_probe_fails)

    async def report_synth_failure(self) -> None:
        """HANG 型故障喂入（TCP 通但合成失败——探测探不到）；连续 2 次判死。"""
        self._consec_synth_fails += 1
        if self._consec_synth_fails >= self.params.death_confirm_count:
            self._consec_synth_fails = 0
            await self._handle_death("连续 %d 次合成失败（疑似引擎挂起）"
                                     % self.params.death_confirm_count)

    def report_synth_success(self) -> None:
        """合成成功即清除 HANG 连败计数。"""
        self._consec_synth_fails = 0

    # ------------------------------------------------------------------
    # 死亡处理 / 熔断 / 半开恢复
    # ------------------------------------------------------------------

    async def _handle_death(self, reason: str, *, respawn: bool = True) -> None:
        """统一死亡处理：核验击杀自有 PID → 记事件 → 熔断判定 → 后台重拉起。

        respawn=False（预算耗尽/拉起失败的调用点已处于死亡链内）时不再追加
        后台任务；respawn=True（稳态判死）时重拉起放后台任务，绝不阻塞调用方。
        """
        self.state = STATE_SUSPECTED_DOWN
        self.last_error = reason
        logger.warning("[lifecycle] 引擎判死：%s（ownership=%s，pid=%s）",
                       reason, self.ownership, self.pid)
        await asyncio.to_thread(self.kill_own_sync)
        self._consec_probe_fails = 0
        self._consec_synth_fails = 0

        now = self._clock()
        self._death_events = [t for t in self._death_events
                              if now - t < self.params.breaker_window]
        self._death_events.append(now)
        self._deaths_since_ready += 1
        if (len(self._death_events) >= self.params.breaker_threshold
                or self._deaths_since_ready >= self.params.breaker_threshold):
            self.state = STATE_DEGRADED
            self._degraded_since = now
            logger.error("[lifecycle] %.0f 分钟内第 %d 次死亡，熔断 degraded"
                         "（冷却 %.0f 秒后半开重试）",
                         self.params.breaker_window / 60, len(self._death_events),
                         self.params.cooldown)
            return
        if not self.params.auto_start:
            self.state = STATE_NOT_STARTED
            logger.warning("[lifecycle] auto_start=false，死亡后不自愈，保持 not_started")
            return
        if respawn:
            self.state = STATE_RESTARTING
            if self._spawn_busy:  # 已有重拉起在途
                return
            self._spawn_busy = True
            self._respawn_task = asyncio.ensure_future(self._respawn_once())
        else:
            self.state = STATE_RESTARTING
            await self._spawn_and_wait()

    async def _respawn_once(self) -> None:
        """后台重拉起一次（结果成败均由 _spawn_and_wait 内部状态机收口）。"""
        try:
            await self._spawn_and_wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 后台任务绝不把异常漏到事件循环
            logger.exception("[lifecycle] 后台重拉起异常（等待下一轮探测）")
        finally:
            self._spawn_busy = False
            self._respawn_task = None

    async def maybe_recover(self) -> None:
        """degraded 冷却结束后的半开恢复：清空熔断窗口，重试一次拉起。"""
        if self.state != STATE_DEGRADED:
            return
        if self._clock() - self._degraded_since < self.params.cooldown:
            return
        logger.info("[lifecycle] degraded 冷却结束，半开重试一次")
        self._death_events = []        # 冷却已过：熔断窗口重置（再死 3 次才再熔断）
        self._deaths_since_ready = 0   # 半开重试的失败额度同样重置
        if not self.params.auto_start:
            self.state = STATE_NOT_STARTED
            return
        self.state = STATE_RESTARTING
        await self._spawn_and_wait()

    # ------------------------------------------------------------------
    # PID / 击杀（防误杀核验链）
    # ------------------------------------------------------------------

    def kill_own_sync(self) -> bool:
        """终止**自有**进程（同步版）：pid 记录 + 命令行核验双门，缺一不杀。

        - ownership 非 owned / 无 pid 记录：直接返回（外部实例绝不碰）；
        - 命令行读不到（进程已死/权限不足）：不杀，仅清 pid 记录；
        - 命令行不含 voice_engine_entry.py（核验标记取尾部子串 entry.py）：
          **拒绝击杀**并保留 pid 文件（供人工排查），打 error 日志——宁可不杀
          不可误杀。
        """
        if not self.owned or self.pid is None:
            return False
        pid = self.pid
        cmdline = self._cmdline_reader(pid)
        if cmdline is None:
            logger.warning("[lifecycle] pid=%s 命令行不可读（进程可能已退出），跳过击杀", pid)
            self.pid = None
            self.ownership = None
            self._remove_pid_file()
            return False
        if _CMDLINE_MARKER not in cmdline.lower():
            logger.error("[lifecycle] 命令行核验失败，拒绝击杀 pid=%s（cmdline=%r 不含 %r）"
                         "——请人工确认后处理", pid, cmdline, _CMDLINE_MARKER)
            return False
        ok = bool(self._killer(pid))
        if ok:
            logger.info("[lifecycle] 已终止自有 GSVI 进程（pid=%s）", pid)
        self.pid = None
        self.ownership = None
        self._remove_pid_file()
        return ok

    def _remove_pid_file(self) -> None:
        try:
            self.pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_pid_file(self, pid: int) -> None:
        try:
            self.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(int(pid)), encoding="ascii")
        except OSError as exc:
            logger.error("[lifecycle] pid 文件写入失败（%s）：%s", self.pid_path, exc)

    # ------------------------------------------------------------------
    # 默认拉起（Popen argv/cwd/日志重定向/pid 落盘）
    # ------------------------------------------------------------------

    def _open_log_with_rotation(self):
        """打开 voice-engine.log 追加句柄；超 5MB 先按 .1/.2/.3 换名轮转（保留 3 代）。"""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX_BYTES:
                for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
                    src = Path(str(self.log_path) + f".{i}")
                    dst = Path(str(self.log_path) + f".{i + 1}")
                    if src.exists():
                        os.replace(src, dst)
                os.replace(self.log_path, Path(str(self.log_path) + ".1"))
        except OSError as exc:
            logger.warning("[lifecycle] 日志轮转失败（继续追加）：%s", exc)
        return open(self.log_path, "ab")

    def _inject_server_config(self) -> None:
        """拉起前把监听 host 与认证凭据注入引擎 Inference/config.json（FUSION_SPEC §3.1）。

        - tts_host：强制 params.host（vl.toml [tts].base_url 归一化产物，恒为
          127.0.0.1）——上游 config.json 静态值可能被手滑改回 0.0.0.0，每次拉起
          兜底改写，引擎侧永不监听非回环；
        - 认证口令单源 vl.toml [tts].tts_auth_password（params.auth_password）：
          非空则写 enable_auth="true" 与 user={GSVI_AUTH_USER: 口令}。上游按
          字符串 ``.lower()`` 判定 enable_auth——必须是字符串 "true"，落 JSON
          bool 会在 import 时 AttributeError；user 整体覆盖，顺带清掉上游示例
          弱口令 admin123。空口令=不武装（保持现状，防火墙告警照旧）。

        上游在 import tts_backend 时读该文件，故注入必须先于 Popen。改写保全
        其余键；读失败/文件缺失只告警不阻断拉起（引擎侧此时走上游默认，wrapper
        仍只绑 127.0.0.1）。引擎正在运行时改写无副作用（文件只在进程启动时读）。
        """
        root = Path(self.params.gsvi_root)
        if not root.is_absolute():
            root = self.repo_root / root
        config_path = root / "Inference" / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("config.json 顶层不是对象")
        except (OSError, ValueError) as exc:
            logger.warning("[lifecycle] 引擎 config.json 注入跳过（%s）：%s",
                           config_path, exc)
            return
        changed: "list[str]" = []
        want_host = str(self.params.host or "127.0.0.1")
        if data.get("tts_host") != want_host:
            data["tts_host"] = want_host
            changed.append(f"tts_host={want_host}")
        password = str(self.params.auth_password or "")
        if password:
            # 上游 enable_auth 判定是 _config.get("enable_auth", "false").lower()
            # ——必须是字符串 "true"，JSON bool 会让引擎进程 import 即崩
            if data.get("enable_auth") != "true":
                data["enable_auth"] = "true"
                changed.append('enable_auth="true"')
            if data.get("user") != {GSVI_AUTH_USER: password}:
                data["user"] = {GSVI_AUTH_USER: password}
                changed.append(f"user={{{GSVI_AUTH_USER}:***（长度 {len(password)}）}}")
        if not changed:
            return
        try:
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=4)
                fh.write("\n")
        except OSError as exc:
            logger.warning("[lifecycle] 引擎 config.json 写回失败（%s）：%s",
                           config_path, exc)
            return
        logger.info("[lifecycle] 引擎 config.json 已注入（%s）", "、".join(changed))

    def _spawn(self) -> int:
        """拉起入口：解析固定 argv/cwd → 注入引擎 config.json → 交 spawner。

        argv/cwd 契约在解析后传给 spawner——注入的假 spawner 也能断言
        「argv=[<引擎根>/runtime/python.exe, <仓库根>/scripts/voice_engine_entry.py]、
        cwd=引擎根」（VOICE_SPEC §1 / §4，R8 起 argv[1] 换 vl 自有 wrapper）。
        注入先于 Popen：上游 import tts_backend 时即读 Inference/config.json，
        进程起来后再改写要等下次重启才生效。
        """
        python_exe, argv, cwd = self._resolve_command()
        self._inject_server_config()
        pid = int(self._spawner(argv, cwd))
        if not self._firewall_warned:
            self._firewall_warned = True
            if self.params.auth_password:
                logger.warning(
                    "[lifecycle] 语音引擎经 wrapper（scripts/voice_engine_entry.py）拉起，"
                    "只绑 %s:%d 回环且已开启 Basic 认证（口令单源 vl.toml [tts]"
                    ".tts_auth_password，不落日志）", self.params.host, self.params.port)
            else:
                logger.warning(
                    "[lifecycle] 语音引擎经 wrapper（scripts/voice_engine_entry.py）拉起，"
                    "只绑 %s:%d 回环，不暴露局域网；但未配置 [tts].tts_auth_password，"
                    "引擎无鉴权（本机其他本地账户理论上可调用），建议在 vl.toml 配置口令"
                    "（FUSION_SPEC §3.1）", self.params.host, self.params.port)
        logger.info("[lifecycle] 拉起 argv=%s cwd=%s", argv, cwd)
        return pid

    def _resolve_command(self) -> "tuple[str, list, str]":
        """解析拉起命令：python.exe 路径 + 固定 argv + cwd=GSVI 根。

        这里**不做**存在性检查——注入的假 spawner 场景下无真实运行时也必须能
        断言 argv/cwd 契约；存在性检查归默认 Popen 路径（缺失给中文 SpawnError）。
        """
        root = Path(self.params.gsvi_root)
        if not root.is_absolute():
            root = self.repo_root / root
        python_exe = root / "runtime" / "python.exe"
        if not python_exe.is_file():
            posix_python = root / "runtime" / "python"
            if posix_python.is_file():
                python_exe = posix_python
        argv = [str(python_exe),
                str(self.repo_root / "scripts" / "voice_engine_entry.py")]
        return str(python_exe), argv, str(root)

    def _default_popen(self, argv: list, cwd: str) -> int:
        """默认 spawner：Popen + CREATE_NO_WINDOW + stdout/stderr→voice-engine.log（轮转）。"""
        if not Path(argv[0]).is_file():
            raise SpawnError(
                f"GSVI 运行时缺失：{argv[0]} 不存在。请确认 vl.toml [tts].gsvi_root "
                "指向 voice-engine 根目录"
            )
        log_fh = self._open_log_with_rotation()
        kwargs: "dict[str, Any]" = dict(
            cwd=cwd, stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        if _IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NO_WINDOW  # 不弹窗（§1）
        try:
            proc = subprocess.Popen(argv, **kwargs)  # noqa: S603 —— argv 为框架自构造固定值
        except OSError as exc:
            log_fh.close()
            raise SpawnError(f"Popen 拉起失败：{exc}") from exc
        self._log_fh = log_fh
        logger.info("[lifecycle] Popen argv=%s cwd=%s", argv, cwd)
        return proc.pid

    # ------------------------------------------------------------------
    # 常驻循环（engine.start_background 启动；stop 事件优雅退出）
    # ------------------------------------------------------------------

    async def run_forever(self, stop: "asyncio.Event | None" = None) -> None:
        """常驻循环：启动（adopt-first）→ 30s 周期探测 + degraded 半开恢复。"""
        try:
            await self.adopt_or_start()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 启动失败不终止循环（状态留在当前态）
            logger.exception("[lifecycle] 启动路径异常（进入稳态探测循环）")
        while stop is None or not stop.is_set():
            try:
                await self._sleep(self.params.probe_interval)
            except asyncio.CancelledError:
                raise
            if stop is not None and stop.is_set():
                break
            try:
                await self.probe_tick()
                await self.maybe_recover()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 单轮探测失败不终止循环
                logger.exception("[lifecycle] 稳态探测轮异常")

    async def wait_background(self) -> None:
        """等待在途后台重拉起任务结束（测试收尾/确定性断言用；无任务即返回）。"""
        task = self._respawn_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    async def wait_ready(self, timeout: "Optional[float]" = None) -> bool:
        """预算内等待就绪（控制台试听/启停卡用）。degraded 冷却期直接 False（跳过重试）。"""
        if self.state == STATE_NOT_STARTED and self.params.auto_start:
            await self.adopt_or_start()
        deadline = self._clock() + float(timeout or self.params.ready_budget)
        while self._clock() < deadline:
            if self.state == STATE_READY:
                return True
            if self.state == STATE_DEGRADED:
                return False
            await self._sleep(min(1.0, max(0.0, deadline - self._clock())))
        return self.state == STATE_READY

    async def stop_engine(self) -> dict:
        """停止引擎（控制台启停卡；ownership 门禁在控制台侧，这里再核验一道）。"""
        if self._respawn_task is not None:
            self._respawn_task.cancel()
            self._respawn_task = None
        await asyncio.to_thread(self.kill_own_sync)
        if self.state in (STATE_READY, STATE_SUSPECTED_DOWN, STATE_STARTING,
                          STATE_RESTARTING):
            self.state = STATE_NOT_STARTED
        return {"state": self.state, "ownership": self.ownership}

    def cleanup(self) -> None:
        """同步收尾（__main__ 清理钩子）：终止自有进程 + 关日志句柄。外部实例不碰。"""
        if self._respawn_task is not None:
            self._respawn_task.cancel()
            self._respawn_task = None
        self.kill_own_sync()
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
