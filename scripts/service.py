# -*- coding: utf-8 -*-
"""Virtual-lover 整程序服务编排：一键启动 / 一键关闭 / 状态。

覆盖全部四个部件：
  1. napcat 容器（docker compose 默认组；legacy 组不动）
  2. 本地大脑 llama-server(8002)——仅当 vl.toml 指向本地时自动带起
  3. vl 主进程（python -m virtuallover；独立看护进程崩溃自动重启）
  4. GSVI 语音引擎——不预启动（vl auto_start 首次开口自动拉起）

用法：
  python scripts/service.py start    # 一键启动全家（本地大脑配置时自动带起 8002）
  python scripts/service.py stop     # 一键关闭全家（vl/llama-server/GSVI/napcat 全停）
  python scripts/service.py status   # 四部件状态
  python scripts/service.py _daemon  # 内部：双看护进程（start 自动拉起，勿手动跑）

看护体系（五轮修缮 R2，参数面对齐 voice/lifecycle）：
  - vl：10s 巡检，不在即拉；重启退避改「最近死亡时刻」窗口——连续存活 5min 重置；
  - llama-server(8002)：vl.toml 指本地时 30s 周期探测、连续 2 次失败判死重拉、
    10min 内 3 次死亡熔断（degraded）、5min 冷却后半开重试一次；
  - 探活双门：pid 文件 + CIM 命令行核验（bs.daemon_alive），vl 活看护死时
    vl_start 只补拉看护；杀 pid 一律先核验命令行（bs.kill_pid_verified）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import brain_switch as bs  # noqa: E402 —— 复用其 server 生命周期与端口探测

# 子进程解释器自解析（bat 统一 .venv 优先的进程内兜底）：仓库根有 .venv 就用它，
# 没有才退回当前解释器——即使调用方用 PATH python 跑本脚本，看护与 vl 子进程
# 仍会落在 .venv 里（依赖只在 .venv 装齐）。
VENV_PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
PYTHON = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)

MONITOR_PID = bs.SERVICE_MONITOR_PID   # 看护 pid 文件（唯一真身在 brain_switch）
VL_LOG = REPO / "local" / "vl_data" / "vl_runtime.log"

# ---------------------------------------------------------------------------
# 看护参数面（R2；对齐 voice/lifecycle 的实测裁定）
# ---------------------------------------------------------------------------
VL_TICK = 10.0                  # vl 巡检周期（沿用原 10s）
LLAMA_PROBE_INTERVAL = 30.0     # 8002 探测周期（lifecycle.STEADY_PROBE_INTERVAL）
LLAMA_DEATH_CONFIRM = 2         # 连续 2 次探测失败判死（lifecycle.DEATH_CONFIRM_COUNT）
LLAMA_BREAKER_WINDOW = 600.0    # 熔断窗口 10min（lifecycle.BREAKER_WINDOW_SECONDS）
LLAMA_BREAKER_THRESHOLD = 3     # 窗口内 3 次死亡 → degraded（lifecycle.BREAKER_THRESHOLD）
LLAMA_COOLDOWN = 300.0          # degraded 冷却 5min 后半开（lifecycle.COOLDOWN_SECONDS）
BACKOFF_WINDOW = 300.0          # 退避重置窗口：部件连续存活满 5min → 计数清零
BACKOFF_THRESHOLD = 20          # 短命连崩计数阈值（沿用原 20）
BACKOFF_COOL = 300.0            # 疯狂期节奏：距上次拉起不足 5min 不再重拉


def _log(msg: str) -> None:
    print(msg, flush=True)


def _http_alive(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _pids_by_cmd(pattern: str) -> "list[tuple[str, str]]":
    """返回 [(pid, commandline)]，命令行匹配 pattern 的 python 进程。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         f"Where-Object {{$_.CommandLine -match '{pattern}'}} | "
         "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
        capture_output=True)
    import json as _json
    raw = (out.stdout or b"").decode("utf-8", errors="replace") if isinstance(out.stdout, bytes) else (out.stdout or "")
    try:
        rows = _json.loads(raw or "[]")
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [(str(r.get("ProcessId")), r.get("CommandLine") or "") for r in rows]


def _kill(pid: str) -> None:
    """裸 taskkill——仅剩 gsvi_stop 在用：其 pid 来自 CIM 命令行匹配 'entry.py'
    （来源已核验）。pid 文件来源的击杀一律走 bs.kill_pid_verified 核验门（R2）。"""
    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)


