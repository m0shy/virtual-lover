# -*- coding: utf-8 -*-
"""perf_probe.py —— 端到端延迟实测探针 + §5 验收判定器（自 tmp/perf_probe.py 收编，§6/§7.1-6）。

扮演 NapCat 反向 WS 客户端连入桥（默认 127.0.0.1:6199/ws）：4 轮单发测首段
延迟分布 + 1 轮 0.5s×3 连发测合并行为；记录全部 send_private_msg 出帧。

轮次归因修复（本收编的核心变更，§7.1-6「修 perf_probe 轮次归因 bug（逐轮复位）」）：
- 旧版 `wait_round` 的静默基准 `last_action_perf` 跨轮共享：上一轮收尾后静默
  已累计 5s+，下一轮首条回复若再慢一点，`idle>=QUIET` 在本轮**零 action** 时
  即成立 → 轮被提前关闭，其回复动作全部滚入下一轮的累计切片（分析脚本靠
  `actions_so_far` 差分归轮）→ 单发轮测到空、连发轮测到别人的回复；
- 修复：每轮起跑即复位静默基准（逐轮复位），并在轮记录里落**本轮封闭切片**
  ``r["actions"]``（起跑时记 start_idx，收尾时切片）——消费方不再做累计差分，
  归因只看轮记录自身。

判定器（--check，§7.1-6「新增判定器输出带日期 JSON + --check 退出码对照 §5」）：
按 §7.1-1 含抖动口径对照 §5 指标：
- 单发首段（E2E，首个 send_private_msg 出帧）：p50 ≤2.5s、p95 ≤4s、下限 ≥0.8s；
- 连发（0.5s×3）：末条→合并回复首段 ≤3.5s、首条→说完所有段 ≤12s；
  （§5 原表「末条→首段 ≤3s」已被 §7.1-1 修订为 ≤3.5s，以修订为准。）
输出带日期 JSON（默认 tmp/perf_check_<YYYYMMDD-HHMMSS>.json）；
退出码 0=达标 / 1=未达标 / 2=输入不可用。

用法：
    python scripts/bench/perf_probe.py                    # 实测（选无人对话时段：
                                                          # 会经在跑 vl 发真实消息）
    python scripts/bench/perf_probe.py --check \\
        --result tmp/perf_probe_result_20261012-090000.json

敏感值（token/self_id/QQ）运行时从 local/config/vl.toml 读取，不写死、不打印。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import tomllib
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import websockets

REPO = Path(__file__).resolve().parents[2]

SINGLE_TEXTS = ["在忙什么呀", "今天午饭吃了什么", "周末有什么安排嘛", "刚才在想什么呢"]
BURST_TEXTS = ["突然有点想你了", "今天过得开心嘛", "晚上早点休息呀"]

QUIET_SECONDS = 10.0     # 静默判定：连续 10s 无任何 action 视为本轮结束
SINGLE_CAP = 120.0       # 单轮超时
BURST_CAP = 180.0

# ---------------------------------------------------------------------------
# §5 判定阈值（§7.1-1 含抖动口径修订后的终值）
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "first_seg_p50_seconds": 2.5,     # 含抖动口径 p50 ≤2.5s
    "first_seg_p95_seconds": 4.0,     # p95 ≤4s
    "first_seg_floor_seconds": 0.8,   # 下限 ≥0.8s（拟人抖动，不是越小越好）
    "burst_last_to_first_seconds": 3.5,  # 连发末条→合并回复首段 ≤3.5s（§7.1-1）
    "burst_first_to_done_seconds": 12.0, # 连发首条→说完所有段 ≤12s
}


def emit(fh, obj: dict) -> None:
    obj["wall"] = time.time()
    obj["wall_str"] = time.strftime("%H:%M:%S", time.localtime(obj["wall"])) + (
        f".{int(obj['wall'] * 1000) % 1000:03d}")
    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fh.flush()


class Probe:
    """探针本体：收帧记 action、按轮封闭归因、摘要落盘（带日期文件名）。"""

    def __init__(self, uri: str, headers: dict, out_dir: Path, gate_user: str = "") -> None:
        self.uri = uri
        self.headers = headers
        # 门禁内用户（R5）：消息事件 user_id 用它而非 self_id
        self.gate_user = gate_user or headers.get("X-Self-ID", "")
        self.out_dir = out_dir
        self.ws = None
        self.last_action_perf = 0.0
        self.actions: list[dict] = []
        self.stopped = asyncio.Event()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.trace_path = out_dir / f"perf_probe_trace_{stamp}.jsonl"
        self.result_path = out_dir / f"perf_probe_result_{stamp}.json"
        self.trace_fh = self.trace_path.open("a", encoding="utf-8")

    # -- 桩侧 ----------------------------------------------------------------

    async def send_message_event(self, msg_id: int, text: str) -> float:
        payload = {
            "time": int(time.time()),
            "self_id": int(self.headers["X-Self-ID"]),
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": msg_id,
            "user_id": int(self.gate_user),
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": int(self.gate_user),
                       "nickname": "perf-probe", "card": ""},
        }
        t0 = time.perf_counter()
        await self.ws.send(json.dumps(payload, ensure_ascii=False))
        emit(self.trace_fh, {"event": "sent", "msg_id": msg_id, "text": text, "perf": t0})
        return t0

    async def reader(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    emit(self.trace_fh, {"event": "nonjson_frame", "bytes": len(raw)})
                    continue
                if not isinstance(frame, dict):
                    continue
                action = frame.get("action")
                if action:
                    t_recv = time.perf_counter()
                    self.last_action_perf = t_recv
                    params = frame.get("params") or {}
                    segs = params.get("message") or []
                    seg_types = [s.get("type") for s in segs if isinstance(s, dict)]
                    text = ""
                    for s in segs:
                        if isinstance(s, dict) and s.get("type") == "text":
                            text += str((s.get("data") or {}).get("text") or "")
                    rec = {
                        "event": "action", "action": action,
                        "recv_perf": t_recv, "seg_types": seg_types,
                        "text": text, "user_id": str(params.get("user_id")),
                        "echo": frame.get("echo"),
                    }
                    for s in segs:
                        if isinstance(s, dict) and s.get("type") == "record":
                            b64 = str((s.get("data") or {}).get("file") or "")
                            rec["record_b64_len"] = len(b64)
                    self.actions.append(rec)
                    emit(self.trace_fh, rec)
                    # 假装 NapCat 应答：retcode=0 → 桥按发送成功处理
                    try:
                        await self.ws.send(json.dumps(
                            {"status": "ok", "retcode": 0, "data": None,
                             "echo": frame.get("echo")}, ensure_ascii=False))
                    except Exception:
                        pass
        except websockets.ConnectionClosed as exc:
            emit(self.trace_fh, {"event": "closed",
                                 "code": getattr(exc, "rcvd", None) and exc.rcvd.code,
                                 "reason": str(getattr(exc, "rcvd", None))})

    async def wait_round(self, cap: float) -> None:
        """等本轮动作流结束：静默 QUIET_SECONDS 或超时。

        静默基准 last_action_perf 已在每轮起跑时复位（逐轮复位）——上一轮的
        静默不再泄露进本轮判定，杜绝「本轮零 action 即关轮」的归因错位。
        """
        start = time.perf_counter()
        while True:
            await asyncio.sleep(0.5)
            idle = time.perf_counter() - self.last_action_perf
            if idle >= QUIET_SECONDS:
                return
            if time.perf_counter() - start >= cap:
                emit(self.trace_fh, {"event": "round_cap_timeout"})
                return

    # -- 主流程 ---------------------------------------------------------------

    async def run(self) -> dict:
        for attempt in range(1, 4):
            try:
                self.ws = await websockets.connect(
                    self.uri, additional_headers=self.headers, max_size=32 * 1024 * 1024,
                    open_timeout=10)
                break
            except Exception as exc:
                emit(self.trace_fh, {"event": "connect_fail", "attempt": attempt,
                                     "err": repr(exc)})
                await asyncio.sleep(2)
        else:
            emit(self.trace_fh, {"event": "abort_no_connection"})
            return {"error": "abort_no_connection"}
        emit(self.trace_fh, {"event": "connected"})
        await self.ws.send(json.dumps({
            "time": int(time.time()), "self_id": int(self.headers["X-Self-ID"]),
            "post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect"}, ensure_ascii=False))
        reader_task = asyncio.create_task(self.reader())

        msg_id = 9_100_001
        rounds: list[dict] = []

        async def run_round(kind: str, texts: list[str]) -> dict:
            nonlocal msg_id
            r = {"kind": kind, "msgs": []}
            start_idx = len(self.actions)          # 本轮封闭切片起点（归因修复）
            self.last_action_perf = time.perf_counter()  # 逐轮复位静默基准
            base = time.perf_counter()
            for text in texts:
                t0 = await self.send_message_event(msg_id, text)
                r["msgs"].append({"msg_id": msg_id, "text": text, "sent_perf": t0})
                msg_id += 1
                if len(texts) > 1:
                    await asyncio.sleep(0.5)
            await self.wait_round(BURST_CAP if len(texts) > 1 else SINGLE_CAP)
            r["actions"] = self.actions[start_idx:]  # 本轮归因只看自己的切片
            r["end_wall"] = time.time()
            r["elapsed"] = time.perf_counter() - base
            rounds.append(r)
            emit(self.trace_fh, {"event": "round_done", "kind": kind,
                                 "elapsed": r["elapsed"],
                                 "actions_so_far": len(self.actions),
                                 "round_action_count": len(r["actions"])})
            await asyncio.sleep(5)

        for i, text in enumerate(SINGLE_TEXTS):
            await run_round(f"single_{i + 1}", [text])
        await run_round("burst_x3", BURST_TEXTS)

        reader_task.cancel()
        with suppress(asyncio.CancelledError):
            await reader_task
        await self.ws.close()
        emit(self.trace_fh, {"event": "done", "total_actions": len(self.actions)})
        self.trace_fh.close()

        summary = {"rounds": rounds, "actions": self.actions}
        self.result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        return summary


# ---------------------------------------------------------------------------
# 判定器（--check）：对 result JSON 按 §5/§7.1-1 含抖动口径出报告
# ---------------------------------------------------------------------------

def _percentile(values: list[float], q: float) -> float:
    """线性插值分位数（q∈[0,1]；空表返回 nan 的调用方兜底为不达标）。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _first_text_latency(round_: dict) -> "float | None":
    """单发轮首段时延（§7.1-5 口径）：本轮首个带 text 段的出帧 - 消息送出时刻。"""
    msgs = round_.get("msgs") or []
    actions = round_.get("actions") or []
    if not msgs or not actions:
        return None
    sent = float(msgs[0]["sent_perf"])
    for action in actions:
        if action.get("action") == "send_private_msg" and "text" in action.get("seg_types", []):
            return float(action["recv_perf"]) - sent
    return None


