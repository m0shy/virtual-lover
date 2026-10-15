# -*- coding: utf-8 -*-
"""scripts/service.py + scripts/brain_switch.py 看护体系测试（五轮修缮 R2）。

覆盖任务八点中的守护面：
- 探活双门 daemon_alive：pid 文件 + CIM 命令行核验（修前只看文件在不在）；
- vl 活看护死 → vl_start 只补拉看护（不重复拉 vl）；
- pid 杀一律命令行核验 kill_pid_verified：marker 不合拒绝杀（宁可不杀不误杀），
  vl_stop / stop_llama_server / restart_vl 全走核验门；
- 「最近死亡时刻」窗口退避 _RestartBackoff：连续存活 5min 重置，不再终身累计；
- 双看护状态机 大脑看护：30s 限速 / 连续 2 次判死 / 10min 3 次熔断 /
  5min 冷却后半开 / 切云复位（优先于半开，防凭空拉起 8002）；
- restart_vl 竞态消除：看护在岗绝不自行 Popen（防双 vl 抢端口）；
- napcat_up Docker 未就绪重试 3×30s；open_runtime_log 5MB×3 轮转。

安全规约：CIM/taskkill/Popen/端口探测/时钟/睡眠全部注入假件——绝不触碰真实
进程、真实 pid 文件（一律 tmp_path）与真实 vl.toml；不落任何真实密钥/token/QQ 号。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    """按独立模块名加载脚本（不污染正式名；test_brain_switch_log 同款手法）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bs():
    return _load("brain_switch_service_guard", REPO_ROOT / "scripts" / "brain_switch.py")


@pytest.fixture(scope="module")
def svc():
    return _load("service_guard_test", REPO_ROOT / "scripts" / "service.py")


class _Clock:
    """假 monotonic 钟：手动推进，退避/状态机时间行为全确定性。"""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# 一、探活双门：daemon_alive（任务 1）
# ---------------------------------------------------------------------------

def test_daemon_alive_gates(tmp_path, bs):
    """四道门缺一不可：pid 文件在 + 数字 + 命令行可读 + 含看护标记。"""
    pid_file = tmp_path / "m.pid"
    # 门 1：pid 文件不存在
    assert bs.daemon_alive(pid_path=pid_file) is False
    # 门 2：内容非数字
    pid_file.write_text("not-a-pid", encoding="ascii")
    assert bs.daemon_alive(pid_path=pid_file) is False
    # 门 3：命令行读不到（进程已死——修前这种「遗留文件」误报含看护）
    pid_file.write_text("123", encoding="ascii")
    assert bs.daemon_alive(pid_path=pid_file, reader=lambda p: None) is False
    # 门 4：pid 被 OS 复用成无关进程（命令行不含 _daemon）
    assert bs.daemon_alive(
        pid_path=pid_file,
        reader=lambda p: r"C:\x\python.exe -m virtuallover") is False
    # 全过：真看护命令行（service.py + _daemon）
    assert bs.daemon_alive(
        pid_path=pid_file,
        reader=lambda p: r"C:\tool\x\python.exe C:\repo\scripts\service.py _daemon") is True


# ---------------------------------------------------------------------------
# 二、pid 击杀核验门：kill_pid_verified（任务 4，照抄 lifecycle.kill_own_sync）
# ---------------------------------------------------------------------------

def test_kill_pid_verified_refuses_foreign_cmdline(bs):
    """命令行不含 marker → 拒绝击杀且 killer 不被调（防 pid 复用误杀）。"""
    calls = []

    def killer(pid):
        calls.append(pid)
        return True

    out = bs.kill_pid_verified(
        "999", "llama-server", "llama-server",
        reader=lambda p: r"C:\x\python.exe -m virtuallover",  # 无关进程
        killer=killer)
    assert out == "refused"
    assert calls == []


def test_kill_pid_verified_gone_and_killed_and_failed(bs):
    # gone：命令行读不到（进程已死）→ 不杀
    assert bs.kill_pid_verified("1", "llama-server", "L",
                                reader=lambda p: None,
                                killer=lambda p: True) == "gone"
    # killed：核验通过（marker 命中，大小写不敏感）→ 恰好杀一次
    calls = []

    def killer(pid):
        calls.append(pid)
        return True

    out = bs.kill_pid_verified("7", "LLAMA-SERVER", "L",
                               reader=lambda p: r"D:\x\LLAMA-server.exe -m m",
                               killer=killer)
    assert out == "killed" and calls == ["7"]
    # failed：核验通过但 taskkill 失败
    assert bs.kill_pid_verified("7", "llama-server", "L",
                                reader=lambda p: "llama-server.exe",
                                killer=lambda p: False) == "failed"


