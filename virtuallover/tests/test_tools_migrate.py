# -*- coding: utf-8 -*-
"""迁移三件套测试（骨架组职责，SPEC §11）：config_migrate + history_migrate。

用 tmp 目录构造完整的假旧栈（cmd_config / 四插件配置 / data_v4.db / napcat
onebot11_*.json），全部占位值（QQ=10000/999999999、key=sk-TEST-*），断言：
- config_migrate：vl.toml 字段映射、token 全新、napcat 改写+备份、dry-run 不落盘、
  报告不含密钥原文；
- history_migrate：120 条守恒、think 剥离（字符串+分片双形态）、system_reminder
  清除（残留=0）、双形态展开、&&tag&& 本体保留、created_at 留空、对账单 sha256。

运行：python -m pytest virtuallover/tests/test_tools_migrate.py -q
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from virtuallover.config import load_config
from virtuallover.tools.config_migrate import migrate as config_migrate
from virtuallover.tools.history_migrate import (
    clear_system_reminder,
    migrate as history_migrate,
    normalize_content,
    strip_think,
)

# 全部为编造占位值（硬规则 3：真实 QQ/key/token 不进测试）
_QQ = "10000"
_SELF_ID = "999999999"
_SESSION = f"测试人格:FriendMessage:{_QQ}"
_KEY = "sk-TEST-CHAT"
_EMB_KEY = "sk-TEST-EMB"
_OLD_WEAK_TOKEN = "test-old-token"  # 模拟旧栈 13 字符弱 token（假值）


# ---------------------------------------------------------------------------
# 假旧栈构造
# ---------------------------------------------------------------------------

def _fake_cmd_config() -> dict:
    """cmd_config.json 最小等价结构（字段名对齐真库实测，见骨架侦查记录）。"""
    return {
        "config_version": 3,
        "platform_settings": {
            "id_whitelist": [],
            "rate_limit": {"time": 30, "count": 10},
            "segmented_reply": {
                "enable": True, "words_count_threshold": 100, "split_mode": "regex",
                "regex": r"[。！？]", "split_words": ["…"], "interval_method": "log",
                "interval": "1.5-3.5", "log_base": 2.6,
            },
        },
        "provider_sources": [{
            "id": "src1", "provider": "openai", "type": "openai_chat_completion",
            "provider_type": "chat_completion", "key": [_KEY],
            "api_base": "https://api.test/v1", "timeout": 120, "proxy": "",
        }],
        "provider": [
            {"id": "chat1", "enable": True, "provider_source_id": "src1",
             "model": "test-model", "max_context_tokens": 256000},
            {"id": "emb1", "enable": True,
             "embedding_api_key": _EMB_KEY, "embedding_api_base": "https://emb.test/v1",
             "embedding_model": "emb-model", "embedding_dimensions": 2048, "timeout": 20},
            {"id": "tts1", "enable": True, "api_base": "http://127.0.0.1:5000/tts",
             "gsv_default_parms": {"gsv_character": "测试角色", "gsv_emotion": "平静",
                                   "gsv_text_lang": "zh"}, "timeout": 60},
        ],
        "provider_tts_settings": {"enable": True, "provider_id": "tts1",
                                  "dual_output": True, "trigger_probability": 1.0},
        "platform": [{"id": "QQ", "type": "aiocqhttp", "enable": True,
                      "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199,
                      "ws_reverse_token": _OLD_WEAK_TOKEN}],
        "admins_id": [_QQ],
        "timezone": "Asia/Shanghai",
        "log_level": "INFO",
    }


def _fake_plugin_configs() -> dict:
    """四个插件配置文件的最小等价结构。"""
    return {
        "astrbot_plugin_proactive_chat_config.json": {
            "friend_settings": {
                "enable": True, "session_list": [_SESSION],
                "auto_trigger_settings": {"enable_auto_trigger": True,
                                          "auto_trigger_after_minutes": 30},
                "proactive_prompt": "{{current_time}} 想你了（{{unanswered_count}}）",
                "schedule_settings": {"min_interval_minutes": 30,
                                      "max_interval_minutes": 120,
                                      "quiet_hours": "23-7", "max_unanswered_times": 3},
                "tts_settings": {"enable_tts": True, "always_send_text": False},
                "segmented_reply_settings": {
                    "enable": True, "words_count_threshold": 80, "split_mode": "regex",
                    "regex": r"[。！？]", "split_words": ["…"], "interval_method": "log",
                    "interval": "1.5-3.5", "log_base": "2.6"},  # 实测 log_base 是字符串
            },
            "group_settings": {"enable": False},  # 群聊已砍（SPEC §1），不迁移
        },
        "vl_mood_engine_config.json": {
            "keyword_groups": {"sweet": "喜欢|抱抱", "praise": "厉害", "rude": "滚"},
            "daily_bonus_enabled": True, "inject_enabled": True,
        },
        "vl_diary_config.json": {
            "diary_time": "22:30", "min_chars": 100, "max_chars": 200,
            "max_history_messages": 60, "max_excerpt_chars": 4000,
            "diary_prompt_template": "以第一人称写今天的日记",
        },
        "astrbot_plugin_mnemosyne_config.json": {
            "num_pairs": 2, "top_k": 2, "max_prompt_chars_for_embedding": 4000,
            "memory_injection_method": "user_prompt",
            # use_summary_time_anchor 保留在假旧栈里（R3 起为死键，迁移应忽略不落库）
            "use_summary_time_anchor": True,
        },
    }


def _make_fake_db(db_path: Path, content: list | None = None) -> None:
    """构造最小 data_v4.db（personas + conversations，列名对齐真库）。

    conversations 默认至少一行（config_migrate 的 session_id 模板取自
    conversations.user_id，空表无法迁移）。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        with con:
            con.execute("CREATE TABLE personas (id INTEGER PRIMARY KEY, persona_id VARCHAR,"
                        " system_prompt TEXT, begin_dialogs JSON)")
            # 列名对齐真库实测 DDL（conversations 主键为 inner_conversation_id）
            con.execute("CREATE TABLE conversations (inner_conversation_id INTEGER"
                        " PRIMARY KEY, conversation_id VARCHAR, user_id VARCHAR,"
                        " content JSON, persona_id VARCHAR)")
            con.execute("INSERT INTO personas VALUES (1, '测试人格', ?, ?)",
                        ("她的人格全文。\n第二行，含\"引号\"。",
                         json.dumps(["你好", "嗯？", "在吗", "在的"], ensure_ascii=False)))
            if content is None:
                content = [{"role": "user", "content": "占位"}]
            con.execute("INSERT INTO conversations VALUES (1, 'uuid-0001', ?, ?, NULL)",
                        (_SESSION, json.dumps(content, ensure_ascii=False)))
    finally:
        con.close()