def _burst_metrics(round_: dict) -> "tuple[float | None, float | None]":
    """连发轮：(末条→首段, 首条→说完)。同样只看本轮封闭切片。"""
    msgs = round_.get("msgs") or []
    actions = [a for a in (round_.get("actions") or [])
               if a.get("action") == "send_private_msg" and "text" in a.get("seg_types", [])]
    if len(msgs) < 2 or not actions:
        return None, None
    last_sent = max(float(m["sent_perf"]) for m in msgs)
    first_sent = min(float(m["sent_perf"]) for m in msgs)
    first_seg = min(float(a["recv_perf"]) for a in actions)
    last_seg = max(float(a["recv_perf"]) for a in actions)
    return first_seg - last_sent, last_seg - first_sent


def check_result(result: dict) -> dict:
    """把一份 result JSON 对照 §5（含 §7.1-1 抖动口径）判达标，返回报告 dict。"""
    rounds = result.get("rounds") or []
    singles = [r for r in rounds if str(r.get("kind", "")).startswith("single_")]
    bursts = [r for r in rounds if r.get("kind") == "burst_x3"]

    first_latencies = [v for v in (_first_text_latency(r) for r in singles) if v is not None]
    p50 = _percentile(first_latencies, 0.50) if first_latencies else float("nan")
    p95 = _percentile(first_latencies, 0.95) if first_latencies else float("nan")
    floor = min(first_latencies) if first_latencies else float("nan")

    last_to_first = [v for v in (_burst_metrics(r)[0] for r in bursts) if v is not None]
    first_to_done = [v for v in (_burst_metrics(r)[1] for r in bursts) if v is not None]
    b_l2f = min(last_to_first) if last_to_first else float("nan")
    b_f2d = max(first_to_done) if first_to_done else float("nan")

    def _metric(name: str, value: float, target: float, direction: str) -> dict:
        # R5 修语义：数据缺失（NaN，如 burst 轮零出帧被 cap 截断）≠ 不达标——
        # 修前 NaN<=target 恒 False，把「未采集到」误报成「超标」，掩盖了
        # burst 全静默这一真故障的定位入口。缺失记 pass=None（skipped），
        # 顶层 pass 用「无 False 即过」但 sample 面明示缺口，人工判读。
        if value != value:  # NaN
            return {"metric": name, "value": None,
                    "target": target, "direction": direction, "pass": None,
                    "note": "未采集到样本（轮零出帧/被 cap 截断），不计达标判定"}
        ok = (value <= target) if direction == "le" else (value >= target)
        return {"metric": name, "value": round(value, 3),
                "target": target, "direction": direction, "pass": bool(ok)}

    metrics = [
        _metric("first_seg_p50_seconds", p50, THRESHOLDS["first_seg_p50_seconds"], "le"),
        _metric("first_seg_p95_seconds", p95, THRESHOLDS["first_seg_p95_seconds"], "le"),
        _metric("first_seg_floor_seconds", floor, THRESHOLDS["first_seg_floor_seconds"], "ge"),
        _metric("burst_last_to_first_seconds", b_l2f,
                THRESHOLDS["burst_last_to_first_seconds"], "le"),
        _metric("burst_first_to_done_seconds", b_f2d,
                THRESHOLDS["burst_first_to_done_seconds"], "le"),
    ]
    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "criteria": "PERF_SPEC §5 + §7.1-1（含抖动口径，E2E=首个 send_private_msg 出帧）",
        "sample": {"single_rounds": len(singles), "burst_rounds": len(bursts),
                   "first_seg_samples": len(first_latencies)},
        "metrics": metrics,
        # pass=None（skipped）不算失败；有任一 False 才判不达标。
        "pass": not any(m["pass"] is False for m in metrics),
    }