# ---------------------------------------------------------------------------
# 部件：napcat 容器
# ---------------------------------------------------------------------------

def _compose_up() -> "tuple[bool, str]":
    """docker compose up -d 一次尝试，返回 (是否成功, 输出摘要)。"""
    r = subprocess.run(["docker", "compose", "up", "-d", "--remove-orphans"],
                       cwd=str(REPO), capture_output=True)
    return r.returncode == 0, (r.stderr or r.stdout)


def napcat_up(attempts: int = 3, interval: float = 30.0, *,
              runner=None, sleeper=None) -> None:
    """起 napcat 容器；Docker 未就绪按 3 次 × 间隔 30s 重试（R2，开机自启场景）。

    ONLOGON 自启时 Docker Desktop 常比用户登录晚就绪，首连必失败——修前一次
    失败即 SystemExit，全家起不来。runner/sleeper 可注入（pytest 假件直测）。
    """
    runner = runner or _compose_up
    sleeper = sleeper or time.sleep
    last = ""
    for i in range(1, attempts + 1):
        ok, out = runner()
        if ok:
            _log("napcat 容器已启动" + (f"（第 {i} 次尝试成功）" if i > 1 else ""))
            return
        last = (out or "").strip()[:200]
        if i < attempts:
            _log(f"docker compose up 失败（{i}/{attempts}）：{last}"
                 f"——{interval:.0f}s 后重试（Docker 可能未就绪）")
            sleeper(interval)
    raise SystemExit(f"docker compose up 失败（已重试 {attempts} 次）：{last}")


def napcat_down() -> None:
    subprocess.run(["docker", "compose", "stop"], cwd=str(REPO), capture_output=True)
    _log("napcat 容器已停止")


# ---------------------------------------------------------------------------
# 部件：vl 主进程 + 看护
# ---------------------------------------------------------------------------

def _spawn_daemon() -> None:
    """拉起独立看护进程（后台、无窗口、stdout→vl_runtime.log 轮转句柄）。"""
    subprocess.Popen(
        [str(PYTHON), str(Path(__file__).resolve()), "_daemon"],
        cwd=str(REPO), stdout=bs.open_runtime_log(VL_LOG), stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)


def _spawn_vl() -> None:
    """拉起 vl 主进程（后台、无窗口、stdout→vl_runtime.log 轮转句柄）。

    pid 同步落盘（bs.VL_PID）：无 .venv 部署形态命令行不含仓库路径，_vl_pids
    靠 pid 文件在 vl 尚未监听端口的启动早期就认领它——看护不误判双拉。"""
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "virtuallover"], cwd=str(REPO),
        stdout=bs.open_runtime_log(VL_LOG), stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)
    bs.write_vl_pid(proc.pid)


def vl_start() -> None:
    """起 vl + 独立看护；vl 已活但看护死 → 只补拉看护（R2 探活修复）。

    修前探活只看 vl 进程在不在、看护只看 pid 文件在不在——看护死后 vl_start
    误判「已在运行（复用）」直接返回，vl 从此裸奔无自愈；现在 daemon_alive
    双门（pid 文件 + CIM 命令行）判看护真身，死了就补拉。
    """
    if bs._vl_pids():
        if bs.daemon_alive():
            _log("vl 进程已在运行（复用，看护在岗）")
            return
        _log("vl 活但看护已死 → 补拉看护（恢复崩溃自愈）")
        _spawn_daemon()
        for _ in range(15):
            time.sleep(1)
            if bs.daemon_alive():
                _log("看护已补拉（pid 文件已核验）")
                return
        _log("警告：看护补拉 15s 未就绪，看日志：" + str(VL_LOG))
        return
    _spawn_daemon()
    for _ in range(30):
        time.sleep(1)
        if bs._vl_pids():
            _log("vl 进程已启动（含看护：崩溃自动重启）")
            return
    _log("警告：vl 30s 未起来，看日志：" + str(VL_LOG))