def _make_fake_stack(tmp_path: Path, db_content: list | None = None) -> Path:
    """组装完整假旧栈目录树：tmp/{astrbot_data,napcat_config}。返回 root=tmp_path。"""
    root = tmp_path / "local"
    (root / "astrbot_data" / "config").mkdir(parents=True)
    (root / "napcat_config").mkdir(parents=True)
    (root / "astrbot_data" / "cmd_config.json").write_text(
        json.dumps(_fake_cmd_config(), ensure_ascii=False), encoding="utf-8")
    for name, data in _fake_plugin_configs().items():
        (root / "astrbot_data" / "config" / name).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _make_fake_db(root / "astrbot_data" / "data_v4.db", db_content)
    onebot = {"network": {"websocketClients": [{
        "enable": True, "name": "测试人格", "url": "ws://astrbot:6199/ws",
        "messagePostFormat": "array", "token": _OLD_WEAK_TOKEN,
        "heartInterval": 5000, "reconnectInterval": 5000}]}}
    (root / "napcat_config" / f"onebot11_{_SELF_ID}.json").write_text(
        json.dumps(onebot, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def _fake_history_content() -> list:
    """120 条假历史（混合形态，比例与守恒无关）：覆盖 think/reminder/双形态/
    &&tag&&/未知分片。

    与真库实测形态一致（think 以 content parts 的 type='think' 分片存在；
    system_reminder 是 user 消息里的文本残留；&&tag&& 属对话本体须保留）。
    """
    msgs: list = []

    def user(i: int, body: str) -> None:
        msgs.append({"role": "user", "content": f"用户消息{i}：{body}"})

    def assistant_text(i: int, body: str) -> None:
        msgs.append({"role": "assistant", "content": f"回复{i}：{body}"})

    def assistant_parts(i: int, text: str, think: str = "内心戏") -> None:
        parts = [{"type": "text", "text": f"回复{i}：{text}"},
                 {"type": "think", "text": think}]
        msgs.append({"role": "assistant", "content": parts})

    for i in range(50):
        user(i, "普通输入")
    for i in range(5):  # 字符串形态 think 段
        msgs.append({"role": "assistant",
                     "content": f"<think>我不该说</think>回复T{i}：好的"})
    for i in range(3):  # 字符串形态 system_reminder 残留
        user(100 + i, f"<system_reminder>当前时间 2026-01-0{i + 1}</system_reminder>喂")
    for i in range(50):  # 列表形态（text+think 分片）
        assistant_parts(i, "分段正文")
    for i in range(10):  # 列表形态含未知分片 / &&tag&& 保留
        if i == 0:
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": "看这个&&抱抱&&"},
                {"type": "image", "url": "https://example.test/x.png"}]})
        elif i == 1:  # 纯 think 分片 → 正文清空但条数守恒
            msgs.append({"role": "assistant", "content": [
                {"type": "think", "text": "整条都是内心戏"}]})
        else:
            assistant_parts(200 + i, "普通")
    assert len(msgs) == 50 + 5 + 3 + 50 + 10 == 118
    # 补齐到 120（user 67 = 50+3+14；assistant 53 = 5+50+... 微调）
    while len(msgs) < 120:
        msgs.append({"role": "user", "content": f"补位消息{len(msgs)}"})
    return msgs[:120]


