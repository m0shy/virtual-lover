# -*- coding: utf-8 -*-
"""一次性配置迁移：AstrBot 旧栈 → vl.toml（SPEC §11.1，离线执行）。

输入（全部位于 --root，默认 local/）：
- astrbot_data/cmd_config.json（utf-8-sig，实测带 BOM）
- astrbot_data/config/ 下四个插件配置：vl_mood_engine / proactive_chat / vl_diary
  / mnemosyne
- astrbot_data/data_v4.db：personas（人格全文+开场白）、conversations（session_id
  模板实值 `<人格名>:FriendMessage:<QQ号>`）
- napcat_config/onebot11_*.json：文件名 glob 动态发现（禁硬编码 QQ 号）

输出：
- {root}/config/vl.toml —— key 原文迁入 local/（gitignored），本脚本报告只记长度；
- napcat 端 onebot11_*.json 改写：url → ws://host.docker.internal:6199/ws、
  token → 新生成的 secrets.token_urlsafe(32)（禁沿用旧 13 字符弱 token，SPEC §3.3），
  写前逐文件备份为 *.bak-<时间戳>；
- {out 同目录}/migrate_report.json —— 结构化对账报告，字符串字段一律只记长度。

用法：
    python -m virtuallover.tools.config_migrate [--root local] [--dry-run]
                                                 [--napcat-url ws://host.docker.internal:6199/ws]
    python -m virtuallover.tools.config_migrate --rollback-napcat [--napcat-dir ...]
                                                 [--restart-napcat]
    # --rollback-napcat：回退模式（不迁移），把 onebot11_*.json 从最近的
    # *.bak-<时间戳> 恢复（napcat 指回旧栈）——workbench 退役后 vl console 之外的
    # 独立回退入口，与 docs/回退.md 的 SOP 配套（O11 前提：runbook 成文后才能删 workbench）。

安全规约：QQ 号/token/API key 不进入本文件源码、报告与日志；旧栈弱 token 一律
不迁移（bridge.token 全新生成）。
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

from virtuallover.config import (
    SCHEMA_VERSION,
    SegmentedReplyConfig,
    VlConfig,
    dump_config,
)

__all__ = [
    "migrate", "main",
    "DEFAULT_NAPCAT_URL",
    "switch_napcat_target", "rollback_napcat_target", "set_bridge_token",
    "rollback_napcat_cli",
]

DEFAULT_NAPCAT_URL = "ws://host.docker.internal:6199/ws"  # SPEC §3.3 终裁值
_NEW_TOKEN = "token_urlsafe(32)"  # 报告里的占位写法（不落真值）


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    """utf-8-sig 读 JSON（实测 cmd_config.json 带 BOM；其他文件同样兼容）。"""
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _first_str(seq: Any) -> str:
    """旧栈 key/列表字段取第一个字符串元素（如 provider_sources.key=['sk-…']）。"""
    if isinstance(seq, str):
        return seq
    if isinstance(seq, list):
        for item in seq:
            if isinstance(item, str) and item:
                return item
    return ""


def _to_float(v: Any, default: float) -> float:
    """旧栈部分数值以字符串存放（实测 proactive 的 log_base='2.6'）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class _Report:
    """迁移对账报告收集器：字符串只记长度，绝不落敏感原文。"""

    def __init__(self) -> None:
        self.entries: List[dict] = []

    def add(self, section: str, key: str, source: str, value: Any) -> None:
        if isinstance(value, str):
            shown: Any = {"kind": "str", "len": len(value)}
        elif isinstance(value, list):
            shown = {"kind": "list", "len": len(value)}
        elif isinstance(value, dict):
            shown = {"kind": "dict", "len": len(value)}
        else:
            shown = {"kind": type(value).__name__, "value": value}
        self.entries.append({"section": section, "key": key, "source": source, **shown})

    def dump(self) -> dict:
        return {"entries": self.entries}


# ---------------------------------------------------------------------------
# 各源读取与映射
# ---------------------------------------------------------------------------