def vl_stop() -> None:
    """停 vl：先杀看护（防被杀后立刻拉起 vl）再杀 vl，最后清 vl.pid。

    R2：pid 文件来源的 pid（看护）一律先命令行核验再杀（kill_pid_verified，
    照抄 lifecycle.kill_own_sync）；refused/failed 保留 pid 文件供人工排查。
    _vl_pids() 返回的 pid 本身来自 CIM 命令行匹配，仍过一道核验门统一口径。
    """
    if MONITOR_PID.is_file():
        pid = MONITOR_PID.read_text(encoding="ascii").strip()
        if pid.isdigit():
            outcome = bs.kill_pid_verified(pid, bs.DAEMON_MARKER, "看护进程")
            if outcome in ("killed", "gone"):
                MONITOR_PID.unlink(missing_ok=True)
        else:
            MONITOR_PID.unlink(missing_ok=True)
    for pid in bs._vl_pids():
        bs.kill_pid_verified(pid, bs.VL_MARKER, "vl 进程")
    bs.VL_PID.unlink(missing_ok=True)   # 残留 pid 会污染下次 _vl_pids 兜底认领


class _RestartBackoff:
    """「最近死亡时刻」窗口退避（R2，vl 与 llama-server 重拉共用同一实例）。

    修前缺陷：``restarts`` 只增不减——vl 稳定跑几天后偶崩一次，重启前也要先吃
    5 分钟长睡，终身累计劣化到 5min 节奏。修后语义：
      - 计数只在「短命」时累计：任一部件连续存活满 window（5min）即清零；
      - 疯狂期（计数满 threshold=20）重拉须距上次拉起 ≥ cool（5min）。
    clock/sleeper 可注入（pytest 假钟直测，不真睡）。
    """

    def __init__(self, *, window: float = BACKOFF_WINDOW,
                 threshold: int = BACKOFF_THRESHOLD, cool: float = BACKOFF_COOL,
                 clock=time.monotonic, sleeper=time.sleep) -> None:
        self.window = float(window)
        self.threshold = int(threshold)
        self.cool = float(cool)
        self._clock = clock
        self._sleep = sleeper
        self.count = 0
        self.last_start = None    # 上次拉起时刻（monotonic；None=尚未拉过）

    def observe_alive(self, since: float) -> None:
        """部件自 since 起持续存活：满 window → 计数清零（连续存活 5min 重置）。"""
        if self.last_start is not None and self._clock() - since >= self.window:
            self.count = 0

    def note_restart(self) -> None:
        """记录一次重拉：距上次拉起已稳定满 window 则先重开计数。"""
        now = self._clock()
        if self.last_start is not None and now - self.last_start >= self.window:
            self.count = 0
        self.count += 1
        self.last_start = now

    def wait_if_hot(self) -> None:
        """疯狂期限流：计数满阈值且距上次拉起不足 cool → 睡到点再放行。

        阻塞看护循环——仅在短命连崩 20 次的退化场景触发，与修前 sleep(300)
        同位同量级；正常偶崩（存活满 5min）计数已被清零，永不触发。
        """
        if self.count < self.threshold or self.last_start is None:
            return
        remain = self.cool - (self._clock() - self.last_start)
        if remain > 0:
            self._sleep(remain)


class VlAbsentConfirm:
    """vl 巡检的连续判空确认（R4.5；对齐 大脑看护 的 death_confirm 语义）。

    修前单跳判空即拉——_vl_pids 的 CIM/端口/pid 文件链上任一查询偶发空
    （PowerShell 瞬时卡顿等），看护就把活着的 vl 误判死亡、双拉新 vl 抢
    6199/8787（r4_final 全新安装真机复现：第二个 vl 绑定失败退出）。现在
    连续 confirm 跳（默认 2，覆盖 20s）都判空才拉；任一跳判活即清零。
    """

    def __init__(self, confirm: int = 2) -> None:
        self.confirm = max(1, int(confirm))
        self.streak = 0

    def observe(self, alive: bool) -> bool:
        """喂一跳巡检结果，返回「是否应拉起」。判活即清零。"""
        if alive:
            self.streak = 0
            return False
        self.streak += 1
        if self.streak >= self.confirm:
            self.streak = 0
            return True
        return False