# ---------------------------------------------------------------------------
# config_migrate
# ---------------------------------------------------------------------------

def test_config_migrate_full_mapping(tmp_path):
    root = _make_fake_stack(tmp_path)
    out = root / "config" / "vl.toml"
    cfg = config_migrate(root=root, out=out)

    assert out.is_file()
    got = load_config(out)  # 产物必须直接通过启动校验
    assert got == cfg

    # provider 映射（key 原文进 local/，不进断言外的任何日志）
    assert got.chat.model == "test-model"
    assert got.chat.api_key == _KEY
    assert got.chat.api_base == "https://api.test/v1"
    # O5/§7.1-3：[chat].max_context_tokens 死键不再迁移，窗口走 [context] 新默认
    assert got.context.max_context_tokens == 32768
    # embedding（dim=2048 硬约定）
    assert got.embedding.dim == 2048 and got.embedding.timeout == 20
    assert got.embedding.api_key == _EMB_KEY
    # tts：/tts 后缀归一为 base（dual_output 不再迁 [tts]——R5 删死键，真开关在 [proactive]）
    assert got.tts.base_url == "http://127.0.0.1:5000"
    assert got.tts.character == "测试角色" and got.tts.text_lang == "zh"
    assert got.tts.enabled is True
    # persona 全文 + 开场白
    assert got.persona.name == "测试人格"
    assert "她的人格全文" in got.persona.system_prompt
    assert got.persona.begin_dialogs == ["你好", "嗯？", "在吗", "在的"]
    # session_id 模板实值
    assert got.session_id == _SESSION
    # proactive（终裁参数：30-120 / 23-7 / 3 / 启动 30 / tts 优先只发语音 / 阈值 80）
    assert got.proactive.enable is True
    assert got.proactive.session_id == _SESSION
    assert (got.proactive.min_interval_minutes, got.proactive.max_interval_minutes) == (30, 120)
    assert got.proactive.quiet_hours == "23-7" and got.proactive.max_unanswered == 3
    assert got.proactive.boot_trigger_minutes == 30
    assert got.proactive.tts_enabled is True and got.proactive.always_send_text is False
    assert got.proactive.segmented.words_count_threshold == 80
    assert got.proactive.segmented.log_base == 2.6  # 旧值字符串 '2.6' → float
    # mood / diary / mnemosyne
    assert got.mood.keyword_groups["rude"] == "滚"
    assert got.diary.diary_time == "22:30"
    # O5 兜底补丁：旧栈 top_k=2 归一化为 3（PERF_SPEC §3-O5）
    assert (got.mnemosyne.num_pairs, got.mnemosyne.top_k) == (2, 3)
    assert got.mnemosyne.max_prompt_chars == 4000
    # injection_position 不再迁移（R5 删死键：注入位置固定 user_prompt prepend）
    # 主回复分段与限流（O2a：旧默认 100 迁移即归一为 30，与加载期归一化同语义）
    assert got.reply.segmented.words_count_threshold == 30
    assert got.reply.rate_limit.max_count == 10
    # bridge：host 终裁 127.0.0.1、self_id 来自文件名、token 全新强随机
    assert got.bridge.host == "127.0.0.1" and got.bridge.port == 6199
    assert got.bridge.self_id == _SELF_ID
    assert len(got.bridge.token) >= 32 and got.bridge.token != _OLD_WEAK_TOKEN
    assert got.admins == [_QQ]
    # timezone 不再迁移（五轮修缮 R4：VlConfig 已删该死键；旧栈源数据里的
    # "timezone": "Asia/Shanghai" 应被忽略，dump 产物零残留）
    assert not hasattr(got, "timezone")
    assert "timezone" not in out.read_text(encoding="utf-8")