def _pick_chat_provider(cmd: dict, report: _Report) -> tuple[str, str, str, int]:
    """从 cmd_config 选启用中的 chat provider → (api_base, api_key, model, timeout)。

    结构：provider[] 启用条目持 provider_source_id，指向 provider_sources[] 里的
    连接信息（api_base/timeout/key）。若无启用条目，回退到第一个 chat 型 source。

    注：旧栈 provider.max_context_tokens 不再迁移（PERF_SPEC §3-O5/§7.1-3：
    [chat].max_context_tokens 是死键；窗口上限由 [context].max_context_tokens
    承载，默认 32768，逃生档由用户显式配置，不静默沿用旧栈 256k）。
    """
    providers = cmd.get("provider") or []
    chosen = next((p for p in providers
                   if p.get("enable") and p.get("provider_source_id")), None)
    if chosen is None:
        chosen = next((p for p in providers if p.get("provider_source_id")), None)
    if chosen is None:
        raise SystemExit("迁移失败：cmd_config.json 的 provider 列表没有可用的 chat provider")
    sources = {s.get("id"): s for s in (cmd.get("provider_sources") or [])}
    src = sources.get(chosen.get("provider_source_id"))
    if src is None:
        raise SystemExit(
            f"迁移失败：provider 条目引用的 provider_source_id 在 provider_sources 中不存在")
    api_base = src.get("api_base", "")
    api_key = _first_str(src.get("key"))
    timeout = int(src.get("timeout") or 120)
    model = chosen.get("model", "")
    report.add("chat", "api_base", "cmd_config.provider_sources", api_base)
    report.add("chat", "api_key", "cmd_config.provider_sources.key[0]", api_key)
    report.add("chat", "model", "cmd_config.provider.model", model)
    report.add("chat", "timeout", "cmd_config.provider_sources.timeout", timeout)
    return api_base, api_key, model, timeout


def _pick_embedding(cmd: dict, report: _Report) -> tuple[str, str, str, int, int]:
    """从 cmd_config 选内嵌 embedding provider（含 embedding_api_key 的条目）。"""
    hit = next((p for p in (cmd.get("provider") or []) if p.get("embedding_api_key")), None)
    if hit is None:
        raise SystemExit("迁移失败：cmd_config.json 中没有 embedding provider 条目")
    vals = (hit.get("embedding_api_base", ""), _first_str(hit.get("embedding_api_key")),
            hit.get("embedding_model", ""), int(hit.get("embedding_dimensions") or 2048),
            int(hit.get("timeout") or 20))
    for key, v in zip(("api_base", "api_key", "model", "dim", "timeout"), vals):
        report.add("embedding", key, "cmd_config.provider(embedding 条目)", v)
    return vals


def _pick_tts(cmd: dict, report: _Report) -> dict:
    """TTS 参数：识别带 gsv_default_parms 的 provider 条目（GPT-SoVITS）。

    api_base 若带了 /tts 后缀则剥掉（新客户端统一 拼 {base}/tts）。
    """
    settings = cmd.get("provider_tts_settings") or {}
    hit = next((p for p in (cmd.get("provider") or [])
                if "gsv_default_parms" in p), None)
    base = ""
    character = emotion = text_lang = ""
    timeout = 60
    if hit is not None:
        base = (hit.get("api_base") or "").rstrip("/")
        if base.endswith("/tts"):  # 旧值形如 http://127.0.0.1:5000/tts，归一为 base
            base = base[: -len("/tts")]
        parms = hit.get("gsv_default_parms") or {}
        character = parms.get("gsv_character", "")
        emotion = parms.get("gsv_emotion", "")
        text_lang = parms.get("gsv_text_lang", "") or "zh"
        timeout = int(hit.get("timeout") or 60)
    out = {
        "enabled": bool(settings.get("enable", True)),
        "base_url": base or "http://127.0.0.1:5000",  # SPEC §9 默认宿主机直连
        "character": character,
        "emotion": emotion,
        "text_lang": text_lang,
        "timeout": timeout,
        # always_send_text（旧栈 dual_output）不再迁入 [tts]：R3 终裁主聊天恒为
        # 「文字先发、语音随后附加」双轨，[tts] 侧该键是死键（R5 删除，见
        # config.py TtsConfig 注释）；主动消息侧真开关 [proactive].always_send_text
        # 由 _proactive_from 从 friend_settings.tts_settings 迁移。
    }
    for k, v in out.items():
        report.add("tts", k, "cmd_config(provider_tts/gsv 条目)", v)
    return out