class 大脑看护:
    """llama-server(8002) 看护状态机（voice/lifecycle 参数面的脚本侧移植）。

    状态转移（``state`` 字符串即 vl_runtime.log 里的口径）::

        ok ──30s 探测失败──▶ suspected_down ──连续 2 次失败──▶ 判死：记熔断窗口 → 重拉
         ▲                                                        │ 10min 内第 3 次
         └────────────── 探测成功 ──────────── restarting ◀─────── degraded
                                                    ▲              （冷却 ≥5min 后
                                                    └── 半开重试放行   半开：清窗口重试）

    判活用 bs._llama_alive（R7：GET /health 有响应即活，模型加载期 503 也算
    活——看护的职责是「进程死了拉起来」，不是「模型没加载完就重启」；且
    /health 免鉴权，--api-key 武装后 /v1/models 探活会恒 401 假阴性）；重拉是
    非阻塞 Popen，就绪交给下一轮 30s 探测确认，绝不阻塞看护循环。
    uses_local_brain 为 False（切回云端）时整体复位——8002 由 cmd_cloud 主动停，
    不归看护管，否则会出现「切云后看护反复拉起 8002」的新竞态。
    R4：默认注入的 uses_local_brain（service._vl_toml_uses_local_brain）已改
    tomllib 结构化读 [chat].api_base——修前全文子串匹配被 dump 必写的
    fallback_api_base 默认值污染，cmd_cloud 切回云端后看护仍误判本地、
    每跳重拉 8002；判定修准即本状态机的切云复位语义真实生效。
    uses_local_brain / probe / spawner / clock 全部可注入（pytest 直测）。
    """

    def __init__(self, uses_local_brain, probe, spawner, *,
                 clock=time.monotonic,
                 probe_interval: float = LLAMA_PROBE_INTERVAL,
                 death_confirm: int = LLAMA_DEATH_CONFIRM,
                 breaker_window: float = LLAMA_BREAKER_WINDOW,
                 breaker_threshold: int = LLAMA_BREAKER_THRESHOLD,
                 cooldown: float = LLAMA_COOLDOWN) -> None:
        self._uses_local = uses_local_brain   # () -> bool：vl.toml 是否指本地
        self._probe = probe                   # () -> bool：8002 有响应即 True
        self._spawner = spawner               # () -> None：非阻塞重拉
        self._clock = clock
        self.probe_interval = float(probe_interval)
        self.death_confirm = int(death_confirm)
        self.breaker_window = float(breaker_window)
        self.breaker_threshold = int(breaker_threshold)
        self.cooldown = float(cooldown)
        self.state = "ok"
        self.fails = 0                        # 探测连败数（连续 death_confirm 判死）
        self.deaths = []                      # 熔断窗口内的死亡时刻（monotonic）
        self.degraded_since = None
        self._next_probe = 0.0                # 0 = 首跳立即探

    def reset(self) -> None:
        """清零全部状态回 ok（切云 / 不再指本地时调用）。"""
        self.state = "ok"
        self.fails = 0
        self.deaths = []
        self.degraded_since = None
        self._next_probe = 0.0

    def tick(self) -> None:
        """一跳：本地判定 → degraded 冷却检查 → 30s 限速 → 探测/判死。

        切云判定优先于 degraded 半开：cmd_cloud 已主动停 8002，看护若在冷却满
        瞬间先半开重拉再等下一跳复位，会凭空拉起一个无人管理的 8002。"""
        now = self._clock()
        if not self._uses_local():
            if (self.state != "ok" or self.fails or self.deaths
                    or self.degraded_since is not None):
                _log("vl.toml 已不指本地大脑：llama-server 看护复位（8002 交还手动管理）")
                self.reset()
            return
        if self.state == "degraded":
            if now - (self.degraded_since if self.degraded_since is not None else now) \
                    >= self.cooldown:
                _log(f"llama-server(8002) degraded 冷却 {self.cooldown / 60:.0f}min 结束，"
                     "半开重试一次（熔断窗口清零）")
                self.deaths = []
                self.degraded_since = None
                self.state = "restarting"
                self._next_probe = now + self.probe_interval
                self._spawner()
            return
        if now < self._next_probe:
            return
        self._next_probe = now + self.probe_interval
        if self._probe():
            self.fails = 0
            self.state = "ok"
            return
        self.fails += 1
        self.state = "suspected_down"
        if self.fails < self.death_confirm:
            return
        self.fails = 0
        self._handle_death(now)

    def _handle_death(self, now: float) -> None:
        """判死：剪枝熔断窗口 → 记死亡时刻 → 达阈值熔断，否则非阻塞重拉。"""
        self.deaths = [t for t in self.deaths if now - t < self.breaker_window]
        self.deaths.append(now)
        if len(self.deaths) >= self.breaker_threshold:
            self.state = "degraded"
            self.degraded_since = now
            _log(f"llama-server(8002) {self.breaker_window / 60:.0f} 分钟内第 "
                 f"{len(self.deaths)} 次死亡，熔断 degraded"
                 f"（冷却 {self.cooldown / 60:.0f} 分钟后半开重试）")
            return
        self.state = "restarting"
        _log(f"llama-server(8002) 连续 {self.death_confirm} 次探测失败判死 → 非阻塞重拉"
             f"（窗口内第 {len(self.deaths)} 次死亡）")
        self._spawner()