def test_config_migrate_rewrites_napcat_with_backup(tmp_path):
    root = _make_fake_stack(tmp_path)
    onebot_path = root / "napcat_config" / f"onebot11_{_SELF_ID}.json"
    config_migrate(root=root, out=root / "config" / "vl.toml")

    cfg = load_config(root / "config" / "vl.toml")
    data = json.loads(onebot_path.read_text(encoding="utf-8"))
    client = data["network"]["websocketClients"][0]
    assert client["url"] == "ws://host.docker.internal:6199/ws"  # SPEC §3.3 终裁值
    assert client["token"] == cfg.bridge.token
    assert client["token"] != _OLD_WEAK_TOKEN
    backups = list(onebot_path.parent.glob("onebot11_*.bak-*"))
    assert len(backups) == 1
    assert _OLD_WEAK_TOKEN in backups[0].read_text(encoding="utf-8")  # 备份=改前原文


def test_config_migrate_report_has_no_secrets(tmp_path):
    root = _make_fake_stack(tmp_path)
    config_migrate(root=root, out=root / "config" / "vl.toml")
    report_text = (root / "config" / "migrate_report.json").read_text(encoding="utf-8")
    for secret in (_KEY, _EMB_KEY, _OLD_WEAK_TOKEN, cfg_token(root)):
        assert secret not in report_text, "报告泄漏敏感原文"


def cfg_token(root: Path) -> str:
    return load_config(root / "config" / "vl.toml").bridge.token


def test_config_migrate_dry_run_writes_nothing(tmp_path):
    root = _make_fake_stack(tmp_path)
    onebot_path = root / "napcat_config" / f"onebot11_{_SELF_ID}.json"
    before = onebot_path.read_text(encoding="utf-8")
    config_migrate(root=root, out=root / "config" / "vl.toml", dry_run=True)
    assert not (root / "config" / "vl.toml").exists()  # 不落 vl.toml
    assert not list(onebot_path.parent.glob("*.bak-*"))  # 不备份
    assert onebot_path.read_text(encoding="utf-8") == before  # 不改写


def test_config_migrate_missing_source_fails(tmp_path):
    with pytest.raises(SystemExit):
        config_migrate(root=tmp_path, out=tmp_path / "config" / "vl.toml")


# ---------------------------------------------------------------------------
# --rollback-napcat CLI 子命令（PERF_SPEC §4-O10 收尾 + §7.2 napcat_rollback 归属：
# workbench 整删的前提就是本入口可用——vl 进程死后唯一不依赖任何活进程的回退路径）
# ---------------------------------------------------------------------------