def _read_persona(db_path: Path, report: _Report) -> tuple[str, str, List[str]]:
    """personas 表第一行 → (persona_id, system_prompt, begin_dialogs)。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT persona_id, system_prompt, begin_dialogs FROM personas "
            "ORDER BY id LIMIT 1").fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit("迁移失败：data_v4.db 的 personas 表为空，无法迁移人格全文")
    name, prompt, raw_begin = row[0] or "", row[1] or "", row[2]
    try:
        parsed = json.loads(raw_begin) if raw_begin else []
        begin = [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        raise SystemExit("迁移失败：personas.begin_dialogs 不是合法 JSON 数组")
    report.add("persona", "name", "data_v4.db.personas.persona_id", name)
    report.add("persona", "system_prompt", "data_v4.db.personas.system_prompt", prompt)
    report.add("persona", "begin_dialogs", "data_v4.db.personas.begin_dialogs", begin)
    return name, prompt, begin


def _read_session_id(db_path: Path, report: _Report) -> str:
    """conversations.user_id 原文 = unified_msg_origin 实值（session_id 模板）。

    真库主键列为 inner_conversation_id（实测 DDL），排序键用它而非想当然的 id。
    """
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT user_id FROM conversations ORDER BY inner_conversation_id "
            "LIMIT 1").fetchone()
    finally:
        con.close()
    if row is None or not row[0]:
        raise SystemExit("迁移失败：data_v4.db 的 conversations 表为空，无法确定 session_id 模板")
    report.add("session_id", "session_id", "data_v4.db.conversations.user_id", row[0])
    return row[0]


def _segmented_from(raw: dict, report: _Report, section: str) -> SegmentedReplyConfig:
    """旧栈 segmented_reply dict → SegmentedReplyConfig（缺项走默认值）。

    words_count_threshold 迁移即归一（PERF_SPEC §2 O2a 裁决 100→30，与
    load_config 的存量归一化同语义）：旧默认 100 落盘前改写 30，迁移产物
    dump→load 往返稳定，不再依赖加载期归一化。
    """
    raw = raw or {}
    threshold = int(raw.get("words_count_threshold") or 30)
    if threshold == 100:  # 旧栈默认值：O2a 裁决迁移即归一
        threshold = 30
    seg = SegmentedReplyConfig(
        enable=bool(raw.get("enable", True)),
        words_count_threshold=threshold,
        split_mode=str(raw.get("split_mode") or "regex"),
        regex=str(raw.get("regex") or ""),
        split_words=[str(x) for x in (raw.get("split_words") or [])],
        interval_method=str(raw.get("interval_method") or "log"),
        interval=str(raw.get("interval") or ""),
        log_base=_to_float(raw.get("log_base"), 2.6),
    )
    for f in ("enable", "words_count_threshold", "split_mode", "regex",
              "split_words", "interval_method", "interval", "log_base"):
        report.add(section, f, "旧栈 segmented_reply", getattr(seg, f))
    return seg


def _proactive_from(path: Path, report: _Report) -> dict:
    """proactive_chat 插件配置 friend_settings → [proactive] 各字段。

    群聊路径已砍（SPEC §1），group_settings 一律不迁。
    """
    data = _read_json(path)
    friend = (data.get("friend_settings") or {})
    schedule = friend.get("schedule_settings") or {}
    tts = friend.get("tts_settings") or {}
    seg_raw = friend.get("segmented_reply_settings") or {}
    auto = friend.get("auto_trigger_settings") or {}
    session_list = [s for s in (friend.get("session_list") or []) if isinstance(s, str) and s]
    out = {
        "enable": bool(friend.get("enable", False)) and bool(session_list),
        "session_id": session_list[0] if session_list else "",
        "min_interval_minutes": int(schedule.get("min_interval_minutes") or 30),
        "max_interval_minutes": int(schedule.get("max_interval_minutes") or 120),
        "quiet_hours": str(schedule.get("quiet_hours") or "23-7"),
        "max_unanswered": int(schedule.get("max_unanswered_times") or 3),
        "boot_trigger_minutes": int(auto.get("auto_trigger_after_minutes") or 30),
        "prompt_template": str(friend.get("proactive_prompt") or ""),
        "tts_enabled": bool(tts.get("enable_tts", True)),
        "always_send_text": bool(tts.get("always_send_text", True)),
        "segmented": _segmented_from(seg_raw, report, "proactive.segmented"),
    }
    for k, v in out.items():
        if k != "segmented":
            report.add("proactive", k, "astrbot_plugin_proactive_chat_config", v)
    return out


def _bridge_from(cmd: dict, napcat_dir: Path, report: _Report) -> dict:
    """platform 段 + onebot11 文件名 → [bridge]。host 终裁 127.0.0.1（不迁旧值）。"""
    plat = next((p for p in (cmd.get("platform") or [])
                 if p.get("type") == "aiocqhttp" and p.get("enable")), None)
    if plat is None:
        raise SystemExit("迁移失败：cmd_config.json 没有启用的 aiocqhttp 平台条目")
    self_id = ""
    onebot_files = sorted(napcat_dir.glob("onebot11_*.json"))
    if onebot_files:
        m = re.fullmatch(r"onebot11_(\d+)\.json", onebot_files[0].name)
        self_id = m.group(1) if m else ""
    out = {
        "host": "127.0.0.1",  # 终裁：零局域网暴露（旧栈 0.0.0.0 不迁移）
        "port": int(plat.get("ws_reverse_port") or 6199),
        "path": "/ws",
        # token 全新生成（迁移函数主流程填入），旧 13 字符弱 token 禁止沿用
        "token": "",
        "self_id": self_id,
        "napcat_config_dir": "",  # 由 main 按 root 换算，避免硬编码绝对路径
    }
    report.add("bridge", "port", "cmd_config.platform", out["port"])
    report.add("bridge", "self_id", "napcat onebot11_*.json 文件名", self_id)
    report.add("bridge", "token", "新生成", _NEW_TOKEN)
    return out


# ---------------------------------------------------------------------------
# napcat onebot11 改写
# ---------------------------------------------------------------------------

def _rewrite_napcat(napcat_dir: Path, new_token: str, napcat_url: str,
                    dry_run: bool, report: _Report, port: int, path: str) -> List[Path]:
    """改写 onebot11_*.json 的 url/token；写前备份；返回被改写文件清单。"""
    changed: List[Path] = []
    for f in sorted(napcat_dir.glob("onebot11_*.json")):
        data = _read_json(f)
        clients = ((data.get("network") or {}).get("websocketClients")) or []
        if not clients:
            report.add("napcat", f.name, "onebot11（无 websocketClients，跳过）", [])
            continue
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = f.with_name(f.name + f".bak-{stamp}")
        for c in clients:
            old_url = str(c.get("url", ""))
            c["url"] = napcat_url
            c["token"] = new_token
            report.add("napcat", f"{f.name}.url", f"旧值长度 {len(old_url)}", napcat_url)
            report.add("napcat", f"{f.name}.token", "新生成（旧值不迁移）", _NEW_TOKEN)
        if dry_run:
            continue
        f.with_name(backup.name).write_bytes(f.read_bytes())  # 写前备份原文件
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        changed.append(f)
    return changed


# ---------------------------------------------------------------------------
# 主迁移流程
# ---------------------------------------------------------------------------

def migrate(root: str | Path = "local", out: str | Path | None = None,
            napcat_url: str = DEFAULT_NAPCAT_URL, dry_run: bool = False,
            report_path: str | Path | None = None,
            skip_napcat: bool = False) -> VlConfig:
    """执行迁移，返回构建好的 VlConfig（dry-run 时不写任何文件）。

    skip_napcat=True 时只生成 vl.toml 不动 napcat 端文件——迁移与切换是两个
    时刻（SPEC §11.1 vs M4），切换由控制台"指向切换"动作同步 url/token。
    """
    root = Path(root)
    data_dir = root / "astrbot_data"
    cmd_path = data_dir / "cmd_config.json"
    if not cmd_path.is_file():
        raise SystemExit(f"迁移失败：找不到 {cmd_path}（旧栈配置不存在，无需迁移？）")
    cmd = _read_json(cmd_path)
    report = _Report()

    cfg = VlConfig(schema_version=SCHEMA_VERSION)
    # [chat].max_context_tokens 死键不再迁移（O5/§7.1-3）：context.max_context_tokens
    # 保持 dataclass 默认 32768，逃生档由用户显式改配，绝不静默沿用旧栈 256k
    (cfg.chat.api_base, cfg.chat.api_key, cfg.chat.model,
     cfg.chat.timeout) = _pick_chat_provider(cmd, report)
    (cfg.embedding.api_base, cfg.embedding.api_key, cfg.embedding.model,
     cfg.embedding.dim, cfg.embedding.timeout) = _pick_embedding(cmd, report)
    # 火山方舟 + ep- 接入点：实测可能指向多模态嵌入模型（标准 /embeddings 会 400
    # "does not support this api"），改走 /embeddings/multimodal（纯文本可用）。
    # 账号若开通了标准文本嵌入接入点，改回 style="openai" 即可。
    if "volces.com" in cfg.embedding.api_base and cfg.embedding.model.startswith("ep-"):
        cfg.embedding.style = "multimodal_single"   # R6 中性化（原 ark_multimodal，llm.py 同源映射）
        report.add("embedding", "style", "多模态端点启发式（方舟 + ep- 接入点）",
                   "multimodal_single（多模态端点，纯文本可用）")

    tts = _pick_tts(cmd, report)
    cfg.tts.enabled = tts["enabled"]
    cfg.tts.base_url = tts["base_url"]
    cfg.tts.character = tts["character"]
    cfg.tts.emotion = tts["emotion"]
    cfg.tts.text_lang = tts["text_lang"]
    cfg.tts.timeout = tts["timeout"]

    cfg.persona.name, cfg.persona.system_prompt, cfg.persona.begin_dialogs = \
        _read_persona(data_dir / "data_v4.db", report)
    cfg.session_id = _read_session_id(data_dir / "data_v4.db", report)

    plat = cmd.get("platform_settings") or {}
    cfg.admins = [str(a) for a in (cmd.get("admins_id") or [])]
    cfg.whitelist = [str(w) for w in (plat.get("id_whitelist") or [])]
    # 旧栈 cmd_config.timezone 不再迁移（五轮修缮 R4：VlConfig 已删该死键——
    # diary/proactive 时刻基准从未读取它，一律取本机系统时区）
    cfg.log_level = str(cmd.get("log_level") or "INFO")
    for name, val in (("admins", cfg.admins), ("whitelist", cfg.whitelist),
                      ("log_level", cfg.log_level)):
        report.add("root", name, "cmd_config", val)

    cfg.reply.segmented = _segmented_from(plat.get("segmented_reply") or {}, report, "reply.segmented")
    rl = plat.get("rate_limit") or {}
    cfg.reply.rate_limit.window_seconds = int(rl.get("time") or 30)
    cfg.reply.rate_limit.max_count = int(rl.get("count") or 10)
    report.add("reply.rate_limit", "window_seconds/max_count", "cmd_config.platform_settings",
               [cfg.reply.rate_limit.window_seconds, cfg.reply.rate_limit.max_count])

    pro = _proactive_from(data_dir / "config" / "astrbot_plugin_proactive_chat_config.json", report)
    for key, val in pro.items():
        if key == "segmented":
            cfg.proactive.segmented = val
        else:
            setattr(cfg.proactive, key, val)

    mood = _read_json(data_dir / "config" / "vl_mood_engine_config.json")
    cfg.mood.keyword_groups = dict(mood.get("keyword_groups") or {})
    cfg.mood.daily_bonus_enabled = bool(mood.get("daily_bonus_enabled", True))
    cfg.mood.inject_enabled = bool(mood.get("inject_enabled", True))
    report.add("mood", "keyword_groups", "vl_mood_engine_config", cfg.mood.keyword_groups)
    report.add("mood", "daily_bonus_enabled/inject_enabled", "vl_mood_engine_config",
               [cfg.mood.daily_bonus_enabled, cfg.mood.inject_enabled])

    diary = _read_json(data_dir / "config" / "vl_diary_config.json")
    cfg.diary.diary_time = str(diary.get("diary_time") or "22:30")
    cfg.diary.min_chars = int(diary.get("min_chars") or 100)
    cfg.diary.max_chars = int(diary.get("max_chars") or 200)
    cfg.diary.max_history_messages = int(diary.get("max_history_messages") or 60)
    cfg.diary.max_excerpt_chars = int(diary.get("max_excerpt_chars") or 4000)
    cfg.diary.prompt_template = str(diary.get("diary_prompt_template") or "")
    for f in ("diary_time", "min_chars", "max_chars", "max_history_messages",
              "max_excerpt_chars", "prompt_template"):
        report.add("diary", f, "vl_diary_config", getattr(cfg.diary, f))

    mne = _read_json(data_dir / "config" / "astrbot_plugin_mnemosyne_config.json")
    cfg.mnemosyne.num_pairs = int(mne.get("num_pairs") or 2)
    # 旧栈 top_k=2 是 O5 前实测默认：迁移即归一为 3（PERF_SPEC §3-O5 检索兜底
    # 补丁，与 load_config 的存量归一化同语义），迁移产物 dump→load 往返稳定
    top_k = int(mne.get("top_k") or 2)
    if top_k == 2:
        top_k = 3
    cfg.mnemosyne.top_k = top_k
    cfg.mnemosyne.max_prompt_chars = int(mne.get("max_prompt_chars_for_embedding") or 4000)
    # injection_position 不再迁移（R5 删除）：SPEC §7 终裁注入位置固定 user_prompt
    # prepend（禁 system），旧键 memory_injection_method 读到也不落库。
    # time_anchor 映射已删除（五轮修缮 R3）：死配置不再迁移（见 config.py
    # MnemosyneConfig 注释）；旧键 use_summary_time_anchor 读到也不落库。
    for f in ("num_pairs", "top_k", "max_prompt_chars"):
        report.add("mnemosyne", f, "astrbot_plugin_mnemosyne_config", getattr(cfg.mnemosyne, f))

    napcat_dir = root / "napcat_config"
    bridge = _bridge_from(cmd, napcat_dir, report)
    cfg.bridge.host = bridge["host"]
    cfg.bridge.port = bridge["port"]
    cfg.bridge.path = bridge["path"]
    cfg.bridge.self_id = bridge["self_id"]
    cfg.bridge.napcat_config_dir = str(Path("local") / "napcat_config")
    cfg.bridge.token = secrets.token_urlsafe(32)  # 全新生成，禁沿用旧弱 token
    cfg.storage.data_dir = str(Path("local") / "vl_data")
    cfg.storage.history_db = str(Path("local") / "vl_data" / "history.db")
    cfg.storage.mirror_file = str(Path("local") / "vl_data" / "mirror_v4.json")
    # napcat_backup_dir 不再写入（R5 删除）：备份落点由各动作自管
    # （迁移/回退=同目录 .bak-<时间戳>，一键备份=scripts/backup.bat）。
    for f in ("data_dir", "history_db", "mirror_file"):
        report.add("storage", f, "默认值（SPEC §6/§2）", getattr(cfg.storage, f))

    out_path = Path(out) if out else root / "config" / "vl.toml"
    if not skip_napcat:
        _rewrite_napcat(napcat_dir, cfg.bridge.token, napcat_url,
                        dry_run=dry_run, report=report,
                        port=cfg.bridge.port, path=cfg.bridge.path)
    # 迁移产物先过一遍运行期归一化（PERF_SPEC §7.1-3，与 load_config 同一函数）：
    # 旧栈读出的 timeout/words_count_threshold 等直接以归一后口径落盘，
    # 保证「迁移产物 → load_config 往返幂等」（O2a 的 100→30 也在此生效）。
    from virtuallover.config import _normalize_perf

    _normalize_perf(cfg)
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(dump_config(cfg), encoding="utf-8")
        rp = Path(report_path) if report_path else out_path.parent / "migrate_report.json"
        rp.write_text(json.dumps(report.dump(), ensure_ascii=False, indent=2),
                      encoding="utf-8")
        print(f"迁移完成：{out_path}（schema_version={SCHEMA_VERSION}）")
        if skip_napcat:
            print("napcat 改写已跳过（--skip-napcat）：切换时刻由控制台指向切换动作同步 url/token")
        else:
            print("napcat 改写完成（原文件已备份为 *.bak-<时间戳>）")
        print(f"对账报告（敏感字段只记长度）：{rp}")
    else:
        print("[dry-run] 未写入任何文件；vl.toml 目标：", out_path)
        if skip_napcat:
            print("[dry-run] napcat 改写已跳过（--skip-napcat）")
        else:
            print("[dry-run] napcat onebot11_*.json 将改写 url/token（写前备份 *.bak-<时间戳>）")
    return cfg


def rollback_napcat_cli(napcat_dir: str | Path, restart: bool = False,
                        cwd: str | Path = ".") -> int:
    """--rollback-napcat 子命令主体：napcat 指回旧栈（workbench 退役后的官方回退入口）。

    背景（PERF_SPEC §4-O11 + §7.2）：workbench 整删后其"napcat_rollback 按钮随
    vl console 存续"，但 vl 进程死后（比如升级翻车起不来）控制台也没了——本 CLI
    是不依赖任何活进程的独立回退路径，与 docs/回退.md 的 SOP 互为印证。

    行为：对 <napcat_dir>（默认 local/napcat_config）下每个 onebot11_*.json 从
    最近的 *.bak-<时间戳> 恢复（rollback_napcat_target 的逐文件尽力语义）；可选
    --restart 时定点重启协议端容器（docker compose restart heart，仅本机
    docker 可用时）。返回进程退出码：0=有文件被恢复（或 --restart 成功），
    1=无任何备份可恢复（调用方据此知道"从未执行过指向切换"）。
    """
    restored, notes = rollback_napcat_target(napcat_dir)
    for p in restored:
        print(f"已恢复 {p.name}（协议端指回旧栈；文件内容与最近一次指向切换前一致）")
    for note in notes:
        print(f"[跳过] {note}")
    if not restored:
        print("回退中止：没有任何 onebot11_*.json 存在可用的 *.bak-* 备份——"
              "从未执行过指向切换，无需回退。")
        return 1
    if restart:
        import subprocess
        argv = ["docker", "compose", "restart", "heart"]
        try:
            completed = subprocess.run(argv, cwd=str(cwd), timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[警告] 协议端定点重启失败（{exc}）：请手动执行 "
                  f"docker compose restart heart，或改跑旧栈整组。")
            return 0  # 文件已恢复；重启失败不掩盖回退本体已完成
        print("协议端定点重启：" + ("完成" if completed.returncode == 0 else
                                  f"失败（退出码 {completed.returncode}），请 docker compose ps 核对"))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="一次性配置迁移：AstrBot 旧栈 → local/config/vl.toml（SPEC §11.1）")
    parser.add_argument("--root", default="local", help="旧栈数据根目录（默认 local/）")
    parser.add_argument("--out", default=None, help="vl.toml 输出路径（默认 <root>/config/vl.toml）")
    parser.add_argument("--napcat-url", default=DEFAULT_NAPCAT_URL,
                        help="onebot11 websocketClients 的新 url（默认容器视角地址）")
    parser.add_argument("--dry-run", action="store_true", help="只演算不写盘，报告照常打印")
    parser.add_argument("--skip-napcat", action="store_true",
                        help="只生成 vl.toml，不改写 napcat 端（切换时刻由控制台同步）")
    parser.add_argument("--rollback-napcat", action="store_true",
                        help="回退模式：不迁移，把 onebot11_*.json 从最近的 *.bak-* 备份恢复"
                             "（napcat 指回旧栈；workbench 退役后的独立回退入口，见 docs/回退.md）")
    parser.add_argument("--napcat-dir", default=None,
                        help="napcat 配置目录（默认 <root>/napcat_config；仅 --rollback-napcat 用）")
    parser.add_argument("--restart-napcat", action="store_true",
                        help="回退恢复文件后定点重启协议端容器（docker compose restart heart）")
    args = parser.parse_args(argv)
    if args.rollback_napcat:
        napcat_dir = args.napcat_dir or str(Path(args.root) / "napcat_config")
        return rollback_napcat_cli(napcat_dir, restart=args.restart_napcat, cwd=".")
    migrate(root=args.root, out=args.out, napcat_url=args.napcat_url, dry_run=args.dry_run,
            skip_napcat=args.skip_napcat)
    return 0


# ---------------------------------------------------------------------------
# 控制台"napcat 指向切换/回退"动作的公共改写函数（SPEC §10，console/control 调用）
# ---------------------------------------------------------------------------

def switch_napcat_target(napcat_dir: str | Path, new_token: str,
                         napcat_url: str = DEFAULT_NAPCAT_URL, *,
                         port: int = 6199, path: str = "/ws") -> List[Path]:
    """把 napcat 端 onebot11_*.json 指向新栈桥（url/token 改写 + 写前备份）。

    供管理控制台的"指向切换"动作调用（SPEC §10）：文件名动态发现（禁硬编码
    QQ 号），token 由调用方生成（secrets.token_urlsafe(32)）。返回被改写的
    文件清单；没有可改写的 websocketClients 时返回空列表。
    """
    report = _Report()
    return _rewrite_napcat(Path(napcat_dir), new_token, napcat_url,
                           dry_run=False, report=report, port=port, path=path)


def rollback_napcat_target(napcat_dir: str | Path) -> "tuple[List[Path], List[str]]":
    """回退动作：把 onebot11_*.json 从最近的 *.bak-<时间戳> 恢复（napcat 指回旧栈）。

    返回 (已恢复文件清单, 说明列表)；某文件没有备份时记入说明列表而不中断
    其余文件的恢复（M4 回退演练要求 ≤5 分钟，逐文件尽力恢复）。
    """
    root = Path(napcat_dir)
    restored: List[Path] = []
    notes: List[str] = []
    for f in sorted(root.glob("onebot11_*.json")):
        backups = sorted(root.glob(f.name + ".bak-*"))
        if not backups:
            notes.append(f"{f.name}: 无 *.bak-* 备份，跳过（从未执行过指向切换？）")
            continue
        latest = backups[-1]  # 时间戳后缀，字符串序即时间序
        f.write_bytes(latest.read_bytes())
        restored.append(f)
        report = _Report()
        report.add("napcat", f.name, f"回退自 {latest.name}", "<restored>")
    return restored, notes


def set_bridge_token(vl_toml_path: str | Path, token: str) -> bool:
    """把新桥 token 同步写回 vl.toml 的 [bridge]（供指向切换动作联动）。

    vl.toml 是迁移生成物，整体重写不保留注释（与 ensure_bridge_token 同一口径）。
    文件缺失返回 False；配置校验失败（如必填项缺失）原样抛 ConfigError，由
    调用方转成 HTTP 错误响应。
    """
    from virtuallover.config import dump_config, load_config

    p = Path(vl_toml_path)
    if not p.is_file():
        return False
    cfg = load_config(p)
    cfg.bridge.token = token
    p.write_text(dump_config(cfg), encoding="utf-8")
    return True


if __name__ == "__main__":
    sys.exit(main())