def test_kill_pid_verified_rejects_bad_pid(bs):
    """pid 非数字直接拒绝，reader/killer 都不碰。"""
    touched = []
    out = bs.kill_pid_verified("abc", "m", "L",
                               reader=lambda p: touched.append(p),
                               killer=lambda p: touched.append(p))
    assert out == "refused" and touched == []


# ---------------------------------------------------------------------------
# 三、vl_stop：看护 pid 文件来源核验 + refused 保留 pid 文件（任务 1/4）
# ---------------------------------------------------------------------------

def test_vl_stop_verifies_and_keeps_pidfile_on_refuse(tmp_path, svc, monkeypatch):
    pid_file = tmp_path / "m.pid"
    pid_file.write_text("999", encoding="ascii")
    monkeypatch.setattr(svc, "MONITOR_PID", pid_file)
    seen = []

    def fake_kill(pid, marker, label, **kw):
        seen.append((pid, marker))
        return "refused"

    monkeypatch.setattr(svc.bs, "kill_pid_verified", fake_kill)
    monkeypatch.setattr(svc.bs, "_vl_pids", lambda: ["111", "222"])
    svc.vl_stop()
    # 看护走 DAEMON_MARKER、vl 走 VL_MARKER，全部先核验
    assert seen == [("999", svc.bs.DAEMON_MARKER),
                    ("111", svc.bs.VL_MARKER),
                    ("222", svc.bs.VL_MARKER)]
    assert pid_file.exists()  # refused → pid 文件保留供人工排查


def test_vl_stop_cleans_pidfile_when_killed(tmp_path, svc, monkeypatch):
    pid_file = tmp_path / "m.pid"
    pid_file.write_text("999", encoding="ascii")
    monkeypatch.setattr(svc, "MONITOR_PID", pid_file)
    monkeypatch.setattr(svc.bs, "kill_pid_verified", lambda *a, **kw: "killed")
    monkeypatch.setattr(svc.bs, "_vl_pids", lambda: [])
    svc.vl_stop()
    assert not pid_file.exists()


# ---------------------------------------------------------------------------
# 四、vl_start：vl 活看护死 → 只补拉看护（任务 1）
# ---------------------------------------------------------------------------

def test_vl_start_pulls_daemon_when_vl_alive_guard_dead(svc, monkeypatch):
    monkeypatch.setattr(svc.bs, "_vl_pids", lambda: ["123"])  # vl 活
    alive = iter([False] + [True] * 30)  # 看护先死，补拉后核验通过
    monkeypatch.setattr(svc.bs, "daemon_alive", lambda: next(alive))
    spawned = []
    monkeypatch.setattr(svc, "_spawn_daemon", lambda: spawned.append(1))
    monkeypatch.setattr(svc.time, "sleep", lambda s: None)
    svc.vl_start()
    assert spawned == [1]  # 只补看护，绝不再拉 vl


def test_vl_start_reuses_when_guard_alive(svc, monkeypatch):
    monkeypatch.setattr(svc.bs, "_vl_pids", lambda: ["123"])
    monkeypatch.setattr(svc.bs, "daemon_alive", lambda: True)
    spawned = []
    monkeypatch.setattr(svc, "_spawn_daemon", lambda: spawned.append(1))
    svc.vl_start()
    assert spawned == []  # 双活：什么都不做


def test_vl_start_fresh_boot_spawns_daemon(svc, monkeypatch):
    seq = iter([[], ["456"]])
    monkeypatch.setattr(svc.bs, "_vl_pids", lambda: next(seq, ["456"]))
    monkeypatch.setattr(svc.bs, "daemon_alive", lambda: True)
    spawned = []
    monkeypatch.setattr(svc, "_spawn_daemon", lambda: spawned.append(1))
    monkeypatch.setattr(svc.time, "sleep", lambda s: None)
    svc.vl_start()
    assert spawned == [1]


def test_cmd_status_uses_daemon_alive(svc, monkeypatch, capsys):
    """status 同口径：看护死 → 显示裸进程（修前 pid 文件遗留时假报「含看护」）。"""
    monkeypatch.setattr(svc.bs, "_vl_pids", lambda: ["1"])
    monkeypatch.setattr(svc.bs, "daemon_alive", lambda: False)
    svc.cmd_status()
    out = capsys.readouterr().out
    assert "裸进程" in out and "含看护" not in out