def test_rollback_napcat_cli_restores_latest_backup(tmp_path, capsys):
    """迁移→改写→回退 全链路：onebot11 从最近 *.bak-* 恢复为改前原文，退出码 0。"""
    root = _make_fake_stack(tmp_path)
    onebot_path = root / "napcat_config" / f"onebot11_{_SELF_ID}.json"
    original = onebot_path.read_text(encoding="utf-8")
    config_migrate(root=root, out=root / "config" / "vl.toml")  # 改写 + 写前备份
    assert onebot_path.read_text(encoding="utf-8") != original

    from virtuallover.tools.config_migrate import rollback_napcat_cli
    code = rollback_napcat_cli(root / "napcat_config")

    assert code == 0
    assert onebot_path.read_text(encoding="utf-8") == original  # 恢复=改前原文
    out = capsys.readouterr().out
    assert f"onebot11_{_SELF_ID}.json" in out and "已恢复" in out


def test_rollback_napcat_cli_without_backup_exits_1(tmp_path, capsys):
    """从未执行过指向切换（无 *.bak-*）：退出码 1，说明文本明确，绝不改文件。"""
    napcat_dir = tmp_path / "napcat_config"
    napcat_dir.mkdir()
    onebot_path = napcat_dir / f"onebot11_{_SELF_ID}.json"
    onebot_path.write_text('{"network": {}}', encoding="utf-8")

    from virtuallover.tools.config_migrate import rollback_napcat_cli
    code = rollback_napcat_cli(napcat_dir)

    assert code == 1
    assert "无需回退" in capsys.readouterr().out
    assert onebot_path.read_text(encoding="utf-8") == '{"network": {}}'  # 文件未被触碰


def test_rollback_napcat_cli_restart_failure_does_not_mask_rollback(tmp_path, capsys, monkeypatch):
    """--restart 路径：docker 不可用时告警但不掩盖"文件恢复已完成"（退出码仍 0）。"""
    import subprocess as sp

    from virtuallover.tools.config_migrate import rollback_napcat_cli
    root = _make_fake_stack(tmp_path)
    onebot_path = root / "napcat_config" / f"onebot11_{_SELF_ID}.json"
    original = onebot_path.read_text(encoding="utf-8")
    config_migrate(root=root, out=root / "config" / "vl.toml")

    def _boom(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sp, "run", _boom)
    code = rollback_napcat_cli(root / "napcat_config", restart=True)

    assert code == 0
    assert onebot_path.read_text(encoding="utf-8") == original  # 回退本体不受重启失败影响
    out = capsys.readouterr().out
    assert "警告" in out and "restart heart" in out


# ---------------------------------------------------------------------------
# 归一化纯函数
# ---------------------------------------------------------------------------

def test_strip_think_paired_and_open():
    assert strip_think("前言<think>内心</think>后语") == "前言后语"
    assert strip_think("前言<think>未闭合到结尾") == "前言"
    assert "&&抱抱&&" in strip_think("&&抱抱&&")  # 对话本体不受影响


def test_clear_system_reminder_paired_and_orphan():
    assert clear_system_reminder("a<system_reminder>t</system_reminder>b") == "ab"
    assert "system_reminder" not in clear_system_reminder("孤<li产><system_reminder>残")
    assert "system_reminder" not in clear_system_reminder("</system_reminder>尾")


def test_normalize_content_dual_forms():
    text, stats = normalize_content("纯文本")
    assert text == "纯文本" and not stats["list_expanded"]
    flat, stats = normalize_content([{"type": "text", "text": "a"},
                                     {"type": "think", "text": "b"}])
    assert flat == "a" and stats["think_part_stripped"] and stats["list_expanded"]
    flat, stats = normalize_content([{"type": "image", "url": "x"}])
    assert flat == "[不支持的内容:image]" and stats["unknown_part"] == 1


# ---------------------------------------------------------------------------
# history_migrate
# ---------------------------------------------------------------------------

