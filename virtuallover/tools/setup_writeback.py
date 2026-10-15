# -*- coding: utf-8 -*-
"""setup.bat v2 调用的交互收集值写回（O12，PERF_SPEC §7.1-7 ①）+ 全新安装闭环。

setup.bat 交互收集的值经环境变量传入本脚本（不走命令行参数——Key 含特殊字符
时免转义、不进 cmd 历史），写回运行根：

- ``VL_SETUP_API_KEY``  → [chat].api_key 与 [embedding].api_key（同 Key 场景；
  空值跳过不改）；
- ``VL_SETUP_QQ``       → 机器人小号 QQ：直写 ``bridge.self_id``（她的账号
  语义）；会话侧（session_id 尾段 / admins / whitelist）按主人语义写（见下）；
- ``VL_SETUP_OWNER_QQ`` → 主人常用 QQ（可选；setup.bat 第 1 步交互收集，
  回车跳过）。提供且与机器人号不同时，按生产 vl.toml 实测结构写：
  session_id 尾段 = 主人 QQ、admins = [主人]，whitelist = 主人+机器人两号
  都加白（宽松模式——门禁 = whitelist ∪ admins，宁多加白不少挡人）。
  未提供时降级为单号宽松模式：三处会话侧全部用机器人 QQ（单号自测场景），
  并打印提示——主人用另一个 QQ 聊天时，桥侧门禁与会话归属都按号段匹配，
  须在 vl.toml 补改或设本变量重跑写回，否则主人说话她收不到；
- whitelist 修复（五轮修缮 R4 审计②）：init 骨架 whitelist=["10000"] 占位，
  修前写回漏写 whitelist → 占位残留 + admins 只写了机器人号 → 门禁把真主人
  挡在外，主人说话她永远沉默。现 QQ 写回必整体重写 whitelist，占位一并清除；
- ``VL_SETUP_PROVIDER`` → chat 服务商预设（ark / deepseek / custom）：前两者
  直接写 api_base+model 预设；custom 且 Key 以 ``ark-`` 开头时按前缀自动猜回
  方舟预设，否则用 ``VL_SETUP_API_BASE``/``VL_SETUP_MODEL`` 自定义输入；
- ``VL_SETUP_EMBEDDING``→ embedding 单独问法：``skip`` = 记忆功能暂闭（写
  [embedding] enabled=false，vl 侧记忆装配整体跳过、聊天不报错）；``ark`` /
  ``custom`` = 写 embedding 端点与模型（方舟 ep- 接入点自动配多模态端点风格）；
- ``VL_SETUP_ROOT``     → 运行根（默认当前目录）。

napcat 反向 WS 闭环（全新装机最后一环）：写 vl.toml 的同时检查
``local/napcat_config/onebot11_*.json``——一个都没有（全新装）就按机器人 QQ
代写 ``onebot11_<机器人QQ>.json``（url=ws://host.docker.internal:6199/ws +
[bridge] token）；已有文件（重装残留）则复用 config_migrate.switch_napcat_target
改写指向（写前自动备份）。协议端容器重启后生效（setup.bat 随后自动定点重启）。

vl.toml 是生成物：整体重写不保留注释（与 set_bridge_token 同口径）。
退出码：0=成功（或无可写项），1=失败（配置缺失/校验不过/QQ 非法/embedding
选了配置但缺模型名）。

安全规约：Key/token 只打长度不回显；QQ 打码打印；真值只落 local/（gitignored）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from pathlib import Path

from virtuallover.config import dump_config, load_config

_QQ_RE = re.compile(r"^\d{5,12}$")  # QQ 号现网口径 5-12 位纯数字

# chat 服务商预设（api_base + 默认模型；型号会随各家上新漂移，写回后可随时改）
_PROVIDER_PRESETS: dict = {
    "ark": {
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-1-6-flash-250828",
    },
    "deepseek": {
        "api_base": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
}
_ARK_KEY_PREFIX = "ark-"  # 方舟 Key 前缀：选 custom 时凭它自动猜回方舟预设
_EMBED_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"


def _mask(qq: str) -> str:
    return qq[:2] + "***" + qq[-2:] if len(qq) >= 6 else "***"


# ---------------------------------------------------------------------------
# napcat 反向 WS（任务：全新安装闭环）
# ---------------------------------------------------------------------------

def _onebot11_template(url: str, token: str, name: str) -> dict:
    """NapCat onebot11_<QQ>.json 骨架：字段集对齐现网真实文件（network 六类 +
    timeout 等），只有 websocketClients 一项是必真——反向 WS 指 vl 桥。

    现网文件由 NapCat 自身生成，字段随版本演进；这里只给"容器一起就能反连"的
    最小启动形态，登录后 NapCat 会照常读写该文件。
    """
    return {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [
                {
                    "enable": True,
                    "name": name,
                    "url": url,
                    "reportSelfMessage": False,
                    "messagePostFormat": "array",
                    "token": token,
                    "debug": False,
                    "heartInterval": 5000,
                    "reconnectInterval": 5000,
                }
            ],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
        "timeout": {
            "baseTimeout": 10000,
            "uploadSpeedKBps": 256,
            "downloadSpeedKBps": 256,
            "maxTimeout": 1800000,
        },
    }


def ensure_napcat_reverse_ws(root: "str | Path", qq: str, token: str,
                             name: str = "virtuallover") -> str:
    """napcat 端 onebot11_*.json 的全新安装闭环：缺则代写，有则改写指向。

    - 一个 onebot11_*.json 都没有（全新装机）→ 代写 ``onebot11_<QQ>.json``：
      url=ws://host.docker.internal:6199/ws、token=vl.toml [bridge].token——
      否则她扫码上线后没人告诉 napcat 往哪反连，QQ 永远哑；
    - 文件已存在（重装/旧栈残留）→ 复用 config_migrate.switch_napcat_target
      改写 url/token（写前自动备份 *.bak-<时间戳>），与控制台"指向切换"同码。

    返回动作说明字符串（供打印；token 只记长度）。生效需协议端容器重启。
    """
    from virtuallover.tools.config_migrate import DEFAULT_NAPCAT_URL, switch_napcat_target

    napcat_dir = Path(root) / "local" / "napcat_config"
    existing = sorted(napcat_dir.glob("onebot11_*.json"))
    if existing:
        changed = switch_napcat_target(napcat_dir, token)
        if changed:
            return ("协议端反向 WS 指向已改写 " + "、".join(p.name for p in changed)
                    + f"（→ {DEFAULT_NAPCAT_URL}，原文件已备份）")
        return ("协议端 onebot11 文件已存在但无 websocketClients 可改写，跳过"
                "（如需反连请在 NapCat WebUI 网络配置里手动添加）")
    napcat_dir.mkdir(parents=True, exist_ok=True)
    target = napcat_dir / f"onebot11_{qq}.json"
    data = _onebot11_template(DEFAULT_NAPCAT_URL, token, name)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return (f"协议端反向 WS 配置已代写：{target.name}"
            f"（url → {DEFAULT_NAPCAT_URL}，token 长度 {len(token)}，不回显）")


# ---------------------------------------------------------------------------
# 写回主体
# ---------------------------------------------------------------------------

def writeback(root: "str | Path" = ".",
              api_key: str | None = None, qq: str | None = None,
              owner_qq: str | None = None,
              provider: str | None = None,
              api_base: str | None = None, model: str | None = None,
              embedding: str | None = None,
              embed_base: str | None = None, embed_model: str | None = None,
              embed_key: str | None = None,
              embed_dim: str | None = None) -> int:
    root = Path(root)
    vl_toml = root / "local" / "config" / "vl.toml"
    if not vl_toml.is_file():
        print(f"[writeback] 找不到 {vl_toml}——请先跑 --init-config", file=sys.stderr)
        return 1
    cfg = load_config(vl_toml)  # 校验失败原样抛 ConfigError → 退出码 1

    changed: list[str] = []
    if api_key:
        cfg.chat.api_key = api_key
        cfg.embedding.api_key = api_key  # 同 Key 写入两侧；不同服务商见下方覆盖逻辑
        changed.append(f"chat/embedding api_key（长度 {len(api_key)}，不回显）")
    if qq or owner_qq:
        # R4 两套语义分开：qq=机器人小号（bridge.self_id），owner_qq=主人常用号
        # （session_id 尾段 / admins；生产 vl.toml 实测结构：session 尾段与 admins
        # 同源为主人号，self_id 是机器人号，两者不同）。setup.bat 只收机器人号，
        # 主人号缺省时按宽松模式用机器人号兜底（单号自测可通），并提示补配。
        if not qq:
            print("[writeback] 写回中止：提供了主人 QQ 但缺机器人 QQ（VL_SETUP_QQ）——"
                  "bridge.self_id 与反向 WS 代写都靠它", file=sys.stderr)
            return 1
        bot_qq = qq.strip()
        owner = (owner_qq or "").strip() or bot_qq
        for label, value in (("VL_SETUP_QQ", bot_qq), ("VL_SETUP_OWNER_QQ", owner)):
            if not _QQ_RE.fullmatch(value):
                print(f"[writeback] {label} 格式非法（应为 5-12 位数字）：{_mask(value)}",
                      file=sys.stderr)
                return 1
        persona_name = cfg.session_id.split(":", 1)[0] if cfg.session_id else "小语"
        cfg.session_id = f"{persona_name}:FriendMessage:{owner}"
        cfg.admins = [owner]
        # R4 审计②：whitelist 必写（整体重写）——init 骨架的占位 10000 残留会把
        # 真主人挡在门禁外（桥侧门禁 = whitelist ∪ admins，残留即「主人说话她
        # 永远沉默」）。宽松模式：主人 + 机器人两号都加白。
        cfg.whitelist = [owner] if owner == bot_qq else [owner, bot_qq]
        cfg.bridge.self_id = bot_qq
        if owner == bot_qq:
            changed.append(f"session 尾段/admins/whitelist ← QQ {_mask(bot_qq)}"
                           "（宽松模式：未单独提供主人 QQ），bridge.self_id ← 同号")
            print("[writeback] 提示：主人 QQ 未单独提供（VL_SETUP_OWNER_QQ），已按"
                  "宽松模式把机器人 QQ 同时当主人加白。主人若用另一个 QQ 聊天，请在 "
                  "vl.toml 把 session_id 尾段/admins/whitelist 补上主人 QQ 后重启",
                  file=sys.stderr)
        else:
            changed.append(f"session 尾段/admins ← 主人 QQ {_mask(owner)}，"
                           f"whitelist ← 主人+机器人两号加白，"
                           f"bridge.self_id ← 机器人 QQ {_mask(bot_qq)}")

    # ---- chat 服务商预设（选择优先，Key 前缀兜底猜测）----
    if provider:
        provider = provider.strip().lower()
        preset = _PROVIDER_PRESETS.get(provider)
        if preset:
            cfg.chat.api_base = preset["api_base"]
            cfg.chat.model = preset["model"]
            if api_key and provider == "ark" and not api_key.startswith(_ARK_KEY_PREFIX):
                print("[writeback] 提示：方舟 Key 一般以 ark- 开头，当前不像——"
                      "请在 vl.toml [chat] 或控制台核对", file=sys.stderr)
            changed.append(f"chat api_base/model ← {provider} 预设")
        elif provider == "custom":
            if api_key and api_key.startswith(_ARK_KEY_PREFIX):
                preset = _PROVIDER_PRESETS["ark"]  # Key 前缀自动猜测
                cfg.chat.api_base = preset["api_base"]
                cfg.chat.model = preset["model"]
                changed.append("chat api_base/model ← 方舟预设（凭 Key 的 ark- 前缀自动猜回）")
            elif api_base and model:
                cfg.chat.api_base = api_base.strip()
                cfg.chat.model = model.strip()
                changed.append("chat api_base/model ← 自定义输入")
            else:
                print("[writeback] 提示：选了其他服务商但未提供 api_base/模型名，"
                      "沿用 init 模板默认（可随时改 vl.toml [chat]）", file=sys.stderr)
        else:
            print(f"[writeback] 提示：未知服务商 {provider!r}，chat 端点保持 init 模板默认",
                  file=sys.stderr)

    # ---- embedding 单独问法（skip = 记忆暂闭，vl 侧降级不报错）----
    if embedding:
        embedding = embedding.strip().lower()
        if embedding == "skip":
            cfg.embedding.enabled = False
            cfg.embedding.api_base = ""
            cfg.embedding.api_key = ""
            cfg.embedding.model = ""
            changed.append("[embedding] enabled=false（记忆功能暂闭；聊天/日记不受影响，"
                           "补配后重启即恢复）")
        else:
            cfg.embedding.enabled = True
            if embedding == "ark":
                if not embed_model:
                    print("[writeback] 写回中止：选了配置 embedding 但没给模型名"
                          "（方舟 embedding 形如 ep-2024xxxxxxxx 或 doubao-embedding-*）",
                          file=sys.stderr)
                    return 1
                cfg.embedding.api_base = _EMBED_ARK_BASE
            elif embedding == "custom":
                if not (embed_base and embed_model):
                    print("[writeback] 写回中止：自定义 embedding 需要同时提供 "
                          "api_base 与模型名", file=sys.stderr)
                    return 1
                cfg.embedding.api_base = embed_base.strip()
            else:
                print(f"[writeback] 提示：未知 embedding 选项 {embedding!r}，忽略",
                      file=sys.stderr)
                embedding = None
            if embedding:
                cfg.embedding.model = embed_model.strip()
                # 端点风格显式定夺（init 模板默认 multimodal_single，按本次选择纠正）：
                # 方舟 ep- 接入点 → 多模态嵌入端点（与 config_migrate 迁移同款启发式，
                # 实测标准 /embeddings 会 400）；方舟直连模型名（doubao-embedding-*）
                # 与其他 OpenAI 兼容端点 → 标准 /embeddings
                if "volces.com" in cfg.embedding.api_base and cfg.embedding.model.startswith("ep-"):
                    cfg.embedding.style = "multimodal_single"   # R6 中性化（原 ark_multimodal，语义不变）
                else:
                    cfg.embedding.style = "openai"
                key_src = embed_key or api_key
                if key_src:
                    cfg.embedding.api_key = key_src
                if embed_dim:
                    try:
                        cfg.embedding.dim = int(embed_dim)
                    except ValueError:
                        print("[writeback] 提示：向量维度不是数字，保持默认 2048",
                              file=sys.stderr)
                changed.append(f"embedding 端点/模型（模型名长度 {len(cfg.embedding.model)}，"
                               f"Key {'单独提供' if embed_key else '沿用 chat Key'}）")
                if embed_key is None and api_key and not api_key.startswith(_ARK_KEY_PREFIX):
                    print("[writeback] 提示：embedding Key 暂沿用 chat Key——若 chat 不是"
                          "方舟账号（如 DeepSeek，其无 embedding 接口），请把方舟 Key 填入 "
                          "vl.toml [embedding] api_key，否则记忆检索会 401", file=sys.stderr)

    # ---- napcat 反向 WS 闭环（token 缺失则生成并一并写回 vl.toml）----
    napcat_note: str | None = None
    if qq:
        if not cfg.bridge.token:
            cfg.bridge.token = secrets.token_urlsafe(32)
            changed.append("bridge.token（新生成，长度 43，不回显）")
        napcat_note = ensure_napcat_reverse_ws(
            root, qq, cfg.bridge.token, name=cfg.persona.name or "virtuallover")

    # ---- 本地大脑 --api-key（R7，FUSION_SPEC §3.3）：本次确有写回（全新安装
    # 必带 Key/QQ）且现值缺失/占位 "local"/过短 → 强生成一并写回——setup 一次
    # 到位，brain_switch.ensure_local_api_key 拉起前兜底同口径（幂等：强 key
    # 原样保留）。挂在 changed 非空之后：空参调用保持「无可写项=配置原样」契约
    if changed and (not cfg.chat.fallback_api_key
                    or cfg.chat.fallback_api_key == "local"
                    or len(cfg.chat.fallback_api_key) < 24):
        cfg.chat.fallback_api_key = secrets.token_urlsafe(32)
        changed.append("chat.fallback_api_key（新生成，长度 43，不回显——"
                       "本地大脑 --api-key，brain_switch 拉起时同源携带）")

    if not changed:
        print("[writeback] 无可写项（环境变量未提供 Key/QQ），配置原样。")
        return 0

    vl_toml.write_text(dump_config(cfg), encoding="utf-8")
    load_config(vl_toml)  # 写回后校验往返（--smoke 就绪前提）
    for item in changed:
        print(f"[writeback] 已写回 {item}")
    if napcat_note:
        print(f"[writeback] {napcat_note}")
        print("[writeback] 协议端容器重启后生效（setup.bat 随后自动重启；"
              "手动：docker compose restart heart）")
    return 0


def main() -> int:
    return writeback(
        os.environ.get("VL_SETUP_ROOT") or ".",
        api_key=os.environ.get("VL_SETUP_API_KEY") or None,
        qq=os.environ.get("VL_SETUP_QQ") or None,
        owner_qq=os.environ.get("VL_SETUP_OWNER_QQ") or None,
        provider=os.environ.get("VL_SETUP_PROVIDER") or None,
        api_base=os.environ.get("VL_SETUP_API_BASE") or None,
        model=os.environ.get("VL_SETUP_MODEL") or None,
        embedding=os.environ.get("VL_SETUP_EMBEDDING") or None,
        embed_base=os.environ.get("VL_SETUP_EMBED_BASE") or None,
        embed_model=os.environ.get("VL_SETUP_EMBED_MODEL") or None,
        embed_key=os.environ.get("VL_SETUP_EMBED_KEY") or None,
        embed_dim=os.environ.get("VL_SETUP_EMBED_DIM") or None,
    )


if __name__ == "__main__":
    sys.exit(main())