# ---------------------------------------------------------------------------
# 五、「最近死亡时刻」窗口退避：_RestartBackoff（任务 5）
# ---------------------------------------------------------------------------

def test_backoff_resets_after_stable_window(svc):
    """连崩 5 次后稳定跑满 5min 再死：计数重开（修前终身累计 → 永远 5min 节奏）。"""
    ck = _Clock()
    b = svc._RestartBackoff(clock=ck, sleeper=lambda s: None)
    for _ in range(5):
        b.note_restart()
    assert b.count == 5
    ck.advance(400)  # 距上次拉起 400s ≥ window(300)：稳定期
    b.note_restart()
    assert b.count == 1  # 先重置再 +1，不再背着历史


def test_backoff_observe_alive_resets(svc):
    """vl 连续存活满 5min → 计数清零（存活期每轮巡检都会触发）。"""
    ck = _Clock()
    b = svc._RestartBackoff(clock=ck, sleeper=lambda s: None)
    b.note_restart()          # t=0：count=1
    ck.advance(100)
    b.observe_alive(0)        # 存活 100 < 300：不清
    assert b.count == 1
    ck.advance(250)           # 存活 350 ≥ 300
    b.observe_alive(0)
    assert b.count == 0


def test_backoff_wait_if_hot_sleeps_then_releases(svc):
    """疯狂期（计数满阈值）限流：距上次拉起不足 cool 时睡到点，满 cool 放行。"""
    ck = _Clock()
    slept = []
    b = svc._RestartBackoff(clock=ck, sleeper=slept.append, threshold=3, cool=300)
    for _ in range(3):
        b.note_restart()      # count=3，last_start=0
    ck.advance(100)
    b.wait_if_hot()
    assert slept == [200.0]   # t=100：还差 200s 到 cool，睡足差额
    ck.advance(300)           # t=400：距上次拉起已满 cool
    b.wait_if_hot()
    assert slept == [200.0]   # 到点放行，不再睡


def test_backoff_calm_never_sleeps(svc):
    """正常偶崩（计数 < 阈值）永不触发长睡。"""
    ck = _Clock()
    slept = []
    b = svc._RestartBackoff(clock=ck, sleeper=slept.append, threshold=20)
    b.note_restart()
    b.wait_if_hot()
    assert slept == []


# ---------------------------------------------------------------------------
# 六、双看护状态机：大脑看护（任务 2，voice 参数面 30s/2 死/10min 3 次/5min）
# ---------------------------------------------------------------------------

def _make_guard(svc, ck, probe_seq, spawner, uses_local=None):
    it = iter(probe_seq)

    def probe():
        return next(it, probe_seq[-1])

    n = [0]

    def spawn():
        n[0] += 1
        spawner(n[0])

    return svc.大脑看护(
        uses_local_brain=uses_local or (lambda: True),
        probe=probe, spawner=spawn, clock=ck,
        probe_interval=30, death_confirm=2,
        breaker_window=600, breaker_threshold=3, cooldown=300)


def test_llama_guard_probe_rate_limited_to_30s(svc):
    ck = _Clock()
    probes = []

    def probe():
        probes.append(1)
        return True

    g = svc.大脑看护(uses_local_brain=lambda: True, probe=probe,
                       spawner=lambda: None, clock=ck, probe_interval=30)
    g.tick()
    g.tick()
    g.tick()                       # 同一时刻连跳：只首跳探一次（30s 限速）
    assert len(probes) == 1
    ck.advance(30)
    g.tick()
    assert len(probes) == 2


def test_llama_guard_two_fails_confirm_death(svc):
    ck = _Clock()
    spawned = []
    g = _make_guard(svc, ck, [False, False, True], spawned.append)
    g.tick()                       # t=0：失败 1 次 → suspected，不拉
    assert g.state == "suspected_down" and spawned == []
    ck.advance(30)
    g.tick()                       # t=30：连续 2 次 → 判死重拉
    assert spawned == [1] and g.state == "restarting"
    ck.advance(30)
    g.tick()                       # t=60：探测恢复 → ok，连败清零
    assert g.state == "ok" and g.fails == 0


