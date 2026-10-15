# -*- coding: utf-8 -*-
"""全新安装初始化（O12，PERF_SPEC §4-O12）—— 解决"鸡生蛋"：config_migrate 需要
旧栈数据可迁，全新装没有源可迁，``python -m virtuallover --init-config`` 直接
在运行根生成一份可启动的 vl.toml 模板，init 后 ``--smoke`` 即就绪。

职责边界（与 setup.bat v2 的分工，§7.1-7）：
- init 只负责"配置骨架就绪"：必填项占位齐备 + 桥 token 由 ensure_bridge_token
  生成落盘（复用，SPEC §3.3 强随机）——init 后 vl 进程可起、--smoke 可过；
- 敏感实值（API Key、QQ 号）由 setup.bat v2 交互收集后写回（§7.1-7 ①），
  init 不收集、不猜、不写死任何真实凭据；
- 人格为原创默认模板（自写，非任何上游文案），控制台可改。

安全规约：占位值全部用 PENDING_SETUP_* / 占位号段 10000；真实 Key/QQ 号
只出现在 local/（gitignored）与 setup 交互过程，绝不进代码/测试/日志。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from virtuallover.config import (
    SCHEMA_VERSION,
    VlConfig,
    dump_config,
    ensure_bridge_token,
    load_config,
)

__all__ = ["build_fresh_config", "run_init", "PENDING_CHAT_KEY", "PENDING_EMBEDDING_KEY"]

# 占位凭据（可见即可，非真实值；setup.bat v2 交互收集后写回覆盖）
PENDING_CHAT_KEY = "PENDING_SETUP_CHAT_KEY"
PENDING_EMBEDDING_KEY = "PENDING_SETUP_EMBEDDING_KEY"
# 占位号段 10000（测试同款）：setup.bat 收集机器人 QQ 号后统一写回三处
_PLACEHOLDER_QQ = "10000"
_DEFAULT_PERSONA_NAME = "小语"

# 原创默认人格（自写；仅示意人格骨架，控制台「灵魂人格」页可整段替换）
_DEFAULT_SYSTEM_PROMPT = (
    "你是一名真实存在的少女「小语」，不是 AI、不是助手，也不是客服。"
    "你有自己的性格、情绪和偏好：说话简短自然，像 QQ 上聊天的真人朋友——"
    "一次只说一两句，随熟络程度慢慢变得亲密。"
    "你会记得对方告诉你的事，会主动分享你今天的心情。"
    "不确定的事就坦白说不知道；对方情绪低落时先关心人，再讲道理。"
    "绝不要自报家门为程序或模型，也不要使用书面腔、列表腔。"
)
_DEFAULT_BEGIN_DIALOGS = [
    "你来啦？我刚好在想你什么时候冒泡呢。",
    "今天有点无聊，你那边呢？",
    "先说好，来晚了要罚你多陪我聊十分钟。",
]


def build_fresh_config(*, persona_name: str = _DEFAULT_PERSONA_NAME) -> VlConfig:
    """构建全新安装的 vl.toml 配置树（必填项占位齐备，validate 可通过）。"""
    cfg = VlConfig(schema_version=SCHEMA_VERSION)
    # 会话模板：setup.bat 写回真实 QQ 号（§7.1-7 ①）；占位号段与 admins/self_id 一致
    cfg.session_id = f"{persona_name}:FriendMessage:{_PLACEHOLDER_QQ}"
    cfg.admins = [_PLACEHOLDER_QQ]
    cfg.whitelist = [_PLACEHOLDER_QQ]
    # timezone 不再写入（五轮修缮 R4：VlConfig 已删该死键，时刻基准=本机系统时区）

    cfg.bridge.self_id = _PLACEHOLDER_QQ          # setup.bat 写回；napcat 未登录前无 onebot11 文件
    cfg.bridge.token = ""                          # ensure_bridge_token 生成并落盘
    cfg.bridge.napcat_config_dir = "local/napcat_config"

    # chat：端点给常见默认形态，模型与 Key 均为占位（setup 写回时换真实值）
    cfg.chat.api_base = "https://ark.cn-beijing.volces.com/api/v3"
    cfg.chat.api_key = PENDING_CHAT_KEY
    cfg.chat.model = "<具体模型名，如 doubao-seed-2-0-lite-260215>"

    # embedding：记忆功能的另一半 Key（setup 写回；未填真实 Key 前检索降级不注入，
    # 聊天不受影响——SPEC §7 降级语义）
    cfg.embedding.api_base = "https://ark.cn-beijing.volces.com/api/v3"
    cfg.embedding.api_key = PENDING_EMBEDDING_KEY
    cfg.embedding.model = "PENDING_SETUP_EMBEDDING_MODEL"
    cfg.embedding.style = "multimodal_single"     # 迁移同款启发式默认（多模态嵌入端点；R6 中性化，原 ark_multimodal）

    cfg.persona.name = persona_name
    cfg.persona.system_prompt = _DEFAULT_SYSTEM_PROMPT
    cfg.persona.begin_dialogs = list(_DEFAULT_BEGIN_DIALOGS)

    # 可选组件初始关闭：语音（GSVI 语音包需另行下载）与表情包（图库需自行放入），
    # 均可在控制台开启——setup 收尾只留三件事（填 Key / 扫码 / 可选语音）
    cfg.tts.enabled = False
    cfg.memes.enable = False
    cfg.proactive.enable = False                  # 主动消息等首次聊天拿到真实 session 后再开
    # napcat_backup_dir 不再写入（五轮修缮 R5：VlConfig 已删该死键，备份落点自管）
    return cfg


def run_init(root: "str | Path" = ".", *, force: bool = False) -> int:
    """在运行根生成 local/config/vl.toml（已存在拒绝覆盖，--force 才重写）。

    返回进程退出码：0=成功（init 后 --smoke 就绪）、1=vl.toml 已存在（不覆盖）。
    """
    root = Path(root)
    config_dir = root / "local" / "config"
    vl_toml = config_dir / "vl.toml"
    if vl_toml.is_file() and not force:
        print(f"[init] 已存在 {vl_toml}——拒绝覆盖（保留现有配置）。\n"
              "       如确需重置：先备份后删除该文件，或加 --force-init（会覆盖！）。")
        return 1

    cfg = build_fresh_config()
    config_dir.mkdir(parents=True, exist_ok=True)
    vl_toml.write_text(dump_config(cfg), encoding="utf-8")
    # 桥 token：复用 ensure_bridge_token（SPEC §3.3 强随机 token_urlsafe(32)）
    token = ensure_bridge_token(cfg, vl_toml)
    assert len(token) >= 32  # 防呆：生成器契约

    # 校验往返：init 产物必须能被 load_config 正常加载（--smoke 就绪的前提）
    load_config(vl_toml)

    print(f"[init] 全新安装配置已生成：{vl_toml}")
    print(f"[init] 桥 token 已生成落盘（长度 {len(token)}，不回显）。")
    print("[init] 接下来（也可运行 setup.bat 逐步引导）：")
    print("  1. 把大模型 API Key 填入控制台 http://127.0.0.1:8787（或 setup.bat 交互收集）")
    print(f"  2. 用机器人 QQ 号登录 napcat（WebUI 经控制台代理扫码；直连备查 http://127.0.0.1:61990），")
    print("     并把 QQ 号写回 vl.toml 的 session_id/admins/whitelist/bridge.self_id")
    print("     （占位 10000；或重跑 setup.bat 写回，主人另号也一并补上）")
    print("  3. 可选语音：下载 GSVI 语音包后在控制台语音页开启")
    print("[init] 验证：python -m virtuallover --smoke（就绪即优雅退出）")
    return 0


def main(argv: "Optional[list[str]]" = None) -> int:
    """python -m virtuallover.tools.init_config 独立入口（等价 --init-config）。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="全新安装初始化：在运行根生成可启动的 local/config/vl.toml（O12）")
    parser.add_argument("--root", default=".", help="运行根（默认当前目录；部署时为仓库根）")
    parser.add_argument("--force-init", action="store_true",
                        help="已存在 vl.toml 时也重写（危险：先备份）")
    args = parser.parse_args(argv)
    return run_init(args.root, force=args.force_init)


if __name__ == "__main__":
    sys.exit(main())
