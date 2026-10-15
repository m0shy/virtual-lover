# -*- coding: utf-8 -*-
"""probe_ark_thinking.py —— thinking 开关与长 prompt 对首字延迟的影响（自 tmp/ 收编，§6）。

定位（与 scripts/bench/smoke_ark_thinking.py 的分工）：
- 本脚本 = **裸探针**：urllib 直连 /chat/completions，不经过 brain.llm 装配，
  用于延迟矩阵观测（thinking on/off × 流式/非流式 × 短/生产级 system prompt）
  与线格式的独立复核——即便装配层改动，直连格式结论不受牵连；
- smoke_ark_thinking.py = **回归冒烟**：走生产装配路径（build_chat_clients）
  断言主客户端关思考生效（§7.1-8）。两者退出码口径一致（0/1/2）。

--check 模式（PERF_SPEC §7.2「probe_ark_thinking.py --check 模式（无 key 独立
退出码 skip）」）：发一次非流式小请求（thinking disabled），断言
``has_reasoning=False``（O1 回归冒烟固化，§2-O1「断言入库」）。

退出码（--check）：
    0 = 通过（关思考在 wire 格式层生效）
    1 = 失败（带 reasoning / 请求失败——排查模型端点或请求体）
    2 = skip（VL_ROOT 无 vl.toml 或 chat 配置缺 api_key/model——无 key 环境）

本脚本不打印 api_key；不打印 prompt 原文（只报长度与延迟）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK, EXIT_FAIL, EXIT_SKIP = 0, 1, 2

_PROBE_QUESTION = "用一句话打个招呼"


def load_cfg() -> dict:
    root = Path(os.environ.get("VL_ROOT") or REPO_ROOT)
    with open(root / "local" / "config" / "vl.toml", "rb") as fh:
        return tomllib.load(fh)


def stream_once(base, key, model, messages, extra=None):
    payload = {"model": model, "messages": messages, "stream": True}
    payload.update(extra or {})
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    ttft_reasoning = ttft_content = None
    total = None
    n_content_chunks = 0
    with urllib.request.urlopen(req, timeout=90) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = ((obj.get("choices") or [{}])[0]).get("delta") or {}
            now = time.perf_counter() - t0
            if delta.get("reasoning_content") and ttft_reasoning is None:
                ttft_reasoning = now
            if delta.get("content") and ttft_content is None:
                ttft_content = now
            if delta.get("content"):
                n_content_chunks += 1
            total = now
    return ttft_reasoning, ttft_content, total, n_content_chunks


def nonstream_once(base, key, model, messages, extra=None):
    payload = {"model": model, "messages": messages, "stream": False}
    payload.update(extra or {})
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    dt = time.perf_counter() - t0
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    usage = body.get("usage") or {}
    has_reasoning = bool(msg.get("reasoning_content"))
    return dt, has_reasoning, usage.get("prompt_tokens"), usage.get("completion_tokens")


def _ms(v):
    return f"{v*1000:.0f}ms" if v is not None else "None"


# ---------------------------------------------------------------------------
# --check：O1 回归的 wire 层断言（非流式 + thinking disabled → has_reasoning=False）
# ---------------------------------------------------------------------------

def run_check() -> int:
    try:
        cfg = load_cfg()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"[skip] 配置不可用（{exc}）", file=sys.stderr)
        return EXIT_SKIP
    chat = cfg.get("chat") or {}
    base = str(chat.get("api_base") or "").rstrip("/")
    key = str(chat.get("api_key") or "")
    model = str(chat.get("model") or "")
    if not base or not key or not model:
        print("[skip] 无 key（chat 配置缺 api_base/api_key/model）——无 key 环境跳过",
              file=sys.stderr)
        return EXIT_SKIP

    messages = [{"role": "user", "content": _PROBE_QUESTION}]
    off = {"thinking": {"type": "disabled"}}
    try:
        dt, has_reasoning, _pt, _ct = nonstream_once(base, key, model, messages, off)
    except Exception as exc:  # noqa: BLE001 —— 网络/HTTP 失败=断言无法执行，判失败
        print(f"[fail] 非流式探测请求失败：{exc}", file=sys.stderr)
        return EXIT_FAIL
    print(f"[probe] nonstream+thinkoff total={dt*1000:.0f}ms has_reasoning={has_reasoning}")
    if has_reasoning:
        print("[fail] thinking=disabled 仍返回 reasoning_content —— "
              "wire 格式或上游行为漂移，排查 extra_body 与模型端点", file=sys.stderr)
        return EXIT_FAIL
    print("[pass] 非流式 has_reasoning=False（O1 回归冒烟固化，直连层）")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 默认模式：延迟矩阵（少量小请求；报告只报长度与延迟，不贴原文）
# ---------------------------------------------------------------------------

def run_probe() -> int:
    cfg = load_cfg()
    chat = cfg["chat"]
    base, key, model = chat["api_base"].rstrip("/"), chat["api_key"], chat["model"]

    # 生产人格 system prompt 实际长度（从 vl.toml 取，报告只报长度不贴原文）
    sys_prompt = (cfg.get("persona") or {}).get("system_prompt") or ""
    print(f"persona system_prompt 长度: {len(sys_prompt)} chars")

    short = [{"role": "user", "content": _PROBE_QUESTION}]
    long = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": _PROBE_QUESTION}]
    off = {"thinking": {"type": "disabled"}}

    # 1) 短 prompt + thinking off + 流式
    for i in range(2):
        tr, tc, tot, n = stream_once(base, key, model, short, off)
        print(f"[short+thinkoff+stream #{i+1}] TTFT_reasoning={_ms(tr)} "
              f"TTFT_content={_ms(tc)} last={_ms(tot)} content_chunks={n}")
    # 2) 短 prompt + thinking off + 非流式
    for i in range(2):
        dt, hr, pt, ct = nonstream_once(base, key, model, short, off)
        print(f"[short+thinkoff+nonstream #{i+1}] total={dt*1000:.0f}ms "
              f"has_reasoning={hr} prompt={pt} completion={ct}")
    # 3) 生产级长 system prompt + 流式（thinking 默认）
    tr, tc, tot, n = stream_once(base, key, model, long)
    print(f"[prod-prompt+stream] TTFT_reasoning={_ms(tr)} TTFT_content={_ms(tc)} "
          f"last={_ms(tot)} content_chunks={n}")
    # 4) 生产级长 system prompt + thinkoff + 流式
    tr, tc, tot, n = stream_once(base, key, model, long, off)
    print(f"[prod-prompt+thinkoff+stream] TTFT_reasoning={_ms(tr)} "
          f"TTFT_content={_ms(tc)} last={_ms(tot)} content_chunks={n}")
    return EXIT_OK


def main(argv: "list[str] | None" = None) -> int:
    if "--check" in (argv if argv is not None else sys.argv[1:]):
        return run_check()
    return run_probe()


if __name__ == "__main__":
    sys.exit(main())