def test_history_migrate_conservation_and_normalization(tmp_path):
    content = _fake_history_content()
    assert len(content) == 120
    root = _make_fake_stack(tmp_path, db_content=content)
    src_db = root / "astrbot_data" / "data_v4.db"
    out_db = tmp_path / "vl_data" / "history.db"
    report_dir = tmp_path / "backup"

    report = history_migrate(source_db=src_db, out_db=out_db, report_dir=report_dir)

    con = sqlite3.connect(str(out_db))
    try:
        rows = con.execute("SELECT session_id, role, content, created_at, meta "
                           "FROM messages ORDER BY id").fetchall()
    finally:
        con.close()

    # 120 条守恒 + meta 全带迁移标记
    assert report["conservation_ok"] is True
    assert len(rows) == 120
    assert all(r[4] and json.loads(r[4])["migrated"] is True for r in rows)
    # session_id = conversations.user_id 原值
    assert {r[0] for r in rows} == {_SESSION}
    # created_at 留空：旧数据时间戳不可伪造（SPEC §6）
    assert all(r[3] is None for r in rows)

    joined = "\n".join(r[2] for r in rows)
    # 注入脚手架残留=0（字符串 think + 分片 think + reminder）
    assert "<think" not in joined and "system_reminder" not in joined
    assert "我不该说" not in joined and "内心戏" not in joined
    assert "当前时间" not in joined
    # 对话本体保留：&&tag&& 与普通正文
    assert "&&抱抱&&" in joined
    assert "用户消息0：普通输入" in joined and "回复T0：好的" in joined
    # 未知分片降级占位（含 image url 不泄漏进正文）
    assert "[不支持的内容:image]" in joined
    assert "example.test" not in joined
    # 纯 think 分片条目：正文清空但条数仍在（守恒优先）
    empty = [r[2] for r in rows if r[2] == ""]
    assert len(empty) == 1

    # 对账单：sha256 与源库字节一致、逐表行数、messages_in=120
    rpath = next(report_dir.glob("history_migrate_report_*.json"))
    saved = json.loads(rpath.read_text(encoding="utf-8"))
    assert saved["source_sha256"] == hashlib.sha256(src_db.read_bytes()).hexdigest()
    assert saved["messages_in"] == 120 and saved["messages_migrated"] == 120
    assert saved["source_tables"]["conversations"] == 1
    assert saved["normalization"]["reminder_cleared"] == 3
    assert saved["normalization"]["think_part_stripped"] == 59  # 50+8 组合分片 + 1 纯分片


def test_history_migrate_refuses_overwrite(tmp_path):
    root = _make_fake_stack(tmp_path, db_content=_fake_history_content())
    out_db = tmp_path / "vl_data" / "history.db"
    out_db.parent.mkdir(parents=True)
    out_db.write_bytes(b"x")  # 预置目标，防覆盖
    with pytest.raises(SystemExit):
        history_migrate(source_db=root / "astrbot_data" / "data_v4.db",
                        out_db=out_db, report_dir=tmp_path / "backup")


def test_history_migrate_conservation_broken_aborts(tmp_path):
    """源库 content 非法 JSON → 中止退出（不产出半成品库）。"""
    root = tmp_path / "local"
    (root / "astrbot_data").mkdir(parents=True)
    db = root / "astrbot_data" / "data_v4.db"
    con = sqlite3.connect(str(db))
    with con:
        con.execute("CREATE TABLE conversations (inner_conversation_id INTEGER"
                    " PRIMARY KEY, conversation_id VARCHAR, user_id VARCHAR,"
                    " content JSON)")
        con.execute("INSERT INTO conversations VALUES (1, 'u', ?, '{bad json')",
                    (_SESSION,))
    con.close()
    with pytest.raises(SystemExit):
        history_migrate(source_db=db, out_db=tmp_path / "h.db",
                        report_dir=tmp_path / "backup")


def test_history_migrate_dry_run_no_db(tmp_path):
    root = _make_fake_stack(tmp_path, db_content=_fake_history_content())
    out_db = tmp_path / "vl_data" / "history.db"
    report = history_migrate(source_db=root / "astrbot_data" / "data_v4.db",
                             out_db=out_db, report_dir=tmp_path / "backup",
                             dry_run=True)
    assert report["conservation_ok"] is True
    assert not out_db.exists()
    assert not list((tmp_path / "backup").glob("history_migrate_report_*.json"))