def _spawn_llama_server() -> None:
    """看护重拉 llama-server：非阻塞 Popen（就绪由 30s 周期探测确认）。

    拉前按 pid 文件 + 命令行核验清旧进程（HANG 僵尸防双开抢端口）；核验拒绝
    （refused）时放弃重拉——状态不明时宁可不拉，留给人工排查。
    R7（FUSION_SPEC §3.3）：拉起前 ensure_local_api_key——--api-key 武装与
    chat_probe/客户端同 key 单源；toml 不可写时返回空=按无鉴权老路径拉起。
    """
    pid_file = bs.LLAMA_PID
    if pid_file.is_file():
        pid = pid_file.read_text(encoding="ascii").strip()
        if pid.isdigit():
            outcome = bs.kill_pid_verified(pid, bs.LLAMA_MARKER, "旧 llama-server")
            if outcome == "refused":
                _log("旧 llama-server pid 核验拒绝 → 放弃重拉（pid 文件保留，人工排查）")
                return
            if outcome in ("killed", "gone"):
                pid_file.unlink(missing_ok=True)
    bs.ensure_local_api_key()
    if not bs.GGUF_Q4.is_file():
        _log(f"GGUF 不存在：{bs.GGUF_Q4}（模型在别的盘/目录时写 vl.toml [chat] "
             "fallback_gguf），无法重拉 llama-server")
        return
    log_file = REPO / "local" / "vl_data" / "logs" / "brain-local.log"
    try:
        proc = subprocess.Popen(
            [str(bs.LLAMA_EXE)] + bs.LLAMA_ARGS,
            cwd=str(bs.LLAMA_EXE.parent),
            stdout=bs.open_runtime_log(log_file), stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        _log(f"llama-server 重拉失败：{exc}")
        return
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid), encoding="ascii")
    _log(f"llama-server 已重拉（pid {proc.pid}，就绪由 30s 探测确认）")


def _daemon_main() -> None:
    """双看护循环（自身被杀 → vl/8002 变裸进程，可接受降级）。

    - vl：10s 巡检，连续 2 跳判空才拉（VlAbsentConfirm——_vl_pids 查询链
      偶发空不再触发双拉抢端口）；重启退避共用 _RestartBackoff；
    - llama-server(8002)：大脑看护 按上述参数面巡检；
    - 单轮异常只记日志不终止循环（CIM 瞬时失败等不致看护猝死）。
    """
    MONITOR_PID.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_PID.write_text(str(_self_pid()), encoding="ascii")
    _log("看护已上线：vl 10s 巡检 + llama-server(8002) 30s 探测（vl.toml 指本地时）")
    backoff = _RestartBackoff()

    def respawn_llama() -> None:
        """8002 重拉入口：先过共用退避门（疯狂期不再雪上加霜）再非阻塞拉起。"""
        backoff.wait_if_hot()
        _spawn_llama_server()
        backoff.note_restart()

    guard = 大脑看护(
        uses_local_brain=_vl_toml_uses_local_brain,
        # R7（FUSION_SPEC §3.3）：探活改 /health（bs._llama_alive，免鉴权）——
        # --api-key 武装后 /v1/models 未带 Bearer 恒 401，「有响应即活」的
        # 语义不再靠 401 的偶然成立
        probe=lambda: bs._llama_alive(bs.SERVER_PORT),
        spawner=respawn_llama)
    absent = VlAbsentConfirm()
    vl_alive_since = None    # 本段稳定期内首次见到 vl 活的时刻（monotonic）
    while True:
        try:
            if bs._vl_pids():
                absent.observe(True)
                now = time.monotonic()
                if vl_alive_since is None:
                    vl_alive_since = now
                backoff.observe_alive(vl_alive_since)   # 连续存活 5min → 退避清零
            elif absent.observe(False):   # 连续 2 跳判空才拉（单跳查询抖动不双拉）
                vl_alive_since = None
                backoff.wait_if_hot()
                _spawn_vl()
                backoff.note_restart()
                vl_alive_since = time.monotonic()
            guard.tick()
        except Exception as exc:  # noqa: BLE001 —— 单轮异常不终止看护
            try:
                _log(f"看护轮异常（继续）：{exc}")
            except Exception:  # noqa: BLE001 —— 日志通道坏死也不死循环崩溃
                pass
        time.sleep(VL_TICK)


