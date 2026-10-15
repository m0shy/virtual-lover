# -*- coding: utf-8 -*-
"""smoke_ark_thinking.py —— O1 thinking 双实例回归冒烟（PERF_SPEC §2 O1 + §7.2）。

定位：tmp/probe_ark_thinking.py 的「沉淀入库」版（§6）。probe 证明
``{"thinking": {"type": "disabled"}}`` 经 extra_body 通道可关掉 ark/doubao 的
思考（非流式 has_reasoning=False）；本脚本把该结论固化为可重复执行的回归冒烟，
走**生产装配路径**（brain.llm.build_chat_clients 构造的双客户端）发真实小请求。

用法：
    python scripts/bench/smoke_ark_thinking.py            # 完整探测：主 off / bg on
                                                          # 各 1 次，打印延迟与 reasoning
    python scripts/bench/smoke_ark_thinking.py --check    # 回归断言：主客户端非流式
                                                          # has_reasoning 必须为 False
                                                          # （bg on 仅报告不强断言）

退出码（--check）：
    0 = 通过（主客户端关思考生效）
    1 = 失败（思考没关掉 / 请求失败——O1 回归，立即排查）
    2 = skip（vl.toml 缺失或 chat 配置不完整——无 key 环境，如 CI）

本脚本不打印任何密钥；消息体只用一句固定占位问句。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from virtuallover.brain.llm import LLMError, build_chat_clients  # noqa: E402
from virtuallover.config import ConfigError, VlConfig, load_config  # noqa: E402

# 固定占位问句：不携带任何真实用户内容
_PROBE_QUESTION = "用一句话打个招呼"
_PROBE_MESSAGES = [{"role": "user", "content": _PROBE_QUESTION}]

EXIT_OK, EXIT_FAIL, EXIT_SKIP = 0, 1, 2


def _load_cfg() -> VlConfig:
    """加载 VL_ROOT（默认仓库根）下的 local/config/vl.toml。"""
    import os

    root = Path(os.environ.get("VL_ROOT") or REPO_ROOT)
    return load_config(root / "local" / "config" / "vl.toml")


def _reasoning_flag(reply) -> bool:  # noqa: ANN001 —— LLMReply duck typing
    """该次回复是否携带思考内容（reasoning 字段非空，或 raw message 带 reasoning_content）。"""
    if getattr(reply, "reasoning", None):
        return True
    raw = getattr(reply, "raw", None) or {}
    choices = raw.get("choices") or [{}]
    message = choices[0].get("message") or {}
    return bool(message.get("reasoning_content"))


def _probe_once(client, label: str) -> dict:
    """对指定客户端发一次小请求，返回观测摘要（绝不含密钥）。"""
    started = time.perf_counter()
    reply = client.chat(_PROBE_MESSAGES)
    elapsed = time.perf_counter() - started
    summary = {
        "label": label,
        "provider_id": reply.provider_id,
        "attempts": reply.attempts,
        "latency_seconds": round(elapsed, 3),
        "content_chars": len(reply.content or ""),
        "has_reasoning": _reasoning_flag(reply),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="O1 thinking 双实例回归冒烟")
    parser.add_argument("--check", action="store_true",
                        help="回归断言模式：主客户端非流式 has_reasoning 必须为 False")
    args = parser.parse_args(argv)

    try:
        cfg = _load_cfg()
    except ConfigError as exc:
        print(f"[skip] 配置不可用（{exc}）", file=sys.stderr)
        return EXIT_SKIP

    main_client, bg_client = build_chat_clients(cfg.chat)
    try:
        main_summary = _probe_once(main_client, "main(thinking=disabled)")
    except LLMError as exc:
        print(f"[fail] 主客户端请求失败：{exc}", file=sys.stderr)
        return EXIT_FAIL

    bg_summary: dict | None = None
    try:
        bg_summary = _probe_once(bg_client, "bg(thinking=enabled)")
    except LLMError as exc:
        # 后台客户端失败不判 --check 失败：回归锚点是「主回复关思考」
        print(f"[warn] bg 客户端请求失败（仅报告）：{exc}", file=sys.stderr)

    if not args.check:
        return EXIT_OK

    if main_summary["has_reasoning"]:
        print(
            "[fail] O1 回归失败：主客户端（thinking=disabled）返回了 reasoning_content"
            "——extra_body 通道或上游行为漂移，立即排查 build_chat_clients 与模型端点",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(
        "[pass] O1 回归通过：主客户端 has_reasoning=False"
        + (f"（bg has_reasoning={bg_summary['has_reasoning']}，仅报告）"
           if bg_summary else "（bg 未探测）")
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