def _load_probe_config() -> "tuple[str, dict, str]":
    """读 VL_ROOT（默认仓库根）的 vl.toml，拼 WS 地址/鉴权头/门禁内用户（绝不打印）。

    gate_user：R5 修复——探针消息的 user_id 必须是门禁（whitelist∪admins）内
    的用户；此前误用 self_id（机器人号），R4 语义修正后被桥静默丢弃，探针全零。
    优先 admins[0]，空则 whitelist[0]，再空回退 self_id（宽松模式装机形态）。
    """
    import os

    root = Path(os.environ.get("VL_ROOT") or REPO)
    cfg = tomllib.loads((root / "local" / "config" / "vl.toml").read_text(encoding="utf-8"))
    br = cfg["bridge"]
    uri = f"ws://{br['host']}:{br['port']}{br['path']}"
    headers = {
        "Authorization": f"Bearer {br['token']}",
        "X-Self-ID": str(br["self_id"]),
        "X-Client-Role": "universal",
    }
    admins = [str(a) for a in cfg.get("admins", []) if str(a).strip()]
    whitelist = [str(w) for w in cfg.get("whitelist", []) if str(w).strip()]
    gate_user = (admins or whitelist or [str(br["self_id"])])[0]
    return uri, headers, gate_user


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="E2E 延迟探针 + §5 验收判定器")
    parser.add_argument("--check", action="store_true",
                        help="判定器模式：不实测，对 --result 的探针结果判达标")
    parser.add_argument("--result", type=Path, default=None,
                        help="判定器输入：perf_probe 落盘的 result JSON")
    parser.add_argument("--out", type=Path, default=None,
                        help="判定器报告输出路径（默认 tmp/perf_check_<日期>.json）")
    args = parser.parse_args(argv)

    if args.check:
        result_path = args.result
        if result_path is None or not result_path.exists():
            print("[check] 未找到探针结果文件（--result <path>）", file=sys.stderr)
            return 2
        result = json.loads(result_path.read_text(encoding="utf-8"))
        report = check_result(result)
        out = args.out or (REPO / "tmp" / (
            "perf_check_" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"[check] 报告已写入 {out}")
        return 0 if report["pass"] else 1

    uri, headers, gate_user = _load_probe_config()
    probe = Probe(uri, headers, REPO / "tmp", gate_user=gate_user)
    summary = asyncio.run(probe.run())
    if summary.get("error"):
        print(f"[probe] {summary['error']}", file=sys.stderr)
        return 2
    print(f"probe finished → {probe.result_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