def test_llama_guard_breaker_on_third_death(svc):
    ck = _Clock()
    spawned = []
    g = _make_guard(svc, ck, [False, False, True, False, False, True, False, False],
                    spawned.append)
    for _ in range(8):             # 三段「2 连败 + 恢复」在 600s 窗口内
        g.tick()
        ck.advance(30)
    assert spawned == [1, 2]       # 第 3 次判死不再拉
    assert g.state == "degraded"
    assert len(g.deaths) == 3
    n = list(spawned)
    ck.advance(30)
    g.tick()                       # degraded 期内：不探不拉
    assert spawned == n and g.state == "degraded"


def test_llama_guard_half_open_after_cooldown(svc):
    ck = _Clock()
    spawned = []
    g = _make_guard(svc, ck, [False, False, True, False, False, True, False, False, True],
                    spawned.append)
    for _ in range(8):
        g.tick()
        ck.advance(30)
    ck.advance(300)                # 冷却 5min 满
    g.tick()                       # 半开：清窗口重试一次
    assert spawned == [1, 2, 3]
    assert g.deaths == [] and g.state == "restarting"
    ck.advance(30)
    g.tick()                       # 重试成功 → ok
    assert g.state == "ok"


def test_llama_guard_resets_when_cloud_takes_over(svc):
    """切云判定优先于 degraded 半开：reset 绝不凭空拉起 8002。"""
    ck = _Clock()
    spawned = []
    g = _make_guard(svc, ck, [], spawned.append, uses_local=lambda: False)
    g.state = "degraded"           # 模拟切云瞬间残留的熔断态
    g.degraded_since = 0.0
    g.deaths = [1.0, 2.0]
    ck.advance(400)                # 冷却早已期满——修前此处会先半开重拉
    g.tick()
    assert spawned == []
    assert g.state == "ok" and g.deaths == [] and g.degraded_since is None


# ---------------------------------------------------------------------------
# 七、restart_vl 竞态消除（任务 3）：看护在岗绝不自行 Popen
# ---------------------------------------------------------------------------

def test_restart_vl_defers_to_guard(bs, monkeypatch):
    killed = []
    monkeypatch.setattr(bs, "kill_pid_verified",
                        lambda pid, marker, label, **kw:
                        killed.append((pid, marker)) or "killed")
    monkeypatch.setattr(bs, "_vl_pids", lambda: ["123"])
    monkeypatch.setattr(bs, "daemon_alive", lambda: True)
    monkeypatch.setattr(bs, "_port_listening", lambda port: True)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)

    class _FakeSubprocess:
        def Popen(self, *a, **kw):
            raise AssertionError("看护在岗时 restart_vl 不得自行 Popen（双拉竞态）")

    monkeypatch.setattr(bs, "subprocess", _FakeSubprocess())
    bs.restart_vl()
    assert ("123", bs.VL_MARKER) in killed  # 只杀 vl，拉起交给看护


def test_restart_vl_self_popen_when_guard_dead(bs, monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "kill_pid_verified", lambda *a, **kw: "killed")
    monkeypatch.setattr(bs, "_vl_pids", lambda: [])
    monkeypatch.setattr(bs, "daemon_alive", lambda: False)
    monkeypatch.setattr(bs, "_port_listening", lambda port: True)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    monkeypatch.setattr(bs, "open_runtime_log", lambda p: None)  # 不碰真实日志文件
    monkeypatch.setattr(bs, "VL_PID", tmp_path / "vl.pid")       # pid 落盘指 tmp
    popen_calls = []

    class _FakeSubprocess:
        STDOUT = subprocess.STDOUT
        CREATE_NO_WINDOW = 0x08000000

        def Popen(self, *a, **kw):
            popen_calls.append(a)
            from types import SimpleNamespace
            return SimpleNamespace(pid=4321)   # write_vl_pid 只读 .pid

    monkeypatch.setattr(bs, "subprocess", _FakeSubprocess())
    bs.restart_vl()
    assert len(popen_calls) == 1  # 看护死：保留自行拉起兜底
    assert (tmp_path / "vl.pid").read_text(encoding="ascii") == "4321"  # spawn 即写 pid


# ---------------------------------------------------------------------------
# 八、stop_llama_server 核验门（任务 4）
# ---------------------------------------------------------------------------

def test_stop_llama_server_verifies_then_cleans(tmp_path, bs, monkeypatch):
    pid_file = tmp_path / "l.pid"
    pid_file.write_text("888", encoding="ascii")
    monkeypatch.setattr(bs, "LLAMA_PID", pid_file)
    seen = {}

    def fake_kill(pid, marker, label, **kw):
        seen["args"] = (pid, marker)
        return "killed"

    monkeypatch.setattr(bs, "kill_pid_verified", fake_kill)
    bs.stop_llama_server()
    assert seen["args"] == ("888", bs.LLAMA_MARKER)
    assert not pid_file.exists()  # killed → 清 pid 文件