def _self_pid() -> int:
    import os
    return os.getpid()


# ---------------------------------------------------------------------------
# 部件：GSVI（只关不启——vl auto_start 管理）
# ---------------------------------------------------------------------------

def gsvi_stop() -> None:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process | Where-Object "
         "{$_.CommandLine -match 'entry\\.py'} | "
         "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
        capture_output=True).stdout
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    import json as _json
    try:
        rows = _json.loads(out or "[]")
    except ValueError:
        return
    if isinstance(rows, dict):
        rows = [rows]
    n = 0
    for r in rows:
        pid = str(r.get("ProcessId", ""))
        if pid.isdigit():
            _kill(pid)
            n += 1
    _log(f"GSVI 语音引擎已停止（{n} 个进程）" if n else "GSVI 未在运行")


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def _vl_toml_uses_local_brain() -> bool:
    """vl.toml 当前是否指向本地大脑（8002）。

    R4 判定精确化：委托 brain_switch.vl_toml_uses_local_brain（tomllib 结构化
    读 [chat].api_base，以 http://127.0.0.1:8002 开头才算；读失败回退 False）。
    修前全文子串匹配 '127.0.0.1:8002'——dump_config 会把 [chat].fallback_api_base
    的默认值（同为 http://127.0.0.1:8002/v1）写进 vl.toml，云端新装被误判本地：
    cmd_start 走 ensure_gguf 在无模型的机器 SystemExit 死亡；cmd_cloud 后看护
    （大脑看护 注入的也是本函数，自动同步）误判反复重拉 8002。"""
    return bs.vl_toml_uses_local_brain()


def cmd_start() -> None:
    napcat_up()
    if _vl_toml_uses_local_brain():
        if bs.local_brain_configured():
            _log("检测到本地大脑配置 → 自动带起 llama-server(8002)")
            bs.ensure_local_api_key()
            bs.ensure_gguf()
            bs.start_llama_server()
        else:
            _log("警告：vl.toml 指向本地大脑但路径未配置（fallback_gguf/fallback_llama_exe 为空）")
            _log("她将无法对话——请在控制台→服务管理→路径设置 中配置")
    else:
        _log("当前大脑：云端（不启动本地 llama-server）")
    vl_start()
    console_port = bs.read_console_port()   # R4：读 vl.toml [console].port，失败回退 8787
    for _ in range(20):
        if _http_alive(f"http://127.0.0.1:{console_port}/api/auth/whoami"):
            break
        time.sleep(1)
    brain = "本地大脑" if _vl_toml_uses_local_brain() else "云端 ark"
    _log("")
    _log("============================================================")
    _log(f"  她已上线。控制台：http://127.0.0.1:{console_port}")
    _log(f"  大脑：{brain} | 语音引擎：首次开口自动拉起")
    _log("  控制台密码：local/console.json 或启动日志")
    _log("============================================================")


def cmd_stop() -> None:
    vl_stop()
    bs.stop_llama_server()
    gsvi_stop()
    napcat_down()
    _log("")
    _log("已全部停止（vl / 本地大脑 / GSVI / napcat）。再见。")


def cmd_status() -> None:
    vl = bool(bs._vl_pids())
    # R2 同口径：看护状态走 daemon_alive 双门（pid 文件 + CIM 命令行核验）——
    # 修前只看 pid 文件在不在，看护死了 status 仍显示「含看护」全绿假象。
    mon = bs.daemon_alive()
    _log(f"vl 进程：{'运行中' + ('（含看护）' if mon else '（裸进程）') if vl else '未运行'}")
    _log(f"llama-server(8002)：{'运行中' if bs._llama_alive(bs.SERVER_PORT) else '未运行'}")
    _log(f"GSVI(5000)：{'运行中' if _http_alive('http://127.0.0.1:5000/character_list') else '未运行'}")
    r = subprocess.run(["docker", "compose", "ps"], cwd=str(REPO),
                       capture_output=True)
    _stdout = r.stdout.decode("utf-8", errors="replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
    _log("napcat 容器：")
    for ln in _stdout.splitlines():
        if ln.strip():
            _log("  " + ln.strip())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Virtual-lover 一键启停")
    p.add_argument("cmd", choices=["start", "stop", "status", "_daemon"])
    args = p.parse_args(argv)
    if args.cmd == "_daemon":
        _daemon_main()
        return 0
    {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}[args.cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
