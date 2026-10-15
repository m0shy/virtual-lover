# -*- coding: utf-8 -*-
"""config.py 契约测试：toml 往返 + 启动校验（骨架组职责，SPEC §11.1）。

运行：python -m pytest virtuallover/tests/test_config.py -q
"""

from __future__ import annotations

import tomllib
import warnings

import pytest

from virtuallover.config import (
    SCHEMA_VERSION,
    AnniversaryConfig,
    ChatProviderConfig,
    ConfigError,
    ContextConfig,
    DiaryConfig,
    EmbeddingConfig,
    MoodConfig,
    MnemosyneConfig,
    PersonaConfig,
    ProactiveConfig,
    ReplyConfig,
    RoutineConfig,
    ScheduleConfig,
    SegmentedReplyConfig,
    StorageConfig,
    ToolsConfig,
    TtsConfig,
    VlConfig,
    dump_config,
    ensure_bridge_token,
    load_config,
)


def _full_cfg() -> VlConfig:
    """构造一棵全字段的配置树（全部占位值，不含任何真实账号/密钥）。"""
    return VlConfig(
        schema_version=SCHEMA_VERSION,
        session_id="测试人格:FriendMessage:10000",
        admins=["10000"],
        whitelist=["10000"],
        log_level="INFO",
        persona=PersonaConfig(
            name="测试人格",
            system_prompt="你是\"她\"。\n多行系统提示，含引号\"与反斜杠\\，以及 &&抱抱&& 标记。",
            begin_dialogs=["你好", "嗯？", "在吗", "在的"],
        ),
        chat=ChatProviderConfig(api_base="https://api.test/v1", api_key="sk-TEST-CHAT",
                                model="test-model", timeout=120),
        embedding=EmbeddingConfig(api_base="https://emb.test/v1", api_key="sk-TEST-EMB",
                                  model="emb-model", dim=2048, timeout=20),
        tts=TtsConfig(enabled=True, base_url="http://127.0.0.1:5000", character="角色",
                      emotion="平静", text_lang="zh", audio_format="wav",
                      timeout=60, retries=3),
        reply=ReplyConfig(
            segmented=SegmentedReplyConfig(enable=True, words_count_threshold=30,
                                           regex="[。！？]", split_words=["…"],
                                           interval="1.5-3.5", log_base=2.6,
                                           min_gap=1.5, max_gap=3.5),
        ),
        proactive=ProactiveConfig(
            enable=True, session_id="测试人格:FriendMessage:10000",
            min_interval_minutes=30, max_interval_minutes=120, quiet_hours="23-7",
            max_unanswered=3, boot_trigger_minutes=30,
            prompt_template="{{current_time}} 试试{{unanswered_count}}",
            tts_enabled=True, always_send_text=False,
            segmented=SegmentedReplyConfig(words_count_threshold=80),
        ),
        mood=MoodConfig(keyword_groups={"sweet": "喜欢|抱抱", "rude": "滚"},
                        daily_bonus_enabled=True, inject_enabled=True),
        diary=DiaryConfig(diary_time="22:30", min_chars=100, max_chars=200,
                          max_history_messages=60, max_excerpt_chars=4000,
                          prompt_template="写一篇日记"),
        mnemosyne=MnemosyneConfig(num_pairs=2, top_k=3, candidate_k=8,
                                  max_prompt_chars=4000),
        storage=StorageConfig(),
        context=ContextConfig(max_context_tokens=32768, truncate_ratio=0.82),  # O5 终裁默认
    )


# ---------------------------------------------------------------------------
# TOML 往返
# ---------------------------------------------------------------------------

def test_dump_then_load_roundtrip(tmp_path):
    """完整配置树 dump → tomllib 裸读 → load_config，字段逐项相等。"""
    cfg = _full_cfg()
    text = dump_config(cfg)
    path = tmp_path / "vl.toml"
    path.write_text(text, encoding="utf-8")

    # tomllib 裸读：验证生成的 TOML 语法本身合法、分节归属正确
    raw = tomllib.loads(text)
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["reply"]["segmented"]["words_count_threshold"] == 30  # O2a 新默认
    assert raw["reply"]["rate_limit"]["max_count"] == 10  # 标量不能落进子表
    assert raw["proactive"]["segmented"]["words_count_threshold"] == 80
    assert raw["mood"]["keyword_groups"]["sweet"] == "喜欢|抱抱"

    got = load_config(path)
    assert got == cfg  # dataclass 全字段相等 = 无损往返