def test_stop_llama_server_keeps_pidfile_on_refuse(tmp_path, bs, monkeypatch):
    pid_file = tmp_path / "l.pid"
    pid_file.write_text("888", encoding="ascii")
    monkeypatch.setattr(bs, "LLAMA_PID", pid_file)
    monkeypatch.setattr(bs, "kill_pid_verified", lambda *a, **kw: "refused")
    bs.stop_llama_server()
    assert pid_file.exists()  # 核验拒绝 → 保留人工排查


def test_stop_llama_server_noop_without_pidfile(tmp_path, bs, monkeypatch):
    monkeypatch.setattr(bs, "LLAMA_PID", tmp_path / "absent.pid")
    touched = []
    monkeypatch.setattr(bs, "kill_pid_verified", lambda *a, **kw: touched.append(1))
    bs.stop_llama_server()
    assert touched == []


# ---------------------------------------------------------------------------
# 九、napcat_up：Docker 未就绪重试 3×30s（任务 6）
# ---------------------------------------------------------------------------

def test_napcat_up_retries_until_ready(svc, capsys):
    seq = iter([(False, "docker not ready"), (False, "still not"), (True, "")])
    slept = []
    svc.napcat_up(runner=lambda: next(seq), sleeper=slept.append)
    assert slept == [30.0, 30.0]  # 两次失败各歇 30s
    assert "第 3 次" in capsys.readouterr().out


def test_napcat_up_gives_up_after_attempts(svc):
    seq = iter([(False, "docker not ready")] * 5)
    with pytest.raises(SystemExit, match="3"):
        svc.napcat_up(runner=lambda: next(seq), sleeper=lambda s: None)


# ---------------------------------------------------------------------------
# 十、运行日志轮转：open_runtime_log 5MB×3（任务 7）
# ---------------------------------------------------------------------------

def test_open_runtime_log_rotates(tmp_path, bs, monkeypatch):
    monkeypatch.setattr(bs, "LOG_MAX_BYTES", 16)  # 阈值调小便于测试
    log = tmp_path / "x.log"
    log.write_text("a" * 20, encoding="ascii")    # 已超阈值
    fh = bs.open_runtime_log(log)
    fh.write(b"b")
    fh.close()
    assert Path(str(log) + ".1").read_text() == "a" * 20   # 旧内容整体挪到 .1
    assert log.read_text() == "b"                          # 新句柄从空文件追加
    with bs.open_runtime_log(log) as fh:                   # 1 字节 < 16：不轮转
        fh.write(b"c" * 20)
    fh3 = bs.open_runtime_log(log)                         # 21 字节 > 16：.1→.2
    fh3.close()
    assert Path(str(log) + ".2").read_text() == "a" * 20
    assert Path(str(log) + ".1").read_text() == "b" + "c" * 20
    assert log.read_text() == ""


# ---------------------------------------------------------------------------
# 十一、_vl_toml_uses_local_brain 容错（看护 30s 读一次，不能因文件瞬断炸掉）
#
# R4 判定精确化：实现委托 brain_switch.vl_toml_uses_local_brain（tomllib
# 结构化读 [chat].api_base），注入点由 svc.REPO 变为 bs.VL_TOML；判定语义
# 的回归用例（fallback_api_base 默认值污染）在 test_install_fixes.py。
# ---------------------------------------------------------------------------

def test_vl_toml_missing_returns_false(tmp_path, svc, monkeypatch):
    monkeypatch.setattr(svc.bs, "VL_TOML", tmp_path / "local" / "config" / "vl.toml")
    assert svc._vl_toml_uses_local_brain() is False


def test_vl_toml_local_detection(tmp_path, svc, monkeypatch):
    cfg = tmp_path / "local" / "config"
    cfg.mkdir(parents=True)
    (cfg / "vl.toml").write_text('[chat]\napi_base = "http://127.0.0.1:8002/v1"\n',
                                 encoding="utf-8")
    monkeypatch.setattr(svc.bs, "VL_TOML", cfg / "vl.toml")
    assert svc._vl_toml_uses_local_brain() is True
    (cfg / "vl.toml").write_text('[chat]\napi_base = "https://ark.example/api/v3"\n',
                                 encoding="utf-8")
    assert svc._vl_toml_uses_local_brain() is False
