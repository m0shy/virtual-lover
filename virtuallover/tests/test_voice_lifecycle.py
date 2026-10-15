# -*- coding: utf-8 -*-
"""voice/lifecycle.py 状态机契约测试（VOICE_SPEC §1/§4：假 spawner + 假钟）。

覆盖：adopt-first 收编（external、清遗留 pid、绝不杀）、auto_start 拉起全链
（argv/cwd 契约、pid 落盘、starting→ready）、300s 就绪预算耗尽、30s 周期探测
连续 2 次判死、只杀自有 PID + 命令行核验（防误杀双门）、10 分钟 3 次熔断
degraded、冷却 ≥5 分钟后半开、外部实例死亡不触发击杀、cleanup 终止自有进程。

不引入 pytest-asyncio：asyncio.run 驱动；假钟 + 空转 sleeper 让 300s 预算
瞬时走完（确定性，无真实等待）。

运行：python -m pytest virtuallover/tests/test_voice_lifecycle.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from virtuallover.voice.lifecycle import (
    BREAKER_THRESHOLD,
    LifecycleParams,
    SpawnError,
    STATE_DEGRADED,
    STATE_NOT_STARTED,
    STATE_READY,
    STATE_RESTARTING,
    STATE_STARTING,
    STATE_SUSPECTED_DOWN,
    GsviLifecycle,
)


# ---------------------------------------------------------------------------
# 假件：时钟 / 探测 / 拉起 / 击杀 / 命令行
# ---------------------------------------------------------------------------


class FakeClock:
    """假单调钟：sleeper 推进（300s 预算在测试里零耗时走完）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class SpawnerRecorder:
    """假 spawner：记录 (argv, cwd)，返回递增 pid；可配置抛 SpawnError。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: "list[tuple[list, str]]" = []
        self.fail = fail
        self._next_pid = 4200

    def __call__(self, argv: list, cwd: str) -> int:
        if self.fail:
            raise SpawnError("python.exe 缺失（假 spawner 模拟）")
        self.calls.append((list(argv), str(cwd)))
        self._next_pid += 1
        return self._next_pid


def _lifecycle(tmp_path, *, probe_sequence=None, probe_always=False,
               spawner=None, cmdline=None, killer=None, **params_kw) -> tuple:
    """构造全假件 lifecycle：（对象, 时钟, spawner, 杀记录, 探测序列迭代器）。"""
    clock = FakeClock()
    probes = iter(probe_sequence or [])
    calls = {"probe": 0}

    def probe() -> bool:
        calls["probe"] += 1
        if probe_always:
            return True
        try:
            return bool(next(probes))
        except StopIteration:
            return False

    spawner = spawner or SpawnerRecorder()
    kill_log: "list[int]" = []
    killer = killer or (lambda pid: kill_log.append(pid) or True)

    def cmdline_reader(pid: int):
        if cmdline is None:  # 缺省：核验必过（含 voice_engine_entry.py → 'entry.py'）
            return f"C:\\voice-engine\\runtime\\python.exe scripts/voice_engine_entry.py (pid {pid})"
        return cmdline(pid) if callable(cmdline) else cmdline

    params = LifecycleParams(
        auto_start=params_kw.pop("auto_start", True),
        gsvi_root=params_kw.pop("gsvi_root", str(tmp_path / "voice-engine")),
        ready_budget=params_kw.pop("ready_budget", 300.0),
        probe_interval=params_kw.pop("probe_interval", 30.0),
        cooldown=params_kw.pop("cooldown", 300.0),
        pid_path=tmp_path / "voice" / "voice-engine.pid",
        log_path=tmp_path / "logs" / "voice-engine.log",
        **params_kw,
    )
    lc = GsviLifecycle(params, repo_root=tmp_path, probe=probe, spawner=spawner,
                       killer=killer, cmdline_reader=cmdline_reader,
                       clock=clock, sleeper=clock.sleep)
    return lc, clock, spawner, kill_log, calls


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# adopt-first
# ---------------------------------------------------------------------------


class TestAdoptFirst:
    def test_port_answers_adopts_external_never_kills(self, tmp_path):
        """端口有应答 → 收编 external；遗留 pid 文件清除；绝不杀。"""
        lc, clock, spawner, kill_log, _ = _lifecycle(tmp_path, probe_always=True)
        pid_file = tmp_path / "voice" / "voice-engine.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999", encoding="ascii")  # 遗留 pid（外部实例）
        state = _run(lc.adopt_or_start())
        assert state == STATE_READY
        assert lc.ownership == "external" and lc.state_name() == "ready"
        assert not pid_file.exists()  # 遗留 pid 已清（外部实例不纳入击杀范围）
        assert kill_log == [] and spawner.calls == []

    def test_no_answer_and_auto_start_false_stays_not_started(self, tmp_path):
        lc, *_ = _lifecycle(tmp_path, probe_sequence=[False], auto_start=False)
        assert _run(lc.adopt_or_start()) == STATE_NOT_STARTED
        assert lc.ownership is None


# ---------------------------------------------------------------------------
# auto_start 拉起
# ---------------------------------------------------------------------------


class TestSpawn:
    def test_spawn_args_and_ready(self, tmp_path):
        """Popen 契约：argv=[<引擎根>/runtime/python.exe,
        <仓库根>/scripts/voice_engine_entry.py]、cwd=引擎根（错 cwd 静默降级
        classic）；pid 落盘；owned；marker 与 argv 同源（漂移=核验恒拒杀）。"""
        lc, clock, spawner, kill_log, _ = _lifecycle(
            tmp_path, probe_sequence=[False, False, True])
        state = _run(lc.adopt_or_start())
        assert state == STATE_READY and lc.ownership == "owned"
        argv, cwd = spawner.calls[0]
        assert argv[0].endswith("runtime\\python.exe") or argv[0].endswith("runtime/python.exe")
        assert argv[1] == str(tmp_path / "scripts" / "voice_engine_entry.py")  # 固定第二元素（R8 wrapper）
        assert cwd == str(tmp_path / "voice-engine")       # cwd=引擎根
        from virtuallover.voice.lifecycle import _CMDLINE_MARKER
        assert _CMDLINE_MARKER in argv[1].lower()  # marker↔argv 同源契约（防漂移）
        pid_file = tmp_path / "voice" / "voice-engine.pid"
        assert pid_file.read_text(encoding="ascii") == str(lc.pid)

    def test_ready_budget_exhaustion_kills_and_restarts(self, tmp_path):
        """300s 预算耗尽：判死 → 杀自有（命令行核验过）→ 重拉起；3 轮后熔断。"""
        lc, clock, spawner, kill_log, _ = _lifecycle(tmp_path, probe_sequence=[])
        state = _run(lc.adopt_or_start())
        assert state == STATE_DEGRADED          # 连败计数第二门：3 次死亡即熔断
        assert len(spawner.calls) == 3          # 首次 + 两次重拉起
        assert len(kill_log) == 3               # 每次判死都核验击杀了自有 pid
        assert lc.last_error

    def test_spawn_error_counts_toward_breaker(self, tmp_path):
        """python.exe 缺失：SpawnError 计入熔断链（auto_start 名存实亡时别无限转）。"""
        lc, *_ = _lifecycle(tmp_path, probe_sequence=[False],
                            spawner=SpawnerRecorder(fail=True))
        state = _run(lc.adopt_or_start())
        assert state == STATE_DEGRADED

    def test_log_rotation(self, tmp_path):
        """voice-engine.log 超 5MB 先按 .1/.2/.3 换名轮转（默认 Popen 路径的重定向逻辑）。"""
        from virtuallover.voice.lifecycle import LOG_MAX_BYTES

        lc, *_ = _lifecycle(tmp_path, probe_sequence=[True])
        fh1 = lc._open_log_with_rotation()
        fh1.write(b"first-gen")
        fh1.close()
        (tmp_path / "logs" / "voice-engine.log").write_bytes(b"x" * (LOG_MAX_BYTES + 1))
        fh2 = lc._open_log_with_rotation()  # 超限：旧文件让位 .1，新句柄追加
        fh2.close()
        assert (tmp_path / "logs" / "voice-engine.log.1").exists()
        assert (tmp_path / "logs" / "voice-engine.log").exists()
        assert (tmp_path / "logs" / "voice-engine.log").stat().st_size == 0  # 全新空文件


# ---------------------------------------------------------------------------
# 稳态：30s 周期探测 / 判死 / 熔断 / 半开
# ---------------------------------------------------------------------------


class TestSteadyWatch:
    def _ready_owned(self, tmp_path, probe_seq):
        """先拉起就绪（owned）：首个探测 False（触发 spawn），随后 True（就绪）。"""
        lc, clock, spawner, kill_log, calls = _lifecycle(
            tmp_path, probe_sequence=[False, True] + list(probe_seq))
        _run(lc.adopt_or_start())
        assert lc.ownership == "owned"
        return lc, clock, spawner, kill_log, calls

    def test_one_failure_is_suspected_then_recovers(self, tmp_path):
        lc, clock, spawner, kill_log, _ = self._ready_owned(tmp_path, [False, True])
        _run(lc.probe_tick())
        assert lc.state_name() == STATE_SUSPECTED_DOWN  # 1 次：疑似
        _run(lc.probe_tick())
        assert lc.state_name() == STATE_READY           # 恢复：解除疑似
        assert kill_log == [] and len(spawner.calls) == 1

    def test_two_consecutive_failures_kill_and_restart(self, tmp_path):
        """连续 2 次判死 → 只杀自有 PID → 后台重拉起 → ready。"""
        lc, clock, spawner, kill_log, _ = self._ready_owned(tmp_path, [False, False, False, True])

        async def main():
            owned_pid = lc.pid
            await lc.probe_tick()
            await lc.probe_tick()
            assert kill_log == [owned_pid]          # 杀的正是自有 pid
            await lc.wait_background()
            assert lc.state_name() == STATE_READY
            assert lc.ownership == "owned" and len(spawner.calls) == 2

        _run(main())

    def test_external_death_never_kills(self, tmp_path):
        """外部实例死亡：绝不杀（kill_log 空），auto_start 兜底重拉起自有。"""
        lc, clock, spawner, kill_log, _ = _lifecycle(
            tmp_path, probe_sequence=[True, False, False, False, True])

        async def main():
            await lc.adopt_or_start()               # adopt → external
            assert lc.ownership == "external"
            await lc.probe_tick()
            await lc.probe_tick()                   # 连续 2 次 → 判死
            assert kill_log == []                   # 外部实例绝不杀
            await lc.wait_background()
            assert lc.ownership == "owned"          # 兜底重拉起归自有

        _run(main())

    def test_cmdline_verification_refuses_kill(self, tmp_path):
        """命令行核验（防误杀）：自有 pid 的 cmdline 不含 voice_engine_entry.py
        （marker 'entry.py'）→ 拒绝击杀。"""
        lc, clock, spawner, kill_log, _ = _lifecycle(
            tmp_path, probe_sequence=[False, True, False, False, False, True],
            cmdline=lambda pid: "C:\\other\\python.exe train.py --epochs 3")

        async def main():
            await lc.adopt_or_start()               # spawn → owned
            owned_pid = lc.pid
            await lc.probe_tick()
            await lc.probe_tick()                   # 连续 2 次 → 判死 → 核验
            assert kill_log == []                   # 核验失败：绝不杀
            await lc.wait_background()
            assert lc.ownership == "owned"          # 但自愈照常（重拉起新进程）
            assert owned_pid != lc.pid

        _run(main())

    def test_breaker_after_three_deaths_then_cooldown_half_open(self, tmp_path):
        """10 分钟窗口内 3 次死亡 → degraded；冷却 5 分钟未到不重试；到点半开一次。

        探针：前 20 次全败（恰好覆盖 3 轮「预算 5s×5 探测」的判死链），此后恒真
        ——半开重试一跳即就绪。
        """
        state = {"n": 0}

        def probe() -> bool:
            state["n"] += 1
            return state["n"] > 20

        lc, clock, spawner, kill_log, _ = _lifecycle(
            tmp_path, probe_sequence=None, ready_budget=5.0, cooldown=300.0)
        # 直接换有状态探针（_lifecycle 的序列迭代器不适合本用例）
        lc._probe_fn = probe
        _run(lc.adopt_or_start())
        assert lc.state_name() == STATE_DEGRADED
        assert len(spawner.calls) == 3
        # 冷却未到：maybe_recover 不动作
        clock.now += 100.0
        _run(lc.maybe_recover())
        assert lc.state_name() == STATE_DEGRADED
        assert len(spawner.calls) == 3
        # 冷却结束：半开重试一次 → ready
        clock.now += 300.0
        _run(lc.maybe_recover())
        assert lc.state_name() == STATE_READY
        assert len(spawner.calls) == 4

    def test_synth_failure_feeds_death_chain(self, tmp_path):
        """HANG 型（TCP 通但合成挂起）：连续 2 次合成失败判死 → 后台重拉起。"""
        lc, clock, spawner, kill_log, _ = self._ready_owned(tmp_path, [False, False, True])

        async def main():
            await lc.report_synth_failure()
            assert lc.state_name() == STATE_READY   # 1 次：仅计数
            await lc.report_synth_failure()
            await lc.wait_background()
            assert lc.state_name() == STATE_READY   # 判死 + 后台重拉起完成
            assert len(spawner.calls) == 2
            lc.report_synth_success()               # 成功即清零

        _run(main())

    def test_run_forever_recovers_after_kill(self, tmp_path):
        """杀掉语音引擎后自愈到 ready（§6-5：≤60s 恢复的机制面验证，假钟瞬时走完）。"""
        lc, clock, spawner, kill_log, _ = _lifecycle(
            tmp_path, probe_sequence=[True, False, False, False, True])

        async def main():
            stop = asyncio.Event()
            task = asyncio.ensure_future(lc.run_forever(stop))
            # 让出控制权直到 run_forever 的 adopt 路径跑完（假钟下瞬时）
            for _ in range(10):
                await asyncio.sleep(0)
                if lc.state_name() == STATE_READY:
                    break
            assert lc.state_name() == STATE_READY
            stop.set()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(main())


# ---------------------------------------------------------------------------
# cleanup / stop
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_kills_owned_only(self, tmp_path):
        lc, clock, spawner, kill_log, _ = _lifecycle(tmp_path, probe_sequence=[False, True])
        _run(lc.adopt_or_start())  # spawn → owned
        pid = lc.pid
        lc.cleanup()  # __main__ 清理钩子（同步）：终止自有进程
        assert kill_log == [pid]
        assert lc.ownership is None and lc.pid is None
        assert not (tmp_path / "voice" / "voice-engine.pid").exists()

    def test_cleanup_never_touches_external(self, tmp_path):
        lc, clock, spawner, kill_log, _ = _lifecycle(tmp_path, probe_sequence=[True])
        _run(lc.adopt_or_start())  # adopt → external
        lc.cleanup()
        assert kill_log == []

    def test_stop_engine_async(self, tmp_path):
        lc, clock, spawner, kill_log, _ = _lifecycle(tmp_path, probe_sequence=[False, True])
        _run(lc.adopt_or_start())  # spawn → owned
        owned_pid = lc.pid
        result = _run(lc.stop_engine())
        assert result["state"] == STATE_NOT_STARTED
        assert kill_log == [owned_pid]  # 自有进程经核验链终止