def test_roundtrip_preserves_multiline_and_marks(tmp_path):
    """人格全文的换行/引号/反斜杠/&&tag&& 经转义后原样返回（迁移保真底线）。"""
    cfg = _full_cfg()
    path = tmp_path / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    got = load_config(path)
    assert "&&抱抱&&" in got.persona.system_prompt
    assert "\n" in got.persona.system_prompt
    assert '\\"' not in got.persona.system_prompt  # 转义只在 TOML 文本层，不残留


# ---------------------------------------------------------------------------
# 启动校验（中文报错）
# ---------------------------------------------------------------------------

def _write_minimal(path, **patches):
    """写一份仅含必填项的 vl.toml，patches 覆盖顶层/分节字段后重载校验。"""
    cfg = _full_cfg()
    for key, val in patches.items():
        section, _, field = key.partition(".")
        obj = getattr(cfg, section) if field else cfg
        target = field if field else section
        setattr(obj, target, val)
    path.write_text(dump_config(cfg), encoding="utf-8")


def test_validate_reports_missing_required_fields_in_chinese(tmp_path):
    path = tmp_path / "vl.toml"
    cfg = _full_cfg()
    cfg.chat.model = ""
    cfg.admins = []
    cfg.persona.system_prompt = ""
    path.write_text(dump_config(cfg), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    msg = str(exc.value)
    assert "必填项缺失" in msg
    assert "chat.model" in msg and "admins" in msg and "persona.system_prompt" in msg


def test_validate_rejects_wrong_schema_version(tmp_path):
    path = tmp_path / "vl.toml"
    _write_minimal(path, schema_version=2)
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "schema_version" in str(exc.value)


def test_load_missing_file_gives_migration_hint():
    with pytest.raises(ConfigError) as exc:
        load_config("local/config/definitely_not_exists.toml")
    assert "config_migrate" in str(exc.value)


def test_validate_rejects_weak_bridge_token(tmp_path):
    path = tmp_path / "vl.toml"
    _write_minimal(path, **{"bridge.token": "short"})  # 模拟旧栈弱 token
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "bridge.token" in str(exc.value)


def test_embedding_disabled_skips_required_checks(tmp_path):
    """[embedding] enabled=false：embedding 三个必填豁免（五轮修缮 R1）。

    setup 交互跳过 embedding 时 setup_writeback 写 enabled=false——vl 必须能
    正常启动（记忆功能暂闭、聊天不受影响），而不是启动校验直接炸。
    """
    path = tmp_path / "vl.toml"
    _write_minimal(path, **{"embedding.enabled": False, "embedding.api_base": "",
                            "embedding.api_key": "", "embedding.model": ""})
    cfg = load_config(path)  # 不抛 = 豁免生效
    assert cfg.embedding.enabled is False
    assert cfg.chat.model  # chat 侧必填校验不受影响


def test_embedding_enabled_requires_fields(tmp_path):
    """enabled 保持默认 true 时维持原 fail-fast：embedding 三字段缺失照报中文缺失项。"""
    path = tmp_path / "vl.toml"
    _write_minimal(path, **{"embedding.api_base": "", "embedding.api_key": "",
                            "embedding.model": ""})
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    msg = str(exc.value)
    assert "embedding.api_base" in msg
    assert "embedding.api_key" in msg
    assert "embedding.model" in msg


# ---------------------------------------------------------------------------
# token 自动生成与回写
# ---------------------------------------------------------------------------

def test_ensure_bridge_token_generates_and_persists(tmp_path):
    path = tmp_path / "vl.toml"
    _write_minimal(path)
    cfg = load_config(path)
    assert cfg.bridge.token == ""
    token = ensure_bridge_token(cfg, path)
    assert len(token) >= 32  # token_urlsafe(32) → 43 字符
    reloaded = load_config(path)  # 回写后再次加载取到同一 token
    assert reloaded.bridge.token == token
    assert ensure_bridge_token(reloaded, path) == token  # 已有 token 不再生成


def test_unknown_section_key_warns(tmp_path):
    """未知键给 warnings 提示但不硬失败（向后兼容）。"""
    path = tmp_path / "vl.toml"
    text = dump_config(_full_cfg())
    path.write_text(text.replace("[mood]", "[mood]\nnot_a_field = 1"), encoding="utf-8")
    with pytest.warns(UserWarning, match="not_a_field"):
        load_config(path)


# ---------------------------------------------------------------------------
# PERF_SPEC §7.1-3 存量归一化（O5/O6：clamp+warning+启动日志；死键指引）
# ---------------------------------------------------------------------------

def test_chat_max_context_tokens_dead_key_warns_with_guidance(tmp_path):
    """存量 vl.toml 残留 [chat].max_context_tokens：明确指引改配 [context]。"""
    cfg = _full_cfg()
    cfg.context.max_context_tokens = 32768  # 显式锁定，验证死键不影响 [context]
    path = tmp_path / "vl.toml"
    text = dump_config(cfg)
    path.write_text(text.replace("[chat]", "[chat]\nmax_context_tokens = 256000"),
                    encoding="utf-8")
    with pytest.warns(UserWarning, match="max_context_tokens"):
        got = load_config(path)
    # 死键被忽略：窗口上限只来自 [context]
    assert got.context.max_context_tokens == 32768


def test_timezone_dead_key_warns_with_guidance(tmp_path):
    """存量 vl.toml 残留顶层 timezone：五轮修缮 R4 已删（从未被读取的死键），
    残留时给迁移指引告警并忽略，不阻断启动。"""
    path = tmp_path / "vl.toml"
    text = dump_config(_full_cfg())
    path.write_text('timezone = "Asia/Shanghai"\n' + text, encoding="utf-8")
    with pytest.warns(UserWarning, match="timezone"):
        load_config(path)


def test_fallback_provider_ids_fake_key_warns_with_guidance(tmp_path):
    """存量 vl.toml 残留 [chat].fallback_provider_ids：R2 起假键已删（从未被
    读取），残留时指引改配 fallback_* 键组，不阻断启动。"""
    path = tmp_path / "vl.toml"
    text = dump_config(_full_cfg())
    path.write_text(text.replace("[chat]", "[chat]\nfallback_provider_ids = []"),
                    encoding="utf-8")
    with pytest.warns(UserWarning, match="fallback_provider_ids"):
        got = load_config(path)
    # 假键不复活：R2 fallback_* 键组默认值不受残留影响
    assert got.chat.fallback_enabled is False


def test_r5_dead_keys_warn_with_guidance(tmp_path):
    """R5 删除的三死键存量残留：各给迁移指引告警并忽略，不阻断启动。

    - [tts] always_send_text：R3 终裁主聊天恒双轨，真开关在 [proactive]；
    - [mnemosyne] injection_position：注入位置固定 user_prompt prepend；
    - [storage] napcat_backup_dir：备份落点由各动作自管。
    """
    path = tmp_path / "vl.toml"
    text = (dump_config(_full_cfg())
            .replace("[tts]", '[tts]\nalways_send_text = false')
            .replace("[mnemosyne]", '[mnemosyne]\ninjection_position = "system"')
            .replace("[storage]", '[storage]\nnapcat_backup_dir = "backup"'))
    path.write_text(text, encoding="utf-8")
    with pytest.warns(UserWarning, match="always_send_text"):
        load_config(path)
    with pytest.warns(UserWarning, match="injection_position"):
        load_config(path)
    with pytest.warns(UserWarning, match="napcat_backup_dir"):
        load_config(path)


def test_context_max_context_tokens_default_32768(tmp_path):
    """O5 终裁默认 32768（128k/256k 是显式逃生档，不是默认）。"""
    path = tmp_path / "vl.toml"
    path.write_text(dump_config(_full_cfg()), encoding="utf-8")
    assert load_config(path).context.max_context_tokens == 32768


def test_context_max_context_tokens_legacy_256k_normalized(tmp_path, caplog):
    """存量 vl.toml 的 256000 是旧默认 dump 残留：归一为 32768 并告警。

    逃生档是显式的 131072/262144，写 256000 只可能是旧默认残留——
    不归一则 O5 的 32k 终裁对现网落不了地。
    """
    import logging as _logging

    cfg = _full_cfg()
    cfg.context.max_context_tokens = 256000  # 旧默认残留（现网实况）
    path = tmp_path / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    with caplog.at_level(_logging.WARNING, logger="virtuallover.config"):
        got = load_config(path)
    assert got.context.max_context_tokens == 32768
    assert "256000 是旧默认" in caplog.text
    # 显式逃生档值不受影响
    for escape in (131072, 262144):
        cfg2 = _full_cfg()
        cfg2.context.max_context_tokens = escape
        path.write_text(dump_config(cfg2), encoding="utf-8")
        assert load_config(path).context.max_context_tokens == escape


def test_context_max_context_tokens_clamped_and_warned(tmp_path, caplog):
    """越界窗口被 clamp 到 [1k, 256k] 并告警；启动日志打印生效值。"""
    import logging as _logging

    cfg = _full_cfg()
    cfg.context.max_context_tokens = 1000000  # 超出 256k 逃生档封顶
    path = tmp_path / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    with caplog.at_level(_logging.WARNING, logger="virtuallover.config"):
        got = load_config(path)
    assert got.context.max_context_tokens == 262144
    assert "max_context_tokens" in caplog.text and "clamp" in caplog.text
    cfg2 = _full_cfg()
    cfg2.context.max_context_tokens = 100  # 低于 1k 下限
    path2 = tmp_path / "vl2.toml"
    path2.write_text(dump_config(cfg2), encoding="utf-8")
    assert load_config(path2).context.max_context_tokens == 1024


def test_normalize_logs_effective_values(tmp_path, caplog):
    """§7.1-3 启动日志：三项生效值（timeout/threshold/max_context_tokens）同行可查。"""
    import logging as _logging

    path = tmp_path / "vl.toml"
    path.write_text(dump_config(_full_cfg()), encoding="utf-8")
    with caplog.at_level(_logging.INFO, logger="virtuallover.config"):
        cfg = load_config(path)
    assert "[perf] 生效值" in caplog.text
    assert f"context.max_context_tokens={cfg.context.max_context_tokens}" in caplog.text
    assert f"chat.timeout_main={cfg.chat.timeout_main}" in caplog.text
    assert ("reply.segmented.words_count_threshold="
            f"{cfg.reply.segmented.words_count_threshold}") in caplog.text


def test_mood_fragment_position_default_and_fallback(tmp_path, caplog):
    """O6 回退键：默认 user；非法值回退 user 并告警；system 原样保留。"""
    import logging as _logging

    path = tmp_path / "vl.toml"
    path.write_text(dump_config(_full_cfg()), encoding="utf-8")
    assert load_config(path).mood.fragment_position == "user"  # 默认

    cfg = _full_cfg()
    cfg.mood.fragment_position = "sidebar"  # 非法
    path.write_text(dump_config(cfg), encoding="utf-8")
    with caplog.at_level(_logging.WARNING, logger="virtuallover.config"):
        got = load_config(path)
    assert got.mood.fragment_position == "user"
    assert "fragment_position" in caplog.text

    cfg2 = _full_cfg()
    cfg2.mood.fragment_position = "system"  # 旧行为回退键合法
    path.write_text(dump_config(cfg2), encoding="utf-8")
    assert load_config(path).mood.fragment_position == "system"


def test_mnemosyne_top_k_legacy_2_normalized_to_3(tmp_path, caplog):
    """O5 兜底补丁：存量显式 top_k=2 归一化为 3 并告警（不改默认值落不了地）。"""
    import logging as _logging

    cfg = _full_cfg()
    cfg.mnemosyne.top_k = 2  # 旧默认（现网存量值）
    path = tmp_path / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    with caplog.at_level(_logging.WARNING, logger="virtuallover.config"):
        got = load_config(path)
    assert got.mnemosyne.top_k == 3
    assert "top_k=2 是旧默认" in caplog.text


def test_mnemosyne_min_similarity_default():
    """O5 相似度下限默认 0.3；0=关闭（旧行为）。"""
    assert _full_cfg().mnemosyne.min_similarity == 0.3


# ---------------------------------------------------------------------------
# __main__ 装配冒烟（--smoke：setup 全走一遍即优雅退出；模块缺失=告警跳过）
# ---------------------------------------------------------------------------

def test_main_smoke_assembles_and_exits(tmp_path, capsys, monkeypatch):
    """--smoke 在只有骨架文件的目录上跑通：配置加载→token 落盘→模块告警跳过→退出 0。"""
    import virtuallover.__main__ as main_mod
    from virtuallover.config import BridgeConfig

    cfg = _full_cfg()
    cfg.bridge = BridgeConfig(host="127.0.0.1", port=6199, token="")  # 留空触发自动生成
    (tmp_path / "local" / "config").mkdir(parents=True)
    (tmp_path / "local" / "config" / "vl.toml").write_text(dump_config(cfg),
                                                          encoding="utf-8")
    monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
    assert main_mod.main(["--smoke"]) == 0
    captured = capsys.readouterr()
    # 装配统计走 logging（stderr），启动横幅走 stdout——两边都查
    assert "装配完成" in captured.out + captured.err
    # token 已自动生成并回写
    assert len(load_config(tmp_path / "local" / "config" / "vl.toml").bridge.token) >= 32


def test_main_missing_config_exit_2(tmp_path, capsys, monkeypatch):
    """vl.toml 缺失 → 中文报错退出 2（引导先跑迁移）。"""
    import virtuallover.__main__ as main_mod

    monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
    assert main_mod.main(["--smoke"]) == 2
    assert "config_migrate" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 配置地基：新段/新键（[schedule] [routine] [anniversary]、weather_city、
# typing_cps、proactive 担心型追问四键、[proactive].anniversary 默认开）
# ---------------------------------------------------------------------------

def test_config_foundation_defaults():
    """新段/新键的出厂默认值（7 位模块工程师并行读这些键，键名与默认值是契约）。"""
    cfg = VlConfig()
    # [schedule]：默认课表 11 条（工作日 7 + 周末 4），enabled 默认开
    assert cfg.schedule.enabled is True
    weekdays = [e for e in cfg.schedule.entries if e["days"] == "1-5"]
    weekend = [e for e in cfg.schedule.entries if e["days"] == "0,6"]
    assert len(weekdays) == 7 and len(weekend) == 4
    assert weekdays[0] == {"days": "1-5", "start": "08:00", "end": "09:40",
                           "activity": "在上早八课"}
    assert weekdays[-1] == {"days": "1-5", "start": "19:00", "end": "22:00",
                            "activity": "在图书馆自习"}
    assert weekend[0]["activity"] == "睡懒觉、窝在床上追动漫"
    assert weekend[-1] == {"days": "0,6", "start": "18:00", "end": "23:00",
                           "activity": "在宿舍追剧、打游戏"}
    # [routine]：晚安锚点 22:50 与 [diary] diary_time=22:30 错开
    assert (cfg.routine.enabled, cfg.routine.morning_time,
            cfg.routine.morning_jitter_minutes, cfg.routine.night_time,
            cfg.routine.night_jitter_minutes) == (True, "07:30", 10, "22:50", 5)
    # [anniversary]：两个生日默认空=不启用
    assert cfg.anniversary.user_birthday == "" and cfg.anniversary.her_birthday == ""
    # [tools].weather_city：空=关闭天气感知
    assert cfg.tools.weather_city == ""
    # [reply.segmented].typing_cps：每字打字秒数，0=关闭模拟
    assert cfg.reply.segmented.typing_cps == 0.08
    assert cfg.proactive.segmented.typing_cps == 0.08  # 与主回复共用 dataclass
    # [proactive]：担心型追问 + 每日话题；纪念日默认开（配置地基翻转）
    assert cfg.proactive.anniversary is True
    assert cfg.proactive.worried_after_hours == 24.0
    assert cfg.proactive.give_up_after_hours == 72.0
    assert cfg.proactive.daily_topic is True
    assert "{{last_user_gap}}" in cfg.proactive.prompt_template_worried
    assert "看到消息回我一下好不好" in cfg.proactive.prompt_template_worried


def test_schedule_entries_default_factory_isolates_instances():
    """entries 是 default_factory：改一个实例的课表，另一个实例不受牵连。"""
    a, b = ScheduleConfig(), ScheduleConfig()
    a.entries[0]["activity"] = "被改掉了"
    a.entries.append({"days": "1", "start": "00:00", "end": "23:59", "activity": "x"})
    assert b.entries[0]["activity"] == "在上早八课"
    assert len(b.entries) == 11


def test_foundation_keys_toml_roundtrip(tmp_path):
    """新段/新键 dump → tomllib 裸读 → load_config 无损往返。"""
    cfg = _full_cfg()
    cfg.schedule = ScheduleConfig(enabled=False, entries=[
        {"days": "0", "start": "13:00", "end": "15:00", "activity": "在午睡"},
        {"days": "2-4", "start": "20:00", "end": "21:00", "activity": "在操场跑步"},
    ])
    cfg.routine = RoutineConfig(enabled=False, morning_time="08:10",
                                morning_jitter_minutes=3, night_time="23:10",
                                night_jitter_minutes=7)
    cfg.anniversary = AnniversaryConfig(user_birthday="01-01", her_birthday="12-31")
    cfg.tools = ToolsConfig(web_search_enabled=False, search_provider="bing",
                            tavily_api_key="", weather_city="测试市")
    cfg.reply.segmented.typing_cps = 0.1
    cfg.proactive.prompt_template_worried = "自定义模板 {{last_user_gap}}"
    cfg.proactive.worried_after_hours = 12.5
    cfg.proactive.give_up_after_hours = 96.0
    cfg.proactive.daily_topic = False

    text = dump_config(cfg)
    path = tmp_path / "vl.toml"
    path.write_text(text, encoding="utf-8")

    raw = tomllib.loads(text)
    assert raw["schedule"] == {"enabled": False, "entries": cfg.schedule.entries}
    assert raw["routine"] == {"enabled": False, "morning_time": "08:10",
                              "morning_jitter_minutes": 3, "night_time": "23:10",
                              "night_jitter_minutes": 7}
    assert raw["anniversary"] == {"user_birthday": "01-01", "her_birthday": "12-31"}
    assert raw["tools"]["weather_city"] == "测试市"
    assert raw["reply"]["segmented"]["typing_cps"] == 0.1
    assert raw["proactive"]["prompt_template_worried"] == "自定义模板 {{last_user_gap}}"
    assert raw["proactive"]["daily_topic"] is False

    assert load_config(path) == cfg  # dataclass 全字段相等 = 无损往返


def test_legacy_config_without_new_keys_falls_back_to_defaults(tmp_path):
    """旧 vl.toml 缺全部新段/新键：一律回落 dataclass 默认值（向后兼容底线）。"""
    path = tmp_path / "vl.toml"
    path.write_text("\n".join([
        "schema_version = 1",
        'session_id = "测试女友:FriendMessage:10000"',
        'admins = ["10000"]',
        "[bridge]",
        'host = "127.0.0.1"',
        "[chat]",
        'api_base = "https://api.test/v1"',
        'api_key = "sk-TEST-CHAT"',
        'model = "test-model"',
        "[embedding]",
        "enabled = false",  # 旧装可能没配 embedding：豁免三必填
        "[persona]",
        'name = "测试女友"',
        'system_prompt = "你是她。"',
    ]), encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 未知键/死键告警一律视为失败
        cfg = load_config(path)
    assert cfg.schedule.enabled is True and len(cfg.schedule.entries) == 11
    assert cfg.routine == RoutineConfig()
    assert cfg.anniversary == AnniversaryConfig()
    assert cfg.tools.weather_city == ""
    assert cfg.reply.segmented.typing_cps == 0.08
    assert cfg.proactive.anniversary is True
    assert cfg.proactive.worried_after_hours == 24.0
    assert cfg.proactive.give_up_after_hours == 72.0
    assert cfg.proactive.daily_topic is True
    assert "{{last_user_gap}}" in cfg.proactive.prompt_template_worried
